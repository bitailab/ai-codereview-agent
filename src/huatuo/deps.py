from __future__ import annotations

from dataclasses import dataclass, field

from .diff_parser import FileDiff, parse_diff
from .git_repo import Mirror
from .gitlab_client import GitLab
from .settings import Settings, get_settings
from .store import Store


@dataclass
class Deps:
    settings: Settings
    store: Store
    gl: GitLab
    _mirrors: dict[str, Mirror] = field(default_factory=dict)
    _diffs: dict[tuple, dict[str, FileDiff]] = field(default_factory=dict)

    @classmethod
    def create(cls) -> "Deps":
        s = get_settings()
        return cls(settings=s, store=Store(s.config.data_path / "state.db"), gl=GitLab(s.env.gitlab_url, s.env.gitlab_token))

    @property
    def cfg(self):
        return self.settings.config

    @property
    def bot_username(self) -> str:
        return self.gl.me.username

    def mirror(self, project_path: str) -> Mirror:
        if project_path not in self._mirrors:
            p = self.gl.project(project_path)
            if self.cfg.git_url_style == "ssh":
                m = Mirror(self.cfg.data_path, project_path, p.ssh_url_to_repo)
            else:
                m = Mirror(self.cfg.data_path, project_path, p.http_url_to_repo, self.settings.env.gitlab_token)
            self._mirrors[project_path] = m
        return self._mirrors[project_path]

    def mr_diff(self, project_path: str, base: str, head: str, for_position: bool = False) -> dict[str, FileDiff]:
        """MR 的 diff，按路径索引；缓存最近几个。
        给模型读的版本带完整函数上下文；for_position=True 时用 3 行上下文，与 GitLab 页面上可评论的行保持一致。"""
        key = (project_path, base, head, for_position)
        if key not in self._diffs:
            if len(self._diffs) > 8:
                self._diffs.pop(next(iter(self._diffs)))
            text = self.mirror(project_path).diff(base, head, function_context=not for_position)
            self._diffs[key] = {fd.path: fd for fd in parse_diff(text)}
        return self._diffs[key]
