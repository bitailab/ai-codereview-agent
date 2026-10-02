"""端到端生命周期测试：真实 git 仓库 + 假 GitLab + 假模型。
覆盖：新提交发现 P0 → 开发者说误报 → AI 坚持并升级人工 → 人工确认 → 开发者 push 修复 → 验证通过并 approve。"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from huatuo.deps import Deps
from huatuo.graph import nodes as nodes_mod
from huatuo.graph.build import build_graph
from huatuo.graph.state import DisputeVerdict, FindingList, FixCheck, LLMFinding, ReplyIntent, VerifyVerdict
from huatuo.settings import Config, Env, ReviewConfig, Settings, StaticAnalysisConfig
from huatuo.store import Store

PP = "grp/svc"

BASE = """package svc

var cache = map[string]int{}

func Get(k string) int {
	return cache[k]
}
"""
BUGGY = BASE + """
func Put(k string, v int) {
	go func() {
		cache[k] = v
	}()
}
"""
FIXED = BASE.replace("var cache", "var mu sync.Mutex\nvar cache") + """
func Put(k string, v int) {
	mu.Lock()
	defer mu.Unlock()
	cache[k] = v
}
"""


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeGitLab:
    def __init__(self, origin):
        self.me = SimpleNamespace(username="ai-bot")
        self.origin = origin
        self.discussions_created, self.replies, self.resolved = [], [], {}
        self.labels, self.approved, self.notes = set(), None, {}
        self.head = self.base = self.start = None

    def project(self, pp):
        return SimpleNamespace(ssh_url_to_repo=str(self.origin), http_url_to_repo=str(self.origin))

    def user_id(self, username):
        return 1

    def mr(self, pp, iid):
        return SimpleNamespace(attributes={
            "iid": iid, "title": "新增 Put 接口", "description": "", "source_branch": "feat", "target_branch": "main",
            "state": "opened", "sha": self.head, "project_id": 1, "author": {"username": "dev"},
            "diff_refs": {"base_sha": self.base, "start_sha": self.start, "head_sha": self.head},
        })

    def create_discussion(self, pp, iid, body, position):
        did = f"d{len(self.discussions_created) + 1}"
        self.discussions_created.append((did, body, position))
        return did

    def reply(self, pp, iid, did, body):
        self.replies.append((did, body))

    def set_resolved(self, pp, iid, did, resolved):
        self.resolved[did] = resolved

    def upsert_note(self, pp, iid, note_id, body):
        self.notes[note_id or 1] = body
        return note_id or 1

    def approve(self, pp, iid):
        self.approved = True

    def unapprove(self, pp, iid):
        self.approved = False

    def set_label(self, pp, iid, label, present):
        (self.labels.add if present else self.labels.discard)(label)


@pytest.fixture
def env(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "t@t")
    git(origin, "config", "user.name", "t")
    (origin / "svc.go").write_text(BASE)
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "base")
    main_sha = git(origin, "rev-parse", "HEAD")
    git(origin, "checkout", "-qb", "feat")
    (origin / "svc.go").write_text(BUGGY)
    git(origin, "commit", "-qam", "add Put")
    head1 = git(origin, "rev-parse", "HEAD")
    git(origin, "update-ref", "refs/merge-requests/7/head", head1)

    gl = FakeGitLab(origin)
    gl.base = gl.start = main_sha
    gl.head = head1
    cfg = Config(projects=[PP], human_reviewer="alden", data_dir=str(tmp_path / "data"),
                 review=ReviewConfig(passes=["all"], enable_tools=False,
                                     static_analysis=StaticAnalysisConfig(enabled=False)))
    cfg.data_path.mkdir(parents=True)
    settings = Settings(env=Env(gitlab_url="http://x", gitlab_token="t"), config=cfg)
    deps = Deps(settings=settings, store=Store(cfg.data_path / "state.db"), gl=gl)

    llm = SimpleNamespace(dispute="maintain", fix="fixed", votes=[], vote_calls=0)  # votes：依次返回的复核结论

    def fake_structured(schema, messages, role="review", temperature=None):
        if schema is FindingList:
            return FindingList(analysis="", findings=[LLMFinding(
                file="svc.go", line=11, severity="P0", category="concurrency", title="map 并发写导致 panic",
                detail="goroutine 中无锁写全局 map，与 Get 并发时触发 fatal error。",
                evidence="cache[k] = v", suggestion="使用 sync.Mutex 保护。")])
        if schema is VerifyVerdict:
            llm.vote_calls += 1
            valid = llm.votes.pop(0) if llm.votes else True
            return VerifyVerdict(analysis="", valid=valid, severity="P0", reason="确实无锁" if valid else "不会并发")
        if schema is ReplyIntent:
            return ReplyIntent(intent="dispute", summary="开发者认为不会并发")
        if schema is DisputeVerdict:
            return DisputeVerdict(analysis="", verdict=llm.dispute, reason="Get 与 Put 可能被不同请求并发调用")
        if schema is FixCheck:
            return FixCheck(analysis="", status=llm.fix, reason="已使用互斥锁保护写入")
        raise AssertionError(schema)

    monkeypatch.setattr(nodes_mod, "invoke_structured", fake_structured)
    monkeypatch.setattr(nodes_mod, "invoke_text", lambda messages, role="review": "理解/意图")
    return SimpleNamespace(deps=deps, gl=gl, origin=origin, llm=llm, graph=build_graph(deps))


def run(env, kind, **payload):
    event = {"kind": kind, "project_path": PP, "mr_iid": 7, **payload}
    return env.graph.invoke({"event": event, "dry_run": False, "full": False})


def test_full_lifecycle(env):
    gl, store = env.gl, env.deps.store

    # 1. 新提交：发现 P0，行内评论 + unapprove
    s = run(env, "new_push")
    assert s["conclusion"] == "REQUEST_CHANGES" and gl.approved is False
    [(did, body, position)] = gl.discussions_created
    assert position["new_line"] == 11 and "map 并发写" in body
    [f] = store.findings(PP, 7)
    assert f["status"] == "OPEN" and f["discussion_id"] == did

    # 同一个 head 不会重复审查
    run(env, "new_push")
    assert len(gl.discussions_created) == 1

    # 2. 开发者说误报 → AI 坚持 → 升级人工
    run(env, "dev_reply", fingerprint=f["fingerprint"], note_body="这里不会并发调用，误报", thread="", author="dev")
    f = store.findings(PP, 7)[0]
    assert f["status"] == "ESCALATED" and "ai-review::needs-human" in gl.labels
    assert "@alden" in gl.replies[-1][1]
    assert gl.approved is False  # P0 升级后仍阻断

    # 3. 人工确认问题成立
    run(env, "human_command", fingerprint=f["fingerprint"], command="confirm", arg="会并发", author="alden")
    assert store.findings(PP, 7)[0]["status"] == "OPEN"
    assert "ai-review::needs-human" not in gl.labels

    # 4. 开发者 push 修复 → 验证通过 → resolve + approve
    git(env.origin, "checkout", "-q", "feat")
    (env.origin / "svc.go").write_text(FIXED)
    git(env.origin, "commit", "-qam", "fix")
    gl.head = git(env.origin, "rev-parse", "HEAD")
    git(env.origin, "update-ref", "refs/merge-requests/7/head", gl.head)
    s = run(env, "new_push")
    f = store.findings(PP, 7)[0]
    assert f["status"] == "FIXED" and gl.resolved[did] is True
    assert s["conclusion"] == "APPROVE" and gl.approved is True
    assert "已修复" in gl.notes[1]
    kinds = [e["kind"] for e in store.events(f["id"])]
    assert kinds[0] == "created" and "ESCALATED" in kinds and kinds[-1] == "FIXED"


def test_dispute_accepted_withdraws(env):
    env.llm.dispute = "accept"
    run(env, "new_push")
    f = env.deps.store.findings(PP, 7)[0]
    run(env, "dev_reply", fingerprint=f["fingerprint"], note_body="Put 只在启动时调用一次", thread="", author="dev")
    f = env.deps.store.findings(PP, 7)[0]
    assert f["status"] == "WITHDRAWN" and env.gl.approved is True
    assert env.deps.store.feedback(PP, "false_positive")[0]["finding"]["title"] == "map 并发写导致 panic"


def test_poller_scans_discussions(env):
    from huatuo.poller import _scan_discussions

    run(env, "new_push")
    f = env.deps.store.findings(PP, 7)[0]
    did = f["discussion_id"]

    def note(i, author, body, **kw):
        return {"id": i, "system": False, "author": {"username": author}, "body": body, **kw}

    env.gl.discussions = lambda pp, iid: [
        {"id": did, "notes": [
            note(1, "ai-bot", "<!-- ai-cr:finding=x --> 问题"),
            note(2, "dev", "误报，这里单线程"),
            note(3, "dev", "/ai-accept 我自己放行"),  # 非 human_reviewer 的命令 → 当作普通回复
            note(4, "alden", "/ai-confirm"),
        ]},
        {"id": "other", "notes": [note(5, "dev", "/ai-review")]},
    ]
    assert _scan_discussions(env.deps, PP, 7) == 4
    jobs = []
    while j := env.deps.store.next_job():
        jobs.append((j["kind"], j["payload"].get("command"), j["payload"].get("note_id")))
    assert ("dev_reply", None, 2) in jobs and ("dev_reply", None, 3) in jobs
    assert ("human_command", "confirm", 4) in jobs and ("human_command", "review", None) in jobs
    assert _scan_discussions(env.deps, PP, 7) == 0  # 已处理的评论不会重复入队


def test_comment_conclusion_does_not_approve(env):
    """只有 P2 建议时结论为 COMMENT：不 approve，也不 unapprove。"""
    import huatuo.graph.nodes as N
    orig = N.invoke_structured

    def p2_only(schema, messages, **k):
        r = orig(schema, messages, **k)
        if schema in (FindingList, VerifyVerdict):
            for x in (r.findings if schema is FindingList else [r]):
                x.severity = "P2"
        return r
    N.invoke_structured = p2_only
    try:
        s = run(env, "new_push")
    finally:
        N.invoke_structured = orig
    assert s["conclusion"] == "COMMENT" and env.gl.approved is None


def test_lint_findings_lifecycle(env, monkeypatch):
    """lint 新问题：不经模型复核直接发布；落在未改动行上时发普通讨论；lint 不再报告时自动判定修复。"""
    from huatuo.static_analysis import LintIssue

    env.deps.settings.config.review.static_analysis.enabled = True
    unused = LintIssue("unused", "svc.go", 3, "var cache is unused", "var cache = map[string]int{}")
    errcheck = LintIssue("errcheck", "svc.go", 11, "Error return value is not checked", "cache[k] = v")
    current = {"issues": [unused, errcheck]}
    monkeypatch.setattr(nodes_mod, "run_golangci", lambda *a, **k: current["issues"])
    verify_calls = []
    orig = nodes_mod.invoke_structured

    def spy(schema, messages, **k):
        if schema is VerifyVerdict:
            verify_calls.append(messages)
        if schema is FindingList:  # 模型本轮不报问题，只看 lint
            return FindingList(findings=[])
        return orig(schema, messages, **k)
    monkeypatch.setattr(nodes_mod, "invoke_structured", spy)

    s = run(env, "new_push")
    by_title = {f["title"]: f for f in env.deps.store.findings(PP, 7)}
    u, e = by_title["[unused] var cache is unused"], by_title["[errcheck] Error return value is not checked"]
    assert u["source"] == "lint" and u["severity"] == "P2" and u["inline"] == 0   # 第 3 行不在 diff 范围内 → 普通讨论
    assert e["severity"] == "P1" and e["inline"] == 1                              # 第 11 行是新增行 → 行内
    assert not verify_calls                                                        # 不送模型复核
    assert s["conclusion"] == "REQUEST_CHANGES"                                    # P1 阻断

    # 新 push 后 errcheck 消失 → 自动判定修复并 resolve
    git(env.origin, "checkout", "-q", "feat")
    (env.origin / "svc.go").write_text(BUGGY + "\n// touch\n")
    git(env.origin, "commit", "-qam", "fix lint")
    env.gl.head = git(env.origin, "rev-parse", "HEAD")
    git(env.origin, "update-ref", "refs/merge-requests/7/head", env.gl.head)
    current["issues"] = [unused]
    s = run(env, "new_push")
    by_title = {f["title"]: f for f in env.deps.store.findings(PP, 7)}
    assert by_title["[errcheck] Error return value is not checked"]["status"] == "FIXED"
    assert env.gl.resolved[by_title["[errcheck] Error return value is not checked"]["discussion_id"]] is True
    assert by_title["[unused] var cache is unused"]["status"] == "OPEN"
    assert s["conclusion"] == "COMMENT"


def test_p0_needs_majority_of_votes(env):
    # 两票分歧 → 第三票判误报 → 2/3 认为不成立，丢弃，不阻断
    env.llm.votes = [True, False, False]
    s = run(env, "new_push")
    assert env.llm.vote_calls == 3
    assert env.deps.store.findings(PP, 7) == [] and s["conclusion"] != "REQUEST_CHANGES"


def test_p0_kept_when_two_votes_agree(env):
    env.llm.votes = [True, True]
    s = run(env, "new_push")
    assert env.llm.vote_calls == 2  # 两票一致就不再投第三票
    [f] = env.deps.store.findings(PP, 7)
    assert f["severity"] == "P0" and s["conclusion"] == "REQUEST_CHANGES"


def test_test_file_findings_capped_and_use_test_checklist(env, monkeypatch):
    env.deps.cfg.review.test_files = ["svc.go"]  # 把被审文件当作测试文件
    prompts = []
    monkeypatch.setattr(nodes_mod, "invoke_text", lambda messages, role="review": prompts.append(messages[-1].content) or "理解/意图")
    s = run(env, "new_push")
    [f] = env.deps.store.findings(PP, 7)
    assert f["severity"] == "P2"  # 模型报 P0、复核也说 P0，测试文件仍按 P2
    assert any("【本轮检查清单：测试代码质量】" in p for p in prompts)
    assert env.llm.vote_calls == 1 and s["conclusion"] != "REQUEST_CHANGES"


def test_resolve_right_after_reply_is_not_verified_twice(env):
    from huatuo.poller import _scan_discussions

    run(env, "new_push")
    f = env.deps.store.findings(PP, 7)[0]
    did = f["discussion_id"]
    notes = [
        {"id": 1, "system": False, "author": {"username": "ai-bot"}, "body": "<!-- ai-cr:finding=x --> 问题",
         "resolved": True, "resolved_by": {"username": "dev"}, "resolved_at": "2026-09-30T11:04:14Z"},
        {"id": 2, "system": False, "author": {"username": "dev"}, "body": "已在新推送中修复"},
    ]
    env.gl.discussions = lambda pp, iid: [{"id": did, "notes": notes}]

    # 同一轮扫描：回复“已修复”并 resolve → 只入队回复任务
    assert _scan_discussions(env.deps, PP, 7) == 1
    job = env.deps.store.next_job()
    assert job["kind"] == "dev_reply" and job["payload"]["note_id"] == 2
    assert _scan_discussions(env.deps, PP, 7) == 0  # 回复任务仍在执行中

    # 回复任务结束后问题仍未关闭、讨论仍是 resolved → 这时才补做 resolve 验证
    env.deps.store.finish_job(job["id"])
    assert _scan_discussions(env.deps, PP, 7) == 1
    assert env.deps.store.next_job()["payload"]["note_body"] == "（开发者直接 resolve 了该讨论）"


def test_rephrased_report_of_just_fixed_issue_is_verified_against_fix(env, monkeypatch):
    run(env, "new_push")
    [old] = env.deps.store.findings(PP, 7)

    # 开发者修复；新一轮审查把同一问题换个说法、换个位置又报了一遍
    git(env.origin, "checkout", "-q", "feat")
    (env.origin / "svc.go").write_text(FIXED)
    git(env.origin, "commit", "-qam", "fix")
    env.gl.head = git(env.origin, "rev-parse", "HEAD")
    git(env.origin, "update-ref", "refs/merge-requests/7/head", env.gl.head)

    orig = nodes_mod.invoke_structured
    verify_prompts = []

    def fake(schema, messages, role="review", temperature=None):
        if schema is FindingList:
            return FindingList(analysis="", findings=[LLMFinding(
                file="svc.go", line=3, severity="P1", category="resource", title="全局锁缺少清理导致资源问题",
                detail="修复不完整", evidence="var mu sync.Mutex", suggestion="")])  # 与旧问题相距较远、类别不同
        if schema is VerifyVerdict:
            verify_prompts.append(messages[-1].content)
            return VerifyVerdict(analysis="", valid=False, severity="P2", reason="只是重提已修复的问题")
        return orig(schema, messages, role, temperature)

    monkeypatch.setattr(nodes_mod, "invoke_structured", fake)
    s = run(env, "new_push")
    statuses = {f["title"]: f["status"] for f in env.deps.store.findings(PP, 7)}
    assert statuses == {old["title"]: "FIXED"}  # 旧问题先被判定修复，重提的被复核丢弃
    # 复核看到了同一文件刚判定修复的问题及其结论
    assert len(verify_prompts) == 1 and f"{old['title']}（FIXED）：已使用互斥锁保护写入" in verify_prompts[0]
    assert s["conclusion"] == "APPROVE"


def test_reviewing_notice_then_result_in_same_note(env):
    bodies = []
    orig = env.gl.upsert_note
    env.gl.upsert_note = lambda pp, iid, note_id, body: bodies.append((note_id, body)) or orig(pp, iid, note_id, body)
    run(env, "new_push")
    assert "华佗正在审查" in bodies[0][1] and "1 个文件块" in bodies[0][1]
    assert bodies[-1][0] == 1 and "AI 正在审查" not in bodies[-1][1]  # 审查结果原地覆盖同一条评论
    assert len({nid or 1 for nid, _ in bodies}) == 1


def test_dry_run_posts_no_reviewing_notice(env):
    env.graph.invoke({"event": {"kind": "new_push", "project_path": PP, "mr_iid": 7}, "dry_run": True, "full": True})
    assert env.gl.notes == {}


def test_failed_review_replaces_reviewing_notice(env, monkeypatch):
    from huatuo import worker

    env.deps.store.update_mr_state(PP, 7, summary_note_id=1)
    env.gl.notes[1] = "AI 正在审查 ..."
    env.deps.store.enqueue(PP, 7, "new_push", {"head_sha": env.gl.head}, "push:x")
    monkeypatch.setattr(worker, "run_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    worker.drain(env.deps, graph=None)
    assert "未能完成" in env.gl.notes[1] and "AI 正在审查" not in env.gl.notes[1]
