"""流水线门禁：MR 当前 head 的流水线（单测 / 集测 / lint）通过后才开始 CR。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .settings import PipelineConfig

READY, WAIT, FAILED = "ready", "wait", "failed"


@dataclass
class GateResult:
    decision: str      # ready | wait | failed
    reason: str
    pipeline: dict | None = None


def _age_seconds(ts: str | None) -> float:
    if not ts:
        return 0.0
    return (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()


def check_pipeline(mr: dict, head_sha: str, cfg: PipelineConfig) -> GateResult:
    """mr 为单个 MR 接口返回的 attributes（含 head_pipeline）。"""
    if not cfg.enabled:
        return GateResult(READY, "未启用流水线门禁")
    p = mr.get("head_pipeline")
    if not p or p.get("sha") != head_sha:
        # push 后流水线可能还没创建出来，给一段宽限期；超过宽限期仍没有，视为该仓库/分支没有 CI
        since = mr.get("updated_at")
        if _age_seconds(since) < cfg.create_grace_seconds:
            return GateResult(WAIT, "等待流水线创建")
        return GateResult(READY, "没有流水线，直接审查")
    status = p.get("status")
    if status in cfg.pass_statuses:
        return GateResult(READY, f"流水线 #{p['id']} 已通过", p)
    if status in cfg.fail_statuses:
        return GateResult(FAILED, f"流水线 #{p['id']} 状态为 {status}", p)
    if _age_seconds(p.get("created_at")) > cfg.max_wait_minutes * 60:
        return GateResult(READY, f"流水线 #{p['id']} 超过 {cfg.max_wait_minutes} 分钟仍为 {status}，不再等待", p)
    return GateResult(WAIT, f"流水线 #{p['id']} 状态为 {status}", p)
