"""
路由缓存辅助函数 —— 主要用于为 GET 请求接口提供基于 Redis 的缓存读写能力
它通过同步的方式操作 Redis，避免在路由处理中引发异步事件循环冲突
"""
import json
from server.utils.redis_client import redis_client
from server.utils.logger import logger

# 负责从缓存中读取路由数据
def get_cached_route(key: str) -> dict | None:
    try:
        cached = redis_client.get_sync(key)
        if cached:
            logger.debug(f"Route cache HIT: {key[:50]}...")
            return json.loads(cached)
    except Exception:
        pass
    return None

# 负责将路由响应数据写入缓存
def set_cached_route(key: str, data, ttl: int) -> None:
    try:
        serialized = json.dumps(data, default=str, ensure_ascii=False)
        redis_client.set_sync(key, serialized, ttl)
    except Exception:
        pass
