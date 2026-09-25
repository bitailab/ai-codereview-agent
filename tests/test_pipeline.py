from datetime import datetime, timedelta, timezone

from ai_cr.pipeline_gate import FAILED, READY, WAIT, check_pipeline
from ai_cr.settings import PipelineConfig

from test_lifecycle import PP, env  # noqa: F401  复用端到端测试的环境

HEAD = "a" * 40


def ts(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def mr(status=None, sha=HEAD, created=1, updated=10):
    p = None if status is None else {"id": 9, "sha": sha, "status": status, "created_at": ts(created), "web_url": "u"}
    return {"head_pipeline": p, "updated_at": ts(updated)}


def test_check_pipeline_decisions():
    cfg = PipelineConfig()
    assert check_pipeline(mr("success"), HEAD, cfg).decision == READY
    assert check_pipeline(mr("running"), HEAD, cfg).decision == WAIT
    assert check_pipeline(mr("pending"), HEAD, cfg).decision == WAIT
    assert check_pipeline(mr("failed"), HEAD, cfg).decision == FAILED
    assert check_pipeline(mr("canceled"), HEAD, cfg).decision == FAILED
    # 流水线属于旧提交：新提交的流水线还没创建 → 宽限期内等待，超过后视为没有 CI
    assert check_pipeline(mr("success", sha="b" * 40, updated=1), HEAD, cfg).decision == WAIT
    assert check_pipeline(mr(None, updated=1), HEAD, cfg).decision == WAIT
    assert check_pipeline(mr(None, updated=10), HEAD, cfg).decision == READY
    # 跑太久不再等
    assert check_pipeline(mr("running", created=200), HEAD, cfg).decision == READY
    assert check_pipeline(mr("failed"), HEAD, PipelineConfig(enabled=False)).decision == READY


def test_poller_waits_for_pipeline(env):  # noqa: F811
    from ai_cr.poller import poll_once

    gl, store = env.gl, env.deps.store
    gl.pipeline = {"id": 1, "sha": gl.head, "status": "running", "created_at": ts(1), "web_url": "u"}
    gl.updated_at = ts(10)
    gl.discussions = lambda pp, iid: []
    gl.failed_checks = lambda pp, sha, pid: [{"name": "jenkinsci/lint", "description": "lint failed", "url": "http://j/1"}]
    gl.list_open_mrs_for = lambda uid: [{"references": {"full": f"{PP}!7"}, "iid": 7, "sha": gl.head,
                                         "updated_at": gl.updated_at, "draft": False}]
    orig_mr = gl.mr

    def mr_with_pipeline(pp, iid):
        m = orig_mr(pp, iid)
        m.attributes |= {"head_pipeline": gl.pipeline, "updated_at": gl.updated_at}
        return m
    gl.mr = mr_with_pipeline

    def queued():
        return [j for j in store._exec("SELECT kind FROM jobs").fetchall()]

    # 1. 流水线运行中：不入队、不发评论
    assert poll_once(env.deps) == 0 and not queued() and not gl.notes
    # 2. 流水线失败：不入队，汇总评论提示一次；重复轮询不会重复提示
    gl.pipeline["status"] = "failed"
    assert poll_once(env.deps) == 0 and "等待流水线通过" in gl.notes[1]
    assert "[jenkinsci/lint](http://j/1) — lint failed" in gl.notes[1]
    gl.notes.clear()
    poll_once(env.deps)
    assert not gl.notes
    # 3. 失败后被重试：提示更新为“重新运行中”，且只更新一次
    gl.pipeline["status"] = "running"
    assert poll_once(env.deps) == 0 and "重新运行" in gl.notes[1] and "❌" not in gl.notes[1]
    gl.notes.clear()
    poll_once(env.deps)
    assert not gl.notes
    # 4. 重试后通过（MR 的 updated_at 不变）：仍会被重新检查并入队
    gl.pipeline["status"] = "success"
    assert poll_once(env.deps) == 1 and len(queued()) == 1
    assert store.mr_state(PP, 7)["pipeline_wait_sha"] is None
