"""GitLab 评论内容渲染。所有 bot 发出的内容都带隐藏标记，轮询时据此识别。"""
from __future__ import annotations

from datetime import datetime

from ..gitlab_client import BOT_MARKER, SUMMARY_MARKER, finding_marker
from .gate import is_blocking

SEV_ICON = {"P0": "🔴 P0", "P1": "🟠 P1", "P2": "🔵 P2"}
CAT_NAME = {
    "bug": "逻辑缺陷", "concurrency": "并发", "performance": "性能", "security": "安全",
    "resource": "资源泄漏", "error_handling": "错误处理", "style": "风格", "lint": "静态检查",
}
STATUS_NAME = {
    "OPEN": "待修复", "DISPUTED": "复核中", "ESCALATED": "待人工裁决", "VERIFYING": "验证中",
    "FIXED": "✅ 已修复", "WITHDRAWN": "已撤回", "WAIVED": "人工放行", "DEFERRED": "延期处理",
}
CONCLUSION_TEXT = {
    "REQUEST_CHANGES": "🟥 REQUEST_CHANGES（存在阻断问题）",
    "COMMENT": "🟨 COMMENT（无阻断问题，仍有建议或待裁决项）",
    "APPROVE": "🟩 APPROVE",
}
LANG = {".go": "go", ".py": "python", ".java": "java", ".ts": "typescript", ".js": "javascript",
        ".rs": "rust", ".sql": "sql", ".yaml": "yaml", ".yml": "yaml", ".sh": "bash"}


def lang_of(path: str) -> str:
    for ext, lang in LANG.items():
        if path.endswith(ext):
            return lang
    return ""


def finding_body(f: dict) -> str:
    loc = "" if f.get("inline", True) else f"`{f['file']}:{f['line']}`\n\n"
    parts = [
        finding_marker(f["fingerprint"]),
        f"{loc}**{SEV_ICON[f['severity']]} · {CAT_NAME.get(f['category'], f['category'])}** — {f['title']}",
        "",
        f["detail"],
    ]
    if f.get("evidence"):
        parts += ["", "**问题代码**", f"```{lang_of(f['file'])}", f["evidence"].strip("\n"), "```"]
    if f.get("suggestion"):
        parts += ["", "**建议**", "", f["suggestion"]]
    parts += ["", "<sub>🤖 AI Code Review · 认为是误报请直接回复理由；修复后 push 即可自动验证，也可回复“已修复”。</sub>"]
    return "\n".join(parts)


def reply_body(fingerprint: str, text: str) -> str:
    return f"{finding_marker(fingerprint)}\n{text}"


def summary_body(*, head_sha: str, conclusion: str, intent: str, findings: list[dict], notes: list[str],
                 human: str) -> str:
    live = [f for f in findings if f.get("status") != "NEW"]
    order = {"P0": 0, "P1": 1, "P2": 2}
    live.sort(key=lambda f: (f["status"] not in ("OPEN", "DISPUTED", "ESCALATED", "VERIFYING"),
                             order[f["severity"]], f["file"], f.get("line") or 0))
    lines = [
        SUMMARY_MARKER,
        BOT_MARKER,
        "### 🤖 AI Code Review",
        "",
        f"- **当前版本**：`{head_sha[:8]}`",
        f"- **结论**：{CONCLUSION_TEXT[conclusion]}",
        f"- **更新时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if intent:
        lines.append(f"- **改动意图**：{intent}")
    counts = {}
    for f in live:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    if live:
        lines += ["", "| 级别 | 问题 | 位置 | 状态 | 阻断 |", "|---|---|---|---|---|"]
        for f in live:
            title = f["title"].replace("|", "\\|")
            lines.append(
                f"| {SEV_ICON[f['severity']]} | {title} | `{f['file']}:{f.get('line') or '-'}` "
                f"| {STATUS_NAME.get(f['status'], f['status'])} | {'是' if is_blocking(f) else ''} |"
            )
    else:
        lines += ["", "未发现问题。"]
    if notes:
        lines += ["", *[f"> {n}" for n in notes]]
    lines += [
        "",
        "<details><summary>规则说明</summary>",
        "",
        "- P0 未关闭 → 阻断；P1 未修复且无解释 → 阻断；P2 仅建议，不阻断。所有问题关闭后 AI 才会 approve。",
        "- 开发者认为误报：在对应讨论下回复理由，AI 会复核；AI 仍坚持的 P0/P1 会升级给人工裁决。",
        f"- 人工裁决（仅 @{human}）：`/ai-confirm` 问题成立、`/ai-accept` 放行、`/ai-downgrade P2` 调整级别；在 MR 下评论 `/ai-review` 触发全量重审。",
        "",
        "</details>",
    ]
    return "\n".join(lines)


def pipeline_notice_body(head_sha: str, pipeline: dict, previous_summary: str | None,
                         failed_checks: list[dict] | None = None) -> str:
    """流水线失败时的汇总评论：顶部提示 + 保留上一轮的审查结果。"""
    lines = [
        SUMMARY_MARKER,
        BOT_MARKER,
        "### 🤖 AI Code Review",
        "",
        f"⏸️ **等待流水线通过**：当前版本 `{head_sha[:8]}` 的流水线 "
        f"[#{pipeline.get('id')}]({pipeline.get('web_url', '')}) 状态为 **{pipeline.get('status')}**。",
        "",
    ]
    if failed_checks:
        lines += ["", "失败的检查项："]
        for c in failed_checks[:10]:
            desc = f" — {c['description']}" if c.get("description") else ""
            link = f"[{c['name']}]({c['url']})" if c.get("url") else f"`{c['name']}`"
            lines.append(f"- ❌ {link}{desc}")
    failed = pipeline.get("status") in ("failed", "canceled")
    lines += [
        "",
        ("请先修复单测 / 集测 / lint 问题，流水线通过后会自动开始 AI 代码审查。" if failed
         else "流水线正在重新运行，通过后会自动开始 AI 代码审查。")
        + "如需跳过等待，可在 MR 下评论 `/ai-review`。",
    ]
    if previous_summary:
        prev = previous_summary.replace(SUMMARY_MARKER, "").replace(BOT_MARKER, "").strip()
        prev = prev.replace("### 🤖 AI Code Review", "").strip()
        lines += ["", "---", "", "<details><summary>上一轮审查结果</summary>", "", prev, "", "</details>"]
    return "\n".join(lines)
