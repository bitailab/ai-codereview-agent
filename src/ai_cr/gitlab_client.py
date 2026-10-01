"""GitLab 交互封装（基于 python-gitlab）。"""
from __future__ import annotations

import logging
from functools import lru_cache

import gitlab
from gitlab.exceptions import GitlabError

log = logging.getLogger(__name__)

BOT_MARKER = "<!-- ai-cr -->"
SUMMARY_MARKER = "<!-- ai-cr:summary -->"


def finding_marker(fingerprint: str) -> str:
    return f"<!-- ai-cr:finding={fingerprint} -->"


def is_bot_note(body: str) -> bool:
    return "<!-- ai-cr" in (body or "")


class GitLab:
    def __init__(self, url: str, token: str):
        self.gl = gitlab.Gitlab(url.rstrip("/"), private_token=token, retry_transient_errors=True, timeout=60)
        self.gl.auth()
        self.me = self.gl.user  # 当前 token 对应账号（bot）

    @lru_cache(maxsize=64)
    def project(self, project_path: str):
        return self.gl.projects.get(project_path)

    @lru_cache(maxsize=16)
    def user_id(self, username: str) -> int:
        users = self.gl.users.list(username=username)
        if not users:
            raise ValueError(f"GitLab 用户不存在: {username}")
        return users[0].id

    # ---------------- 查询 ----------------
    def list_open_mrs_for(self, user_id: int) -> list[dict]:
        """全局查询：user 是 reviewer 或 assignee 的所有 opened MR。"""
        seen: dict[tuple, dict] = {}
        for key in ("reviewer_id", "assignee_id"):
            for mr in self.gl.mergerequests.list(
                scope="all", state="opened", iterator=True, per_page=100, **{key: user_id}
            ):
                a = mr.attributes
                seen[(a["project_id"], a["iid"])] = a
        return list(seen.values())

    def mr(self, project_path: str, iid: int):
        return self.project(project_path).mergerequests.get(iid)

    def discussions(self, project_path: str, iid: int) -> list[dict]:
        mr = self.project(project_path).mergerequests.get(iid, lazy=True)
        return [d.attributes for d in mr.discussions.list(iterator=True, per_page=100)]

    def failed_checks(self, project_path: str, sha: str, pipeline_id: int | None) -> list[dict]:
        """失败的检查项：GitLab CI 的 job + 外部 CI（Jenkins 等）回报的 commit status。"""
        proj = self.project(project_path)
        out: list[dict] = []
        try:
            if pipeline_id:
                for j in proj.pipelines.get(pipeline_id, lazy=True).jobs.list(scope="failed", iterator=True):
                    out.append({"name": f"{j.stage}/{j.name}", "description": j.failure_reason or "", "url": j.web_url})
            for st in proj.commits.get(sha, lazy=True).statuses.list(iterator=True):
                if st.status in ("failed", "canceled") and st.name not in {o["name"] for o in out}:
                    out.append({"name": st.name, "description": st.description or "", "url": st.target_url or ""})
        except GitlabError as e:
            log.warning("获取失败检查项出错: %s", e)
        return out

    def file_raw(self, project_path: str, path: str, ref: str) -> str | None:
        try:
            return self.project(project_path).files.raw(file_path=path, ref=ref).decode("utf-8", "replace")
        except GitlabError:
            return None

    # ---------------- 写入 ----------------
    def create_discussion(self, project_path: str, iid: int, body: str, position: dict | None) -> str:
        mr = self.project(project_path).mergerequests.get(iid, lazy=True)
        data = {"body": body}
        if position:
            data["position"] = position
        try:
            d = mr.discussions.create(data)
        except GitlabError as e:
            if not position:
                raise
            # 行号不在 diff 里等原因导致行内评论失败，降级为普通讨论
            log.warning("行内评论失败（%s），降级为普通讨论", e)
            d = mr.discussions.create({"body": body})
        return d.id

    def reply(self, project_path: str, iid: int, discussion_id: str, body: str) -> None:
        mr = self.project(project_path).mergerequests.get(iid, lazy=True)
        d = mr.discussions.get(discussion_id)
        d.notes.create({"body": body})

    def set_resolved(self, project_path: str, iid: int, discussion_id: str, resolved: bool) -> None:
        mr = self.project(project_path).mergerequests.get(iid, lazy=True)
        d = mr.discussions.get(discussion_id)
        try:
            d.resolved = resolved
            d.save()
        except GitlabError as e:
            log.warning("设置讨论 %s resolved=%s 失败: %s", discussion_id, resolved, e)

    def upsert_note(self, project_path: str, iid: int, note_id: int | None, body: str) -> int:
        mr = self.project(project_path).mergerequests.get(iid, lazy=True)
        if note_id:
            try:
                note = mr.notes.get(note_id)
                note.body = body
                note.save()
                return note_id
            except GitlabError:
                log.info("汇总评论 %s 已不存在，重新创建", note_id)
        return mr.notes.create({"body": body}).id

    def approve(self, project_path: str, iid: int) -> None:
        mr = self.project(project_path).mergerequests.get(iid, lazy=True)
        try:
            mr.approve()
        except GitlabError as e:
            log.info("approve 未生效（可能已批准或无权限）: %s", e)

    def merge(self, project_path: str, iid: int, sha: str) -> bool:
        """只合并审查过的 head（sha 不匹配说明审查后又有新推送，GitLab 会拒绝）。不可合并时只记日志。"""
        mr = self.project(project_path).mergerequests.get(iid, lazy=True)
        try:
            mr.merge(sha=sha)
            return True
        except GitlabError as e:
            log.warning("自动合并未生效（有冲突、未满足合并条件或无权限）: %s", e)
            return False

    def unapprove(self, project_path: str, iid: int) -> None:
        mr = self.project(project_path).mergerequests.get(iid, lazy=True)
        try:
            mr.unapprove()
        except GitlabError as e:
            log.info("unapprove 未生效（可能本来就未批准）: %s", e)

    def set_label(self, project_path: str, iid: int, label: str, present: bool) -> None:
        mr = self.mr(project_path, iid)
        labels = list(mr.labels)
        if present and label not in labels:
            mr.add_labels = label
        elif not present and label in labels:
            mr.remove_labels = label
        else:
            return
        try:
            mr.save()
        except GitlabError as e:
            log.warning("更新标签失败: %s", e)
