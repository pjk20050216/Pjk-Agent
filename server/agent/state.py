"""
Agent 状态定义 —— LangGraph 状态图中流转的状态结构。

"""
from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    user_request: str
    document_ids: list[int]
    messages: Annotated[list, add_messages] # 对话历史（贯穿全流程）
    plan: list[dict] # plan_node 工具调用的执行计划
    tool_results: dict # tool_node 产出结果
    missing_info: list[str] # plan_node 产出的待澄清的问题
    final_output: str # 产出最终报告
    status: str  # 状态机状态，用于决定 plan_node 之后走哪条分支
    error: str  # 异常信息
