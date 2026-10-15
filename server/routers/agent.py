"""Agent 路由 —— 提供 Agent 执行、历史查询、工具列表等 API 端点。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from server.database import get_db
from server.dependencies import get_current_user
from server.models.user import User
from server.schemas.agent import (
    AgentRunRequest, AgentReplyRequest,
    AgentRunResponse, AgentRunList, AgentToolList, AgentToolInfo,
)
from server.services import agent_service
from server.agent.tool_registry import tool_registry
from server.config import settings
from server.utils.cache import get_cached_route, set_cached_route

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/run")
async def run_agent(
    req: AgentRunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """POST /api/agent/run —— 启动 Agent 执行，返回 SSE 流式进度事件。"""
    # 验证文档归属于当前用户（如果提供了文档 ID）
    if req.document_ids:
        from server.models.document import Document
        for doc_id in req.document_ids:
            doc = db.query(Document).filter(
                Document.id == doc_id,
                Document.user_id == current_user.id,
            ).first()
            if not doc:
                raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    return await agent_service.run_agent_stream(
        db=db,
        user_id=current_user.id,
        request=req.request,
        document_ids=req.document_ids,
        conversation_id=req.conversation_id,
    )


@router.post("/run/{run_id}/reply")
async def reply_to_agent(
    run_id: int,
    req: AgentReplyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """POST /api/agent/run/{id}/reply —— 回复 Agent 的澄清问题，触发重新执行。"""
    result = agent_service.reply_to_agent(db, run_id, current_user.id, req.reply)
    if result is None:
        raise HTTPException(status_code=404, detail="Agent run not found")

    # 用增强后的请求重新执行
    return await agent_service.run_agent_stream(
        db=db,
        user_id=current_user.id,
        request=result,
        document_ids=None,
        conversation_id=None,
    )


@router.get("/runs", response_model=AgentRunList)
def list_agent_runs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """GET /api/agent/runs —— 获取当前用户的历史 Agent 运行记录。"""
    cache_key = f"devflow:route:agent_runs:{current_user.id}"
    cached = get_cached_route(cache_key)
    if cached:
        return cached

    runs = agent_service.get_agent_runs(db, current_user.id)
    result = AgentRunList(
        runs=[AgentRunResponse.model_validate(r) for r in runs],
        total=len(runs),
    )
    set_cached_route(cache_key, result.model_dump(), settings.redis_ttl_route_user)
    return result


@router.get("/runs/{run_id}", response_model=AgentRunResponse)
def get_agent_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """GET /api/agent/runs/{id} —— 获取某次 Agent 运行的详情和结果。"""
    cache_key = f"devflow:route:agent_run:{run_id}:{current_user.id}"
    cached = get_cached_route(cache_key)
    if cached:
        return cached

    run = agent_service.get_agent_run(db, run_id, current_user.id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    result = AgentRunResponse.model_validate(run)
    set_cached_route(cache_key, result.model_dump(), settings.redis_ttl_route_user)
    return result


@router.get("/tools", response_model=AgentToolList)
def list_tools(
    current_user: User = Depends(get_current_user),
):
    """GET /api/agent/tools —— 列出所有可用工具及描述。"""
    cache_key = "devflow:route:agent_tools"
    cached = get_cached_route(cache_key)
    if cached:
        return cached

    tools = [
        AgentToolInfo(
            name=t.name,
            description=t.description,
            parameters=t.parameters_schema,
        )
        for t in tool_registry.list_all()
    ]
    result = AgentToolList(tools=tools)
    set_cached_route(cache_key, result.model_dump(), settings.redis_ttl_route_static)
    return result
