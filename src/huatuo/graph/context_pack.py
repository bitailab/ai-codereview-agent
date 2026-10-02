"""由代码（而不是模型）预先整理上下文：找出改动中引用到的函数/类型在仓库中的定义。目前支持 Go。"""
from __future__ import annotations

import re

from ..diff_parser import FileDiff
from ..git_repo import Mirror

GO_KEYWORDS = {
    "if", "for", "func", "return", "switch", "select", "go", "defer", "make", "new", "len", "cap", "append",
    "panic", "recover", "copy", "delete", "close", "print", "println", "string", "int", "int64", "int32",
    "uint", "uint64", "float64", "bool", "byte", "rune", "error", "map", "chan", "struct", "interface",
    "range", "type", "var", "const", "nil", "true", "false", "Errorf", "Sprintf", "Printf", "New", "Error",
    "Wrap", "Wrapf", "Is", "As", "Background", "TODO", "WithTimeout", "WithCancel", "Lock", "Unlock",
    "RLock", "RUnlock", "Add", "Done", "Wait", "Get", "Set", "String", "Info", "Infof", "Warn", "Warnf",
    "Debug", "Debugf", "Errorw", "Infow", "Now", "Since", "Duration", "Sleep", "Marshal", "Unmarshal",
}
CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
TYPE_RE = re.compile(r"\b([A-Z]\w{2,})\b")


def _identifiers(fd: FileDiff, limit: int) -> list[str]:
    counts: dict[str, int] = {}
    for h in fd.hunks:
        for ln in h.lines:
            if ln.kind != "+":
                continue
            text = ln.text.split("//", 1)[0]
            for name in CALL_RE.findall(text) + TYPE_RE.findall(text):
                if name not in GO_KEYWORDS and len(name) > 2:
                    counts[name] = counts.get(name, 0) + 1
    return [n for n, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]


def _definition_block(content: str, line_no: int, max_lines: int = 25) -> str:
    lines = content.splitlines()
    out = []
    for i in range(line_no - 1, min(len(lines), line_no - 1 + max_lines)):
        out.append(lines[i])
        if i > line_no - 1 and lines[i].startswith("}"):
            break
    return "\n".join(out)


def build_context_pack(mirror: Mirror, sha: str, fd: FileDiff, max_defs: int = 8, max_chars: int = 6000) -> str:
    if not fd.new_path.endswith(".go"):
        return "（无）"
    blocks: list[str] = []
    total = 0
    for name in _identifiers(fd, limit=20):
        if len(blocks) >= max_defs:
            break
        pattern = rf"^(func (\([^)]*\) )?{name}\(|type {name} )"
        hits = [h for h in mirror.grep(sha, pattern, "*.go", max_results=3) if not h.startswith(fd.new_path + ":")]
        if not hits:
            continue
        path, line_no, _ = hits[0].split(":", 2)
        content = mirror.show(sha, path)
        if not content:
            continue
        block = f"// {path}:{line_no}\n{_definition_block(content, int(line_no))}"
        if total + len(block) > max_chars:
            break
        blocks.append(block)
        total += len(block)
    return "\n\n".join(blocks) if blocks else "（无）"
