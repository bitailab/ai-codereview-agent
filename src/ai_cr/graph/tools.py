"""理解阶段给模型用的只读工具：全部基于 bare mirror 的某个 commit。"""
from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from ..git_repo import Mirror


def numbered(content: str, start: int = 1, end: int | None = None) -> str:
    lines = content.splitlines()
    start = max(start, 1)
    end = min(end or len(lines), len(lines))
    return "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1))


def make_tools(mirror: Mirror, sha: str) -> list[BaseTool]:
    @tool
    def read_file(path: str, start_line: int = 1, end_line: int = 150) -> str:
        """读取仓库中某个文件的指定行范围（带行号），单次最多 300 行。"""
        content = mirror.show(sha, path)
        if content is None:
            return f"文件不存在: {path}"
        end_line = min(end_line, start_line + 299)
        return numbered(content, start_line, end_line) or "(空)"

    @tool
    def grep(pattern: str, path_glob: str = "") -> str:
        """在仓库中按正则（ERE）搜索代码，返回 `路径:行号:内容`，最多 40 条。path_glob 例如 '*.go' 或 'internal/'。"""
        hits = mirror.grep(sha, pattern, path_glob or None)
        return "\n".join(hits) if hits else "无匹配"

    @tool
    def list_dir(path: str = "") -> str:
        """列出仓库某个目录下的文件和子目录。"""
        items = mirror.ls_tree(sha, path.rstrip("/"))
        return "\n".join(items) if items else "目录不存在或为空"

    @tool
    def git_log(path: str) -> str:
        """查看某个文件最近 5 次提交记录。"""
        return mirror.log(sha, path) or "无记录"

    return [read_file, grep, list_dir, git_log]
