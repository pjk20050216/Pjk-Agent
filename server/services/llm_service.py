"""LLM 服务门面 —— 对外暴露统一对话接口，内部委托给 provider 工厂选择的适配器。

保留缓存逻辑与同步包装，真正 API 调用由适配器完成；
切换 provider 时 model/缓存 key 随之变化，调用方零改动。
"""
import asyncio

from server.llm.factory import provider_factory


class LLMService:
    """LLM 客户端门面：委托 provider_factory 的激活适配器，叠加缓存与同步包装。"""

    @property
    def model(self) -> str:
        """当前激活 provider 的对话模型名（供缓存 key 使用）。"""
        return provider_factory.get_active().model

    @property
    def classifier_model(self) -> str:
        """当前激活 provider 的意图识别专用模型名（未配置则为空串，调用方回退到默认模型）。"""
        return provider_factory.get_active().config.classifier_model

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        model: str | None = None,
    ) -> str:
        """一次性聊天补全：查缓存 → 委托适配器 → 写缓存。

        model 非空时覆盖默认对话模型（用于意图识别的小模型），缓存 key 随之使用该模型名，
        避免与主模型（同提示词、不同模型）互相串缓存。
        """
        from server.utils.llm_cache import llm_cache_get, llm_cache_set

        effective_model = model or self.model

        cached = await llm_cache_get(messages, effective_model, temperature, max_tokens)
        if cached is not None:
            return cached

        provider = provider_factory.get_active()
        result = await provider.chat(messages, temperature, max_tokens, model=model)

        await llm_cache_set(messages, effective_model, temperature, max_tokens, result)
        return result

    async def chat_stream(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 4096):
        """流式聊天补全：逐 token yield，供 SSE 接口消费。"""
        provider = provider_factory.get_active()
        async for token in provider.chat_stream(messages, temperature, max_tokens):
            yield token

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> dict:
        """支持 Function Calling 的聊天补全：查缓存 → 委托适配器 → 写缓存。"""
        from server.utils.llm_cache import llm_cache_get_tools, llm_cache_set_tools

        cached = await llm_cache_get_tools(messages, tools, self.model, temperature, max_tokens)
        if cached is not None:
            return cached

        provider = provider_factory.get_active()
        result = await provider.chat_with_tools(messages, tools, temperature, max_tokens)

        await llm_cache_set_tools(messages, tools, self.model, temperature, max_tokens, result)
        return result

    async def chat_stream_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ):
        """流式 + Function Calling：逐 token yield，完成后 yield tool_calls 事件。"""
        provider = provider_factory.get_active()
        async for item in provider.chat_stream_with_tools(messages, tools, temperature, max_tokens):
            yield item

    def chat_sync(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 4096) -> str:
        """同步包装器：在同步上下文中调用 async chat 方法。"""
        return asyncio.run(self.chat(messages, temperature, max_tokens))

    def chat_with_tools_sync(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> dict:
        """同步包装器：在同步上下文中调用 async chat_with_tools 方法。"""
        return asyncio.run(self.chat_with_tools(messages, tools or [], temperature, max_tokens))


# 全局单例 LLM 服务门面
llm_service = LLMService()
