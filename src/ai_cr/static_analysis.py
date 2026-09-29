"""golangci-lint 静态分析：在 base 和 head 各跑一次取差集，只报告本次 MR 新引入的问题。

为什么不用 --new-from-rev：它只看改动行，而“调用方被删导致函数变成死代码”这类问题落在未改动的声明行上，会被漏掉。
以仓库自己的 .golangci.yml 为准（团队规范），可额外叠加 extra_linters（默认 unused）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .diff_parser import FileDiff
from .git_repo import Mirror

log = logging.getLogger(__name__)

V1_VERSION_RE = re.compile(r"""^version:[ \t]*["']?1["']?[ \t]*(#.*)?\n?""", re.M)


@dataclass(frozen=True)
class LintIssue:
    linter: str
    file: str
    line: int
    text: str
    source_line: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        # 不含行号：同一问题在 base/head 上行号可能不同
        return (self.linter, self.file, self.text)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha1(f"lint|{self.linter}|{self.file}|{self.text}".encode()).hexdigest()[:12]


def find_golangci(configured: str | None) -> str | None:
    for cand in (configured, "~/.local/share/ai-cr/bin/golangci-lint"):
        if cand and Path(os.path.expanduser(cand)).is_file():
            return os.path.expanduser(cand)
    return shutil.which("golangci-lint")


def new_issues(head: list[LintIssue], base: list[LintIssue]) -> list[LintIssue]:
    base_keys = {i.key for i in base}
    return [i for i in head if i.key not in base_keys]


def _packages(mirror: Mirror, sha: str, dirs: set[str]) -> list[str]:
    """只保留在该 commit 中存在的目录（base 上可能还没有新建的包）。"""
    out = []
    for d in sorted(dirs):
        if d == "." or mirror.ls_tree(sha, d):
            out.append("./" if d == "." else f"./{d}/")
    return out


def _prepare_config(lint: str, wt: Path) -> list[str]:
    """v1 格式的配置用 golangci-lint migrate 在 worktree 内转换；转换失败则不用仓库配置。"""
    cfg = next((wt / n for n in (".golangci.yml", ".golangci.yaml", ".golangci.toml", ".golangci.json") if (wt / n).exists()), None)
    if cfg is None:
        return []
    text = cfg.read_text(encoding="utf-8", errors="replace")
    if "version:" in text and ('"2"' in text or "'2'" in text or "version: 2" in text):
        return []
    # 显式写了 version: "1" 的配置，migrate 会报 "configuration version is already set: 1" 拒绝转换
    stripped = V1_VERSION_RE.sub("", text)
    if stripped != text:
        cfg.write_text(stripped, encoding="utf-8")  # worktree 是临时副本，可以直接改
    res = subprocess.run([lint, "migrate", "--config", str(cfg), "--skip-validation"], cwd=wt, capture_output=True, text=True, timeout=120)
    if res.returncode == 0:
        log.info("已将 v1 格式的 %s 转换为 v2", cfg.name)
        return []
    log.warning("配置迁移失败，改为不使用仓库配置: %s", res.stderr.strip()[:300])
    return ["--no-config"]


def _run(lint: str, wt: Path, pkgs: list[str], extra_linters: list[str], timeout: int) -> list[LintIssue] | None:
    if not pkgs:
        return []
    args = [lint, "run", "--output.json.path=stdout", "--output.text.path=/dev/null", "--show-stats=false",
            "--max-issues-per-linter=0", "--max-same-issues=0", *_prepare_config(lint, wt)]
    for name in extra_linters:
        args += ["-E", name]
    env = os.environ | {"GOTOOLCHAIN": "auto"}
    try:
        res = subprocess.run([*args, *pkgs], cwd=wt, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        log.warning("golangci-lint 超时（%ss）", timeout)
        return None
    if res.returncode not in (0, 1):  # 0 无问题，1 有问题，其他为执行错误
        log.warning("golangci-lint 执行失败（exit %s）: %s", res.returncode, res.stderr.strip()[-500:])
        return None
    try:
        data = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        log.warning("golangci-lint 输出无法解析: %s", res.stdout[:200])
        return None
    out = []
    for i in data.get("Issues") or []:
        pos = i.get("Pos") or {}
        out.append(LintIssue(
            linter=i.get("FromLinter", "?"), file=pos.get("Filename", ""), line=int(pos.get("Line") or 0),
            text=i.get("Text", ""), source_line=((i.get("SourceLines") or [""])[0]).strip(),
        ))
    return out


def run_golangci(mirror: Mirror, base_sha: str, head_sha: str, files: list[FileDiff], workdir: Path,
                 *, configured_path: str | None, extra_linters: list[str], timeout: int) -> list[LintIssue] | None:
    """返回本次 MR 新引入的问题；不可用或执行失败返回 None（与“没有问题”区分）。"""
    go_files = [f for f in files if f.new_path.endswith(".go") and not f.deleted]
    if not go_files:
        return []
    lint = find_golangci(configured_path)
    if not lint:
        log.warning("未找到 golangci-lint，跳过静态分析")
        return None
    dirs = {str(Path(f.new_path).parent) for f in go_files}
    results: dict[str, list[LintIssue] | None] = {}
    for side, sha in (("head", head_sha), ("base", base_sha)):
        wt = workdir / side
        try:
            mirror.worktree(sha, wt)
            results[side] = _run(lint, wt, _packages(mirror, sha, dirs), extra_linters, timeout)
        finally:
            mirror.remove_worktree(wt)
        if results[side] is None:
            shutil.rmtree(workdir, ignore_errors=True)
            return None
    shutil.rmtree(workdir, ignore_errors=True)
    found = new_issues(results["head"], results["base"])
    log.info("golangci-lint：head %d 条，base %d 条，新引入 %d 条", len(results["head"]), len(results["base"]), len(found))
    return found
