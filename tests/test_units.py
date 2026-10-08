from huatuo.diff_parser import parse_diff
from huatuo.git_repo import locate_snippet
from huatuo.graph.gate import compute_conclusion
from huatuo.poller import parse_command

DIFF = """diff --git a/svc/a.go b/svc/a.go
index 1111111..2222222 100644
--- a/svc/a.go
+++ b/svc/a.go
@@ -10,6 +10,8 @@ func Run() {
 	x := 1
 	y := 2
-	z := 3
+	z := x + y
+	m[key] = z
+	go work(m)
 	return
 }
 
diff --git a/new.go b/new.go
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.go
@@ -0,0 +1,2 @@
+package main
+func f() {}
"""


def test_parse_diff_line_numbers():
    files = parse_diff(DIFF)
    assert [f.path for f in files] == ["svc/a.go", "new.go"]
    a = files[0]
    assert a.added_lines() == {12, 13, 14}
    assert a.position_for(13) == {"new_line": 13}
    assert a.position_for(10) == {"new_line": 10, "old_line": 10}
    assert a.position_for(99) is None
    assert a.nearest_commentable(16) == 14
    assert "L13 +" in a.annotated() and "     - " in a.annotated()
    assert files[1].new_file and files[1].added_lines() == {1, 2}


def test_locate_snippet_ignores_whitespace_and_picks_nearest():
    content = "a\n  foo(x)\nb\nfoo(x)\nc\n"
    assert locate_snippet(content, "foo(x)", 4) == 4
    assert locate_snippet(content, "  foo(x)  \n b", 1) == 2
    assert locate_snippet(content, "bar()", 1) is None


def f(sev, status):
    return {"severity": sev, "status": status}


def test_gate():
    assert compute_conclusion([]) == "APPROVE"
    assert compute_conclusion([f("P0", "OPEN")]) == "REQUEST_CHANGES"
    assert compute_conclusion([f("P0", "ESCALATED")]) == "REQUEST_CHANGES"
    assert compute_conclusion([f("P0", "WAIVED"), f("P0", "FIXED"), f("P0", "WITHDRAWN")]) == "APPROVE"
    assert compute_conclusion([f("P1", "OPEN")]) == "REQUEST_CHANGES"
    assert compute_conclusion([f("P1", "ESCALATED")]) == "COMMENT"
    assert compute_conclusion([f("P1", "DEFERRED")]) == "APPROVE"
    assert compute_conclusion([f("P2", "OPEN")]) == "COMMENT"
    assert compute_conclusion([f("P0", "NEW")]) == "APPROVE"


def test_parse_command():
    assert parse_command("/ai-confirm 确实有竞态") == ("confirm", "确实有竞态")
    assert parse_command("/ai-downgrade P2") == ("downgrade", "P2")
    assert parse_command("/AI-review") == ("review", "")
    assert parse_command("我觉得 /ai-accept") is None


def test_clean_evidence():
    from huatuo.git_repo import clean_evidence
    raw = 'L10 +  go func() {\\\\nL11 + \\\\tcache[k] = v\\\\nL12 + }()\\n} '
    assert [x.strip() for x in clean_evidence(raw).splitlines()] == ["go func() {", "cache[k] = v", "}()", "}"]
    assert clean_evidence("   12| x := 1\n   13| y := 2") == "x := 1\ny := 2"
    assert clean_evidence("```go\nmu.Lock()\n```") == "mu.Lock()"
    assert clean_evidence("cache[k] = v") == "cache[k] = v"


def test_locate_evidence_fuzzy_prefers_occurrence_nearest_the_hint():
    from huatuo.git_repo import locate_evidence
    # 两个函数都有 `p.lock.Lock()`；证据带 `...` 占位符无法整段匹配，逐行模糊时必须取离 hint 最近的那处
    content = ("func add() {\n\tp.lock.Lock()\n\tdefer p.lock.Unlock()\n\tp.queue = append(p.queue, x)\n}\n\n"
               "func pop() {\n\tp.lock.Lock()\n\tdefer p.lock.Unlock()\n\tfor {\n\t\tselect {\n\t\tcase p.nextCh <- n:\n\t\t}\n\t}\n}\n")
    ev = "p.lock.Lock()\n\tdefer p.lock.Unlock()\n\tfor {\n...\n\tcase p.nextCh <- n:"
    assert locate_evidence(content, ev, 8) == 8  # pop 里的那处，而不是 add 里的第 2 行


def test_locate_evidence_handles_escaped_and_fuzzy():
    from huatuo.git_repo import locate_evidence
    content = "package svc\n\nfunc Put(k string, v int) {\n\tgo func() {\n\t\tcache[k] = v\n\t}()\n}\n"
    escaped = 'func Put(k string, v int) {\\\\n\\\\\\tgo func() {\\\\n\\\\\\t    cache[k] = v\\\\n\\\\\\t}()\\\\n}'
    assert locate_evidence(content, escaped, 3) == 3
    # 模型改写了一行，但大部分行仍能在附近找到
    assert locate_evidence(content, "go func() {\n    cache[k] = v // 写入\n}()", 4) == 4
    assert locate_evidence(content, "mu.Lock()\nother()", 4) is None


def test_lint_new_issues_ignores_line_shift():
    from huatuo.static_analysis import LintIssue, new_issues
    base = [LintIssue("errcheck", "a.go", 10, "x not checked"), LintIssue("unused", "a.go", 30, "func old is unused")]
    head = [LintIssue("errcheck", "a.go", 14, "x not checked"),          # 历史问题，行号因改动下移
            LintIssue("unused", "a.go", 50, "func helper is unused")]     # 本次引入
    assert [i.text for i in new_issues(head, base)] == ["func helper is unused"]


def test_match_dedupes_same_title_nearby():
    from huatuo.graph.nodes import Nodes
    a = {"fingerprint": "a", "file": "x.go", "line": 214, "category": "bug", "title": "retries 为 0 时行为未明确"}
    b = {"fingerprint": "b", "file": "x.go", "line": 217, "category": "error_handling", "title": "retries 为 0 时行为未明确"}
    c = {"fingerprint": "c", "file": "x.go", "line": 240, "category": "bug", "title": "retries 为 0 时行为未明确"}
    assert Nodes._match(b, [a]) == 0
    assert Nodes._match(c, [a]) is None


def test_is_generated():
    from huatuo.graph.nodes import is_generated
    assert is_generated("// Code generated by MockGen. DO NOT EDIT.\n// Source: x.go\n\npackage svc\n")
    assert is_generated("// Copyright x\n\n// Code generated by protoc-gen-go. DO NOT EDIT.\npackage pb\n")
    assert not is_generated("package svc\n\n// Code generated elsewhere, feel free to edit.\n")
    assert not is_generated(None)


def test_v1_version_line_stripped_before_migrate():
    from huatuo.static_analysis import V1_VERSION_RE
    assert V1_VERSION_RE.sub("", 'version: "1"\n\nrun:\n  go: "1.25"\n') == '\nrun:\n  go: "1.25"\n'
    assert V1_VERSION_RE.sub("", "version: 1 # old\nlinters: {}\n") == "linters: {}\n"
    assert V1_VERSION_RE.sub("", 'run:\n  version: "1"\n') == 'run:\n  version: "1"\n'  # 只动顶层


def test_parse_job_log_takes_latest_segment():
    from huatuo.ui import parse_job_log
    lines = """2026-09-28 21:50:00,000 INFO huatuo.worker: 处理任务 #7 new_push g/p!1
2026-09-28 21:50:01,000 INFO huatuo.graph.nodes: g/p!1 发布完成，结论 APPROVE
2026-09-28 21:56:13,827 INFO huatuo.worker: 处理任务 #11 new_push g/p!462
2026-09-28 21:56:19,143 WARNING huatuo.static_analysis: golangci-lint 执行失败（exit 3）: boom
level=error msg="续行"
2026-09-28 21:57:18,619 INFO huatuo.graph.nodes: g/p!462 待审 3 个文件块
2026-09-28 21:58:00,000 INFO httpx2: HTTP Request: POST http://127.0.0.1:1234/v1/chat/completions
2026-09-28 21:58:30,000 INFO huatuo.graph.nodes:   a/b.go (1/2): 2 条候选问题
2026-09-28 21:59:00,000 INFO huatuo.graph.nodes:   a/b.go (2/2): 0 条候选问题
2026-09-28 22:00:00,000 INFO huatuo.worker: 处理任务 #12 dev_reply g/p!1""".splitlines()
    p = parse_job_log(lines, 11)
    assert p["phase"] == "review" and p["total"] == 3 and p["conclusion"] is None
    assert [(c["chunk"], c["n"], c["since"][11:]) for c in p["chunks"]] == [(1, 2, "21:57:18"), (2, 0, "21:58:30")]
    assert p["cursor_ts"].endswith("21:59:00")
    assert "续行" in p["warnings"][0] and not any("HTTP Request" in x for x in p["lines"])
    assert parse_job_log(lines, 7)["phase"] == "done"
    assert parse_job_log(lines, 99) == {"found": False}


def test_is_generated_detects_minified():
    from huatuo.graph.nodes import is_generated

    assert is_generated("const _0x5b0f=_0x2898;" + "x" * 5000)
    assert not is_generated("package main\n\nfunc f() {}\n")


def test_chunks_split_oversized_hunk_and_truncate_long_lines():
    from huatuo.diff_parser import DiffLine, FileDiff, Hunk

    lines = [DiffLine("-", 1, None, "x" * 50000), DiffLine("+", None, 1, "y" * 50000)]
    lines += [DiffLine("+", None, i, "z" * 200) for i in range(2, 600)]
    fd = FileDiff("a.js", "a.js", hunks=[Hunk("@@ -1 +1,599 @@", lines)])
    groups = fd.chunks(48000)
    assert len(groups) > 1
    assert all(len(fd.annotated(g)) <= 48000 for g in groups)
    text = "\n".join(fd.annotated(g) for g in groups)
    assert "截断，原长 50000 字符" in text and "L599 +" in text


def test_is_unavailable():
    import httpx
    import openai

    from huatuo.llm import is_unavailable

    req = httpx.Request("POST", "http://127.0.0.1:1234/v1/chat/completions")
    assert is_unavailable(openai.APIConnectionError(request=req))
    assert not is_unavailable(openai.APITimeoutError(request=req))
    assert is_unavailable(RuntimeError("Error code: 400 - {'error': 'Model unloaded.'}"))
    assert not is_unavailable(RuntimeError("request (64879 tokens) exceeds the available context size"))


def test_drain_requeues_when_model_unavailable(monkeypatch):
    from types import SimpleNamespace

    from huatuo import worker

    class Store:
        def __init__(self):
            self.jobs = [{"id": 1}, {"id": 2}]
            self.log = []

        def next_job(self):
            return self.jobs.pop(0) if self.jobs else None

        def requeue_job(self, job_id):
            self.log.append(("requeue", job_id))

        def finish_job(self, job_id, error=None):
            self.log.append(("finish", job_id, error))

    def run_job(deps, graph, job, resume=False):
        raise RuntimeError("Error code: 400 - {'error': 'Model unloaded.'}")

    monkeypatch.setattr(worker, "run_job", run_job)
    store = Store()
    worker.drain(SimpleNamespace(store=store), graph=None)
    # 放回队列并停止消费，等模型就绪；不能把后面的任务也一个个标记失败
    assert store.log == [("requeue", 1)]
    assert store.jobs == [{"id": 2}]


def test_path_affinity_prefers_same_file_then_dir():
    from huatuo.graph.nodes import _path_affinity

    target = "app/edge/internal/events/service.go"
    paths = ["pkg/x.go", "app/edge/internal/handlers/a.go", "app/edge/internal/events/types.go", target]
    assert sorted(paths, key=lambda p: _path_affinity(p, target), reverse=True) == list(reversed(paths))


def test_trace_records_call_with_step(tmp_path):
    from uuid import uuid4

    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from huatuo.trace import TraceHandler, TraceReader, TraceStore, step

    h = TraceHandler(TraceStore(tmp_path / "t.db"))
    run = uuid4()
    with step("审查 a.go [correctness]"):
        h.on_chat_model_start({}, [[SystemMessage("sys"), HumanMessage("看看这段 diff")]], run_id=run,
                              metadata={"thread_id": "job-7", "langgraph_node": "review_file"},
                              invocation_params={"model": "m", "temperature": 0.2})
    h.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage("确认的问题：无"))]],
                           llm_output={"token_usage": {"prompt_tokens": 12, "completion_tokens": 5}}), run_id=run)
    fail = uuid4()
    h.on_chat_model_start({}, [[HumanMessage("x")]], run_id=fail, metadata={"thread_id": "job-7"})
    h.on_llm_error(RuntimeError("boom"), run_id=fail)

    r = TraceReader(tmp_path / "t.db")
    calls = r.calls("job-7")
    assert [(c["step"], c["node"], c["prompt_tokens"], c["error"]) for c in calls] == [
        ("审查 a.go [correctness]", "review_file", 12, None), ("", None, None, "boom")]
    full = r.call(calls[0]["id"])
    assert [m["role"] for m in full["request"]["messages"]] == ["system", "human"]
    assert full["response"]["content"] == "确认的问题：无"
    assert r.calls("job-8") == [] and TraceReader(tmp_path / "missing.db").calls("job-7") == []


def test_parse_job_log_resumed_job():
    from huatuo.ui import parse_job_log

    lines = [
        "2026-09-30 15:12:36,566 INFO huatuo.worker: 处理任务 #13 new_push g/edgenode!9",
        "2026-09-30 15:12:36,569 INFO huatuo.worker: 从 checkpoint 恢复任务 #13",
        "2026-09-30 15:14:36,879 INFO huatuo.graph.nodes:   cmd/main.go (1/1): 4 条候选问题",
    ]
    p = parse_job_log(lines, 13)
    assert p["resumed"] and p["chunks"][0]["since"] == "2026-09-30 15:12:36"


def test_match_keeps_distinct_issues_on_same_line_apart():
    from huatuo.graph.nodes import Nodes

    ignored = {"fingerprint": "a", "file": "s.go", "line": 9, "category": "error_handling", "title": "db.Exec 错误被忽略"}
    nil_db = {"fingerprint": "b", "file": "s.go", "line": 9, "category": "nil", "title": "db 未判空导致 panic"}
    lint = {"fingerprint": "c", "file": "s.go", "line": 9, "category": "lint", "title": "[errcheck] ...", "source": "lint"}
    assert Nodes._match(nil_db, [ignored], cross_category=False) is None  # 本轮新问题：不同类别分开保留
    assert Nodes._match(nil_db, [ignored]) == 0                           # 与已有问题比较：仍宽松去重
    assert Nodes._match(ignored, [lint], cross_category=False) == 0       # 模型重复报告 lint 问题仍会并入


def test_parse_command_accepts_huatuo_alias():
    assert parse_command("/huatuo-confirm 确实有竞态") == ("confirm", "确实有竞态")
    assert parse_command("/Huatuo-review") == ("review", "")
    assert parse_command("/huatuo-unknown") is None


def test_previous_summary_strips_both_headers():
    from huatuo.graph import render as R
    for header in (R.HEADER, R.LEGACY_HEADER):
        out = "\n".join(R._previous_summary(f"{header}\n\n结论：APPROVE"))
        assert "###" not in out and "结论：APPROVE" in out


def _fd(path, added, new_file=False):
    from huatuo.diff_parser import DiffLine, FileDiff, Hunk

    h = Hunk("@@ -1,1 +1,1 @@", [DiffLine("+", None, i + 1, t) for i, t in enumerate(added)])
    return FileDiff(path, path, new_file=new_file, hunks=[h])


def test_rank_files_weights_importance_not_just_size():
    from huatuo.priority import rank_files

    test_files, light = ["*_test.go"], ["*.yaml", "*.md", "*.json"]
    boiler = _fd("app/model/entity.go", ["x := 1"] * 200)                       # 大段样板
    auth = _fd("app/auth/token.go", ["func Verify(t string) bool { return true }"] * 5)  # 小改动但关键
    conc = _fd("app/biz/cache.go", ["go func() { mu.Lock() }()", "m[k] = v"] * 3)
    cfg = _fd("deploy/values.yaml", ["a: 1"] * 150)
    big_test = _fd("app/biz/cache_test.go", ["t.Run()"] * 300)
    ranked = [f.path for f in rank_files([boiler, cfg, big_test, conc, auth], test_files, light)]
    assert ranked.index("app/auth/token.go") < ranked.index("app/model/entity.go")  # 关键小改动胜过大段样板
    assert ranked.index("app/biz/cache.go") < ranked.index("app/model/entity.go")   # 并发信号 + 业务路径
    assert ranked.index("deploy/values.yaml") > ranked.index("app/model/entity.go")  # 配置类降权
    assert ranked[-1] == "app/biz/cache_test.go"  # 测试文件始终在最后，哪怕改动最大


def test_rank_files_is_stable_for_ties():
    from huatuo.priority import rank_files

    a, b = _fd("pkg/b.go", ["x"] * 3), _fd("pkg/a.go", ["x"] * 3)
    assert [f.path for f in rank_files([a, b], [], [])] == ["pkg/a.go", "pkg/b.go"]


def test_quality_stats_classification_and_precision():
    from huatuo.quality import classify, quality_stats

    def row(sev, st, reason="", cat="correctness", proj="g/p"):
        return {"project_path": proj, "mr_iid": 1, "severity": sev, "category": cat, "status": st,
                "status_reason": reason, "created_at": "2026-09-30T10:00:00+00:00", "dispute_rounds": 0}

    rows = [row("P1", "FIXED"), row("P1", "FIXED"), row("P1", "WAIVED"),
            row("P1", "WITHDRAWN", "🙆 接受解释，撤回该问题：xxx"),
            row("P2", "WITHDRAWN", "💬 仍建议调整：xxx"),  # 仍认为有问题，不算误报
            row("P0", "OPEN"), row("P0", "ESCALATED")]
    assert [classify(r) for r in rows] == ["valid", "valid", "valid", "false_positive", "advisory", "pending", "pending"]
    s = quality_stats(rows)
    assert s["overall"]["precision"] == 0.75 and s["overall"]["pending"] == 2 and s["overall"]["advisory"] == 1
    by_sev = {x["key"]: x for x in s["severity"]}
    assert by_sev["P1"]["precision"] == 0.75 and by_sev["P0"]["precision"] is None  # P0 还没有已决的，不显示 0%
    assert s["week"][0]["key"] == "2026-W40"


PURE_DELETE = """diff --git a/w.go b/w.go
--- a/w.go
+++ b/w.go
@@ -10,6 +10,3 @@ func writeShortstr(w io.Writer, s string) error {
 	b := []byte(s)
-	if len(b) > 255 {
-		return ErrShortstrTooLong
-	}
 	length := uint8(len(b))
 	return write(w, length)
"""


def test_pure_deletion_has_change_anchors():
    fd = parse_diff(PURE_DELETE)[0]
    assert fd.added_lines() == set()
    assert fd.changed_lines() == {10, 11}  # 删除点前后各一行
    assert fd.removed_lines()[0] == (11, "\tif len(b) > 255 {")


def test_locate_removed_finds_deleted_evidence_only():
    from huatuo.graph.nodes import _locate_removed
    fd = parse_diff(PURE_DELETE)[0]
    assert _locate_removed(fd, "if len(b) > 255 {\n    return ErrShortstrTooLong\n}") == 11
    assert _locate_removed(fd, "length := uint8(len(b))") is None  # 仍在文件中的代码不走这条路径
    assert _locate_removed(None, "if len(b) > 255 {") is None


def test_final_severity_drops_p0_unless_all_valid_votes_agree():
    from huatuo.graph.nodes import Nodes
    from huatuo.graph.state import VerifyVerdict
    V = lambda valid, sev: VerifyVerdict(analysis="", valid=valid, severity=sev, reason="")  # noqa: E731
    f = Nodes._final_severity
    assert f([V(True, "P0"), V(True, "P0")]) == "P0"
    assert f([V(True, "P0"), V(True, "P1")]) == "P1"            # 意见分裂：不阻断
    assert f([V(True, "P0"), V(False, "P1"), V(True, "P0")]) == "P0"  # 否决票不参与级别，成立票一致
    assert f([V(True, "P0"), V(True, "P2"), V(True, "P1")]) == "P1"  # 取成立票里较重的非 P0 级别
    assert f([V(True, "P1")]) == "P1" and f([V(True, "P1"), V(True, "P2"), V(True, "P1")]) == "P1"


def test_match_merges_same_spot_findings_only_when_titles_overlap():
    from huatuo.graph.nodes import Nodes, _title_overlap
    base = {"file": "a.go", "line": 384, "fingerprint": "x", "category": "bug", "source": "llm",
            "title": "`done` channel 触发后未退出循环，导致 goroutine 永久挂起"}
    dup = base | {"fingerprint": "y", "category": "resource", "title": "`done` channel 触发后未退出循环，导致函数永久挂起"}
    other = base | {"fingerprint": "z", "category": "concurrency", "title": "共享变量 cnt 无同步访问存在数据竞争"}
    assert Nodes._match(dup, [base], cross_category=False) == 0
    assert Nodes._match(other, [base], cross_category=False) is None
    assert _title_overlap(base["title"], dup["title"]) > 0.8 and _title_overlap(base["title"], other["title"]) < 0.2
