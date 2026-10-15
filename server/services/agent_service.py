"""
Agent 服务 —— 执行 LangGraph Agent 状态图，生成 SSE 流式进度事件。
"""
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from fastapi import HTTPException
from server.agent.state import AgentState
from server.agent.graph import build_agent_graph
from server.agent.tool_registry import tool_registry
from server.models.agent_run import AgentRun
from server.security import intent_guard
from server.utils.logger import logger
from server.utils.sse import sse_event, sse_response


async def run_agent_stream(
    db: Session,
    user_id: int,
    request: str,
    document_ids: list[int] | None = None,
    conversation_id: int | None = None,
):
    """流式执行 Agent：创建 AgentRun 记录，运行 LangGraph 状态图，生成 SSE 事件。"""
    doc_ids = document_ids or []

    # 意图识别：命中即拦截（在创建记录之前，避免产生垃圾运行记录）
    verdict = await intent_guard.check(request)
    if verdict.blocked:
        logger.warning(
            f"[IntentGuard] blocked: category={verdict.category} user_id={user_id} "
            f"reason={verdict.reason} content={request[:50]!r}"
        )
        raise HTTPException(status_code=403, detail=f"请求被拒绝：{verdict.reason}")

    # 创建数据库记录（这段代码相当于使用 Spring Boot 中的 @Builder）
    agent_run = AgentRun(
        user_id=user_id,
        conversation_id=conversation_id,
        request=request,
        status="running",
    )

    db.add(agent_run)
    # 程序向数据库发送 Insert 语句
    db.commit()
    # 程序向数据库发送 Select 语句，获取刚刚插入的记录，更新到 agent_run 对象中
    db.refresh(agent_run)

    # 构建初始状态
    initial_state: AgentState = {
        "user_request": request,
        "document_ids": doc_ids,
        "messages": [],
        "plan": [],
        "tool_results": {},
        "missing_info": [],
        "final_output": "",
        "status": "planning",
        "error": "",
    }

    async def event_generator():
        # 没有 nonlocal 声明，Python 就会在内部函数创建一个局部的新变量，导致外部真正的 agent_run 和数据库脱节
        nonlocal agent_run
        try:
            graph = build_agent_graph()
            config = {"configurable":
                          {"thread_id": f"agent_run_{agent_run.id}"}
                      }

            # 流式执行 LangGraph
            step_idx = 0
            async for event in graph.astream(initial_state, config):
                step_idx += 1
                for node_name, node_state in event.items():
                    status = node_state.get("status", "")

                    if node_name == "plan":
                        missing = node_state.get("missing_info", [])
                        if status == "waiting_user":
                            questions = "\n".join([f"- {q}" for q in missing])
                            yield sse_event("asking", {
                                "message": "需要确认以下信息",
                                "questions": missing,
                                "agent_run_id": agent_run.id,
                            })
                            # 更新数据库状态
                            agent_run.status = "waiting_user"
                            agent_run.final_output = questions
                            db.commit()
                            return
                        elif status == "executing":
                            plan = node_state.get("plan", [])
                            yield sse_event("plan", {
                                "status": "executing",
                                "steps": [
                                    {"tool": p.get("tool"), "reason": p.get("reason")}
                                    for p in plan
                                ],
                                "agent_run_id": agent_run.id,
                            })
                            agent_run.plan = {
                                "steps": plan,
                            }
                            db.commit()
                        elif status == "error":
                            yield sse_event("error", {"detail": node_state.get("error", "规划失败")})
                            agent_run.status = "error"
                            db.commit()
                            return

                    elif node_name == "tool":
                        results = node_state.get("tool_results", {})
                        for key, val in results.items():
                            yield sse_event("tool_result", {
                                "step": key,
                                "tool": val.get("tool", ""),
                                "success": val.get("success", False),
                                "summary": _summarize_result(val),
                            })
                        agent_run.tool_results = results
                        db.commit()

                    elif node_name == "observe":
                        yield sse_event("observing", {"message": "检查工具执行结果"})

                    elif node_name == "aggregate":
                        final = node_state.get("final_output", "")
                        yield sse_event("aggregating", {"message": "正在生成汇总报告"})

                    elif node_name == "clarify":
                        yield sse_event("asking", {
                            "message": "需要更多信息",
                            "questions": node_state.get("missing_info", []),
                            "agent_run_id": agent_run.id,
                        })
                        break

            # 获取最终状态
            final_state = graph.get_state(config)
            if final_state and final_state.values:
                status_val = final_state.values.get("status", "")
                if status_val == "done":
                    final_output = final_state.values.get("final_output", "")

                    yield sse_event("final", {
                        "content": final_output,
                        "agent_run_id": agent_run.id,
                    })
                    agent_run.final_output = final_output
                    agent_run.status = "completed"
                elif status_val == "error":
                    error_msg = final_state.values.get("error", "未知错误")
                    yield sse_event("error", {"detail": error_msg})
                    agent_run.status = "error"
                db.commit()

            yield sse_event("done", {"agent_run_id": agent_run.id})
        except Exception as e:
            logger.error(f"Agent run error: {e}")
            agent_run.status = "error"
            agent_run.final_output = f"执行出错: {e}"
            db.commit()
            yield sse_event("error", {"detail": str(e)})

    return sse_response(event_generator())


def _summarize_result(val: dict) -> str:
    """
    生成工具结果的简短摘要。
    """
    if not val.get("success"):
        return val.get("error", "执行失败")
    data = val.get("data", {})
    if isinstance(data, dict):
        if "analysis" in data:
            # 提取前 200 个字符
            return data["analysis"][:200] + "..."
        if "tech_plan" in data:
            return data["tech_plan"][:200] + "..."
        if "tasks" in data:
            return f"生成了 {data.get('count', len(data['tasks']))} 个开发任务"
        if "database_design" in data:
            return data["database_design"][:200] + "..."
        if "results" in data:
            return f"联网检索到 {len(data['results'])} 条结果"
        return str(list(data.keys()))
    if isinstance(data, str):
        return data[:200] + ("..." if len(data) > 200 else "")
    return str(type(data))


def get_agent_runs(db: Session, user_id: int) -> list[AgentRun]:
    """获取用户的历史 Agent 运行记录。"""
    return (
        db.query(AgentRun)
        .filter(AgentRun.user_id == user_id)
        .order_by(AgentRun.created_at.desc())
        .all()
    )


def get_agent_run(db: Session, run_id: int, user_id: int) -> AgentRun | None:
    """
    获取单次 Agent 运行详情。
    """
    return db.query(AgentRun).filter(
        AgentRun.id == run_id,
        AgentRun.user_id == user_id,
    ).first()


def reply_to_agent(db: Session, run_id: int, user_id: int, reply: str):
    """用户回复 Agent 的澄清问题后，用补充信息重新执行 Agent。"""
    agent_run = get_agent_run(db, run_id, user_id)
    if not agent_run:
        return None

    # 将用户回复追加到原始请求，重新执行
    enhanced_request = f"{agent_run.request}\n\n用户补充信息：{reply}"
    return enhanced_request
