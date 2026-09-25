"""端到端生命周期测试：真实 git 仓库 + 假 GitLab + 假模型。
覆盖：新提交发现 P0 → 开发者说误报 → AI 坚持并升级人工 → 人工确认 → 开发者 push 修复 → 验证通过并 approve。"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from ai_cr.deps import Deps
from ai_cr.graph import nodes as nodes_mod
from ai_cr.graph.build import build_graph
from ai_cr.graph.state import DisputeVerdict, FindingList, FixCheck, LLMFinding, ReplyIntent, VerifyVerdict
from ai_cr.settings import Config, Env, ReviewConfig, Settings
from ai_cr.store import Store

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
                 review=ReviewConfig(passes=["all"], enable_tools=False))
    cfg.data_path.mkdir(parents=True)
    settings = Settings(env=Env(gitlab_url="http://x", gitlab_token="t"), config=cfg)
    deps = Deps(settings=settings, store=Store(cfg.data_path / "state.db"), gl=gl)

    llm = SimpleNamespace(dispute="maintain", fix="fixed")

    def fake_structured(schema, messages, role="review", temperature=None):
        if schema is FindingList:
            return FindingList(analysis="", findings=[LLMFinding(
                file="svc.go", line=11, severity="P0", category="concurrency", title="map 并发写导致 panic",
                detail="goroutine 中无锁写全局 map，与 Get 并发时触发 fatal error。",
                evidence="cache[k] = v", suggestion="使用 sync.Mutex 保护。")])
        if schema is VerifyVerdict:
            return VerifyVerdict(analysis="", valid=True, severity="P0", reason="确实无锁")
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
    from ai_cr.poller import _scan_discussions

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
    import ai_cr.graph.nodes as N
    orig = N.invoke_structured

    def p2_only(schema, messages, **k):
        r = orig(schema, messages, **k)
        if schema is FindingList:
            r.findings[0].severity = "P2"
        return r
    N.invoke_structured = p2_only
    try:
        s = run(env, "new_push")
    finally:
        N.invoke_structured = orig
    assert s["conclusion"] == "COMMENT" and env.gl.approved is None
