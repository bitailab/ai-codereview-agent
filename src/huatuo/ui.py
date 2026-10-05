"""本地只读状态页：任务队列、当前任务进度、MR 问题列表。

数据来源：state.db（任务与问题）、checkpoints.db（待审文件块等图状态）、agent.log（逐文件进度，
checkpoint 只在整个扇出步骤结束后才落盘，看不到进行中的文件块）。只监听 127.0.0.1。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from langgraph.checkpoint.sqlite import SqliteSaver

from .deps import Deps
from .graph.build import build_graph
from .llm import model_ready
from .quality import quality_stats
from .trace import TraceReader

log = logging.getLogger(__name__)

LOG_TAIL_BYTES = 8 * 1024 * 1024
LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (\w+) ([\w.]+): (.*)$")
START_RE = re.compile(r"处理任务 #(\d+) ")
CHUNK_RE = re.compile(r"^\s+(.+) \((\d+)/(\d+)\): (\d+) 条候选问题$")
TOTAL_RE = re.compile(r"待审 (\d+) 个文件块")
AGG_RE = re.compile(r"聚合：新问题 (\d+) 条，丢弃 (\d+) 条")
DONE_RE = re.compile(r"发布完成，结论 (\w+)")
SKIP_RE = re.compile(r"状态为 (\w+)，跳过")
RESUME_RE = re.compile(r"从 checkpoint 恢复任务")


def _read_log_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - LOG_TAIL_BYTES))
        return fh.read().decode("utf-8", errors="replace").splitlines()


def parse_job_log(lines: list[str], job_id: int) -> dict:
    """从日志中截取某个任务最近一次执行的片段，提取阶段与逐文件进度。"""
    start = None
    for i, line in enumerate(lines):
        m = START_RE.search(line)
        if m and int(m.group(1)) == job_id:
            start = i
    if start is None:
        return {"found": False}
    seg: list[dict] = []
    for line in lines[start:]:
        m = LINE_RE.match(line)
        if not m:  # 多行日志（traceback、lint 输出）的续行
            if seg:
                seg[-1]["msg"] += "\n" + line
            continue
        ts, level, name, msg = m.groups()
        if seg and START_RE.search(msg):
            break
        seg.append({"ts": ts, "level": level, "name": name, "msg": msg})

    out: dict = {"found": True, "started": seg[0]["ts"], "last": seg[-1]["ts"], "total": None,
                 "chunks": [], "aggregate": None, "conclusion": None, "warnings": [], "phase": "load"}
    prev_ts = None
    for e in seg:
        msg = e["msg"]
        if m := TOTAL_RE.search(msg):
            out["total"] = int(m.group(1))
            prev_ts = e["ts"]
        elif RESUME_RE.search(msg):  # 从 checkpoint 恢复时不会重新规划，日志里没有“待审 N 个文件块”
            out["resumed"] = True
            prev_ts = e["ts"]
        elif m := CHUNK_RE.match(msg):
            out["chunks"].append({"path": m.group(1), "chunk": int(m.group(2)), "chunks": int(m.group(3)),
                                  "n": int(m.group(4)), "ts": e["ts"], "since": prev_ts})
            prev_ts = e["ts"]
        elif m := AGG_RE.search(msg):
            out["aggregate"] = {"new": int(m.group(1)), "dropped": int(m.group(2))}
        elif m := DONE_RE.search(msg):
            out["conclusion"] = m.group(1)
        elif m := SKIP_RE.search(msg):
            out["conclusion"] = f"跳过（MR {m.group(1)}）"
        if e["level"] in ("WARNING", "ERROR"):
            out["warnings"].append(f"{e['ts'][11:]} {msg[:300]}")

    out["cursor_ts"] = prev_ts  # 当前文件块开始的时间
    if out["conclusion"]:
        out["phase"] = "done"
    elif out["aggregate"]:
        out["phase"] = "verify"
    elif out["total"] is not None:
        out["phase"] = "review"
    elif len(seg) > 1:
        out["phase"] = "plan"
    out["lines"] = [f"{e['ts'][11:]} {e['level'][0]} {e['msg'][:400]}" for e in seg if e["name"] != "httpx2"][-80:]
    return out


class UI:
    def __init__(self, deps: Deps):
        self.d = deps
        self.log_path = deps.cfg.data_path / "agent.log"
        conn = sqlite3.connect(f"file:{deps.cfg.data_path / 'checkpoints.db'}?mode=ro", uri=True,
                               check_same_thread=False)
        self.graph = build_graph(deps, SqliteSaver(conn))
        self.trace = TraceReader(deps.cfg.data_path / "llm_trace.db")
        self._mr_state: dict[tuple[str, int], tuple[float, str]] = {}  # (项目, iid) -> (查询时间, state)

    def _mr_is_open(self, pp: str, iid: int) -> bool:
        """MR 是否仍为 opened；状态缓存 60 秒，查询失败时按仍打开处理（宁可多提醒）。"""
        hit = self._mr_state.get((pp, iid))
        if not hit or time.time() - hit[0] > 60:
            try:
                hit = (time.time(), self.d.gl.mr(pp, iid).attributes.get("state"))
            except Exception as e:  # noqa: BLE001
                log.warning("%s!%s 查询 MR 状态失败，仍显示提醒: %s", pp, iid, e)
                return True
            self._mr_state[(pp, iid)] = hit
        return hit[1] == "opened"

    def mr_url(self, pp: str, iid: int) -> str:
        return f"{self.d.settings.env.gitlab_url.rstrip('/')}/{pp}/-/merge_requests/{iid}"

    def overview(self) -> dict:
        ok, why = model_ready()
        jobs = self.d.store.recent_jobs(60)
        return {
            "model": {"name": self.d.settings.env.llm_model, "ready": ok, "detail": why},
            "counts": {s: sum(1 for j in jobs if j["status"] == s) for s in ("pending", "running", "failed")},
            "jobs": [{k: j[k] for k in ("id", "project_path", "mr_iid", "kind", "status", "created_at", "updated_at")}
                     | {"error": (j["error"] or "")[:200]} for j in jobs],
            "waiting": [{"project_path": w["project_path"], "mr_iid": w["mr_iid"], "notice": w.get("pipeline_notice"),
                         "url": self.mr_url(w["project_path"], w["mr_iid"])} for w in self.d.store.waiting_pipeline()],
            "needs_human": [f | {"url": self.mr_url(f["project_path"], f["mr_iid"])}
                            for f in self.d.store.escalated_findings()
                            if self._mr_is_open(f["project_path"], f["mr_iid"])],
        }

    def _graph_state(self, job_id: int) -> dict:
        try:
            s = self.graph.get_state({"configurable": {"thread_id": f"job-{job_id}"}})
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}
        v = s.values or {}
        return {
            "next": list(dict.fromkeys(s.next)),
            "files": [{"path": f["path"], "chunk": f["chunk"], "chunks": f["chunks"]} for f in v.get("files") or []],
            "intent": v.get("intent"),
            "notes": v.get("notes") or [],
            "lint_new": None if v.get("lint_issues") is None else len(v["lint_issues"]),
            "conclusion": v.get("conclusion"),
        }

    def job(self, job_id: int) -> dict | None:
        job = self.d.store.job(job_id)
        if not job:
            return None
        pp, iid = job["project_path"], job["mr_iid"]
        prog = parse_job_log(_read_log_lines(self.log_path), job_id)
        gs = self._graph_state(job_id)
        # 恢复执行的任务日志里没有规划阶段的标记，用 checkpoint 中待执行的节点判断当前阶段
        if prog.get("found") and prog["phase"] in ("load", "plan") and "review_file" in gs.get("next", []):
            prog["phase"] = "review"
        done = {(c["path"], c["chunk"]): c for c in prog.get("chunks", [])}
        planned = gs.get("files") or [{k: c[k] for k in ("path", "chunk", "chunks")} for c in prog.get("chunks", [])]
        files, current_marked = [], False
        for f in planned:
            c = done.get((f["path"], f["chunk"]))
            if c:
                files.append(f | {"state": "done", "n": c["n"], "since": c["since"], "ts": c["ts"]})
            elif job["status"] == "running" and prog.get("phase") == "review" and not current_marked:
                files.append(f | {"state": "running", "since": prog.get("cursor_ts")})
                current_marked = True
            else:
                files.append(f | {"state": "pending"})
        findings = []
        for f in self.d.store.findings(pp, iid):
            findings.append({k: f[k] for k in ("id", "severity", "status", "file", "line", "title", "detail", "evidence",
                                               "suggestion", "category", "source", "status_reason", "updated_at")}
                            | {"events": self.d.store.events(f["id"])})
        return {
            "job": {k: job[k] for k in ("id", "project_path", "mr_iid", "kind", "status", "error", "created_at",
                                        "updated_at")} | {"payload": job["payload"], "url": self.mr_url(pp, iid)},
            "progress": prog, "graph": gs, "files": files, "findings": findings,
            "reviews": [{k: r[k] for k in ("head_sha", "conclusion", "created_at")} for r in self.d.store.reviews(pp, iid)],
            "calls": self.trace.calls(f"job-{job_id}"),
        }


def make_handler(ui: UI):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 不刷访问日志
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            try:
                if u.path == "/":
                    self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                elif u.path == "/api/overview":
                    self._json(ui.overview())
                elif u.path == "/quality":
                    self._send(200, QUALITY_PAGE.encode(), "text/html; charset=utf-8")
                elif u.path == "/api/quality":
                    self._json(quality_stats(ui.d.store.quality_rows()))
                elif u.path == "/api/job":
                    data = ui.job(int(parse_qs(u.query)["id"][0]))
                    self._json(data if data else {"error": "not found"}, 200 if data else 404)
                elif u.path == "/api/call":
                    data = ui.trace.call(int(parse_qs(u.query)["id"][0]))
                    self._json(data if data else {"error": "not found"}, 200 if data else 404)
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as e:  # noqa: BLE001
                log.exception("状态页请求失败")
                self._json({"error": str(e)[:300]}, 500)

    return Handler


def serve_ui(deps: Deps, port: int = 8765) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(UI(deps)))
    print(f"状态页：http://127.0.0.1:{port}  （Ctrl+C 退出）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


QUALITY_PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>华佗 质量</title>
<style>
:root { --bg:#f6f7f9; --panel:#fff; --text:#1d2330; --muted:#6b7385; --line:#e3e6ec; --ok:#1f9d55; --warn:#c27c0e; --bad:#d23c3c; }
@media (prefers-color-scheme: dark) { :root { --bg:#14161b; --panel:#1c1f26; --text:#e6e8ee; --muted:#9098a9; --line:#2c313b; --ok:#3fbf7f; --warn:#e0a33a; --bad:#f06a6a; } }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif; padding:16px; }
a { color:inherit; } h1 { font-size:16px; margin:0 0 12px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:14px; overflow-x:auto; }
.card h2 { font-size:14px; margin:0 0 10px; } .muted { color:var(--muted); font-size:12px; }
table { border-collapse:collapse; width:100%; } th,td { text-align:right; padding:5px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }
th:first-child,td:first-child { text-align:left; } th { color:var(--muted); font-weight:500; }
.big { font-size:28px; font-weight:600; }
</style></head><body>
<h1>华佗 审查质量 <a class="muted" href="/">← 返回状态页</a></h1>
<div id="root" class="muted">加载中…</div>
<script>
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct = p => p == null ? "—" : (p * 100).toFixed(0) + "%";
const color = p => p == null ? "" : p >= 0.8 ? "var(--ok)" : p >= 0.6 ? "var(--warn)" : "var(--bad)";
function table(title, rows) {
  return `<div class="card"><h2>${title}</h2><table><tr><th></th><th>总数</th><th>有效</th><th>误报</th><th>建议类撤回</th><th>未决</th><th>精确率</th></tr>` +
    rows.map(r => `<tr><td>${esc(r.key)}</td><td>${r.total}</td><td>${r.valid}</td><td>${r.false_positive}</td><td>${r.advisory}</td><td>${r.pending}</td>` +
      `<td style="color:${color(r.precision)}">${pct(r.precision)}${r.decided < 5 && r.decided ? " <span class=muted>(n=" + r.decided + ")</span>" : ""}</td></tr>`).join("") + `</table></div>`;
}
fetch("/api/quality").then(r => r.json()).then(d => {
  const o = d.overall;
  document.getElementById("root").className = "";
  document.getElementById("root").innerHTML =
    `<div class="card"><div class="big" style="color:${color(o.precision)}">${pct(o.precision)}</div>
     <div>精确率 = 有效 ${o.valid} / (有效 ${o.valid} + 误报 ${o.false_positive})，共 ${o.total} 条发现，${o.pending} 条未决，${o.advisory} 条为“仍建议调整”的 P2 撤回（不计入）。</div>
     <div class="muted">有效：已修复 / 人工放行 / 延期。误报：开发者或人工认为华佗错了而撤回。样本很小时（n&lt;5）仅供参考；
     开发者可能为省事而修了非问题，也可能被说服了真问题，需配合抽样人工标注校准。</div></div>` +
    table("按严重度", d.severity) + table("按类别", d.category) + table("按项目", d.project) + table("按周", d.week);
});
</script></body></html>"""

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>华佗 状态</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --text: #1d2330; --muted: #6b7385; --line: #e3e6ec;
  --accent: #3563e9; --ok: #1f9d55; --warn: #c27c0e; --bad: #d23c3c; --chip: #eef1f6; --code: #f3f4f7;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161b; --panel: #1c1f26; --text: #e6e8ee; --muted: #9098a9; --line: #2c313b;
    --accent: #6f93ff; --ok: #3fbf7f; --warn: #e0a33a; --bad: #f06a6a; --chip: #262a33; --code: #232730;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "PingFang SC", "Segoe UI", sans-serif; }
header { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; padding: 12px 16px;
  border-bottom: 1px solid var(--line); background: var(--panel); position: sticky; top: 0; z-index: 2; }
header h1 { font-size: 16px; margin: 0 8px 0 0; }
.pill { padding: 2px 10px; border-radius: 999px; background: var(--chip); font-size: 12px; white-space: nowrap; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
.layout { display: grid; grid-template-columns: 320px 1fr; min-height: calc(100vh - 53px); }
@media (max-width: 860px) { .layout { grid-template-columns: 1fr; } aside { max-height: 260px; } }
aside { border-right: 1px solid var(--line); overflow-y: auto; background: var(--panel); }
.job { padding: 10px 14px; border-bottom: 1px solid var(--line); cursor: pointer; }
.job:hover { background: var(--chip); }
.job.sel { background: var(--chip); box-shadow: inset 3px 0 var(--accent); }
.job .t { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.job .m { color: var(--muted); font-size: 12px; }
main { padding: 16px; min-width: 0; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; margin-bottom: 14px; }
.card h2 { font-size: 14px; margin: 0 0 10px; }
.muted { color: var(--muted); }
a { color: var(--accent); text-decoration: none; }
.steps { display: flex; gap: 6px; flex-wrap: wrap; margin: 10px 0; }
.step { flex: 1 1 110px; padding: 6px 10px; border-radius: 8px; background: var(--chip); color: var(--muted); font-size: 12px; }
.step.done { color: var(--ok); }
.step.cur { background: var(--accent); color: #fff; }
.bar { height: 6px; background: var(--chip); border-radius: 3px; overflow: hidden; }
.bar > div { height: 100%; background: var(--accent); transition: width .4s; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 500; }
td.path { word-break: break-all; font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
.sev { font-weight: 700; } .P0 { color: var(--bad); } .P1 { color: var(--warn); } .P2 { color: var(--muted); }
.st-running { color: var(--accent); } .st-done { color: var(--ok); } .st-failed { color: var(--bad); } .st-pending { color: var(--muted); }
details summary { cursor: pointer; }
pre { background: var(--code); padding: 8px 10px; border-radius: 6px; overflow-x: auto; white-space: pre-wrap;
  word-break: break-word; font-size: 12px; margin: 6px 0; }
.log { max-height: 360px; overflow-y: auto; }
.msg { margin: 8px 0; }
.msg .role { font-size: 12px; font-weight: 600; color: var(--muted); }
.calls td.step { word-break: break-all; }
.calls .bad { color: var(--bad); }
.empty { padding: 40px; text-align: center; color: var(--muted); }
.alert { margin: 0; padding: 10px 16px; background: color-mix(in srgb, var(--bad) 14%, var(--panel));
  border-bottom: 2px solid var(--bad); }
.alert b { color: var(--bad); }
.alert ul { margin: 6px 0 0; padding-left: 20px; }
.alert code { background: var(--code); padding: 0 4px; border-radius: 4px; }
.spin { display: inline-block; animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<header>
  <h1>华佗 状态</h1>
  <span class="pill" id="model">模型…</span>
  <span class="pill" id="counts"></span>
  <span class="pill muted" id="updated"></span>
  <a class="pill" href="/quality" style="margin-left:auto;text-decoration:none;color:inherit">质量统计</a>
</header>
<div class="alert" id="alert" hidden></div>
<div class="layout">
  <aside id="jobs"></aside>
  <main id="main"><div class="empty">加载中…</div></main>
</div>
<script>
const KIND = {new_push: "代码审查", dev_reply: "处理回复", human_command: "人工命令"};
const STATUS = {pending: "排队", running: "进行中", done: "完成", failed: "失败"};
const PHASES = [["load", "加载 MR"], ["plan", "规划 / 静态分析"], ["review", "逐文件审查"], ["verify", "聚合与复核"], ["done", "发布"]];
let selected = null, pinned = false, openDetails = new Set();
const callCache = new Map();  // 调用 id → 请求/回复数据（内容大，展开时才加载，刷新时复用）
let lastJobRaw = "";  // 数据没变就不重绘，避免展开的长 prompt 每 3 秒被重置
const ROLE = {system: "system", human: "user", ai: "assistant", tool: "tool"};

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const secs = (a, b) => a && b ? Math.round((new Date(b.replace(" ", "T")) - new Date(a.replace(" ", "T"))) / 1000) : null;
const dur = s => s == null ? "" : s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s` : `${Math.floor(s / 3600)}h${Math.floor(s % 3600 / 60)}m`;
const local = iso => iso ? new Date(iso).toLocaleString("zh-CN", {hour12: false}) : "";
const nowStr = () => { const d = new Date(), p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; };

async function get(url) { const r = await fetch(url); return r.json(); }

const utcLocal = s => s ? new Date(s.replace(" ", "T") + "Z").toLocaleTimeString("zh-CN", {hour12: false}) : "";
const text = c => typeof c === "string" ? c : JSON.stringify(c, null, 2);

function renderCall(c) {
  if (c.error && !c.request) return `<pre class="bad">${esc(c.error)}</pre>`;
  const q = c.request || {}, r = c.response || {}, p = q.params || {};
  const dk = k => `data-key="c${c.id}-${k}" ${openDetails.has(`c${c.id}-${k}`) ? "open" : ""}`;
  const meta = [p.model, p.temperature != null ? `temperature ${p.temperature}` : "",
    p.max_tokens || p.max_completion_tokens ? `max_tokens ${p.max_tokens || p.max_completion_tokens}` : "", q.tools ? `工具 ${q.tools.join(", ")}` : "",
    q.response_format ? "json_schema" : ""].filter(Boolean).join(" · ");
  const msgs = (q.messages || []).map(m => `<div class="msg"><div class="role">${esc(ROLE[m.role] || m.role)}${m.tool_call_id ? " · " + esc(m.tool_call_id) : ""}</div>
    <pre>${esc(text(m.content))}${m.tool_calls ? "\n\n" + esc(JSON.stringify(m.tool_calls, null, 2)) : ""}</pre></div>`).join("");
  const reply = c.error ? `<pre class="bad">${esc(c.error)}</pre>` : [
    r.reasoning ? `<details ${dk("think")}><summary class="muted">思考过程</summary><pre>${esc(r.reasoning)}</pre></details>` : "",
    `<pre>${esc(text(r.content)) || '<span class="muted">（空）</span>'}</pre>`,
    r.tool_calls ? `<div class="role">工具调用</div><pre>${esc(JSON.stringify(r.tool_calls, null, 2))}</pre>` : "",
  ].join("");
  return `<div class="muted">${esc(meta)}</div>
    <details ${dk("req")}><summary>请求（${(q.messages || []).length} 条消息）</summary>${msgs}</details>
    <div class="msg"><div class="role">回复</div>${reply}</div>`;
}

function bindDetails(root) {
  root.querySelectorAll("details[data-key]").forEach(el => {
    el.addEventListener("toggle", () => {
      el.open ? openDetails.add(el.dataset.key) : openDetails.delete(el.dataset.key);
      if (el.open && el.dataset.call) loadCall(el);
    });
    if (el.open && el.dataset.call && !callCache.has(+el.dataset.call)) loadCall(el);
  });
}

async function loadCall(el) {
  const id = +el.dataset.call;
  if (!callCache.has(id)) callCache.set(id, await get("/api/call?id=" + id));
  const body = el.querySelector(".body");
  if (body.dataset.loaded) return;
  body.dataset.loaded = "1";
  body.innerHTML = renderCall(callCache.get(id));
  bindDetails(body);
}

function renderOverview(o) {
  const m = o.model;
  document.getElementById("model").innerHTML =
    `<span class="dot" style="background:var(${m.ready ? "--ok" : "--bad"})"></span>${esc(m.name)}${m.ready ? "" : " · " + esc(m.detail)}`;
  document.getElementById("counts").textContent =
    `进行中 ${o.counts.running} · 排队 ${o.counts.pending} · 失败 ${o.counts.failed}` +
    (o.waiting.length ? ` · 等流水线 ${o.waiting.length}` : "");
  const nh = o.needs_human || [], al = document.getElementById("alert");
  al.hidden = !nh.length;
  document.title = (nh.length ? `(${nh.length}) ` : "") + "华佗 状态";
  al.innerHTML = nh.length ? `<b>⚠ ${nh.length} 个问题待你裁决</b>
    <span class="muted">（在 MR 评论中回复 <code>/ai-confirm</code> 或 <code>/ai-accept</code>，或直接 resolve 讨论）</span><ul>` +
    nh.map(f => `<li><a href="${esc(f.url)}" target="_blank">${esc(f.project_path.split("/").pop())}!${f.mr_iid}</a>
      <span class="sev ${f.severity}">${f.severity}</span> ${esc(f.title)}
      <span class="muted">${esc(f.file || "")}${f.line ? ":" + f.line : ""}${f.status_reason ? " · " + esc(f.status_reason) : ""}</span></li>`).join("") + "</ul>" : "";
  document.getElementById("updated").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", {hour12: false});
  if (!pinned) { const r = o.jobs.find(j => j.status === "running") || o.jobs[0]; if (r) selected = r.id; }
  document.getElementById("jobs").innerHTML = o.jobs.map(j => `
    <div class="job ${j.id === selected ? "sel" : ""}" data-id="${j.id}">
      <div class="t">${esc(j.project_path.split("/").pop())}!${j.mr_iid}</div>
      <div class="m"><span class="st-${j.status}">${j.status === "running" ? '<span class="spin">◐</span> ' : ""}${STATUS[j.status] || j.status}</span>
        · ${KIND[j.kind] || j.kind} · #${j.id} · ${local(j.updated_at)}</div>
    </div>`).join("") || '<div class="empty">暂无任务</div>';
  document.querySelectorAll(".job").forEach(el => el.onclick = () => { selected = +el.dataset.id; pinned = true; refresh(); });
}

function renderJob(d) {
  const j = d.job, p = d.progress, g = d.graph;
  const running = j.status === "running";
  const cur = p.found ? p.phase : (j.status === "done" ? "done" : "load");
  const idx = PHASES.findIndex(x => x[0] === cur);
  const total = d.files.length, doneN = d.files.filter(f => f.state === "done").length;
  const elapsed = p.found ? secs(p.started, running ? nowStr() : p.last) : null;
  const steps = PHASES.map(([k, label], i) => {
    const state = i < idx || (i === idx && cur === "done") ? "done" : i === idx && running ? "cur" : "";
    const extra = k === "review" && total ? ` ${doneN}/${total}` : "";
    return `<div class="step ${state}">${state === "done" ? "✓ " : ""}${label}${extra}</div>`;
  }).join("");

  const fileRows = d.files.map(f => `
    <tr><td class="st-${f.state}">${f.state === "done" ? "✓" : f.state === "running" ? '<span class="spin">◐</span>' : "·"}</td>
      <td class="path">${esc(f.path)}${f.chunks > 1 ? ` <span class="muted">(${f.chunk}/${f.chunks})</span>` : ""}</td>
      <td>${f.state === "done" ? f.n : ""}</td>
      <td class="muted">${f.state === "done" ? dur(secs(f.since, f.ts)) : f.state === "running" ? dur(secs(f.since, nowStr())) : ""}</td></tr>`).join("");

  const findings = d.findings.map(f => {
    const key = "f" + f.id;
    return `<tr><td class="sev ${f.severity}">${f.severity}</td><td>${esc(f.status)}</td>
      <td class="path">${esc(f.file)}:${f.line ?? ""}</td>
      <td><details data-key="${key}" ${openDetails.has(key) ? "open" : ""}><summary>${esc(f.title)}${f.source === "lint" ? ' <span class="pill">lint</span>' : ""}</summary>
        <div class="muted">${esc(f.category)} · 更新于 ${local(f.updated_at)}${f.status_reason ? " · " + esc(f.status_reason) : ""}</div>
        <p>${esc(f.detail)}</p>${f.evidence ? `<pre>${esc(f.evidence)}</pre>` : ""}
        ${f.suggestion ? `<div class="muted">建议</div><pre>${esc(f.suggestion)}</pre>` : ""}
        ${f.events.length ? `<div class="muted">事件</div><pre>${f.events.map(e => `${local(e.created_at)} ${e.actor}: ${e.kind} ${(e.content || "").slice(0, 200)}`).map(esc).join("\n")}</pre>` : ""}
      </details></td></tr>`;
  }).join("");

  const calls = d.calls || [];
  const callRows = calls.map(c => {
    const key = "c" + c.id;
    const tok = c.prompt_tokens != null ? `${c.prompt_tokens} → ${c.completion_tokens ?? "?"}` : "";
    return `<tr><td class="muted">${utcLocal(c.started_at)}</td>
      <td class="step"><details data-key="${key}" data-call="${c.id}" ${openDetails.has(key) ? "open" : ""}>
        <summary>${esc(c.step || c.node || "")}${c.error ? ' <span class="bad">失败</span>' : ""}</summary>
        ${callCache.has(c.id) && openDetails.has(key) ? `<div class="body" data-loaded="1">${renderCall(callCache.get(c.id))}</div>`
          : '<div class="body"><span class="muted">加载中…</span></div>'}</details></td>
      <td class="muted">${c.secs != null ? dur(Math.round(c.secs)) : ""}</td><td class="muted">${tok}</td></tr>`;
  }).join("");

  const info = [
    g.intent ? `<div><span class="muted">意图：</span>${esc(g.intent)}</div>` : "",
    g.lint_new != null ? `<div><span class="muted">golangci-lint 新引入：</span>${g.lint_new} 条</div>` : "",
    p.aggregate ? `<div><span class="muted">聚合：</span>新问题 ${p.aggregate.new} 条，丢弃 ${p.aggregate.dropped} 条</div>` : "",
    p.conclusion ? `<div><span class="muted">结论：</span><b>${esc(p.conclusion)}</b></div>` : "",
    (g.notes || []).map(n => `<div class="muted">· ${esc(n)}</div>`).join(""),
    j.error ? `<pre>${esc(j.error)}</pre>` : "",
  ].join("");

  document.getElementById("main").innerHTML = `
    <div class="card">
      <h2><a href="${esc(j.url)}" target="_blank">${esc(j.project_path)}!${j.mr_iid}</a>
        <span class="pill st-${j.status}">${STATUS[j.status] || j.status}</span>
        <span class="pill">${KIND[j.kind] || j.kind}</span>
        <span class="muted" style="font-weight:400">#${j.id}${elapsed != null ? " · 用时 " + dur(elapsed) : ""}</span></h2>
      <div class="steps">${steps}</div>
      ${total ? `<div class="bar"><div style="width:${Math.round(doneN / total * 100)}%"></div></div>` : ""}
      <div style="margin-top:10px">${info}</div>
    </div>
    ${total ? `<div class="card"><h2>文件块（${doneN}/${total}）</h2>
      <table><tr><th></th><th>文件</th><th>候选</th><th>用时</th></tr>${fileRows}</table></div>` : ""}
    <div class="card"><h2>该 MR 的问题（${d.findings.length}）</h2>
      ${d.findings.length ? `<table><tr><th>级别</th><th>状态</th><th>位置</th><th>问题</th></tr>${findings}</table>` : '<div class="muted">暂无</div>'}</div>
    <div class="card"><h2>模型调用（${calls.length}）</h2>
      ${calls.length ? `<table class="calls"><tr><th>时间</th><th>步骤（点击查看 prompt 与回复）</th><th>用时</th><th>tokens 入→出</th></tr>${callRows}</table>`
        : '<div class="muted">暂无记录（开启记录之后的调用才会出现）</div>'}</div>
    ${p.warnings && p.warnings.length ? `<div class="card"><h2>警告（${p.warnings.length}）</h2><pre>${p.warnings.map(esc).join("\n")}</pre></div>` : ""}
    <div class="card"><h2>日志</h2><pre class="log" id="log">${(p.lines || []).map(esc).join("\n") || "（日志中没有这个任务的记录）"}</pre></div>`;
  bindDetails(document.getElementById("main"));
  const lg = document.getElementById("log"); lg.scrollTop = lg.scrollHeight;
}

async function refresh() {
  try {
    renderOverview(await get("/api/overview"));
    if (selected != null) {
      const d = await get("/api/job?id=" + selected);
      const raw = JSON.stringify(d);
      if (!d.error && raw !== lastJobRaw) { lastJobRaw = raw; renderJob(d); }
    } else {
      document.getElementById("main").innerHTML = '<div class="empty">暂无任务</div>';
    }
  } catch (e) {
    document.getElementById("updated").textContent = "连接失败：" + e;
  }
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""
