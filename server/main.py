"""
FastAPI 应用主模块 —— 创建应用实例，注册中间件和路由，挂载静态文件，配置启动/关闭生命周期事件。
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from server.config import settings, PROJECT_ROOT
from server.database import init_db
from server.utils.logger import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库表，注册 Agent 工具，初始化 Redis 缓存。"""
    init_db()
    logger.info("Database initialized")

    # 注册 Agent 工具
    from server.agent.tool_registry import tool_registry
    from server.agent.tools import (
        DocumentReader, TechPlanGenerator, TaskDecomposer, DatabaseDesigner, WebSearchTool,
    )
    tool_registry.register(DocumentReader())
    tool_registry.register(TechPlanGenerator())
    tool_registry.register(TaskDecomposer())
    tool_registry.register(DatabaseDesigner())
    tool_registry.register(WebSearchTool())
    logger.info(f"Agent tools registered: {len(tool_registry.list_all())}")

    # 初始化 Redis + LangChain Embedding 缓存
    try:
        from server.utils.redis_client import redis_client
        from server.config import settings

        if redis_client.client:
            from langchain_community.cache import AsyncRedisCache
            from langchain_core.globals import set_llm_cache

            if redis_client.client_raw:
                set_llm_cache(AsyncRedisCache(
                    redis_=redis_client.client_raw,
                    ttl=settings.redis_ttl_embedding,
                ))
                logger.info(f"LangChain AsyncRedisCache enabled (TTL={settings.redis_ttl_embedding}s)")
        else:
            logger.warning("Redis not available, caching disabled")
    except Exception as e:
        logger.warning(f"Failed to initialize Redis cache: {e}")

    # 探测外部检索依赖（ES 全文 / 博查联网），失败仅告警、非致命
    try:
        from server.utils.elasticsearch_client import es_client
        from server.utils.web_search import web_search_client
        if es_client.client is None:
            logger.warning("Elasticsearch unavailable at startup, full-text retrieval degraded")
        if not web_search_client.available:
            logger.warning("Web search unavailable at startup (disabled or missing key)")
    except Exception as e:
        logger.warning(f"Failed to probe external retrieval services: {e}")

    # 初始化 Agent Harness 运行时（不阻塞：外部依赖均懒连接 + 降级）
    try:
        from harness.runtime import get_harness

        agent_harness = get_harness()
        logger.info(
            f"Harness runtime ready: {len(agent_harness.skills)} skills, "
            f"parallel={agent_harness.config.max_parallel_subagents}, "
            f"workspace={agent_harness.config.workspace_root}"
        )
    except Exception as e:
        logger.warning(f"Failed to initialize harness runtime: {e}")

    # yield 之前的代码会在应用启动前执行，之后的代码会在应用关闭时执行
    yield

    # Shutdown: 关闭 Harness 后台任务 / 沙箱 / Java 桥
    try:
        from harness.runtime import get_harness

        await get_harness().shutdown()
    except Exception:
        pass

    # Shutdown: 关闭 Redis / ES / 联网客户端连接
    try:
        from server.utils.redis_client import redis_client
        await redis_client.close()
    except Exception:
        pass

    try:
        from server.utils.elasticsearch_client import es_client
        from server.utils.web_search import web_search_client
        es_client.close()
        await web_search_client.close()
    except Exception:
        pass


# FastAPI 应用实例
app = FastAPI(
    title="风控策略智能运维多智能体系统",
    version="0.2.0",
    lifespan=lifespan,
    docs_url=None if settings.behind_gateway else "/docs",
    redoc_url=None,
)

# CORS：仅在未使用网关时启用（网关已处理 CORS）
if not settings.behind_gateway:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# 注册各业务模块的 API 路由
from server.routers.auth import router as auth_router
from server.routers.conversations import router as conversations_router
from server.routers.chat import router as chat_router
from server.routers.documents import router as documents_router
from server.routers.models import router as models_router
from server.routers.agent import router as agent_router
from harness.api.router import router as harness_router
app.include_router(auth_router)
app.include_router(conversations_router)
app.include_router(chat_router)
app.include_router(documents_router)
app.include_router(models_router)
app.include_router(agent_router)
app.include_router(harness_router)

# 挂载前端静态文件目录（CSS、JS、第三方库）
static_dir = PROJECT_ROOT / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def root():
    """前端入口 —— 返回 index.html 单页面应用。"""
    from fastapi.responses import FileResponse, HTMLResponse
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return HTMLResponse("<h1>风控策略智能运维多智能体系统</h1><p>Server is running. Visit <a href='/docs'>/docs</a> for API documentation.</p>")


@app.get("/harness")
async def harness_console():
    """Harness 控制台 —— 任务板、事件流、审批、文件、技能与追踪。"""
    from fastapi.responses import FileResponse, HTMLResponse
    page = static_dir / "harness.html"
    if page.exists():
        return FileResponse(str(page))
    return HTMLResponse("<h1>Harness Console</h1><p>static/harness.html not found.</p>")
