"""短期记忆 —— Redis 保存对话历史、任务计划、审批断点与临时数据。

Redis 不可用时自动退化为进程内 KV + 本地 JSON 快照（``data/harness/state``），
保证单机开发与 CI 环境同样可跑；跨进程恢复仍由 SQLite 运行快照兜底。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from harness.config import HarnessConfig, get_harness_config
from harness.contracts import RunSnapshot
from server.utils.logger import logger
from server.utils.redis_client import redis_client

KEY_PREFIX = "harness:st:"


class ShortTermMemory:
    def __init__(self, config: HarnessConfig | None = None) -> None:
        self.config = config or get_harness_config()
        self.ttl = self.config.memory.short_term_ttl
        self._local: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._state_file: Path = self.config.workspace_root / "state" / "short_term.json"
        self._loaded = False

    # ------------------------------------------------------------- KV backend
    def _load_local(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._state_file.exists():
                self._local = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._local = {}

    def _persist_local(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps(self._local, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.debug(f"[ShortTerm] persist failed: {exc}")

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        payload = json.dumps(value, ensure_ascii=False, default=str)
        client = redis_client.client
        if client is not None:
            try:
                await client.set(f"{KEY_PREFIX}{key}", payload, ex=ttl or self.ttl)
                return
            except Exception as exc:
                logger.debug(f"[ShortTerm] redis set failed: {exc}")
        async with self._lock:
            self._load_local()
            self._local[key] = {"value": value, "ts": _now_iso()}
            await asyncio.to_thread(self._persist_local)

    async def get_json(self, key: str) -> Any | None:
        client = redis_client.client
        if client is not None:
            try:
                raw = await client.get(f"{KEY_PREFIX}{key}")
                if raw:
                    return json.loads(raw)
            except Exception as exc:
                logger.debug(f"[ShortTerm] redis get failed: {exc}")
        async with self._lock:
            self._load_local()
            item = self._local.get(key)
            return item.get("value") if isinstance(item, dict) else item

    async def delete(self, *keys: str) -> None:
        client = redis_client.client
        if client is not None:
            try:
                await client.delete(*[f"{KEY_PREFIX}{k}" for k in keys])
            except Exception:
                pass
        async with self._lock:
            self._load_local()
            for key in keys:
                self._local.pop(key, None)
            await asyncio.to_thread(self._persist_local)

    # ------------------------------------------------------------------- runs
    async def save_run(self, snapshot: RunSnapshot) -> None:
        await self.set_json(
            f"run:{snapshot.id}",
            snapshot.model_dump(mode="json"),
            ttl=self.ttl,
        )

    async def load_run(self, run_id: str) -> dict | None:
        return await self.get_json(f"run:{run_id}")

    async def append_message(self, run_id: str, message: dict) -> None:
        key = f"messages:{run_id}"
        history = await self.get_json(key) or []
        history.append(message)
        # 只保留最近 200 条，避免 Redis 无界增长
        await self.set_json(key, history[-200:], ttl=self.ttl)

    async def get_messages(self, run_id: str, limit: int = 50) -> list[dict]:
        history = await self.get_json(f"messages:{run_id}") or []
        return history[-limit:]

    async def save_checkpoint(self, run_id: str, checkpoint: dict) -> None:
        await self.set_json(f"checkpoint:{run_id}", checkpoint, ttl=self.ttl)

    async def load_checkpoint(self, run_id: str) -> dict | None:
        return await self.get_json(f"checkpoint:{run_id}")

    async def save_approval_state(self, approval_id: str, payload: dict) -> None:
        await self.set_json(f"approval:{approval_id}", payload, ttl=self.ttl)

    async def load_approval_state(self, approval_id: str) -> dict | None:
        return await self.get_json(f"approval:{approval_id}")

    async def set_temp(self, run_id: str, key: str, value: Any) -> None:
        await self.set_json(f"temp:{run_id}:{key}", value, ttl=self.ttl)

    async def get_temp(self, run_id: str, key: str) -> Any | None:
        return await self.get_json(f"temp:{run_id}:{key}")


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


short_term = ShortTermMemory()
