"""熔断器状态机 —— 保护外部 API 调用，三种状态：CLOSED → OPEN → HALF_OPEN。"""
import asyncio
import time
from enum import Enum
from server.utils.logger import logger


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(Exception):
    """熔断器开路时抛出，调用方应捕获并返回 503 等服务不可用响应。"""
    pass


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._lock = asyncio.Lock()

    async def call(self, coro):
        await self._maybe_transition()

        if self._state == CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                f"Circuit [{self.name}] is OPEN — temporarily unavailable"
            )

        try:
            result = await coro
        except Exception:
            await self._on_failure()
            raise

        await self._on_success()
        return result

    async def _maybe_transition(self):
        if self._state != CircuitState.OPEN:
            return

        elapsed = time.monotonic() - self._last_failure_time
        if elapsed >= self.recovery_timeout:
            async with self._lock:
                if self._state == CircuitState.OPEN:
                    self._state = CircuitState.HALF_OPEN
                    self._failure_count = 0
                    logger.info(f"Circuit [{self.name}] → HALF_OPEN (probing)")

    async def _on_failure(self):
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning(
                    f"Circuit [{self.name}] → OPEN (probe failed, "
                    f"{self._failure_count} failures)"
                )
            elif (
                self._state == CircuitState.CLOSED
                and self._failure_count >= self.failure_threshold
            ):
                self._state = CircuitState.OPEN
                logger.warning(
                    f"Circuit [{self.name}] → OPEN "
                    f"({self._failure_count} consecutive failures)"
                )

    async def _on_success(self):
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                logger.info(f"Circuit [{self.name}] → CLOSED (probe succeeded)")
            self._failure_count = 0
