"""LangGraph 节点实现。"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict
from collections import Counter

import yaml
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ..deps import Deps
from ..diff_parser import FileDiff
from ..git_repo import clean_evidence, is_ignored, locate_evidence, locate_snippet, normalize_code
from ..llm import (
    chat, clean_text, estimate_tokens, human, invoke_structured, invoke_text, raise_if_unavailable, render, system,
    with_think_mode,
)
from ..static_analysis import run_golangci
from ..trace import set_step
from . import render as R
from .context_pack import build_context_pack
from .gate import compute_conclusion
from .state import (
    CLOSED_STATES, OPEN_STATES, Answer, DisputeVerdict, FindingList, FixCheck, LLMFinding, ReplyIntent,
    ReviewState, VerifyVerdict, fingerprint,
)
from .tools import make_tools, numbered

log = logging.getLogger(__name__)

ORDER = {"P0": 0, "P1": 1, "P2": 2}

PASS_CHECKLISTS = {
    "correctness": (
        "逻辑正确性与并发安全",
        "- 逻辑是否与 MR 意图一致；条件判断、边界值、off-by-one、错误的变量\n"
        "- nil / 空指针 / 越界访问导致 panic：只在代码中能看到 nil 的来源时才报——map 查找未命中、"
        "不带 ok 的类型断言、明确可能返回 nil 的函数、错误分支中仍为 nil 的变量。"
        "函数参数没有判空不算问题，除非 diff 中能看到调用方会传入 nil\n"
        "- 并发：共享变量（map、slice、结构体字段）是否在无锁情况下被多个 goroutine 读写；锁的粒度与顺序是否会死锁；"
        "channel 是否可能阻塞或重复关闭；循环变量被 goroutine 捕获\n"
        "- 数据一致性：先写后读、事务边界、重试导致的重复写入",
    ),
    "robustness": (
        "资源管理与错误处理",
        "- goroutine、连接、文件、Body、Rows、Ticker、context cancel 是否在所有路径（含错误路径）释放\n"
        "- 错误是否被吞掉（`_ =`、只打印不返回）、是否丢失上下文、是否错误地继续执行\n"
        "- 超时与取消：外部调用是否设置超时、是否传递 context\n"
        "- 重试是否有上限与退避",
    ),
    "security_performance": (
        "安全与性能",
        "- 敏感信息硬编码（token、密码、密钥）、日志中打印敏感数据\n"
        "- SQL/命令注入、路径穿越、越权、未校验的外部输入\n"
        "- 循环内的网络/数据库 IO、N+1 查询、O(N²) 算法、大对象拷贝、无界缓存/队列导致内存增长\n"
        "- 热路径上不必要的内存分配、锁竞争",
    ),
    "all": (
        "全部维度",
        "- 逻辑正确性、nil/越界 panic、并发安全（竞态、死锁）\n"
        "- 资源释放、错误处理、超时与取消\n"
        "- 安全（注入、越权、敏感信息）、性能（循环 IO、复杂度、内存）",
    ),
    "test": (
        "测试代码质量",
        "- 测试是否真的覆盖了它声称的场景：调用的函数/参数/配置与用例名和断言意图是否一致，断言是否恒真或缺失\n"
        "- 不稳定（flaky）：依赖 time.Sleep 等待异步结果、goroutine 无同步、t.Parallel 下共享可变状态、依赖执行顺序或外部环境\n"
        "- 资源与 goroutine 泄漏：Server/连接/临时文件未关闭，缺少 t.Cleanup / defer\n"
        "- 错误被忽略导致测试在失败时仍然通过（例如忽略 setup 的 err）\n"
        "不要报：测试中的硬编码值、魔法数字、性能、安全（测试代码不上线）；只在单线程中执行的代码不存在并发问题",
    ),
}
# 测试代码出问题不会影响线上，其问题最高按 P2 处理，不阻断 MR
TEST_MAX_SEVERITY = "P2"

# 生成代码的标准标记（https://go.dev/s/generatedcode，protoc/mockgen/stringer 等都遵循）
GENERATED_RE = re.compile(r"^// Code generated .* DO NOT EDIT\.$", re.M)


def is_generated(content: str | None) -> bool:
    """文件头部（package 声明之前的注释区）带生成标记，或是压缩/混淆过的产物（例如打包后的 JS）。"""
    if not content:
        return False
    return GENERATED_RE.search(content[:2000]) is not None or is_minified(content)


def _finding_line(x: dict) -> str:
    """已有问题的一行摘要；已关闭的附上关闭理由（例如修复验证的结论），供审查和复核参考。"""
    line = f"- L{x.get('line')} [{x['severity']}] {x['title']}（{x['status']}）"
    reason = re.sub(r"^\W*已验证修复（`?\w+`?）：", "", x.get("status_reason") or "").strip()
    if x.get("status") in CLOSED_STATES and reason:
        line += f"：{reason[:200]}"
    return line


def _path_affinity(a: str, b: str) -> int:
    """两个路径的相关度：同文件 > 共同目录层级越深越相关。sorted 是稳定的，相关度相同时保持最近优先。"""
    if a == b:
        return 1000
    n = 0
    for x, y in zip(a.split("/")[:-1], b.split("/")[:-1]):
        if x != y:
            break
        n += 1
    return n

def is_minified(content: str) -> bool:
    # 手写代码几乎不会有上千字符的行；压缩/混淆的 JS 常常整个文件只有一行，按 token 计比字符数更膨胀，会撑爆上下文
    return any(len(line) > 2000 for line in content.splitlines())


DISPUTE_WORDS = ("误报", "不是问题", "不存在", "没问题", "设计如此", "故意", "不需要", "false positive", "by design")
FIXED_WORDS = ("已修复", "已修改", "已改", "修复了", "改了", "fixed", "done")
DEFER_WORDS = ("后续", "以后", "下个版本", "下一版", "TODO", "issue", "later")


def _slim_mr(a: dict) -> dict:
    keys = ("iid", "title", "description", "source_branch", "target_branch", "web_url", "state", "draft", "sha")
    out = {k: a.get(k) for k in keys}
    out["author"] = (a.get("author") or {}).get("username")
    out["project_id"] = a.get("project_id")
    return out


def _code_window(content: str | None, center: int | None, radius: int = 40) -> str:
    if not content:
        return "（文件不存在）"
    center = center or 1
    return numbered(content, center - radius, center + radius)


def _keyword_intent(text: str) -> str:
    t = text.lower()
    if any(w.lower() in t for w in FIXED_WORDS):
        return "claim_fixed"
    if any(w.lower() in t for w in DEFER_WORDS):
        return "defer"
    if any(w.lower() in t for w in DISPUTE_WORDS):
        return "dispute"
    if "?" in t or "？" in t:
        return "question"
    return "other"


class Nodes:
    def __init__(self, deps: Deps):
        self.d = deps

    # ------------------------------------------------------------------ 公共
    def _ctx(self, state: ReviewState):
        ev = state["event"]
        return ev["project_path"], ev["mr_iid"]

    def _sys(self):
        return system(render("system"))

    def load_context(self, state: ReviewState) -> dict:
        pp, iid = self._ctx(state)
        mr = self.d.gl.mr(pp, iid)
        a = mr.attributes
        if a.get("state") != "opened":
            log.info("%s!%s 状态为 %s，跳过", pp, iid, a.get("state"))
            return {"mr": _slim_mr(a), "event": state["event"] | {"skip": True}}
        refs = a.get("diff_refs") or {}
        if not refs.get("head_sha"):
            raise RuntimeError("MR diff_refs 尚未生成，稍后重试")
        mirror = self.d.mirror(pp)
        mirror.fetch_mr(iid, a["target_branch"])
        head = refs["head_sha"]

        ev = state["event"]
        skip = ev["kind"] == "new_push" and not state.get("full") and not state.get("dry_run") \
            and self.d.store.reviewed(pp, iid, head)
        claude_md = mirror.show(head, "CLAUDE.md") or mirror.show(head, "AGENTS.md") or ""
        rules_raw = mirror.show(head, ".ai-review.yaml") or ""
        try:
            rules = (yaml.safe_load(rules_raw) or {}) if rules_raw else {}
        except yaml.YAMLError:
            rules = {}
        repo_rules = "\n".join(f"- {r}" for r in rules.get("rules", [])) or "（无）"
        return {
            "mr": _slim_mr(a) | {"repo_ignore": rules.get("ignore", [])},
            "diff_refs": refs,
            "head_sha": head,
            "claude_md": claude_md[:8000] or "（无）",
            "repo_rules": repo_rules,
            "findings": self.d.store.findings(pp, iid),
            "event": ev | {"skip": skip},
        }

    # ------------------------------------------------------------------ 新提交：审查
    def plan_review(self, state: ReviewState) -> dict:
        pp, iid = self._ctx(state)
        cfg = self.d.cfg.review
        refs, head = state["diff_refs"], state["head_sha"]
        mirror = self.d.mirror(pp)
        diffs = self.d.mr_diff(pp, refs["base_sha"], head)
        notes: list[str] = []

        changed_since = None
        last = self.d.store.mr_state(pp, iid).get("last_reviewed_sha")
        if last and not state.get("full") and last != head and mirror.has_commit(last) and mirror.is_ancestor(last, head):
            changed_since = set(mirror.changed_files(last, head))
            notes.append(f"增量审查：仅审查 `{last[:8]}..{head[:8]}` 之间有变化的文件。")

        ignore = cfg.ignore + list(state["mr"].get("repo_ignore") or [])
        candidates: list[FileDiff] = []
        generated: list[str] = []
        for path, fd in diffs.items():
            if fd.deleted or fd.binary or not fd.hunks or is_ignored(path, ignore):
                continue
            if changed_since is not None and path not in changed_since:
                continue
            if is_generated(mirror.show(head, path)):  # 生成代码没有审查价值，模型缺少源接口时还容易误报
                generated.append(path)
                continue
            candidates.append(fd)
        if generated:
            notes.append(f"跳过 {len(generated)} 个自动生成的文件：" + "、".join(f"`{p}`" for p in generated[:20]))
        if len(candidates) > cfg.max_files:
            candidates.sort(key=lambda f: ("_test." in f.path, -len(f.added_lines())))
            skipped = candidates[cfg.max_files:]
            candidates = candidates[: cfg.max_files]
            notes.append(f"改动文件过多，以下 {len(skipped)} 个文件未审查：" + "、".join(f"`{f.path}`" for f in skipped[:20]))

        files = []
        for fd in candidates:
            groups = fd.chunks(cfg.max_chunk_chars)
            for i, hunks in enumerate(groups):
                files.append({"path": fd.path, "diff": fd.annotated(hunks), "chunk": i + 1, "chunks": len(groups)})

        intent = state["mr"]["title"] or ""
        if files:
            set_step("MR 意图摘要")
            try:
                intent = invoke_text([human(render(
                    "intent", title=state["mr"]["title"], description=(state["mr"]["description"] or "")[:3000],
                    files="\n".join(sorted({f["path"] for f in files})),
                ))])[:200]
            except Exception as e:  # noqa: BLE001
                raise_if_unavailable(e)
                log.warning("生成意图摘要失败: %s", e)

        # 静态分析覆盖整个 MR 的改动（不受增量审查影响），用于新问题发现和 lint 类问题的修复验证
        lint_issues = None
        sa = cfg.static_analysis
        lint_targets = [fd for p_, fd in diffs.items() if not fd.deleted and not fd.binary and not is_ignored(p_, ignore)]
        if sa.enabled and any(fd.new_path.endswith(".go") for fd in lint_targets):
            try:
                found = run_golangci(
                    mirror, refs["base_sha"], head, lint_targets,
                    self.d.cfg.data_path / "worktrees" / f"{pp.replace('/', '__')}-{iid}-{head[:8]}",
                    configured_path=sa.golangci_lint, extra_linters=sa.extra_linters, timeout=sa.timeout_seconds,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("静态分析失败: %s", e)
                found = None
            if found is None:
                notes.append("静态分析（golangci-lint）未能执行，本轮仅基于模型审查。")
            else:
                lint_issues = [asdict(i) | {"fingerprint": i.fingerprint} for i in found]
        hints: dict[str, list[str]] = {}
        for li in lint_issues or []:
            hints.setdefault(li["file"], []).append(f"L{li['line']} [{li['linter']}] {li['text']}")
        for f in files:
            f["static_hints"] = "\n".join(hints.get(f["path"], [])) or "（无）"
        log.info("%s!%s 待审 %d 个文件块", pp, iid, len(files))
        return {"files": files, "intent": intent, "notes": notes, "lint_issues": lint_issues}

    def review_file(self, payload: dict) -> dict:
        """单个文件块：先理解（可调用工具），再按维度逐轮审查。"""
        f = payload["file"]
        pp, _ = payload["event"]["project_path"], payload["event"]["mr_iid"]
        cfg = self.d.cfg.review
        head = payload["head_sha"]
        mirror = self.d.mirror(pp)
        fd = self.d.mr_diff(pp, payload["diff_refs"]["base_sha"], head).get(f["path"])
        try:
            context_pack = build_context_pack(mirror, head, fd) if fd else "（无）"
        except Exception as e:  # noqa: BLE001
            log.warning("context pack 失败: %s", e)
            context_pack = "（无）"

        chunk = f" ({f['chunk']}/{f['chunks']})" if f.get("chunks", 1) > 1 else ""
        set_step(f"理解 {f['path']}{chunk}")
        understanding = self._understand(payload, f, context_pack, mirror, head)

        known = [x for x in payload.get("findings", []) if x["file"] == f["path"]]
        known_text = "\n".join(_finding_line(x) for x in known) or "（无）"
        # 同一文件、同一目录的误报例子最有参考价值，其次才是最近的
        fps = sorted(self.d.store.feedback(pp, "false_positive", 50),
                     key=lambda s: _path_affinity(s["finding"].get("file") or "", f["path"]), reverse=True)[:3]
        feedback = ""
        if fps:
            feedback = "本仓库曾被判定为误报的例子（避免类似判断）：\n" + "\n".join(
                f"- {s['finding']['title']} —— 误报原因：{s['reason']}" for s in fps
            )

        out: list[dict] = []
        if is_ignored(f["path"], cfg.test_files):
            passes = ["test"]
        elif len(cfg.passes) > 1 and is_ignored(f["path"], cfg.light_files):
            passes = ["all"]
        else:
            passes = cfg.passes
        for pass_key in passes:
            name, checklist = PASS_CHECKLISTS.get(pass_key, PASS_CHECKLISTS["all"])
            set_step(f"审查 {f['path']}{chunk} [{pass_key}]")
            # review_pass.md 把各轮相同的内容（规范、理解、diff）放在前面，随轮次变化的清单和已知问题放在末尾，
            # 这样后几轮能复用模型服务对公共前缀的 KV 缓存，只需处理末尾几百个 token
            msg = render(
                "review_pass", pass_name=name, checklist=checklist, claude_md=payload["claude_md"],
                repo_rules=payload["repo_rules"], feedback=feedback, known_findings=known_text,
                intent=payload.get("intent", ""), path=f["path"], understanding=understanding,
                context_pack=context_pack, static_hints=f.get("static_hints", "（无）"), diff=f["diff"],
            )
            try:
                # 两步：先自由文本审查（不受 JSON 约束，思考质量更好），再把结论转成 JSON
                msgs = [self._sys(), human(msg)]
                review_text = invoke_text(msgs)
                if re.search(r"确认的问题[：:]\s*无", review_text):
                    continue
                result = invoke_structured(FindingList, [*msgs, AIMessage(review_text), human(render("to_json", path=f["path"]))])
            except Exception as e:  # noqa: BLE001
                raise_if_unavailable(e)
                log.warning("审查 %s [%s] 失败: %s", f["path"], pass_key, e)
                continue
            for item in result.findings:
                d = item.model_dump()
                d["file"] = f["path"]
                d["pass"] = pass_key
                out.append(d)
            known_text += "".join(f"\n- L{x.line} [{x.severity}] {x.title}（本次）" for x in result.findings)
        log.info("  %s (%d/%d): %d 条候选问题", f["path"], f["chunk"], f["chunks"], len(out))
        return {"raw_findings": out}

    def _understand(self, payload: dict, f: dict, context_pack: str, mirror, head: str) -> str:
        cfg = self.d.cfg.review
        mr = payload["mr"]
        text = render(
            "understand", title=mr["title"], description=(mr.get("description") or "")[:2000],
            intent=payload.get("intent", ""), path=f["path"], context_pack=context_pack, diff=f["diff"],
            tools_hint="如需确认调用方、类型定义或加锁情况，可以调用工具 read_file / grep / list_dir / git_log 查看仓库代码（最多几次）。"
            if cfg.enable_tools else "",
        )
        msgs = with_think_mode([self._sys(), human(text)], "review")
        if not cfg.enable_tools:
            return invoke_text(msgs)
        tools = make_tools(mirror, head)
        tool_map = {t.name: t for t in tools}
        env = self.d.settings.env
        # 工具结果会累积在对话里；给最终回答留出输出空间和余量，超出预算就不再调用工具
        budget = int((env.llm_context_tokens - env.llm_max_tokens) * 0.8)
        try:
            llm = chat("review").bind_tools(tools)
            for _ in range(cfg.max_tool_steps):
                ai = llm.invoke(msgs)
                msgs.append(ai)
                if not ai.tool_calls:
                    return clean_text(ai.content)
                for tc in ai.tool_calls:
                    t = tool_map.get(tc["name"])
                    try:
                        result = t.invoke(tc["args"]) if t else f"未知工具 {tc['name']}"
                    except Exception as e:  # noqa: BLE001
                        result = f"工具调用失败: {e}"
                    room = max(0, (budget - estimate_tokens(msgs)) * 3)
                    content = str(result)[:min(6000, room)] or "（上下文预算已用完，未返回结果）"
                    msgs.append(ToolMessage(content=content, tool_call_id=tc["id"]))
                if estimate_tokens(msgs) >= budget:
                    break
            msgs.append(HumanMessage("请停止调用工具，直接给出理解分析。"))
            return clean_text(chat("review").invoke(msgs).content)
        except Exception as e:  # noqa: BLE001 服务端不支持工具调用时退化为直接理解
            raise_if_unavailable(e)
            log.warning("工具调用模式失败，退化为直接理解: %s", str(e)[:200])
        try:
            return invoke_text([self._sys(), human(text)])
        except Exception as e:  # noqa: BLE001 理解只是辅助，失败时仍按 diff 审查，不能让单个文件拖垮整个 MR
            raise_if_unavailable(e)
            log.warning("理解 %s 失败，跳过理解阶段: %s", f["path"], str(e)[:200])
            return "（无）"

    def aggregate(self, state: ReviewState) -> dict:
        """校验证据、修正行号、去重。"""
        pp, _ = self._ctx(state)
        head, refs = state["head_sha"], state["diff_refs"]
        mirror = self.d.mirror(pp)
        pos_diffs = self.d.mr_diff(pp, refs["base_sha"], head, for_position=True)
        existing = state.get("findings", [])
        new: list[dict] = self._lint_findings(state, existing, pos_diffs)  # lint 结果先入列，模型的重复报告会并入它
        dropped = 0
        cache: dict[str, str | None] = {}
        for r in state.get("raw_findings", []):
            path = r["file"]
            self._cap_test_severity(r)  # 在复核之前降级，测试文件的问题不会走 P0 的复核规则
            if path not in cache:
                cache[path] = mirror.show(head, path)
            content = cache[path]
            loc = locate_evidence(content or "", r.get("evidence", ""), r.get("line"))
            r["evidence"] = clean_evidence(r.get("evidence", ""))
            if loc is None:
                dropped += 1
                log.info("丢弃（证据代码在文件中找不到）: %s L%s %s", path, r.get("line"), r["title"])
                continue
            span = len([x for x in r["evidence"].strip().splitlines() if x.strip()])
            if not (loc <= (r.get("line") or 0) <= loc + span):
                r["line"] = loc
            fd = pos_diffs.get(path)
            near_change = bool(fd) and any(abs(n - r["line"]) <= 3 for n in fd.added_lines())
            if not near_change:
                # 模型读的是带完整函数上下文的 diff，容易把未改动的历史代码也报出来；只保留 P0（以普通讨论发布）
                if r["severity"] != "P0":
                    dropped += 1
                    log.info("丢弃（不在本次改动附近）: %s L%s %s", path, r["line"], r["title"])
                    continue
                r["inline"] = False
            else:
                line = fd.nearest_commentable(r["line"])
                r["line"] = line or r["line"]
                r["inline"] = line is not None
            r["fingerprint"] = fingerprint(path, r["category"], r["evidence"], r["title"])
            if self._match(r, existing) is not None:
                continue
            # 本轮新问题之间不跨类别合并：同一行上的“忽略错误”和“参数可能为 nil”是两个问题，
            # 合并后只留更严重的那条，真问题会被推测性的 P0 吞掉，还会被误算成多轮共识
            i = self._match(r, new, cross_category=False)
            if i is not None:
                # 多轮独立报告同一处：记为共识票数，保留更严重的描述
                x = new[i]
                passes = set(x["passes"]) | {r.get("pass")}
                if ORDER[r["severity"]] < ORDER[x["severity"]]:
                    x = r | {"status": "NEW"}
                new[i] = x | {"passes": sorted(p for p in passes if p)}
                continue
            r["status"] = "NEW"
            r["passes"] = [r.get("pass")] if r.get("pass") else []
            new.append(r)
        log.info("聚合：新问题 %d 条，丢弃 %d 条", len(new), dropped)
        return {"findings": existing + new}

    def _lint_findings(self, state: ReviewState, existing: list[dict], pos_diffs: dict) -> list[dict]:
        sev_map = self.d.cfg.review.static_analysis.severity
        known = {x["fingerprint"] for x in existing}
        out = []
        for li in state.get("lint_issues") or []:
            if li["fingerprint"] in known:
                continue
            fd = pos_diffs.get(li["file"])
            inline = bool(fd and fd.position_for(li["line"]))  # 死代码等问题常落在未改动行上，此时以普通讨论发布
            out.append(self._cap_test_severity({
                "file": li["file"], "line": li["line"], "end_line": None,
                "severity": sev_map.get(li["linter"], sev_map.get("default", "P2")), "category": "lint",
                "title": f"[{li['linter']}] {li['text']}",
                "detail": f"golangci-lint `{li['linter']}` 报告的问题，由本次 MR 引入（目标分支上不存在）：{li['text']}",
                "evidence": li.get("source_line") or "", "suggestion": None,
                "fingerprint": li["fingerprint"], "inline": inline, "status": "NEW", "source": "lint",
                "passes": ["lint"],
            }))
        return out

    @staticmethod
    def _match(r: dict, pool: list[dict], cross_category: bool = True) -> int | None:
        """同一指纹；或同一文件中：相邻位置（同类 ±3 行 / 跨类 ±2 行），或标题相同且相距不超过 10 行。
        cross_category=False 时跨类只在一方是 lint 结果时按位置合并（模型常把 lint 报过的问题再报一遍）。"""
        title = normalize_code(r.get("title", ""))
        for i, x in enumerate(pool):
            dist = abs((x.get("line") or 0) - (r.get("line") or 0))
            near_any = dist <= 2 and (cross_category or "lint" in (x.get("source"), r.get("source")))
            if x["fingerprint"] == r["fingerprint"] or (x["file"] == r["file"] and (
                near_any or (x["category"] == r["category"] and dist <= 3)
                or (dist <= 10 and normalize_code(x.get("title", "")) == title)
            )):
                return i
        return None

    def verify(self, state: ReviewState) -> dict:
        """复核新问题：P0/P1 交给复核模型（可投票），不成立的丢弃。"""
        pp, _ = self._ctx(state)
        head, refs = state["head_sha"], state["diff_refs"]
        mirror = self.d.mirror(pp)
        diffs = self.d.mr_diff(pp, refs["base_sha"], head)
        votes = max(1, self.d.cfg.review.verify_votes)
        result, actions = [], []
        for f in state["findings"]:
            if f.get("status") != "NEW":
                result.append(f)
                continue
            consensus = len(f.get("passes") or [])
            p0 = f["severity"] == "P0"
            # 同一文件里已判定修复/撤回的问题：新问题可能只是换个说法重提，必须复核并让复核看到当时的结论
            closed = [x for x in state["findings"] if x["file"] == f["file"] and x.get("status") in ("FIXED", "WITHDRAWN")]
            skip_by_consensus = consensus >= 2 and not p0 and not closed
            if f.get("source") == "lint":
                pass  # 静态分析结论是确定的，不需要模型复核
            elif skip_by_consensus:
                log.info("多轮共识（%d 轮），跳过复核: %s L%s %s", consensus, f["file"], f["line"], f["title"])
            else:  # 只被一轮报出的问题都要复核；P0 会阻断 MR，即使多轮共识也要复核
                fd = diffs.get(f["file"])
                msg = render(
                    "verify", file=f["file"], line=f["line"], severity=f["severity"], category=f["category"],
                    title=f["title"], detail=f["detail"], evidence=f["evidence"],
                    code=_code_window(mirror.show(head, f["file"]), f["line"]),
                    diff=fd.annotated() if fd else "",
                    closed="\n".join(_finding_line(x) for x in closed) or "（无）",
                )
                set_step(f"复核 {f['file']} L{f['line']} {f['title']}")
                if p0:
                    # P0 多数票：先投两票，意见不一致再加第三票。单票放行曾让两个误报 P0 阻断了 MR
                    verdicts = self._verify_votes(msg, max(2, votes), temperature=0.3)
                    if len(verdicts) == 2 and verdicts[0].valid != verdicts[1].valid:
                        verdicts += self._verify_votes(msg, 1, temperature=0.4)
                else:
                    verdicts = self._verify_votes(msg, votes)
                if verdicts:
                    if sum(v.valid for v in verdicts) * 2 <= len(verdicts):
                        log.info("复核判定误报，丢弃: %s L%s %s（%d/%d 票成立；%s）", f["file"], f["line"], f["title"],
                                 sum(v.valid for v in verdicts), len(verdicts), next(v for v in verdicts if not v.valid).reason)
                        continue
                    f["severity"] = Counter(v.severity for v in verdicts if v.valid).most_common(1)[0][0]
                    self._cap_test_severity(f)  # 复核模型可能把级别调回 P0/P1
            f |= {"status": "OPEN", "first_sha": head, "last_checked_sha": head, "dispute_rounds": 0}
            result.append(f)
            actions.append({"kind": "create", "fingerprint": f["fingerprint"]})
            actions.append({"kind": "event", "fingerprint": f["fingerprint"], "actor": "ai", "event": "created",
                            "content": f["title"]})
        return {"findings": result, "actions": actions}

    def _verify_votes(self, msg: str, n: int, temperature: float | None = None) -> list[VerifyVerdict]:
        out = []
        for _ in range(n):
            try:
                out.append(invoke_structured(VerifyVerdict, [self._sys(), human(msg)], role="verify",
                                             temperature=temperature if temperature is not None else (0.3 if n > 1 else None)))
            except Exception as e:  # noqa: BLE001
                raise_if_unavailable(e)
                log.warning("复核失败，保留原结论: %s", e)
        return out

    # ------------------------------------------------------------------ 修复验证
    def verify_fixes(self, state: ReviewState) -> dict:
        """新 push 后，验证所有未关闭的问题。"""
        pp, _ = self._ctx(state)
        head = state["head_sha"]
        mirror = self.d.mirror(pp)
        findings, actions = [], []
        for f in state["findings"]:
            if f.get("status") not in OPEN_STATES or f.get("last_checked_sha") == head:
                findings.append(f)
                continue
            last = f["last_checked_sha"]
            if f.get("source") == "lint":
                lint = state.get("lint_issues")
                if lint is None:  # 本轮静态分析没跑成，保持原状态
                    findings.append(f)
                elif f["fingerprint"] in {x["fingerprint"] for x in lint}:
                    findings.append(f | {"last_checked_sha": head})
                else:
                    upd, acts = self._close(f | {"last_checked_sha": head}, "FIXED",
                                            f"✅ 已验证修复（`{head[:8]}`）：golangci-lint 不再报告该问题。", resolve=True)
                    findings.append(upd)
                    actions.extend(acts)
                continue
            if mirror.has_commit(last) and f["file"] not in mirror.changed_files(last, head):
                findings.append(f | {"last_checked_sha": head})
                continue
            upd, acts, extra = self._check_fix(state, f, since=last, developer_note="", announce=False)
            findings.append(upd)
            findings.extend(extra)
            actions.extend(acts)
        return {"findings": findings, "actions": actions}

    def _check_fix(self, state: ReviewState, f: dict, since: str, developer_note: str, announce: bool):
        pp, _ = self._ctx(state)
        head = state["head_sha"]
        mirror = self.d.mirror(pp)
        fp = f["fingerprint"]
        content = mirror.show(head, f["file"])
        if content is None:
            return self._close(f, "FIXED", "文件已被删除，问题不再存在。", resolve=True) + ([],)
        if not mirror.has_commit(since):
            since = state["diff_refs"]["base_sha"]
        diff = mirror.diff(since, head, [f["file"]], function_context=False) or "（无变化）"
        loc = locate_snippet(content, f["evidence"], f.get("line"))
        msg = render(
            "fix_check", file=f["file"], line=f.get("line"), severity=f["severity"], category=f["category"],
            title=f["title"], detail=f["detail"], evidence=f["evidence"], developer_note=developer_note or "（无）",
            diff=diff[:30000], code=_code_window(content, loc or f.get("line")),
        )
        set_step(f"修复验证 {f['file']} L{f.get('line')} {f['title']}")
        try:
            check = invoke_structured(FixCheck, [self._sys(), human(msg)], role="verify")
            if check.status == "fixed" and f["severity"] in ("P0", "P1"):
                # 单次判断会把“只修了一半”误判为已修复：P0/P1 要第二票也认为已修复才关闭，否则采用第二票
                second = invoke_structured(FixCheck, [self._sys(), human(msg)], role="verify", temperature=0.3)
                if second.status != "fixed":
                    log.info("修复验证两票不一致（fixed / %s），采用后者: %s", second.status, f["title"])
                    check = second
        except Exception as e:  # noqa: BLE001
            raise_if_unavailable(e)
            log.warning("修复验证失败: %s", e)
            return f, [], []
        f = f | {"last_checked_sha": head, "line": loc or f.get("line")}
        extra: list[dict] = []
        if check.new_issue:
            ni = self._normalize_new(state, check.new_issue, parent=fp)
            if ni:
                extra.append(ni)
        new_actions = [{"kind": "create", "fingerprint": x["fingerprint"]} for x in extra]
        if check.status == "fixed":
            upd, acts = self._close(f, "FIXED", f"✅ 已验证修复（`{head[:8]}`）：{check.reason}", resolve=True)
            acts.append({"kind": "feedback", "fingerprint": fp, "label": "true_positive", "reason": "开发者已修复"})
            return upd, acts + new_actions, extra
        f["status"] = "OPEN" if f["status"] == "VERIFYING" else f["status"]
        acts = [{"kind": "event", "fingerprint": fp, "actor": "ai", "event": check.status, "content": check.reason}]
        if announce or check.status == "partially_fixed":
            label = "⚠️ 部分修复" if check.status == "partially_fixed" else "❌ 尚未修复"
            acts.append({"kind": "reply", "fingerprint": fp,
                         "body": f"{label}（`{head[:8]}`）：{check.reason}", "resolve": False})
        return f, acts + new_actions, extra

    def _normalize_new(self, state: ReviewState, item: LLMFinding, parent: str | None) -> dict | None:
        pp, _ = self._ctx(state)
        head, refs = state["head_sha"], state["diff_refs"]
        d = item.model_dump()
        content = self.d.mirror(pp).show(head, d["file"])
        loc = locate_evidence(content or "", d["evidence"], d["line"])
        d["evidence"] = clean_evidence(d["evidence"])
        if loc is None:
            return None
        fd = self.d.mr_diff(pp, refs["base_sha"], head, for_position=True).get(d["file"])
        line = fd.nearest_commentable(loc) if fd else None
        d |= {
            "line": line or loc, "inline": line is not None, "status": "OPEN", "first_sha": head,
            "last_checked_sha": head, "dispute_rounds": 0, "parent_fingerprint": parent,
            "fingerprint": fingerprint(d["file"], d["category"], d["evidence"], d["title"]),
        }
        return self._cap_test_severity(d)

    def _cap_test_severity(self, f: dict) -> dict:
        if ORDER[f["severity"]] < ORDER[TEST_MAX_SEVERITY] and is_ignored(f["file"], self.d.cfg.review.test_files):
            f["severity"] = TEST_MAX_SEVERITY
        return f

    @staticmethod
    def _close(f: dict, status: str, text: str, resolve: bool) -> tuple[dict, list[dict]]:
        fp = f["fingerprint"]
        return f | {"status": status, "status_reason": text}, [
            {"kind": "reply", "fingerprint": fp, "body": text, "resolve": resolve},
            {"kind": "event", "fingerprint": fp, "actor": "ai", "event": status, "content": text},
        ]

    # ------------------------------------------------------------------ 开发者回复
    def handle_reply(self, state: ReviewState) -> dict:
        ev = state["event"]
        pp, _ = self._ctx(state)
        head = state["head_sha"]
        human_user = self.d.cfg.human_reviewer
        max_rounds = self.d.cfg.lifecycle.max_dispute_rounds
        findings = list(state["findings"])
        idx = next((i for i, f in enumerate(findings) if f["fingerprint"] == ev["fingerprint"]), None)
        if idx is None:
            return {}
        f = dict(findings[idx])
        fp = f["fingerprint"]
        reply, thread = ev.get("note_body", ""), ev.get("thread", "")
        actions: list[dict] = [{"kind": "event", "fingerprint": fp, "actor": ev.get("author", "?"),
                                "event": "reply", "content": reply}]

        intent = ev.get("intent_hint")
        if not intent:
            try:
                intent = invoke_structured(ReplyIntent, [human(render(
                    "reply_intent", finding=f"{f['title']}\n{f['detail']}", thread=thread, reply=reply,
                ))]).intent
            except Exception as e:  # noqa: BLE001
                raise_if_unavailable(e)
                log.warning("意图识别失败，使用关键词: %s", e)
                intent = _keyword_intent(reply)
        log.info("回复意图: %s（%s）", intent, fp)

        mirror = self.d.mirror(pp)
        content = mirror.show(head, f["file"])
        loc = locate_snippet(content or "", f["evidence"], f.get("line"))
        code = _code_window(content, loc or f.get("line"))

        if f["status"] in CLOSED_STATES and intent != "question":
            return {"actions": actions}

        if intent == "dispute":
            if f["dispute_rounds"] >= max_rounds:
                if f["severity"] in ("P0", "P1") and f["status"] != "ESCALATED":
                    f["status"] = "ESCALATED"
                actions.append({"kind": "reply", "fingerprint": fp, "resolve": False,
                                "body": f"已达到 AI 复核次数上限，等待 @{human_user} 人工裁决。"})
            else:
                f["dispute_rounds"] += 1
                f["status"] = "DISPUTED"
                try:
                    v = invoke_structured(DisputeVerdict, [self._sys(), human(render(
                        "dispute", file=f["file"], line=f.get("line"), severity=f["severity"], category=f["category"],
                        title=f["title"], detail=f["detail"], evidence=f["evidence"], thread=thread, reply=reply,
                        code=code, extra_context="",
                    ))], role="verify")
                except Exception as e:  # noqa: BLE001
                    raise_if_unavailable(e)
                    log.warning("复核失败，直接升级人工: %s", e)
                    v = DisputeVerdict(analysis="", verdict="maintain", reason="AI 复核失败，交由人工判断。")
                if v.verdict == "accept":
                    f, acts = self._close(f, "WITHDRAWN", f"🙆 接受解释，撤回该问题：{v.reason}", resolve=True)
                    actions += acts
                    actions.append({"kind": "feedback", "fingerprint": fp, "label": "false_positive", "reason": v.reason})
                elif f["severity"] == "P2":
                    f, acts = self._close(
                        f, "WITHDRAWN", f"💬 仍建议调整：{v.reason}\n\n这是 P2 建议，不阻断合并，由你决定是否修改。", resolve=True)
                    actions += acts
                else:
                    f["status"] = "ESCALATED"
                    ev_code = f"\n\n```{R.lang_of(f['file'])}\n{clean_evidence(v.evidence)}\n```" if v.evidence else ""
                    actions += [
                        {"kind": "reply", "fingerprint": fp, "resolve": False, "body": (
                            f"🔍 复核后仍认为问题存在：{v.reason}{ev_code}\n\n"
                            f"@{human_user} 请裁决：回复 `/ai-confirm` 表示问题成立需修复，`/ai-accept` 表示接受开发者解释。")},
                        {"kind": "event", "fingerprint": fp, "actor": "ai", "event": "ESCALATED", "content": v.reason},
                    ]

        elif intent == "claim_fixed":
            since = f["first_sha"] if f.get("last_checked_sha") == head else f["last_checked_sha"]
            if mirror.has_commit(since) and f["file"] not in mirror.changed_files(since, head):
                actions.append({"kind": "reply", "fingerprint": fp, "resolve": False,
                                "body": f"当前版本 `{head[:8]}` 中 `{f['file']}` 没有相关改动，请 push 修复后再回复。"})
            else:
                f, acts, extra = self._check_fix(state, f, since=since, developer_note=reply, announce=True)
                actions += acts
                findings.extend(extra)

        elif intent == "defer":
            if f["severity"] == "P0":
                f["status"] = "ESCALATED"
                actions.append({"kind": "reply", "fingerprint": fp, "resolve": False,
                                "body": f"P0 问题原则上不能延期处理。@{human_user} 请裁决："
                                        "`/ai-accept` 同意延期，`/ai-confirm` 要求本次修复。"})
            else:
                f, acts = self._close(f, "DEFERRED", f"📌 已记录延期处理：{reply.strip()[:200]}", resolve=True)
                actions += acts

        elif intent == "question":
            try:
                ans = invoke_structured(Answer, [self._sys(), human(render(
                    "answer", title=f["title"], detail=f["detail"], evidence=f["evidence"], thread=thread,
                    reply=reply, code=code,
                ))]).answer
                actions.append({"kind": "reply", "fingerprint": fp, "body": ans, "resolve": None})
            except Exception as e:  # noqa: BLE001
                raise_if_unavailable(e)
                log.warning("回答问题失败: %s", e)

        findings[idx] = f
        return {"findings": findings, "actions": actions}

    # ------------------------------------------------------------------ 人工命令
    def human_command(self, state: ReviewState) -> dict:
        ev = state["event"]
        findings = list(state["findings"])
        idx = next((i for i, f in enumerate(findings) if f["fingerprint"] == ev.get("fingerprint")), None)
        if idx is None:
            return {}
        f = dict(findings[idx])
        fp, cmd, arg = f["fingerprint"], ev["command"], (ev.get("arg") or "").strip()
        who = ev.get("author", self.d.cfg.human_reviewer)
        actions = [{"kind": "event", "fingerprint": fp, "actor": who, "event": f"cmd:{cmd}", "content": arg}]
        reason = f"：{arg}" if arg else "。"
        if cmd == "confirm":
            f["status"] = "OPEN"
            actions += [
                {"kind": "reply", "fingerprint": fp, "resolve": False, "body": f"👤 人工确认问题成立，需要修复{reason}"},
                {"kind": "feedback", "fingerprint": fp, "label": "true_positive", "reason": arg or "人工确认"},
            ]
        elif cmd in ("accept", "resolved"):
            f, acts = self._close(f, "WAIVED", f"👤 人工裁决放行，不阻断合并{reason}", resolve=True)
            actions += acts if cmd == "accept" else acts[1:]
            actions.append({"kind": "feedback", "fingerprint": fp, "label": "waived", "reason": arg or "人工放行"})
        elif cmd == "downgrade":
            sev = arg.upper()[:2]
            if sev in ("P0", "P1", "P2"):
                f["severity"] = sev
                if f["status"] == "ESCALATED":
                    f["status"] = "OPEN"
                actions.append({"kind": "reply", "fingerprint": fp, "resolve": None, "body": f"👤 级别已调整为 {sev}。"})
        findings[idx] = f
        return {"findings": findings, "actions": actions}

    # ------------------------------------------------------------------ 门禁与发布
    def gate(self, state: ReviewState) -> dict:
        return {"conclusion": compute_conclusion(state.get("findings", []))}

    def publish(self, state: ReviewState) -> dict:
        pp, iid = self._ctx(state)
        head, refs = state["head_sha"], state["diff_refs"]
        conclusion = state["conclusion"]
        findings = state.get("findings", [])
        by_fp = {f["fingerprint"]: f for f in findings}
        summary = R.summary_body(head_sha=head, conclusion=conclusion, intent=state.get("intent", ""),
                                 findings=findings, notes=state.get("notes", []), human=self.d.cfg.human_reviewer)
        if state.get("dry_run"):
            self._print_dry_run(state, by_fp, summary)
            return {"summary": summary}

        gl, store = self.d.gl, self.d.store
        stored = {f["fingerprint"]: f for f in store.findings(pp, iid)}
        pos_diffs = self.d.mr_diff(pp, refs["base_sha"], head, for_position=True)

        # 1. 新问题 → 讨论
        for a in state.get("actions", []):
            if a["kind"] != "create":
                continue
            f = by_fp.get(a["fingerprint"])
            if not f or (stored.get(f["fingerprint"]) or {}).get("discussion_id"):
                continue
            position = None
            fd = pos_diffs.get(f["file"])
            if f.get("inline") and fd:
                lines = fd.position_for(f["line"])
                if lines:
                    position = {"position_type": "text", "base_sha": refs["base_sha"], "start_sha": refs["start_sha"],
                                "head_sha": refs["head_sha"], "old_path": fd.old_path, "new_path": fd.new_path, **lines}
            if not position:
                f["inline"] = False
            f["discussion_id"] = gl.create_discussion(pp, iid, R.finding_body(f), position)
            store.upsert_finding(pp, iid, f)

        # 2. 持久化全部问题状态
        ids = {f["fingerprint"]: store.upsert_finding(pp, iid, f) for f in findings if f.get("status") != "NEW"}

        # 3. 回复 / resolve / 事件 / 样本
        for a in state.get("actions", []):
            f = by_fp.get(a.get("fingerprint")) or stored.get(a.get("fingerprint"))
            if not f:
                continue
            fid = ids.get(f["fingerprint"]) or f.get("id")
            if a["kind"] == "reply" and f.get("discussion_id"):
                gl.reply(pp, iid, f["discussion_id"], R.reply_body(f["fingerprint"], a["body"]))
                if a.get("resolve") is not None:
                    gl.set_resolved(pp, iid, f["discussion_id"], a["resolve"])
            elif a["kind"] == "event" and fid:
                store.add_event(fid, a["actor"], a["event"], a.get("content"), head)
            elif a["kind"] == "feedback":
                store.add_feedback(pp, a["label"], f, a.get("reason", ""))

        # 4. 标签、汇总、审批
        lc = self.d.cfg.lifecycle
        gl.set_label(pp, iid, lc.needs_human_label, any(f["status"] == "ESCALATED" for f in findings))
        st = store.mr_state(pp, iid)
        note_id = gl.upsert_note(pp, iid, st.get("summary_note_id"), summary)
        store.update_mr_state(pp, iid, summary_note_id=note_id)
        if lc.auto_approve:
            # 只有完全没有未关闭问题时才 approve；存在阻断问题时撤销；仅有建议/待裁决项时不改变审批状态
            if conclusion == "REQUEST_CHANGES":
                gl.unapprove(pp, iid)
            elif conclusion == "APPROVE":
                gl.approve(pp, iid)
        if state["event"]["kind"] in ("new_push", "human_command") and state["event"].get("command", "review") == "review":
            store.record_review(pp, iid, head, conclusion, summary)
            store.update_mr_state(pp, iid, last_reviewed_sha=head)
        log.info("%s!%s 发布完成，结论 %s", pp, iid, conclusion)
        return {"summary": summary}

    def _print_dry_run(self, state: ReviewState, by_fp: dict, summary: str) -> None:
        print("\n" + "=" * 80 + "\n[DRY RUN] 以下内容不会提交到 GitLab\n" + "=" * 80)
        for a in state.get("actions", []):
            f = by_fp.get(a.get("fingerprint"), {})
            if a["kind"] == "create":
                print(f"\n--- 新讨论 {'(行内)' if f.get('inline') else '(普通)'} {f['file']}:{f['line']} ---")
                print(R.finding_body(f))
            elif a["kind"] == "reply":
                print(f"\n--- 回复 {a['fingerprint']} (resolve={a.get('resolve')}) ---\n{a['body']}")
        print("\n--- 汇总评论 ---\n" + summary)
        print(f"\n结论: {state['conclusion']}")


def route_event(state: ReviewState) -> str:
    ev = state["event"]
    if ev.get("skip"):
        return "skip"
    if ev["kind"] == "new_push" or (ev["kind"] == "human_command" and ev.get("command") == "review"):
        return "plan_review"
    if ev["kind"] == "dev_reply":
        return "handle_reply"
    if ev["kind"] == "human_command":
        return "human_command"
    return "skip"

