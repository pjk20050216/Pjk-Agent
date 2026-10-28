"""风控策略智能运维多智能体系统 API 网关 —— 统一入口（对外端口 8000），负责 JWT 认证、限流、代理转发。"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from gateway.middleware import (
    JWTAuthMiddleware,
    RateLimitMiddleware,
    LLMConcurrencyMiddleware,
)
from gateway.proxy import proxy_request
from gateway.config import LLM_CONCURRENCY_LIMIT

app = FastAPI(
    title="风控策略智能运维多智能体系统网关",
    version="0.2.0",
)

# 自定义中间件。Starlette 的 add_middleware 是 LIFO（后添加者在外层），
# 故此处按「倒序」注册，实际执行顺序为：
#     CORS（最外） → RateLimit → JWTAuth → LLMConcurrency（最内） → 代理转发
# - CORS 必须最外层：否则被内层中间件直接拒绝的 401/429 响应不会经过它，
#   浏览器拿不到 Access-Control-Allow-Origin，只会报笼统的跨域错误，
#   看不到真实状态码；
# - RateLimit 在 JWTAuth 之外：未通过认证的请求同样受限流保护。否则伪造
#   token 的洪水请求会直接拿到 401 而绕过全部限流（该中间件内部会自行验签
#   以按用户分桶，验签失败则回退按 IP 计数）；
# - JWTAuth：拒绝未认证请求，并为下游写入 request.state.user_id；
# - 并发信号量最内层：只覆盖真正会调用 LLM 的端点。
app.add_middleware(LLMConcurrencyMiddleware, max_concurrent=LLM_CONCURRENCY_LIMIT)
app.add_middleware(JWTAuthMiddleware)
app.add_middleware(RateLimitMiddleware)

# CORS：网关层面处理，后端不再重复（后端在 BEHIND_GATEWAY=true 时也会关闭）
# 最后添加 → 位于最外层，确保所有响应（含中间件产生的 401/429）都带上跨域头。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 通配路由必须放在最后，且不注册 "/" 的独立处理器：
# "/{path:path}" 的 path 转换器能匹配空路径，因此 "/" 也由它接管并转发给后端，
# 由后端返回 index.html（单页应用入口）。若在其之前注册 "/" 处理器，
# 该处理器会被通配路由遮蔽而永远不执行。
@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def catch_all(request: Request, path: str):
    return await proxy_request(request)
