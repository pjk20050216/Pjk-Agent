"""Kafka 审计 / 链路事件上报 —— 对接公司链路追踪平台。

生产环境通过 ``aiokafka`` 异步发送；未安装或 Broker 不可达时自动降级为
本地 JSONL 缓冲（``data/harness/audit``），保证审计数据不丢、业务不阻塞。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.config import HarnessConfig, get_harness_config
from server.utils.logger import logger

try:  # 可选依赖：未安装时不影响主流程
    from aiokafka import AIOKafkaProducer  # type: ignore
except Exception:  # pragma: no cover - 取决于部署环境
    AIOKafkaProducer = None  # type: ignore


class KafkaAuditSink:
    """审计与追踪事件的双通道出口：Kafka 优先，本地缓冲兜底。"""

    def __init__(self, config: HarnessConfig | None = None) -> None:
        self.config = config or get_harness_config()
        self._producer: Any = None
        self._start_lock = asyncio.Lock()
        self._started = False
        self._degraded = False
        self._degraded_reason = ""

    @property
    def available(self) -> bool:
        return self._started and not self._degraded

    @property
    def status(self) -> dict:
        return {
            "enabled": self.config.kafka.enabled,
            "available": self.available,
            "degraded": self._degraded,
            "reason": self._degraded_reason,
            "bootstrap_servers": self.config.kafka.bootstrap_servers,
            "buffer_dir": str(self.config.kafka.buffer_dir),
        }

    async def start(self) -> bool:
        if not self.config.kafka.enabled:
            self._degraded_reason = "disabled"
            return False
        if self._started:
            return self.available
        async with self._start_lock:
            if self._started:
                return self.available
            self._started = True
            if AIOKafkaProducer is None:
                self._degraded = True
                self._degraded_reason = "aiokafka not installed"
                logger.warning("[Kafka] aiokafka missing, audit falls back to local buffer")
                return False
            try:
                producer = AIOKafkaProducer(
                    bootstrap_servers=self.config.kafka.bootstrap_servers,
                    client_id=self.config.kafka.client_id,
                    acks="all",
                    enable_idempotence=True,
                    request_timeout_ms=5000,
                )
                await asyncio.wait_for(producer.start(), timeout=5)
                self._producer = producer
                logger.info(
                    f"[Kafka] connected: {self.config.kafka.bootstrap_servers}"
                )
                return True
            except Exception as exc:
                self._degraded = True
                self._degraded_reason = str(exc)
                logger.warning(f"[Kafka] unavailable ({exc}), audit buffered locally")
                return False

    async def emit(self, topic_kind: str, payload: dict) -> None:
        """topic_kind: ``audit`` | ``trace``。"""
        if not self.config.kafka.enabled:
            return
        topic = (
            self.config.kafka.topic_audit
            if topic_kind == "audit"
            else self.config.kafka.topic_trace
        )
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        if not self._started:
            await self.start()
        if self._producer is not None and not self._degraded:
            try:
                await self._producer.send_and_wait(topic, body, timeout=5)
                return
            except Exception as exc:
                self._degraded = True
                self._degraded_reason = str(exc)
                logger.warning(f"[Kafka] send failed ({exc}), switching to local buffer")
        await asyncio.to_thread(self._buffer, topic, body)

    def _buffer(self, topic: str, body: bytes) -> None:
        try:
            self.config.kafka.buffer_dir.mkdir(parents=True, exist_ok=True)
            path: Path = self.config.kafka.buffer_dir / f"{topic}.jsonl"
            with path.open("ab") as fh:
                fh.write(body + b"\n")
        except OSError as exc:
            logger.warning(f"[Kafka] local buffer failed: {exc}")

    async def close(self) -> None:
        if self._producer is not None:
            try:
                await self._producer.stop()
            except Exception:
                pass
            self._producer = None


def audit_payload(
    *,
    event: str,
    run_id: str,
    user_id: int | None = None,
    actor: str = "harness",
    action: str = "",
    target: str = "",
    outcome: str = "ok",
    risk: str = "low",
    detail: dict | None = None,
    trace_id: str = "",
) -> dict:
    """统一审计事件结构，便于公司平台按字段检索。"""
    return {
        "schema": "harness.audit.v1",
        "event": event,
        "run_id": run_id,
        "trace_id": trace_id,
        "user_id": user_id,
        "actor": actor,
        "action": action,
        "target": target,
        "outcome": outcome,
        "risk": risk,
        "detail": detail or {},
        "ts": datetime.now(timezone.utc).isoformat(),
    }


sink = KafkaAuditSink()
