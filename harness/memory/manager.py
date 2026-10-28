"""记忆门面 —— 统一短期（Redis）与长期（Milvus）记忆的读写。"""
from __future__ import annotations

import asyncio
from typing import Any

from harness.config import HarnessConfig, get_harness_config
from harness.contracts import MemoryRecord, RunSnapshot, SubAgentResult
from harness.memory.distillation import distill_run
from harness.memory.long_term import LongTermMemory
from harness.memory.short_term import ShortTermMemory
from server.utils.logger import logger


class MemoryManager:
    def __init__(self, config: HarnessConfig | None = None) -> None:
        self.config = config or get_harness_config()
        self.short = ShortTermMemory(self.config)
        self.long = LongTermMemory(self.config)

    # ------------------------------------------------------------------ recall
    async def recall(
        self,
        query: str,
        *,
        user_id: int | None = None,
        top_k: int | None = None,
        kind: str | None = None,
    ) -> list[MemoryRecord]:
        try:
            records = await self.long.search(
                query, top_k=top_k, kind=kind, user_id=user_id
            )
            return records
        except Exception as exc:
            logger.warning(f"[Memory] recall failed: {exc}")
            return []

    async def remember(self, records: list[MemoryRecord]) -> int:
        count = 0
        for record in records:
            try:
                if await self.long.remember(record):
                    count += 1
            except Exception as exc:
                logger.warning(f"[Memory] remember failed: {exc}")
        return count

    # ------------------------------------------------------------------ runs
    async def save_run(self, snapshot: RunSnapshot) -> None:
        await self.short.save_run(snapshot)

    async def load_run(self, run_id: str) -> dict | None:
        return await self.short.load_run(run_id)

    async def append_message(self, run_id: str, message: dict) -> None:
        await self.short.append_message(run_id, message)

    async def get_messages(self, run_id: str, limit: int = 50) -> list[dict]:
        return await self.short.get_messages(run_id, limit)

    async def checkpoint(self, run_id: str, payload: dict) -> None:
        await self.short.save_checkpoint(run_id, payload)

    async def load_checkpoint(self, run_id: str) -> dict | None:
        return await self.short.load_checkpoint(run_id)

    # ------------------------------------------------------------------ distill
    async def distill(
        self,
        snapshot: RunSnapshot,
        results: list[SubAgentResult],
    ) -> list[MemoryRecord]:
        if not self.config.memory.distill_enabled:
            return []
        try:
            records = await distill_run(snapshot, results)
        except Exception as exc:
            logger.warning(f"[Memory] distillation failed: {exc}")
            return []
        if records:
            await self.remember(records)
        return records

    # ------------------------------------------------------------------- admin
    async def recent(self, limit: int = 20, kind: str | None = None) -> list[MemoryRecord]:
        return await self.long.recent(limit=limit, kind=kind)

    def status(self) -> dict[str, Any]:
        return {
            "short_term": {
                "ttl": self.config.memory.short_term_ttl,
                "backend": "redis+local-file",
            },
            "long_term": self.long.status(),
            "distill_enabled": self.config.memory.distill_enabled,
        }


memory_manager = MemoryManager()
