"""基于 bare mirror 的只读 git 操作：不 checkout，多个 MR 互不干扰。"""
from __future__ import annotations

import base64
import fnmatch
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class GitError(RuntimeError):
    pass


class Mirror:
    def __init__(self, root: Path, project_path: str, clone_url: str, http_token: str | None = None):
        self.dir = root / "mirrors" / (project_path.replace("/", "__") + ".git")
        self.clone_url = clone_url
        self._auth: list[str] = []
        if http_token and clone_url.startswith("http"):
            basic = base64.b64encode(f"oauth2:{http_token}".encode()).decode()
            self._auth = ["-c", f"http.extraHeader=Authorization: Basic {basic}"]

    def _run(self, *args: str, check: bool = True, timeout: int = 120, cwd: Path | None = None) -> str:
        cmd = ["git", *self._auth, *args]
        res = subprocess.run(
            cmd,
            cwd=cwd or self.dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if check and res.returncode != 0:
            raise GitError(f"git {' '.join(args[:3])} 失败: {res.stderr.strip()[:500]}")
        return res.stdout

    def ensure(self) -> None:
        if not self.dir.exists():
            self.dir.parent.mkdir(parents=True, exist_ok=True)
            log.info("克隆镜像 %s", self.clone_url)
            self._run("clone", "--bare", self.clone_url, str(self.dir), cwd=self.dir.parent, timeout=900)

    def fetch_mr(self, iid: int, target_branch: str) -> None:
        self.ensure()
        self._run(
            "fetch", "--force", "--update-head-ok", "origin",
            f"+refs/heads/{target_branch}:refs/heads/{target_branch}",
            f"+refs/merge-requests/{iid}/head:refs/mr/{iid}",
            timeout=600,
        )

    def has_commit(self, sha: str) -> bool:
        return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=self.dir, capture_output=True).returncode == 0

    def is_ancestor(self, a: str, b: str) -> bool:
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=self.dir, capture_output=True).returncode == 0

    def diff(self, base: str, head: str, paths: list[str] | None = None, function_context: bool = True) -> str:
        args = ["diff", "--no-color", "--no-ext-diff", "-M"]
        args.append("--function-context" if function_context else "-U3")
        args += [base, head]
        if paths:
            args += ["--", *paths]
        return self._run(*args, timeout=300)

    def changed_files(self, base: str, head: str) -> list[str]:
        out = self._run("diff", "--name-only", "-M", base, head, check=False)
        return [x for x in out.splitlines() if x.strip()]

    def show(self, sha: str, path: str) -> str | None:
        res = subprocess.run(
            ["git", "show", f"{sha}:{path}"], cwd=self.dir, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        return res.stdout if res.returncode == 0 else None

    def grep(self, sha: str, pattern: str, path_glob: str | None = None, max_results: int = 40) -> list[str]:
        args = ["grep", "-n", "-I", "-E", "--max-count=20", pattern, sha]
        if path_glob:
            args += ["--", path_glob]
        out = self._run(*args, check=False, timeout=120)
        lines = []
        for ln in out.splitlines()[:max_results]:
            # 去掉前缀 "<sha>:"
            lines.append(ln[len(sha) + 1:] if ln.startswith(sha + ":") else ln)
        return lines

    def ls_tree(self, sha: str, path: str = "") -> list[str]:
        target = f"{sha}:{path}" if path else sha
        out = self._run("ls-tree", "--name-only", target, check=False)
        return out.splitlines()

    def log(self, sha: str, path: str, n: int = 5) -> str:
        return self._run("log", f"-{n}", "--format=%h %an %ad %s", "--date=short", sha, "--", path, check=False)

    def worktree(self, sha: str, dest: Path) -> Path:
        """需要真实文件时（静态分析）创建临时 worktree。"""
        if dest.exists():
            self._run("worktree", "remove", "--force", str(dest), check=False)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run("worktree", "add", "--detach", "--force", str(dest), sha, timeout=600)
        return dest

    def remove_worktree(self, dest: Path) -> None:
        self._run("worktree", "remove", "--force", str(dest), check=False)


def is_ignored(path: str, patterns: list[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in patterns)


_WS = re.compile(r"\s+")
_LINE_NO_PREFIX = re.compile(r"^\s*(?:L\d+\s+[+\- ]\s?|L\d+\s+|\d+\|\s?|[+\-]\s?)")


def clean_evidence(evidence: str) -> str:
    """模型常把 diff 的行号/前缀、（多重）转义的换行一起抄进 evidence，这里还原成纯代码。"""
    text = (evidence or "").strip().strip("`")
    if "\n" not in text and ("\\n" in text or "\\t" in text):
        for _ in range(3):
            text = text.replace("\\\\", "\\")
        text = text.replace("\\n", "\n").replace("\\t", "\t")
        text = re.sub(r"\\+(?=\s)", "", text)  # 多重转义残留的反斜杠
    text = re.sub(r"^(?:go|golang|python|java)\n", "", text)
    return "\n".join(_LINE_NO_PREFIX.sub("", ln) for ln in text.splitlines()).strip()


def normalize_code(s: str) -> str:
    return _WS.sub(" ", s).strip()


def locate_snippet(content: str, snippet: str, hint_line: int | None = None) -> int | None:
    """在文件中定位代码片段（忽略空白差异），返回片段首行的行号（1 起始）。多处命中时取离 hint 最近的。"""
    if not snippet or not content:
        return None
    snippet_lines = [normalize_code(x) for x in snippet.strip().splitlines() if x.strip()]
    if not snippet_lines:
        return None
    file_lines = [normalize_code(x) for x in content.splitlines()]
    first = snippet_lines[0]
    hits: list[int] = []
    for i, ln in enumerate(file_lines):
        if first and (ln == first or (len(first) >= 12 and first in ln)):
            ok = True
            j = i + 1
            for s in snippet_lines[1:]:
                while j < len(file_lines) and not file_lines[j]:
                    j += 1
                if j >= len(file_lines) or (file_lines[j] != s and not (len(s) >= 12 and s in file_lines[j])):
                    ok = False
                    break
                j += 1
            if ok:
                hits.append(i + 1)
    if not hits:
        return None
    if hint_line is None:
        return hits[0]
    return min(hits, key=lambda n: abs(n - hint_line))


_TRIVIAL = {"{", "}", "})", "}()", ")", "(", "else {", "} else {", "return", "return nil", "return err"}


def locate_evidence(content: str, evidence: str, hint_line: int | None, window: int = 15) -> int | None:
    """多级定位：原文 → 清洗后 → 逐行模糊（hint 附近 ≥60% 的有效行能在文件中找到）。"""
    if not content or not evidence:
        return None
    cleaned = clean_evidence(evidence)
    for cand in (evidence, cleaned):
        loc = locate_snippet(content, cand, hint_line)
        if loc is not None:
            return loc
    lines = [normalize_code(x) for x in cleaned.splitlines()]
    lines = [x for x in lines if len(x) >= 4 and x not in _TRIVIAL]
    if not lines or hint_line is None:
        return None
    file_lines = [normalize_code(x) for x in content.splitlines()]
    lo, hi = max(0, hint_line - 1 - window), min(len(file_lines), hint_line + window)
    found = []
    for s in lines:
        # 同一行代码可能在窗口里出现多次（别的函数里的 `p.lock.Lock()`）：取离模型给的行号最近的那处，而不是第一处
        hits = [i for i in range(lo, hi)
                if s == file_lines[i] or (len(s) >= 8 and s in file_lines[i]) or (len(file_lines[i]) >= 8 and file_lines[i] in s)]
        if hits:
            found.append(min(hits, key=lambda i: abs(i + 1 - hint_line)) + 1)
    if len(found) * 10 >= len(lines) * 6:
        return min(found)
    return None
