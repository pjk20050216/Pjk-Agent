"""网关中间件 —— JWT 认证、速率限制、LLM 并发控制。

执行顺序（由外到内）：JWTAuth → RateLimit → LLMConcurrency → 代理转发。
顺序由 gateway/main.py 中 add_middleware 的调用次序决定：Starlette 为 LIFO
（后添加者在外层），故那里按「倒序」注册。JWT 必须最外层，限流才能拿到
user_id 做按用户计数；并发信号量最内层，避免被限流拒绝的请求白占名额。
"""
import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from jose import jwt, JWTError

from gateway.config import (
    JWT_SECRET_KEY,
    JWT_ALGORITHM,
    RATE_LIMIT_AUTHENTICATED,
    RATE_LIMIT_UNAUTHENTICATED,
    LLM_CONCURRENCY_LIMIT,
)


# 公开路径 —— 无需 JWT 验证即可访问（精确匹配）
PUBLIC_EXACT = {
    "/", "/harness", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json", "/favicon.ico",
    "/api/auth/register", "/api/auth/login", "/api/auth/refresh",
    "/api/models",
}
# 前缀匹配必须带结尾斜杠：若写成 "/static"，则 "/static-admin/keys" 之类
# 仅以该字符串开头的路径也会被放行，构成潜在认证绕过。
PUBLIC_PREFIXES = ("/static/",)


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """验证 Bearer JWT Token，提取 user_id 存入 request.state。公开路径跳过。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid authentication token"},
            )

        token = auth[7:]
        try:
            payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            return JSONResponse(status_code=401, content={"detail": "Token has expired"})
        except JWTError:
            return JSONResponse(status_code=401, content={"detail": "Invalid token"})

        if payload.get("type") != "access":
            return JSONResponse(status_code=401, content={"detail": "Invalid token type"})

        user_id = payload.get("sub")
        if not user_id:
            return JSONResponse(status_code=401, content={"detail": "Invalid token"})

        request.state.user_id = user_id
        response = await call_next(request)
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """基于 Redis INCR 的固定窗口限流。Redis 不可用时自动降级放行。

    窗口自窗口内「第一个请求」起算，到期整段重置（非滑动窗口）。
    已被拒绝的请求仍会累加计数，因此持续冲击的客户端会一直被挡到窗口结束。

    本中间件位于 JWTAuth 之外，故未通过认证的请求同样受限流保护 ——
    否则伪造 token 或无 token 的洪水请求会直接拿到 401，绕过全部限流。
    由于此时 request.state.user_id 尚未填充，这里自行做一次「验签后」的
    身份提取用于分桶；验签失败一律回退到按 IP 计数，避免攻击者用伪造的
    sub 值把请求摊到无数个桶里。
    """

    def _parse_limit(self, limit_str: str) -> tuple[int, int]:
        parts = limit_str.split("/")
        count = int(parts[0])
        unit = parts[1] if len(parts) > 1 else "minute"
        seconds = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}.get(unit, 60)
        return count, seconds

    @staticmethod
    def _verified_user_id(request: Request) -> str | None:
        """验签通过则返回 token 中的 sub，否则 None（调用方回退按 IP 计数）。"""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        try:
            payload = jwt.decode(auth[7:], JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        except JWTError:
            return None
        except Exception:
            return None
        if payload.get("type") != "access":
            return None
        return payload.get("sub")

    async def dispatch(self, request: Request, call_next):
        user_id = getattr(request.state, "user_id", None) or self._verified_user_id(request)
        if user_id:
            key = f"devflow:rate:user:{user_id}"
            limit_str = RATE_LIMIT_AUTHENTICATED
        else:
            client_ip = request.client.host if request.client else "unknown"
            key = f"devflow:rate:ip:{client_ip}"
            limit_str = RATE_LIMIT_UNAUTHENTICATED

        limit, window = self._parse_limit(limit_str)
        count = await self._increment(key, window)

        if count > limit:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests, please try again later"},
                headers={"Retry-After": str(window)},
            )

        response = await call_next(request)
        return response

    async def _increment(self, key: str, window: int) -> int:
        try:
            from server.utils.redis_client import redis_client
            c = redis_client.client
            if c is None:
                return 1
            pipe = c.pipeline()
            pipe.incr(key)
            pipe.ttl(key)
            results = await pipe.execute()
            count = results[0]
            ttl = results[1]
            if ttl < 0:
                await c.expire(key, window)
            return count
        except Exception:
            return 1


class LLMConcurrencyMiddleware:
    """LLM 相关端点的全局并发控制，用 asyncio.Semaphore 限制最大并发数。

    必须是纯 ASGI 中间件，不能继承 BaseHTTPMiddleware：后者的 call_next()
    在响应头就绪时即返回，而 StreamingResponse 的响应体在其之后才逐块生成。
    因此 `async with sem: await call_next(request)` 会在第一个 token 产出前
    就释放信号量，并发限制形同虚设 —— 实测 max_concurrent=1 时，3 路 SSE
    请求仍在 1.57s 内全部并发跑完（应为 4.5s 串行）。

    纯 ASGI 写法下 await self.app(...) 会一直等到响应体发送完毕，
    信号量得以覆盖整个流式过程。
    """

    # 精确匹配：/api/agent/run（POST 启动 Agent）
    LLM_PATH_EXACT = {"/api/agent/run"}
    # 前缀匹配：/api/chat/{id}、/api/agent/run/{id}/reply
    # 注意不能用 "/api/agent/run" 当前缀，否则 GET /api/agent/runs
    # （历史列表，不调用 LLM）也会占用并发名额。
    LLM_PATH_PREFIXES = ("/api/chat/", "/api/agent/run/")

    def __init__(self, app: ASGIApp, max_concurrent: int = LLM_CONCURRENCY_LIMIT):
        self.app = app
        self._semaphore = asyncio.Semaphore(max_concurrent)

    def _is_llm_path(self, path: str) -> bool:
        return path in self.LLM_PATH_EXACT or path.startswith(self.LLM_PATH_PREFIXES)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._is_llm_path(scope["path"]):
            await self.app(scope, receive, send)
            return

        # 覆盖整个响应生命周期（含 SSE 逐块下发），而非仅请求建立阶段
        async with self._semaphore:
            await self.app(scope, receive, send)
