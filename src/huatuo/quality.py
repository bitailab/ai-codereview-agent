"""审查质量统计：从已关闭的发现推算精确率（报出来的问题有多少是真的）。

口径（保守，只用已有数据，不需要人工标注）：
- 有效：FIXED（开发者修了）、WAIVED/DEFERRED（问题成立，只是放行或延期）
- 误报：WITHDRAWN 且关闭原因是“接受解释，撤回”（开发者或人工认为华佗错了）
- 建议类撤回：WITHDRAWN 且原因是“仍建议调整”（P2，华佗仍认为有问题，只是不阻断）——不计入有效也不计入误报
- 未决：OPEN / DISPUTED / ESCALATED / VERIFYING，不参与精确率

精确率 = 有效 / (有效 + 误报)。它不是严格意义的精确率：开发者可能为了省事修了一个非问题，
也可能被说服了一个真问题，所以要配合抽样人工标注来校准。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

OPEN = {"OPEN", "DISPUTED", "ESCALATED", "VERIFYING"}
VALID = {"FIXED", "WAIVED", "DEFERRED"}


def classify(row: dict) -> str:
    st = row["status"]
    if st in VALID:
        return "valid"
    if st == "WITHDRAWN":
        return "advisory" if (row.get("status_reason") or "").startswith("💬") else "false_positive"
    if st in OPEN:
        return "pending"
    return "other"


def _bucket() -> dict:
    return {"valid": 0, "false_positive": 0, "advisory": 0, "pending": 0, "other": 0}


def _finish(b: dict) -> dict:
    decided = b["valid"] + b["false_positive"]
    return b | {"total": sum(b.values()), "decided": decided,
                "precision": round(b["valid"] / decided, 3) if decided else None}


def _week(ts: str) -> str:
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def quality_stats(rows: list[dict]) -> dict:
    groups: dict[str, dict[str, dict]] = {k: defaultdict(_bucket) for k in ("severity", "category", "project", "week")}
    overall = _bucket()
    for r in rows:
        c = classify(r)
        overall[c] += 1
        groups["severity"][r["severity"]][c] += 1
        groups["category"][r["category"] or "?"][c] += 1
        groups["project"][r["project_path"]][c] += 1
        groups["week"][_week(r["created_at"])][c] += 1
    out = {"overall": _finish(overall)}
    for k, g in groups.items():
        out[k] = [{"key": key} | _finish(b) for key, b in sorted(g.items())]
    return out
