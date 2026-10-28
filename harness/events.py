"""运行事件总线 —— 一次运行的所有关键节点事件。

职责：
1. 生成单调递增 seq，追加写入 ``{run_dir}/events.jsonl``，供 SSE 断线重放与审计；
2. 向当前进程内的订阅者（SSE 连接）广播，支持异步子 Agent 并发发布；
3. 队列满时丢弃最旧事件，避免慢消费者拖垮运行时。
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path
from typing import AsyncIterator

from harness.config import HarnessConfig, get_harness_config
from harness.contracts import RunEvent, RunEventType, utcnow
from server.utils.logger import logger

_TERMINAL_EVENTS = {
    RunEventType.RUN_COMPLETED.value,
    RunEventType.RUN_FAILED.value,
    RunEventType.RUN_CANCELLED.value,
}


class EventBus:
    def __init__(self, config: HarnessConfig | None = None) -> None:
        self.config = config or get_harness_config()
        self._seq: dict[str, int] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._append_lock = asyncio.Lock()

    # ------------------------------------------------------------------ paths
    def run_dir(self, run_id: str) -> Path:
        return self.config.workspace_root / "runs" / run_id

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    # ------------------------------------------------------------------ seq
    def _next_seq(self, run_id: str) -> int:
        if run_id not in self._seq:
            self._seq[run_id] = self._read_last_seq(run_id)
        self._seq[run_id] += 1
        return self._seq[run_id]

    def _read_last_seq(self, run_id: str) -> int:
        path = self.events_path(run_id)
        if not path.exists():
            return 0
        last = 0
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        last = int(json.loads(line).get("seq", last))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
        except OSError:
            return 0
        return last

    # -------------------------------------------------------------- publish
    async def publish(
        self,
        run_id: str,
        event_type: RunEventType | str,
        payload: dict | None = None,
        *,
        trace_id: str = "",
        span_id: str = "",
    ) -> RunEvent:
        event = RunEvent(
            seq=self._next_seq(run_id),
            run_id=run_id,
            type=event_type.value if isinstance(event_type, RunEventType) else str(event_type),
            payload=payload or {},
            ts=utcnow(),
            trace_id=trace_id,
            span_id=span_id,
        )
        # 并发子 Agent 会同时发布事件；串行化落盘，保证 JSONL 顺序与 seq 一致
        async with self._append_lock:
            await asyncio.to_thread(self._append, run_id, event)

        # 广播（非阻塞；慢消费者丢最旧）
        for queue in list(self._subscribers.get(run_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        return event

    def _append(self, run_id: str, event: RunEvent) -> None:
        path = self.events_path(run_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning(f"[EventBus] append failed for {run_id}: {exc}")

    # -------------------------------------------------------------- subscribe
    def subscribe(self, run_id: str, maxsize: int = 1000) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers[run_id].add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        self._subscribers.get(run_id, set()).discard(queue)

    # ------------------------------------------------------------------ read
    def read_events(self, run_id: str, after_seq: int = 0) -> list[RunEvent]:
        path = self.events_path(run_id)
        if not path.exists():
            return []
        events: list[RunEvent] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                        if int(raw.get("seq", 0)) <= after_seq:
                            continue
                        events.append(RunEvent.model_validate(raw))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
        except OSError:
            return events
        return events

    async def stream(
        self,
        run_id: str,
        after_seq: int = 0,
        *,
        idle_timeout: float = 30.0,
        max_wait: float = 3600.0,
    ) -> AsyncIterator[RunEvent]:
        """先回放历史事件，再实时订阅；收到终止事件或超时后结束。"""
        queue = self.subscribe(run_id)
        try:
            history = await asyncio.to_thread(self.read_events, run_id, after_seq)
            for event in history:
                yield event
                if event.type in _TERMINAL_EVENTS:
                    return

            waited = 0.0
            while waited < max_wait:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    waited += idle_timeout
                    # 发送心跳，避免代理层因空闲断开 SSE
                    yield RunEvent(
                        seq=self._seq.get(run_id, 0),
                        run_id=run_id,
                        type="heartbeat",
                        payload={},
                        ts=utcnow(),
                    )
                    continue
                waited = 0.0
                yield event
                if event.type in _TERMINAL_EVENTS:
                    return
        finally:
            self.unsubscribe(run_id, queue)


bus = EventBus()
