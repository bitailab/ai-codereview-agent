from __future__ import annotations

import hashlib
import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

from ..git_repo import normalize_code

Severity = Literal["P0", "P1", "P2"]
Category = Literal["bug", "concurrency", "performance", "security", "resource", "error_handling", "style"]

# 未关闭（仍需处理）的状态
OPEN_STATES = {"OPEN", "DISPUTED", "ESCALATED", "VERIFYING"}
CLOSED_STATES = {"FIXED", "WITHDRAWN", "WAIVED", "DEFERRED"}


# ---------------- 模型输出结构 ----------------
class LLMFinding(BaseModel):
    file: str = Field(description="文件路径，与 diff 中的路径一致")
    line: int = Field(description="问题所在的新文件行号（diff 中 L 开头的数字）")
    end_line: int | None = Field(default=None, description="问题结束行号，可选")
    severity: Severity = Field(description="P0 阻断 / P1 应修 / P2 建议")
    category: Category
    title: str = Field(description="一句话结论")
    detail: str = Field(description="为什么是问题，会在什么场景下出什么事")
    evidence: str = Field(description="从 diff 中原样复制的 1~3 行问题代码（不带行号和 +/- 前缀）")
    suggestion: str | None = Field(default=None, description="修复建议，可附代码")


class FindingList(BaseModel):
    findings: list[LLMFinding] = Field(default_factory=list)


class VerifyVerdict(BaseModel):
    analysis: str = Field(description="先结合代码逐步推理该问题是否成立（纯文字，不超过 200 字，不要写 JSON）")
    valid: bool = Field(description="问题是否真实成立")
    severity: Severity = Field(description="复核后的严重级别")
    reason: str


class FixCheck(BaseModel):
    analysis: str = Field(description="先对比修改前后的代码，逐步推理问题根因是否已消除（纯文字，不超过 200 字，不要写 JSON）")
    status: Literal["fixed", "not_fixed", "partially_fixed"]
    reason: str
    new_issue: LLMFinding | None = Field(default=None, description="修复引入的新问题，没有则为 null")


class ReplyIntent(BaseModel):
    intent: Literal["dispute", "claim_fixed", "defer", "question", "other"]
    summary: str = Field(description="一句话概括开发者的意思")


class DisputeVerdict(BaseModel):
    analysis: str = Field(description="先逐条检验开发者的理由在代码中是否成立（纯文字，不超过 200 字，不要写 JSON）")
    verdict: Literal["accept", "maintain"] = Field(description="accept=接受开发者解释（原问题不成立）；maintain=坚持问题存在")
    reason: str
    evidence: str | None = Field(default=None, description="支撑结论的代码片段")


class Answer(BaseModel):
    answer: str


# ---------------- 图状态 ----------------
class ReviewState(TypedDict, total=False):
    event: dict                 # {kind, project_path, mr_iid, ...}
    dry_run: bool
    full: bool                  # 忽略增量，全量审查
    mr: dict                    # MR 元数据（精简）
    diff_refs: dict
    head_sha: str
    claude_md: str
    repo_rules: str
    intent: str                 # 本次 MR 的意图摘要
    files: list[dict]           # 待审文件块
    file: dict                  # review_file 扇出时的单个文件块
    lint_issues: list[dict] | None  # 本次 MR 新引入的 lint 问题；None 表示静态分析未执行
    findings: list[dict]        # 当前 MR 全部问题（已有 + 新增），节点整体替换
    raw_findings: Annotated[list[dict], operator.add]   # review_file 扇出结果
    actions: Annotated[list[dict], operator.add]        # 待发布到 GitLab 的动作
    notes: Annotated[list[str], operator.add]           # 需要写进汇总的附加说明
    conclusion: str
    summary: str


def fingerprint(file: str, category: str, evidence: str, title: str) -> str:
    key = f"{file}|{category}|{normalize_code(evidence) or normalize_code(title)}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]
