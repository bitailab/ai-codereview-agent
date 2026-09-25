"""轮询 GitLab，把需要处理的事件写入任务队列。GitLab 无法访问本机，因此不使用 webhook。"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from .deps import Deps
from .gitlab_client import is_bot_note
from .graph.render import pipeline_notice_body
from .graph.state import OPEN_STATES
from .pipeline_gate import FAILED, READY, WAIT, check_pipeline
from .store import now

log = logging.getLogger(__name__)

COMMAND_RE = re.compile(r"^\s*/ai-(review|confirm|accept|downgrade)\b[ \t]*(.*)", re.I | re.S)


def parse_command(body: str) -> tuple[str, str] | None:
    m = COMMAND_RE.match(body or "")
    return (m.group(1).lower(), m.group(2).strip()) if m else None


def _age_seconds(ts: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()


def poll_once(deps: Deps) -> int:
    cfg, store, gl = deps.cfg, deps.store, deps.gl
    human_id = gl.user_id(cfg.human_reviewer)
    projects = set(cfg.projects)
    enqueued = 0
    for a in gl.list_open_mrs_for(human_id):
        pp = (a.get("references") or {}).get("full", "").split("!")[0]
        iid = a["iid"]
        if pp not in projects or a.get("draft") or a.get("work_in_progress"):
            continue
        st = store.mr_state(pp, iid)
        updated = a["updated_at"]
        has_open = any(f["status"] in OPEN_STATES for f in store.findings(pp, iid))
        rescan_due = has_open and (
            not st.get("last_discussion_scan") or _age_seconds(st["last_discussion_scan"]) > cfg.rescan_open_findings_seconds
        )
        head = a.get("sha")
        # 流水线结束不会更新 MR 的 updated_at，所以等待流水线的 MR 每轮都要重新检查
        waiting = bool(head) and st.get("pipeline_wait_sha") == head
        if updated == st.get("last_seen_updated_at") and not rescan_due and not waiting:
            continue
        if _age_seconds(updated) < cfg.quiet_seconds:
            log.debug("%s!%s 处于静默期，下次再看", pp, iid)
            continue

        if head and not store.reviewed(pp, iid, head):
            enqueued += _gate_and_enqueue(deps, pp, iid, head, st)
        enqueued += _scan_discussions(deps, pp, iid)
        store.update_mr_state(pp, iid, last_seen_updated_at=updated, last_discussion_scan=now())
    return enqueued


def _gate_and_enqueue(deps: Deps, pp: str, iid: int, head: str, st: dict) -> int:
    """流水线通过（或没有流水线）才入队审查；失败时提示开发者先修复。"""
    cfg, store, gl = deps.cfg, deps.store, deps.gl
    res = check_pipeline(gl.mr(pp, iid).attributes, head, cfg.pipeline)
    if res.decision == READY:
        store.update_mr_state(pp, iid, pipeline_wait_sha=None)
        if store.enqueue(pp, iid, "new_push", {"head_sha": head}, f"push:{pp}:{iid}:{head}"):
            log.info("入队 new_push %s!%s @%s（%s）", pp, iid, head[:8], res.reason)
            return 1
        return 0

    store.update_mr_state(pp, iid, pipeline_wait_sha=head)
    if st.get("pipeline_wait_sha") != head:
        log.info("⏳ %s!%s @%s 暂不审查：%s", pp, iid, head[:8], res.reason)
    p = res.pipeline or {}
    key = f"{head}:{p.get('id')}:{p.get('status')}"
    notice = st.get("pipeline_notice") or ""
    # 失败时提示；已经提示过失败、之后流水线被重试的，更新为“重新运行中”，避免提示过时
    should_notify = cfg.pipeline.notify_on_failure and notice != key and (
        res.decision == FAILED or (res.decision == WAIT and p and notice.startswith(head))
    )
    if should_notify:
        checks = gl.failed_checks(pp, head, p.get("id")) if res.decision == FAILED else []
        body = pipeline_notice_body(head, p, store.last_summary(pp, iid), checks)
        note_id = gl.upsert_note(pp, iid, st.get("summary_note_id"), body)
        store.update_mr_state(pp, iid, summary_note_id=note_id, pipeline_notice=key)
        log.info("%s!%s 流水线状态 %s，已更新提示", pp, iid, p.get("status"))
    return 0


def _thread_text(notes: list[dict], upto: int) -> str:
    out = []
    for n in notes[: upto + 1]:
        who = "AI" if is_bot_note(n["body"]) else n["author"]["username"]
        body = re.sub(r"<!--.*?-->", "", n["body"], flags=re.S).strip()
        out.append(f"[{who}]: {body[:1500]}")
    return "\n---\n".join(out)


def _scan_discussions(deps: Deps, pp: str, iid: int) -> int:
    cfg, store, gl = deps.cfg, deps.store, deps.gl
    human = cfg.human_reviewer
    bot = deps.bot_username
    by_disc = store.discussion_ids(pp, iid)
    count = 0
    for d in gl.discussions(pp, iid):
        notes = [n for n in d["notes"] if not n.get("system")]
        f = by_disc.get(d["id"])
        for i, n in enumerate(notes):
            if store.note_processed(n["id"]):
                continue
            store.mark_note(n["id"])
            body, author = n["body"], n["author"]["username"]
            if is_bot_note(body):
                continue
            cmd = parse_command(body)
            if cmd and cmd[0] != "review" and author != human:
                cmd = None  # 裁决命令只接受 human_reviewer
            if cmd and cmd[0] == "review":
                count += store.enqueue(pp, iid, "human_command", {"command": "review", "full": True, "author": author},
                                       f"note:{n['id']}")
                continue
            if f is None:
                continue  # 与 AI 问题无关的普通讨论
            payload = {"fingerprint": f["fingerprint"], "discussion_id": d["id"], "note_id": n["id"],
                       "author": author, "note_body": body, "thread": _thread_text(notes, i)}
            if cmd:
                payload |= {"command": cmd[0], "arg": cmd[1]}
                count += store.enqueue(pp, iid, "human_command", payload, f"note:{n['id']}")
            else:
                count += store.enqueue(pp, iid, "dev_reply", payload, f"note:{n['id']}")

        # 讨论被人手动 resolve，而 AI 这边问题仍未关闭
        if f and f["status"] in OPEN_STATES and notes and notes[0].get("resolved"):
            by = (notes[0].get("resolved_by") or {}).get("username")
            key = f"resolve:{d['id']}:{notes[0].get('resolved_at') or ''}"
            base = {"fingerprint": f["fingerprint"], "discussion_id": d["id"], "author": by}
            if by == human:
                count += store.enqueue(pp, iid, "human_command", base | {"command": "resolved"}, key)
            elif by and by != bot:
                # 开发者直接 resolve：当作“已修复”去验证，未修复会回复说明
                count += store.enqueue(pp, iid, "dev_reply", base | {
                    "intent_hint": "claim_fixed", "note_body": "（开发者直接 resolve 了该讨论）",
                    "thread": _thread_text(notes, len(notes) - 1)}, key)
    return count
