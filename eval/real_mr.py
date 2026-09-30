"""对真实 MR 做 dry-run 审查（不写 GitLab）。
用法：uv run python eval/real_mr.py <project_path> <mr_iid> [--static] [--fresh]

进度存在 data/eval_checkpoints.db（按 MR + head sha 区分）：中途失败或中断后再次运行会从断点继续，--fresh 从头开始。
"""
import contextlib, io, logging, sqlite3, sys, time

from langgraph.checkpoint.sqlite import SqliteSaver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
from ai_cr.deps import Deps  # noqa: E402
from ai_cr.graph.build import build_graph  # noqa: E402

pp, iid = sys.argv[1], int(sys.argv[2])
deps = Deps.create()
deps.settings.config.review.static_analysis.enabled = "--static" in sys.argv
_orig_mr = deps.gl.mr


def _mr(pp_, iid_):  # 已合并的 MR 伪装为 opened（dry_run 不写 GitLab）
    m = _orig_mr(pp_, iid_); m.state = "opened"; return m


deps.gl.mr = _mr
head = _orig_mr(pp, iid).diff_refs["head_sha"]
conn = sqlite3.connect(deps.cfg.data_path / "eval_checkpoints.db", check_same_thread=False)
g = build_graph(deps, SqliteSaver(conn))
cfg = {"configurable": {"thread_id": f"eval-{pp}!{iid}@{head[:12]}" + (f"-{time.time():.0f}" if "--fresh" in sys.argv else "")},
       "max_concurrency": 1, "recursion_limit": 200}
t = time.time()
with contextlib.redirect_stdout(io.StringIO()):
    if g.get_state(cfg).next:
        logging.info("从断点继续")
        s = g.invoke(None, cfg)
    else:
        s = g.invoke({"event": {"kind": "new_push", "project_path": pp, "mr_iid": iid},
                      "dry_run": True, "full": True}, cfg)
fs = [f for f in s.get("findings", []) if f.get("status") == "OPEN"]
print(f"DONE {pp}!{iid} secs={round(time.time() - t)} n={len(fs)} conclusion={s.get('conclusion')}", flush=True)
for f in fs:
    print(f"  - {f['severity']} {f['file']} L{f['line']} {f['title']}" + (" [旧记录]" if f.get("id") else ""), flush=True)
