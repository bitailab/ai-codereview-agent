from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..deps import Deps
from .nodes import Nodes, route_event
from .state import ReviewState

FANOUT_KEYS = ("event", "mr", "diff_refs", "head_sha", "claude_md", "repo_rules", "intent", "findings")


def build_graph(deps: Deps, checkpointer=None):
    n = Nodes(deps)
    g = StateGraph(ReviewState)
    g.add_node("load_context", n.load_context)
    g.add_node("plan_review", n.plan_review)
    g.add_node("review_file", n.review_file)
    g.add_node("aggregate", n.aggregate)
    g.add_node("verify", n.verify)
    g.add_node("verify_fixes", n.verify_fixes)
    g.add_node("handle_reply", n.handle_reply)
    g.add_node("human_command", n.human_command)
    g.add_node("gate", n.gate)
    g.add_node("publish", n.publish)

    g.add_edge(START, "load_context")
    g.add_conditional_edges("load_context", route_event, {
        "plan_review": "plan_review", "handle_reply": "handle_reply",
        "human_command": "human_command", "skip": END,
    })

    def fan_out(state: ReviewState):
        if not state.get("files"):
            return "aggregate"
        base = {k: state.get(k) for k in FANOUT_KEYS}
        return [Send("review_file", base | {"file": f}) for f in state["files"]]

    # 先验证已有问题是否修复，再审查新代码：审查时“已报告的问题”是最新状态，
    # 复核新问题时也能看到同一文件刚判定修复的结论，避免把刚修好的问题换个说法重报
    g.add_edge("plan_review", "verify_fixes")
    g.add_conditional_edges("verify_fixes", fan_out, ["review_file", "aggregate"])
    g.add_edge("review_file", "aggregate")
    g.add_edge("aggregate", "verify")
    g.add_edge("verify", "gate")
    g.add_edge("handle_reply", "gate")
    g.add_edge("human_command", "gate")
    g.add_edge("gate", "publish")
    g.add_edge("publish", END)
    return g.compile(checkpointer=checkpointer)
