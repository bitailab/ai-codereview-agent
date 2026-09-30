"""解析 unified diff：给每行标注新文件行号，并计算哪些行可以挂 GitLab 行内评论。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class DiffLine:
    kind: str  # "+", "-", " "
    old_no: int | None
    new_no: int | None
    text: str


@dataclass
class Hunk:
    header: str
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class FileDiff:
    old_path: str
    new_path: str
    new_file: bool = False
    deleted: bool = False
    binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.old_path if self.deleted else self.new_path

    def added_lines(self) -> set[int]:
        return {ln.new_no for h in self.hunks for ln in h.lines if ln.kind == "+" and ln.new_no}

    def position_for(self, new_line: int) -> dict | None:
        """返回 GitLab position 需要的 old_line/new_line；不在 diff 中返回 None。"""
        for h in self.hunks:
            for ln in h.lines:
                if ln.new_no == new_line:
                    if ln.kind == "+":
                        return {"new_line": new_line}
                    if ln.kind == " ":
                        return {"new_line": new_line, "old_line": ln.old_no}
        return None

    def nearest_commentable(self, line: int, window: int = 3) -> int | None:
        """行号可能有少量偏差：在 ±window 范围内找最近的新增行，其次是上下文行。"""
        added = self.added_lines()
        context = {ln.new_no for h in self.hunks for ln in h.lines if ln.kind == " " and ln.new_no}
        for pool in (added, context):
            cands = [n for n in pool if abs(n - line) <= window]
            if cands:
                return min(cands, key=lambda n: (abs(n - line), n))
        return None

    def annotated(self, hunks: list[Hunk] | None = None) -> str:
        """带新文件行号的 diff 文本，供模型阅读：`L128 + code`。删除行用 `     - code`。"""
        out: list[str] = []
        for h in hunks if hunks is not None else self.hunks:
            out.append(h.header)
            for ln in h.lines:
                num = f"L{ln.new_no}" if ln.new_no and ln.kind != "-" else ""
                out.append(f"{num:>6} {ln.kind} {ln.text}")
        return "\n".join(out)

    def chunks(self, max_chars: int) -> list[list[Hunk]]:
        """按 hunk 切分，保证每块不超过 max_chars（超大 hunk 先按行拆开，超长行截断）。"""
        groups: list[list[Hunk]] = []
        cur: list[Hunk] = []
        size = 0
        for h in (part for big in self.hunks for part in _split_hunk(big, max_chars)):
            hs = sum(len(ln.text) + 10 for ln in h.lines) + len(h.header)
            if cur and size + hs > max_chars:
                groups.append(cur)
                cur, size = [], 0
            cur.append(h)
            size += hs
        if cur:
            groups.append(cur)
        return groups


MAX_LINE_CHARS = 1000


def _split_hunk(h: Hunk, max_chars: int) -> list[Hunk]:
    """超长行截断；超过 max_chars 的 hunk 按行拆成多个（行号标注在每行上，拆开不影响定位）。"""
    lines = [
        DiffLine(ln.kind, ln.old_no, ln.new_no, ln.text[:MAX_LINE_CHARS] + f" …（截断，原长 {len(ln.text)} 字符）")
        if len(ln.text) > MAX_LINE_CHARS else ln
        for ln in h.lines
    ]
    parts: list[Hunk] = []
    cur = Hunk(h.header)
    size = len(h.header)
    for ln in lines:
        if cur.lines and size + len(ln.text) + 10 > max_chars:
            parts.append(cur)
            cur, size = Hunk(h.header), len(h.header)
        cur.lines.append(ln)
        size += len(ln.text) + 10
    parts.append(cur)
    return parts

def _strip_prefix(p: str) -> str:
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p.startswith(("a/", "b/")):
        return p[2:]
    return p


def parse_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            m = re.match(r'^diff --git (?:"?a/(.+?)"?) (?:"?b/(.+?)"?)$', raw)
            a, b = (m.group(1), m.group(2)) if m else ("", "")
            cur = FileDiff(old_path=a, new_path=b)
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if hunk is None or not raw or raw[0] not in "+- \\":
            if raw.startswith("new file mode"):
                cur.new_file = True
            elif raw.startswith("deleted file mode"):
                cur.deleted = True
            elif raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
                cur.binary = True
            elif raw.startswith("--- ") and hunk is None:
                p = raw[4:]
                if p != "/dev/null":
                    cur.old_path = _strip_prefix(p)
            elif raw.startswith("+++ ") and hunk is None:
                p = raw[4:]
                if p != "/dev/null":
                    cur.new_path = _strip_prefix(p)
            elif raw.startswith("rename to "):
                cur.new_path = raw[len("rename to "):]
            elif raw.startswith("rename from "):
                cur.old_path = raw[len("rename from "):]
        m = HUNK_RE.match(raw)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(3))
            hunk = Hunk(header=raw)
            cur.hunks.append(hunk)
            continue
        if hunk is None or not raw:
            if hunk is not None and raw == "":
                # 空的上下文行（某些工具会去掉行首空格）
                hunk.lines.append(DiffLine(" ", old_no, new_no, ""))
                old_no += 1
                new_no += 1
            continue
        tag = raw[0]
        if tag == "+":
            hunk.lines.append(DiffLine("+", None, new_no, raw[1:]))
            new_no += 1
        elif tag == "-":
            hunk.lines.append(DiffLine("-", old_no, None, raw[1:]))
            old_no += 1
        elif tag == " ":
            hunk.lines.append(DiffLine(" ", old_no, new_no, raw[1:]))
            old_no += 1
            new_no += 1
        # "\ No newline at end of file" 忽略
    return files
