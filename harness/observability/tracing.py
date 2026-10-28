"""链路追踪 —— 覆盖请求、规划、子 Agent 委派、记忆访问、工具、文件、审批等关键节点。

实现要点：
- 用 ``contextvars`` 维护当前 span，天然适配 asyncio 并发子 Agent 的父子关系；
- span 结束时异步落 JSONL（``data/traces/{run_id}.jsonl``）并投递 Kafka；
- 追踪失败绝不影响主流程（全部异常吞掉并告警）。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator

from pydantic import BaseModel

from harness.config import HarnessConfig, get_harness_config
from harness.contracts import TraceSpan, new_id, utcnow
from harness.integrations.kafka import KafkaAuditSink, sink as default_sink
from server.utils.logger import logger

_current_span: ContextVar[str | None] = ContextVar("harness_current_span", default=None)


def _jsonable(value: Any) -> Any:
    """把任意属性值转成可 JSON 序列化的形式。"""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class TraceRecorder:
    def __init__(
        self,
        config: HarnessConfig | None = None,
        sink: KafkaAuditSink | None = None,
    ) -> None:
        self.config = config or get_harness_config()
        self.sink = sink or default_sink
        self._traces: dict[str, dict[str, Any]] = {}

    # --------------------------------------------------------------- lifecycle
    def ensure_trace(self, run_id: str, objective: str = "") -> str:
        state = self._traces.get(run_id)
        if state is None:
            state = {
                "trace_id": new_id("trace_"),
                "run_id": run_id,
                "objective": objective,
                "spans": [],
                "started_at": utcnow(),
            }
            self._traces[run_id] = state
        return state["trace_id"]

    def trace_id(self, run_id: str) -> str:
        return self.ensure_trace(run_id)

    def reset(self, run_id: str) -> None:
        self._traces.pop(run_id, None)

    # ------------------------------------------------------------------- spans
    @asynccontextmanager
    async def span(
        self,
        run_id: str,
        name: str,
        kind: str = "internal",
        attributes: dict[str, Any] | None = None,
    ) -> AsyncIterator[TraceSpan]:
        if not self.config.tracing.enabled:
            # 追踪关闭时返回一个空 span，调用方无需分支
            yield TraceSpan(trace_id="", name=name, kind=kind)
            return

        trace_id = self.ensure_trace(run_id)
        parent = _current_span.get()
        span = TraceSpan(
            trace_id=trace_id,
            parent_span_id=parent,
            name=name,
            kind=kind,
            attributes={k: _jsonable(v) for k, v in (attributes or {}).items()},
        )
        token = _current_span.set(span.span_id)
        try:
            yield span
            span.finish("ok")
        except asyncio.CancelledError:
            span.finish("cancelled")
            raise
        except Exception as exc:
            span.finish("error", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            _current_span.reset(token)
            await self._export(run_id, span)

    async def record_completed_span(
        self,
        run_id: str,
        name: str,
        kind: str = "internal",
        attributes: dict[str, Any] | None = None,
        *,
        duration_ms: int = 0,
        status: str = "ok",
    ) -> None:
        """记录一个已完成的 span（供中间件在 after 钩子中使用）。"""
        if not self.config.tracing.enabled:
            return
        span = TraceSpan(
            trace_id=self.ensure_trace(run_id),
            parent_span_id=_current_span.get(),
            name=name,
            kind=kind,
            status=status,
            attributes={k: _jsonable(v) for k, v in (attributes or {}).items()},
        )
        span.start_ts = utcnow() - timedelta(milliseconds=max(0, duration_ms))
        span.end_ts = utcnow()
        await self._export(run_id, span)

    async def _export(self, run_id: str, span: TraceSpan) -> None:
        try:
            state = self._traces.setdefault(
                run_id,
                {
                    "trace_id": span.trace_id,
                    "run_id": run_id,
                    "objective": "",
                    "spans": [],
                    "started_at": utcnow(),
                },
            )
            state["spans"].append(span.model_dump(mode="json"))
            if len(state["spans"]) > self.config.tracing.max_events:
                state["spans"] = state["spans"][-self.config.tracing.max_events:]

            payload = span.model_dump(mode="json")
            await asyncio.to_thread(self._append_jsonl, run_id, payload)
            await self.sink.emit("trace", payload)
        except Exception as exc:  # 追踪绝不影响业务
            logger.debug(f"[Trace] export failed: {exc}")

    def _append_jsonl(self, run_id: str, payload: dict) -> None:
        path = self.config.tracing.jsonl_dir / f"{run_id}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            logger.debug(f"[Trace] jsonl append failed: {exc}")

    # ------------------------------------------------------------------ query
    def snapshot(self, run_id: str) -> dict[str, Any]:
        state = self._traces.get(run_id)
        if state is None:
            return {"trace_id": "", "run_id": run_id, "spans": []}
        return {
            "trace_id": state["trace_id"],
            "run_id": run_id,
            "objective": state.get("objective", ""),
            "started_at": state["started_at"].isoformat()
            if isinstance(state["started_at"], datetime)
            else state["started_at"],
            "spans": state["spans"],
        }

    def read_spans(self, run_id: str) -> list[dict]:
        """优先内存，缺失时从 JSONL 回读（跨进程/重启场景）。"""
        state = self._traces.get(run_id)
        if state and state["spans"]:
            return state["spans"]
        path = self.config.tracing.jsonl_dir / f"{run_id}.jsonl"
        if not path.exists():
            return []
        spans: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            spans.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            return spans
        return spans


recorder = TraceRecorder()
