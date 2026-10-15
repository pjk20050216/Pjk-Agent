"""联网搜索客户端（博查 Bocha）—— 单例 + 延迟初始化，返回结构兼容 Bing Search API。

复用项目既有基建：CircuitBreaker 保护外部调用，Redis 缓存结果（devflow:websearch:），
未配置 key / 关闭开关 / 调用失败时一律降级返回 []，绝不阻断对话。
"""
import hashlib
import json

import httpx

from server.config import settings
from server.utils.circuit_breaker import CircuitBreaker
from server.utils.redis_client import redis_client
from server.utils.logger import logger

WEB_SEARCH_CACHE_PREFIX = "devflow:websearch:"


class WebSearchClient:
    def __init__(self):
        self._http = None
        self._available = None
        self._cb = CircuitBreaker(
            name="web_search",
            failure_threshold=settings.circuit_breaker_threshold,
            recovery_timeout=settings.circuit_breaker_timeout,
        )

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = settings.web_search_enabled and bool(settings.web_search_api_key)
            if not self._available:
                logger.warning("Web search disabled or BOCHA_API_KEY missing, web search unavailable")
        return self._available

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=settings.web_search_timeout)
        return self._http

    @staticmethod
    def _cache_key(query: str, count: int, freshness: str) -> str:
        payload = json.dumps(
            {"q": query, "count": count, "freshness": freshness}, sort_keys=True, ensure_ascii=False
        )
        return WEB_SEARCH_CACHE_PREFIX + hashlib.md5(payload.encode("utf-8")).hexdigest()

    async def search(
        self, query: str, count: int | None = None, freshness: str = "noLimit"
    ) -> list[dict]:
        """联网搜索，返回 [{title, url, snippet, summary, site, published}]。失败返回 []。"""
        if not self.available:
            return []
        count = count or settings.web_search_default_count

        cache_key = self._cache_key(query, count, freshness)
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                logger.debug(f"Web search cache HIT: {cache_key[:48]}...")
                return json.loads(cached)
        except Exception:
            pass

        try:
            results = await self._cb.call(self._do_search(query, count, freshness))
        except Exception as e:
            logger.warning(f"Web search failed: {e}")
            return []

        try:
            await redis_client.set(
                cache_key, json.dumps(results, ensure_ascii=False), ttl=settings.redis_ttl_websearch
            )
        except Exception:
            pass
        return results

    async def _do_search(self, query: str, count: int, freshness: str) -> list[dict]:
        resp = await self.http.post(
            settings.web_search_base_url,
            headers={
                "Authorization": f"Bearer {settings.web_search_api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query, "summary": True, "count": count, "freshness": freshness},
        )
        resp.raise_for_status()
        data = resp.json()
        pages = (data.get("data") or {}).get("webPages", {}).get("value", []) or []

        results = []
        for p in pages:
            results.append({
                "title": p.get("name", ""),
                "url": p.get("url", ""),
                "snippet": p.get("snippet", ""),
                "summary": p.get("summary", ""),
                "site": p.get("siteName", ""),
                "published": p.get("datePublished") or p.get("dateLastCrawled", ""),
            })
        return results

    async def close(self) -> None:
        try:
            if self._http:
                await self._http.aclose()
                logger.info("Web search HTTP client closed")
        except Exception:
            pass


web_search_client = WebSearchClient()
