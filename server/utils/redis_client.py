"""Redis 客户端封装 —— 提供异步和同步两种 Redis 客户端，单例 + 延迟初始化。"""
import redis.asyncio as aioredis
import redis as sync_redis
from server.config import settings
from server.utils.logger import logger


class RedisClient:
    """
    Redis 客户端：延迟初始化，提供异步（aioredis）和同步（redis）两种连接。
    就是对于是使用同步的方法和类采用同步 redis，对于异步的则采用异步 redis
    """

    def __init__(self):
        self._redis_async = None
        self._redis_raw = None
        self._redis_sync = None
        self._available = None

    def _check_connection(self) -> bool:
        try:
            conn = sync_redis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
                password=settings.redis_password or None,
                socket_connect_timeout=2,
            )
            conn.ping()
            conn.close()
            return True
        except Exception:
            return False

    @property
    def client(self) -> aioredis.Redis | None:
        """
        异步 Redis 客户端（decode_responses=True），用于 async 上下文中的 JSON 缓存。
        其中 decode_responses=True 表示返回的字符串是 utf-8 编码的，而不是字节串。
        """
        if self._redis_async is None and self._available is not False:
            if self._check_connection(): # 判断 redis 是否连接，所以在初始化中会先判断 client 再判断 client_raw
                self._redis_async = aioredis.Redis(
                    host=settings.redis_host,
                    port=settings.redis_port,
                    db=settings.redis_db,
                    password=settings.redis_password or None,
                    decode_responses=True,
                    socket_connect_timeout=2, # 表示连接操作最多等待 2 秒
                    socket_keepalive=True,
                    health_check_interval=30, # 如果发现该连接闲置时间超过了 30 秒，就会在返回连接前自动发送一个 PING 命令验证其存活性。
                )
                self._available = True
                logger.info(f"Redis connected: {settings.redis_host}:{settings.redis_port} (db={settings.redis_db})")
            else:
                self._available = False
                logger.warning(f"Redis unavailable at {settings.redis_host}:{settings.redis_port}, caching disabled")
        return self._redis_async

    @property
    def client_raw(self) -> aioredis.Redis | None:
        """
        异步 Redis 客户端（decode_responses=False），供 LangChain AsyncRedisCache 使用。
        因为 LangChain 的部分缓存组件内部对数据的序列化/反序列化有独立的处理逻辑，要求获取最原始的字节数据
        """
        if self._available and self._redis_raw is None:
            self._redis_raw = aioredis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
                password=settings.redis_password or None,
                decode_responses=False,
                socket_connect_timeout=2,
                socket_keepalive=True,
                health_check_interval=30,
            )
        return self._redis_raw

    @property
    def client_sync(self) -> sync_redis.Redis | None:
        """同步 Redis 客户端（decode_responses=True），用于 sync 上下文（路由、工具执行等）。"""
        if self._available and self._redis_sync is None:
            self._redis_sync = sync_redis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
                password=settings.redis_password or None,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_keepalive=True,
                health_check_interval=30,
            )
        return self._redis_sync

    # 异步方法专门用于异步执行上下文

    # 根据建名获取对应的字符串
    async def get(self, key: str) -> str | None:
        try:
            if self.client:
                return await self.client.get(key)
        except Exception as e:
            logger.warning(f"Redis GET failed: {e}")
        return None

    # 用于异步写入键值对
    async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
        try:
            if self.client:
                await self.client.set(key, value, ex=ttl)
                return True
        except Exception as e:
            logger.warning(f"Redis SET failed: {e}")
        return False

    async def delete(self, *keys: str) -> int:
        try:
            if self.client and keys:
                return await self.client.delete(*keys)
        except Exception as e:
            logger.warning(f"Redis DELETE failed: {e}")
        return 0

    # 模糊匹配批量删除键。每次匹配 100 pattern 键，收集完统一删除
    async def delete_pattern(self, pattern: str) -> int:
        try:
            if self.client:
                cursor = 0
                keys_to_delete = []
                while True:
                    cursor, keys = await self.client.scan(cursor, match=pattern, count=100)
                    keys_to_delete.extend(keys)
                    if cursor == 0:
                        break
                if keys_to_delete:
                    return await self.client.delete(*keys_to_delete)
        except Exception as e:
            logger.warning(f"Redis DELETE_PATTERN failed: {e}")
        return 0

    # 异步清除 redis 中所有数据（高危操作）
    async def flush_all(self) -> bool:
        try:
            if self.client:
                await self.client.flushdb()
                logger.info("Redis cache flushed")
                return True
        except Exception as e:
            logger.warning(f"Redis FLUSHDB failed: {e}")
        return False


    # 同步方法专门用于同步执行上下文
    def get_sync(self, key: str) -> str | None:
        try:
            if self.client_sync:
                return self.client_sync.get(key)
        except Exception as e:
            logger.warning(f"Redis GET (sync) failed: {e}")
        return None

    def set_sync(self, key: str, value: str, ttl: int | None = None) -> bool:
        try:
            if self.client_sync:
                self.client_sync.set(key, value, ex=ttl)
                return True
        except Exception as e:
            logger.warning(f"Redis SET (sync) failed: {e}")
        return False

    def delete_sync(self, *keys: str) -> int:
        try:
            if self.client_sync and keys:
                return self.client_sync.delete(*keys)
        except Exception as e:
            logger.warning(f"Redis DELETE (sync) failed: {e}")
        return 0

    def delete_pattern_sync(self, pattern: str) -> int:
        try:
            if self.client_sync:
                cursor = 0
                keys_to_delete = []
                while True:
                    cursor, keys = self.client_sync.scan(cursor, match=pattern, count=100)
                    keys_to_delete.extend(keys)
                    if cursor == 0:
                        break
                if keys_to_delete:
                    return self.client_sync.delete(*keys_to_delete)
        except Exception as e:
            logger.warning(f"Redis DELETE_PATTERN (sync) failed: {e}")
        return 0

    async def close(self) -> None:
        try:
            if self._redis_async:
                await self._redis_async.close()
            if self._redis_raw:
                await self._redis_raw.close()
            if self._redis_sync:
                self._redis_sync.close()
            logger.info("Redis connections closed")
        except Exception:
            pass


redis_client = RedisClient()
