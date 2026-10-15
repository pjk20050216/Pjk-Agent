"""
LangGraph 状态图 —— 定义 Agent 的 5 个执行节点及状态转换逻辑。

实例：
1、用户说：“根据上传的文档，给我生成技术方案、任务列表和数据库设计”

2、Agent 会自动：
判断是否信息足够
规划要调用哪些工具（检索文档 / 分析需求 / 生成方案）
按顺序执行这些工具
把所有结果整理成一份专业 Markdown 技术文档
"""
import json
import hashlib
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from server.agent.state import AgentState
from server.agent.tool_registry import tool_registry
from server.agent.tool_result import ToolResult
from server.services.llm_service import llm_service
from server.utils.redis_client import redis_client
from server.config import settings
from server.utils.logger import logger

TOOL_CACHE_PREFIX = "devflow:tool:"

PLANNING_SYSTEM_PROMPT = """你是一个智能 Agent 的任务规划器。根据用户请求，决定需要调用哪些工具以及调用顺序。

可用工具：
{tool_descriptions}

请分析用户请求，输出 JSON 格式的执行计划：
```json
{{
  "can_fulfill": true,
  "reasoning": "分析思路",
  "tool_calls": [
    {{"tool": "tool_name", "args": {{"param": "value"}}, "reason": "为什么调用这个工具"}}
  ],
  "missing_info": []
}}
```

规则：
- 如果用户请求明确（如"根据文档生成技术方案"），设置 can_fulfill=true，规划工具调用顺序
- 如果请求模糊、缺少必要信息，设置 can_fulfill=false，在 missing_info 中列出需要用户补充的问题
- 工具调用的 args 必须符合工具定义的参数 schema
- 工具结果会在后续步骤中自动传入下一个工具"""




# 解析 LLM 返回的 JSON 内容，也就是 LLM 要实施的计划
def _load_guard_rail() -> str:
    """加载 prompts/guard_rail.txt 作为安全护栏，追加到规划系统提示词末尾。"""
    from pathlib import Path
    guard_file = Path(__file__).parent.parent.parent / "prompts" / "guard_rail.txt"
    if guard_file.exists():
        return guard_file.read_text(encoding="utf-8").strip()
    return ""


def _parse_json_response(content: str) -> dict | None:

    if not content:
        return None
    try:
        # 因为 LLM 会由于用户的提问而回答的内容格式不一样，所以得尝试各种方式的提取
        if "```json" in content:
            json_str = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            json_str = content.split("```")[1].split("```")[0].strip()
        elif "{" in content:
            start = content.index("{")
            end = content.rindex("}") + 1
            json_str = content[start:end]
        else:
            return None
        return json.loads(json_str)
    except (json.JSONDecodeError, ValueError, IndexError) as e:
        logger.warning(f"Failed to parse JSON response: {e}")
        return None


def plan_node(state: AgentState) -> AgentState:
    """规划节点：分析用户请求，决定工具调用计划或生成反问问题。"""
    logger.info("[Agent] Planning...")
    request = state["user_request"]
    document_ids = state.get("document_ids", [])

    tools_desc = "\n".join(
        # 从工具的注册列表中取出所有工具的 name + description
        [f"- {t.name}: {t.description}" for t in tool_registry.list_all()]
    )
    system_prompt = PLANNING_SYSTEM_PROMPT.format(tool_descriptions=tools_desc) + "\n\n" + _load_guard_rail()

    doc_context = ""
    if document_ids:
        doc_context = f"\n用户关联了文档 ID：{document_ids}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"用户请求：{request}{doc_context}\n\n请生成执行计划。"},
    ]

    try:
        result = llm_service.chat_with_tools_sync(
            messages=messages,
            temperature=0.3,
            max_tokens=2048,
        )
        content = result.get("content", "")
        plan = _parse_json_response(content)

        if plan and plan.get("can_fulfill"):
            state["plan"] = plan.get("tool_calls", [])
            state["missing_info"] = []
            state["status"] = "executing"
            logger.info(f"[Agent] Plan: {len(state['plan'])} tool(s) planned")
        else:
            state["plan"] = []
            state["missing_info"] = plan.get("missing_info", []) if plan else ["请提供更多关于您的需求的信息"]
            state["status"] = "waiting_user"
            logger.info(f"[Agent] Missing info, need user input")

        state["messages"] = [{"role": "assistant", "content": content}]
        state["error"] = ""
    except Exception as e:
        logger.error(f"[Agent] Plan error: {e}")
        state["status"] = "error"
        state["error"] = str(e)

    return state


def tool_node(state: AgentState) -> AgentState:
    """
    工具节点：按计划依次执行工具调用，收集结果。
    """
    logger.info("[Agent] Executing tools...")
    tool_plan = state.get("plan", [])
    tool_results = dict(state.get("tool_results", {}))

    for i, call in enumerate(tool_plan):
        tool_name = call.get("tool", "")
        tool = tool_registry.get(tool_name)
        if not tool:
            tool_results[f"step_{i}_{tool_name}"] = {"error": f"Tool not found: {tool_name}"}
            continue

        try:
            args = call.get("args", {})
            result = _run_tool_sync(tool, args)
            tool_results[f"step_{i}_{tool_name}"] = {
                "tool": tool_name,
                "success": result.success,
                "data": result.data,
                "error": result.error,
            }
            logger.info(f"[Agent] Tool {tool_name}: {'OK' if result.success else 'ERROR'}")
        except Exception as e:
            logger.error(f"[Agent] Tool {tool_name} exception: {e}")
            tool_results[f"step_{i}_{tool_name}"] = {
                "tool": tool_name,
                "success": False,
                "data": None,
                "error": str(e),
            }

    state["tool_results"] = tool_results
    state["status"] = "observing"
    state["messages"] = state.get("messages", []) + [
        {"role": "system", "content": f"工具执行完成，共 {len(tool_plan)} 个工具"}
    ]
    return state


def observe_node(state: AgentState) -> AgentState:
    """观察节点：检查工具执行结果，决定下一步（继续执行/反问用户/生成最终输出）。"""
    logger.info("[Agent] Observing results...")
    tool_results = state.get("tool_results", {})

    # 检查是否有工具失败
    failures = [
        f"{v.get('tool', 'unknown')}: {v.get('error', '')}"
        for v in tool_results.values()
        if not v.get("success")
    ]
    if failures:
        state["status"] = "aggregating"
        state["messages"] = state.get("messages", []) + [
            {"role": "system", "content": f"部分工具执行失败: {failures}"}
        ]
    else:
        state["status"] = "aggregating"

    return state


def aggregate_node(state: AgentState) -> AgentState:
    """汇总节点：将所有工具结果整合，生成结构化的最终输出。"""
    logger.info("[Agent] Aggregating final output...")

    tool_results = state.get("tool_results", {})
    if not tool_results:
        state["final_output"] = "未生成任何结果，请检查工具执行是否正常。"
        state["status"] = "error"
        return state

    # 收集所有工具结果
    results_text_parts = []
    for key, val in tool_results.items():
        if val.get("success") and val.get("data"):
            data = val["data"]
            if isinstance(data, dict):
                results_text_parts.append(
                    f"### {val.get('tool', key)}\n\n{json.dumps(data, ensure_ascii=False, indent=2)}"
                )
            else:
                results_text_parts.append(f"### {val.get('tool', key)}\n\n{data}")

    results_text = "\n\n---\n\n".join(results_text_parts)

    # 让 LLM 生成最终汇总
    summary_prompt = f"""请根据以下工具执行结果，生成一份完整的汇总报告。

用户原始请求：{state['user_request']}

工具执行结果：
{results_text}

请将所有结果整合为一份专业的技术文档，包含：
1. 需求分析摘要
2. 技术方案概览
3. 开发任务列表
4. 数据库设计

使用结构化的 Markdown 格式输出，确保内容完整、专业、可落地。"""

    try:
        messages = [
            {"role": "system", "content": "你是一个资深技术顾问，擅长将分散的分析结果整合为完整的技术文档。"},
            {"role": "user", "content": summary_prompt},
        ]
        final = llm_service.chat_sync(messages, temperature=0.4, max_tokens=8192)
        state["final_output"] = final
        state["status"] = "done"
        logger.info("[Agent] Final output generated")
    except Exception as e:
        logger.error(f"[Agent] Aggregate error: {e}")
        state["final_output"] = results_text
        state["status"] = "done"

    return state


def clarify_node(state: AgentState) -> AgentState:
    """澄清节点：向用户提出需要补充的信息。"""
    missing = state.get("missing_info", [])
    questions = "\n".join([f"- {q}" for q in missing])
    state["final_output"] = f"在开始之前，我需要确认以下信息：\n\n{questions}"
    state["status"] = "waiting_user"
    return state


# 下面代码是利用缓存预防重复调用

def _tool_cache_key(tool_name: str, args: dict) -> str:
    payload = json.dumps({"name": tool_name, "args": args}, sort_keys=True, ensure_ascii=False)
    return TOOL_CACHE_PREFIX + hashlib.md5(payload.encode("utf-8")).hexdigest()

# 在真正的工具执行前，会调用这个函数，在缓存中看工具是否已经执行过
def _get_cached_tool_result(tool_name: str, args: dict):
    key = _tool_cache_key(tool_name, args)
    try:
        cached = redis_client.get_sync(key)
        if cached:
            data = json.loads(cached)
            return ToolResult(**data)
    except Exception:
        pass
    return None

# 负责将工具的执行的结果写入缓存
def _set_cached_tool_result(tool_name: str, args: dict, result: ToolResult) -> None:
    key = _tool_cache_key(tool_name, args)
    try:
        redis_client.set_sync(key, result.model_dump_json(), ttl=settings.redis_ttl_tool)
    except Exception:
        pass

'''
在同步代码环境中调用异步工具，并阻塞等待结果返回
 这段代码既操作工具，也操作缓存
'''
def _run_tool_sync(tool, args: dict):
    """
    异步线程：就是单线程中执行多个任务，例如：厨师边烧水，边切菜
    """
    # 检查工具缓存
    cached = _get_cached_tool_result(tool.name, args)
    if cached is not None:
        logger.info(f"[Agent] Tool cache HIT: {tool.name}")
        return cached

    import asyncio
    try:
        # 获取当前线程是否有正在执行的事件
        loop = asyncio.get_event_loop()
        # 判断当前是否处于异步线程中
        if loop.is_running():
            import concurrent.futures
            # 开启一个全新的操作系统线程
            with concurrent.futures.ThreadPoolExecutor() as executor:
                # 在新线程中执行
                future = executor.submit(asyncio.run, tool.execute(**args))
                # 让当前同步代码阻塞在这里，等待新线程里的异步工具执行完毕并返回结果，最多等 120 秒，超时则抛出异常
                result = future.result(timeout=120)
        else:
            result = asyncio.run(tool.execute(**args))
    except RuntimeError:
        result = asyncio.run(tool.execute(**args))

    # 缓存成功的工具结果
    if result.success:
        _set_cached_tool_result(tool.name, args, result)

    return result


def build_agent_graph():
    """
    画给 LangGraph 的执行引擎用的“机器可读路线图”，
    给 LangGraph 编译出来的“状态机对象”使用的，
    而这个对象最终是给你的业务代码（如 API 接口）调用的。

    """
    workflow = StateGraph(AgentState)

    workflow.add_node("plan", plan_node)
    workflow.add_node("tool", tool_node)
    workflow.add_node("observe", observe_node)
    workflow.add_node("aggregate", aggregate_node)
    workflow.add_node("clarify", clarify_node)

    workflow.set_entry_point("plan")

    workflow.add_conditional_edges(
        "plan",
        lambda s: s["status"],
        {
            "executing": "tool",
            "waiting_user": "clarify",
            "error": END,
        },
    )
    workflow.add_edge("tool", "observe")
    workflow.add_edge("observe", "aggregate")
    workflow.add_edge("aggregate", END)
    workflow.add_edge("clarify", END)

    memory = MemorySaver()
    return workflow.compile(checkpointer=memory)
