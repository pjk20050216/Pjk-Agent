"""异步代理处理器 —— httpx 转发请求到后端，完整支持 SSE 流式透传。"""
import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from gateway.config import BACKEND_URL, INTERNAL_TOKEN

CLIENT_TIMEOUT = httpx.Timeout(300.0, connect=10.0)

HOP_BY_HOP = {
    "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization",
    "proxy-authenticate",
}

# 客户端身份由网关独占写入，绝不能沿用调用方自带的同名头。
# 后端用 Headers.get() 取「第一个」匹配值，若在此追加而非剔除，
# 客户端只要自带 X-User-ID 就能覆盖真实身份，实现任意越权。
IDENTITY_HEADERS = {"x-user-id", "x-internal-token"}

# CORS 响应头由网关的 CORSMiddleware 统一产生。
# 若后端也启用了 CORS（BEHIND_GATEWAY=false 时），原样透传会让浏览器
# 收到重复的 Access-Control-Allow-Origin 并拒绝该响应，故在此剔除。
CORS_RESPONSE_HEADERS = {
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-expose-headers",
    "access-control-max-age",
}


def _client_ip(request: Request) -> str:
    """取真实客户端 IP：优先 X-Forwarded-For 首个地址，否则用直连对端。"""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


async def proxy_request(request: Request) -> Response:
    """将请求转发到后端，保留原始 method、headers、body。SSE 响应逐字节流式透传。"""
    method = request.method
    body = await request.body()

    fwd_headers = []
    for key, value in request.headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP or lower in IDENTITY_HEADERS:
            continue
        fwd_headers.append((key, value))

    # 身份头一律由网关重建，调用方传入的同名头已在上面剔除
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        fwd_headers.append(("x-user-id", str(user_id)))
    # 内部令牌：证明该请求确实经过网关（后端据此才信任 X-User-ID）
    fwd_headers.append(("x-internal-token", INTERNAL_TOKEN))

    # 传递真实客户端 IP，供后端日志/风控使用；X-Forwarded-Proto 便于后端
    # 判断原始协议（如生成绝对 URL）。
    client_ip = _client_ip(request)
    prior_xff = request.headers.get("X-Forwarded-For", "")
    fwd_headers.append(
        ("x-forwarded-for", f"{prior_xff}, {client_ip}" if prior_xff else client_ip)
    )
    fwd_headers.append(("x-forwarded-proto", request.url.scheme))

    path = request.path_params.get("path", "")
    url = f"{BACKEND_URL}/{path}"
    if request.url.query:
        url += f"?{request.url.query.decode('utf-8')}"

    # 注意：这里不能用 `async with httpx.AsyncClient(...)`。
    # 该上下文管理器会在 return 语句处立即退出并关闭连接池，而 StreamingResponse
    # 的生成器要等到 Starlette 迭代时才真正读取后端响应 —— 届时连接已断，
    # 客户端收到 0 字节（实测报 RemoteProtocolError: peer closed connection）。
    # 因此客户端必须活到整个响应体发送完毕，由下方各分支自行收尾。
    client = httpx.AsyncClient(timeout=CLIENT_TIMEOUT)
    req = client.build_request(
        method=method,
        url=url,
        headers=fwd_headers,
        content=body,
    )
    try:
        backend_resp = await client.send(req, stream=True)
    except httpx.ConnectError:
        # 后端未启动 / 拒绝连接：给出明确的 502 而非 500 堆栈
        await client.aclose()
        return JSONResponse(
            status_code=502,
            content={"detail": "Bad Gateway: backend service is unreachable"},
        )
    except httpx.TimeoutException:
        await client.aclose()
        return JSONResponse(
            status_code=504,
            content={"detail": "Gateway Timeout: backend did not respond in time"},
        )

    content_type = backend_resp.headers.get("content-type", "")

    if "text/event-stream" in content_type:
        async def sse_stream():
            try:
                async for chunk in backend_resp.aiter_bytes():
                    yield chunk
            finally:
                await backend_resp.aclose()
                await client.aclose()

        return StreamingResponse(
            sse_stream(),
            status_code=backend_resp.status_code,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        try:
            content = await backend_resp.aread()
        finally:
            await backend_resp.aclose()
            await client.aclose()

        resp_headers = {}
        for key, value in backend_resp.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP or lower in CORS_RESPONSE_HEADERS:
                continue
            resp_headers[key] = value

        return Response(
            content=content,
            status_code=backend_resp.status_code,
            headers=resp_headers,
        )
