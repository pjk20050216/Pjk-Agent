"""OpenAI 兼容 API 适配器 —— 适配所有 OpenAI 兼容协议提供商（DashScope / OpenAI / Ollama 等）。

每个实例持有独立的 AsyncOpenAI 客户端和熔断器，封装非流式/流式/Function Calling 四类调用。
"""
from openai import AsyncOpenAI
from fastapi import HTTPException

from server.config import settings
from server.llm.base import BaseLLMProvider, ProviderConfig
from server.utils.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
from server.utils.logger import logger

_UNAVAILABLE_DETAIL = "LLM service temporarily unavailable, please try again later"


class OpenAICompatProvider(BaseLLMProvider):
    """基于 OpenAI 兼容协议的提供商适配器。"""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self._client: AsyncOpenAI | None = None
        self._cb = CircuitBreaker(
            name=f"llm_api_{config.name}",
            failure_threshold=settings.circuit_breaker_threshold,
            recovery_timeout=settings.circuit_breaker_timeout,
        )

    @property
    def client(self) -> AsyncOpenAI:
        """延迟创建 AsyncOpenAI 客户端（首次调用时初始化，后续复用）。"""
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
            )
            logger.info(f"LLM client created: {self.config.base_url} / {self.config.model}")
        return self._client

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        model: str | None = None,
    ) -> str:
        target_model = model or self.config.model

        async def _api_call():
            response = await self.client.chat.completions.create(
                model=target_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content or ""

        try:
            return await self._cb.call(_api_call())
        except CircuitBreakerOpenError:
            raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL)

    async def chat_stream(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 4096):
        async def _create_stream():
            return await self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        try:
            response = await self._cb.call(_create_stream())
        except CircuitBreakerOpenError:
            raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL)

        async for chunk in response:
            # 部分模型（如 qwen3.7-max / qwen3.8-max）会在流末尾多发一个
            # choices 为空数组的收尾 chunk（携带 usage 统计），直接取 [0] 会抛
            # IndexError 并中断整个流。此处跳过即可。
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> dict:
        async def _api_call():
            response = await self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=temperature,
                max_tokens=max_tokens,
            )
            choice = response.choices[0]
            msg = choice.message
            result = {"content": msg.content}
            if msg.tool_calls:
                result["tool_calls"] = [
                    {
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    }
                    for tc in msg.tool_calls
                ]
            return result

        try:
            return await self._cb.call(_api_call())
        except CircuitBreakerOpenError:
            raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL)

    async def chat_stream_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ):
        async def _create_stream():
            return await self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        try:
            response = await self._cb.call(_create_stream())
        except CircuitBreakerOpenError:
            raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL)

        tool_calls_acc: dict[int, dict] = {}
        async for chunk in response:
            # 同 chat_stream：跳过末尾 choices 为空的 usage 收尾 chunk
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.content:
                yield {"type": "content", "data": delta.content}

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    entry = tool_calls_acc[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["arguments"] += tc_delta.function.arguments

        if tool_calls_acc:
            yield {
                "type": "tool_calls",
                "data": list(tool_calls_acc.values()),
            }
