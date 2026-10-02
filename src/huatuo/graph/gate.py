"""合并门禁：纯规则，不经过模型。"""
from __future__ import annotations

from .state import OPEN_STATES

# P0：只要未关闭（含已升级给人工）就阻断
# P1：未修复且没有给出解释才阻断；开发者有异议、已升级给人工时放行（等待人工裁决，人工 /ai-confirm 后重新阻断）
P1_BLOCKING = {"OPEN", "VERIFYING"}


def is_blocking(f: dict) -> bool:
    if f["severity"] == "P0":
        return f["status"] in OPEN_STATES
    if f["severity"] == "P1":
        return f["status"] in P1_BLOCKING
    return False


def compute_conclusion(findings: list[dict]) -> str:
    live = [f for f in findings if f.get("status") != "NEW"]
    if any(is_blocking(f) for f in live):
        return "REQUEST_CHANGES"
    if any(f["status"] in OPEN_STATES for f in live):
        return "COMMENT"
    return "APPROVE"
