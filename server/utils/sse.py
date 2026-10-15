"""SSE 适配层 —— 统一 Server-Sent Events 的帧格式与响应头，供各流式接口复用。

把「业务事件 → SSE 文本帧」和「事件流 → StreamingResponse」这两件事收敛到一处：
业务代码只负责产出 (event, data)，不必重复拼 `event: ...\\ndata: ...\\n\\n` 或设置响应头。
"""
import json

from fastapi.responses import StreamingResponse

# SSE 响应头：禁用缓存 + 关闭反向代理缓冲，保证逐 token 实时下发
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

SSE_MEDIA_TYPE = "text/event-stream"


def sse_event(event: str, data) -> str:
    """把 (event, data) 编成一条 SSE 帧。

    data 非字符串时自动 JSON 序列化（保留中文）；含换行的文本按 SSE 规范拆成多行 data。
    """
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False)
    payload = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event}\n{payload}\n"


def sse_response(stream, *, status_code: int = 200, headers: dict | None = None) -> StreamingResponse:
    """把事件流包装成带标准 SSE 响应头的 StreamingResponse。"""
    merged = dict(SSE_HEADERS)
    if headers:
        merged.update(headers)
    return StreamingResponse(
        stream,
        status_code=status_code,
        media_type=SSE_MEDIA_TYPE,
        headers=merged,
    )
