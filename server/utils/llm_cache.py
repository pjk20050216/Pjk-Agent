"""
LLM 响应缓存 —— 为原生 OpenAI 调用提供 Redis 缓存层（项目使用 AsyncOpenAI 而非 LangChain ChatOpenAI）。
AsyncOpenAI：专门用于以异步非阻塞的方式与 OpenAI 的 API 服务端进行通信
API 服务端：指的那些执行文本操作的 AI 操作，服务端在其庞大的 GPU 集群运行大语言模型
"""
import hashlib
import json
from server.config import settings
from server.utils.redis_client import redis_client
from server.utils.logger import logger

KEY_PREFIX = "devflow:llm:"

# 每次 LLM 调用的 (messages, model, temperature, max_tokens) 序列化为 JSON 后做 MD5 哈希作为缓存键。
# 请求到达时先算哈希查 Redis，命中则直接返回，未命中才调用 OpenAI API 并将结果缓存：
def _make_key(messages: list[dict], model: str, temperature: float, max_tokens: int) -> str:
    payload = json.dumps({
        "messages": messages,
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }, sort_keys=True, ensure_ascii=False)
    return KEY_PREFIX + hashlib.md5(payload.encode("utf-8")).hexdigest()


def _make_key_with_tools(messages: list[dict], tools: list[dict], model: str, temperature: float, max_tokens: int) -> str:
    payload = json.dumps({
        "messages": messages,
        "tools": tools,
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }, sort_keys=True, ensure_ascii=False)
    return KEY_PREFIX + "tool:" + hashlib.md5(payload.encode("utf-8")).hexdigest()


async def llm_cache_get(messages: list[dict], model: str, temperature: float, max_tokens: int) -> str | None:
    key = _make_key(messages, model, temperature, max_tokens)
    cached = await redis_client.get(key)
    if cached:
        logger.debug(f"LLM cache HIT: {key[:48]}...")
    return cached


async def llm_cache_set(messages: list[dict], model: str, temperature: float, max_tokens: int, response: str) -> None:
    key = _make_key(messages, model, temperature, max_tokens)
    await redis_client.set(key, response, ttl=settings.redis_ttl_llm)
    logger.debug(f"LLM cache SET: {key[:48]}...")


async def llm_cache_get_tools(messages: list[dict], tools: list[dict], model: str, temperature: float, max_tokens: int) -> dict | None:
    key = _make_key_with_tools(messages, tools, model, temperature, max_tokens)
    cached = await redis_client.get(key)
    if cached:
        logger.debug(f"LLM tool cache HIT: {key[:48]}...")
        return json.loads(cached)
    return None


async def llm_cache_set_tools(messages: list[dict], tools: list[dict], model: str, temperature: float, max_tokens: int, response: dict) -> None:
    key = _make_key_with_tools(messages, tools, model, temperature, max_tokens)
    await redis_client.set(key, json.dumps(response, ensure_ascii=False), ttl=settings.redis_ttl_llm)
    logger.debug(f"LLM tool cache SET: {key[:48]}...")
