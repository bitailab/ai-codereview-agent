"""文件重要性打分：改动文件超过 max_files 时，决定保留哪些文件进行审查。

分数 = 改动规模（对数，防止大段样板代码压过关键小改动）+ 路径权重 + 新增代码里的风险信号。
测试文件始终排在非测试文件之后（与之前的行为一致）；配置/文档类（light_files）整体降权。"""
from __future__ import annotations

import math
import re

from .diff_parser import FileDiff
from .git_repo import is_ignored

# 路径片段（目录名或文件名里出现）→ 权重。同一路径取命中的最大值
_PATH_WEIGHTS: list[tuple[int, re.Pattern]] = [
    (12, re.compile(r"auth|login|session|token|secret|crypt|passw|credential|permission|rbac|acl|security|signature|oauth|jwt", re.I)),
    (8, re.compile(r"migrat|schema|\bdao\b|repo(?:sitory)?|store|\bdb\b|\bsql\b|database|storage", re.I)),
    (6, re.compile(r"service|biz|handler|server|controller|usecase|worker|schedul|cron|middleware|interceptor|gateway|proxy|client", re.I)),
    (4, re.compile(r"(?:^|/)main\.\w+$|(?:^|/)cmd/", re.I)),
    (-6, re.compile(r"mock|fixture|testdata|example|sample|docs?/|vendor|\.pb\.|_gen\.|generated", re.I)),
]

# 新增行里的风险信号：（权重，正则）。每类最多计一次，总分封顶 _CONTENT_CAP
_CONTENT_SIGNALS: list[tuple[int, re.Pattern]] = [
    (4, re.compile(r"\bgo\s+(?:func|\w+\()|\bsync\.|\.Lock\(|\.RLock\(|\bchan\b|\batomic\.|\bselect\s*\{|threading|asyncio\.")),
    (3, re.compile(r"\.Exec\(|\.Query\w*\(|\.Prepare\(|\bSELECT\b|\bINSERT\b|\bUPDATE\b|\bDELETE\b", re.I)),
    (3, re.compile(r"exec\.Command|os/exec|\bunsafe\.|http\.(?:Get|Post|Client|NewRequest)|os\.(?:Remove|OpenFile|Open)\(|subprocess|eval\(")),
    (2, re.compile(r"\brecover\(|\bpanic\(")),
    (2, re.compile(r"\b_\s*=\s*\w|,\s*_\s*:?=")),  # 忽略返回值/错误
]
_CONTENT_CAP = 10
_SIZE_CAP_LINES = 400
_LIGHT_PENALTY = 8
_NEW_FILE_BONUS = 2


def file_priority(fd: FileDiff, light_files: list[str] | None = None) -> float:
    added = [ln.text for h in fd.hunks for ln in h.lines if ln.kind == "+"]
    score = 2 * math.log2(1 + min(len(added), _SIZE_CAP_LINES))
    path = fd.path
    score += max((w for w, rx in _PATH_WEIGHTS if rx.search(path)), default=0)
    text = "\n".join(added)
    score += min(sum(w for w, rx in _CONTENT_SIGNALS if rx.search(text)), _CONTENT_CAP)
    if fd.new_file:
        score += _NEW_FILE_BONUS
    if light_files and is_ignored(path, light_files):
        score -= _LIGHT_PENALTY
    return score


def rank_files(files: list[FileDiff], test_files: list[str], light_files: list[str]) -> list[FileDiff]:
    """按重要性从高到低排序：非测试文件在前，同类内按分数降序，分数相同按路径（结果稳定）。"""
    return sorted(files, key=lambda f: (is_ignored(f.path, test_files) or "_test." in f.path,
                                        -file_priority(f, light_files), f.path))
