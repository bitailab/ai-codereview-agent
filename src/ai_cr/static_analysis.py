"""可选：对改动的 Go 包跑 go vet / staticcheck，只保留落在改动行上的结果，作为给模型的“已知信号”。"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from .diff_parser import FileDiff
from .git_repo import Mirror

log = logging.getLogger(__name__)
LINE_RE = re.compile(r"^(?:\./)?(?P<file>[^:\s]+\.go):(?P<line>\d+)(?::\d+)?:\s*(?P<msg>.+)$")


def run_static_analysis(mirror: Mirror, sha: str, files: list[FileDiff], workdir: Path) -> dict[str, list[str]]:
    go_files = [f for f in files if f.new_path.endswith(".go") and not f.deleted]
    if not go_files or not shutil.which("go"):
        return {}
    added = {f.new_path: f.added_lines() for f in go_files}
    pkgs = sorted({"./" + str(Path(f.new_path).parent) for f in go_files})
    wt = mirror.worktree(sha, workdir)
    results: dict[str, list[str]] = {}
    try:
        cmds = [["go", "vet", *pkgs]]
        if shutil.which("staticcheck"):
            cmds.append(["staticcheck", *pkgs])
        for cmd in cmds:
            try:
                res = subprocess.run(cmd, cwd=wt, capture_output=True, text=True, timeout=600)
            except subprocess.TimeoutExpired:
                log.warning("%s 超时", cmd[0])
                continue
            for line in (res.stdout + res.stderr).splitlines():
                m = LINE_RE.match(line.strip())
                if not m:
                    continue
                path, ln = m.group("file"), int(m.group("line"))
                if ln in added.get(path, set()):
                    results.setdefault(path, []).append(f"L{ln} [{cmd[0]}] {m.group('msg')}")
    finally:
        mirror.remove_worktree(wt)
    return results
