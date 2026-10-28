"""Embedding 适配 —— 复用当前激活 provider 的 embedding 模型。

失败返回 None，由长期记忆回退到关键词检索；绝不因向量服务不可用阻断运行。
"""
from __future__ import annotations

import asyncio
from typing import Any

from server.llm.factory import provider_factory
from server.utils.logger import logger

_client: Any = None
_client_key: tuple[str, str, str] | None = None


def _get_client() -> Any:
    global _client, _client_key
    cfg = provider_factory.get_active().config
    key = (cfg.embedding_base_url, cfg.api_key, cfg.embedding_model)
    if _client is None or _client_key != key:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(base_url=cfg.embedding_base_url, api_key=cfg.api_key)
        _client_key = key
    return _client


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    if not texts:
        return []
    try:
        cfg = provider_factory.get_active().config
        if not cfg.api_key or not cfg.embedding_model:
            return None
        client = _get_client()
        response = await client.embeddings.create(
            model=cfg.embedding_model,
            input=[text[:6000] for text in texts],
        )
        return [item.embedding for item in response.data]
    except Exception as exc:
        logger.debug(f"[Embedding] unavailable: {exc}")
        return None


async def embed_one(text: str) -> list[float] | None:
    vectors = await embed_texts([text])
    return vectors[0] if vectors else None


async def embed_texts_sync_safe(texts: list[str]) -> list[list[float]] | None:
    """在同步上下文（如线程池）中安全调用。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(embed_texts(texts))
    return await loop.run_in_executor(None, lambda: asyncio.run(embed_texts(texts)))
