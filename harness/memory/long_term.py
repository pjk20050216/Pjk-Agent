"""长期记忆 —— Milvus 向量库保存规则经验、策略模板、用户偏好与历史案例。

生产路径：Milvus（pymilvus，懒连接） + embedding 向量召回。
降级路径：本地 JSONL + 关键词/标签打分，保证 Milvus / 向量服务不可用时仍可检索。
所有记录都会镜像写入本地，作为跨环境可迁移的“经验底稿”。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.config import HarnessConfig, get_harness_config
from harness.contracts import MemoryRecord
from harness.memory.embeddings import embed_one
from server.utils.logger import logger

_CN_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for word in _WORD_RE.findall(text.lower()):
        tokens.add(word)
    for chunk in _CN_RE.findall(text):
        for i in range(len(chunk) - 1):
            tokens.add(chunk[i: i + 2])
        if len(chunk) == 1:
            tokens.add(chunk)
    return tokens


def _keyword_score(query: str, record: MemoryRecord) -> float:
    q = _tokens(query)
    if not q:
        return 0.0
    body = _tokens(record.text + " " + " ".join(record.tags))
    overlap = len(q & body)
    if overlap == 0:
        return 0.0
    score = overlap / math.sqrt(len(q) * max(1, len(body)))
    score += 0.25 * len(set(t.lower() for t in record.tags) & q)
    # 时间衰减：30 天半衰
    try:
        age_days = (datetime.now(timezone.utc) - record.created_at).days
        score += 0.1 * math.exp(-max(0, age_days) / 30)
    except Exception:
        pass
    return score


class LongTermMemory:
    def __init__(self, config: HarnessConfig | None = None) -> None:
        self.config = config or get_harness_config()
        self._milvus: Any = None
        self._milvus_ready = False
        self._milvus_failed = False
        self._fallback_path = self.config.memory.fallback_dir / "memories.jsonl"
        self._lock = asyncio.Lock()
        self._seen: set[str] = set()
        self._load_seen()

    # ---------------------------------------------------------------- fallback
    def _load_seen(self) -> None:
        if not self._fallback_path.exists():
            return
        try:
            with self._fallback_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        record = json.loads(line)
                        self._seen.add(record.get("id", ""))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass

    def _append_local(self, record: MemoryRecord) -> None:
        try:
            self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with self._fallback_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")
            self._seen.add(record.id)
        except OSError as exc:
            logger.warning(f"[LongTerm] local append failed: {exc}")

    def _read_local(self, limit: int = 5000) -> list[MemoryRecord]:
        if not self._fallback_path.exists():
            return []
        records: list[MemoryRecord] = []
        try:
            with self._fallback_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(MemoryRecord.model_validate(json.loads(line)))
                    except Exception:
                        continue
            return records[-limit:]
        except OSError:
            return records

    # ------------------------------------------------------------------ milvus
    def _ensure_milvus(self) -> Any:
        if self._milvus_ready:
            return self._milvus
        if self._milvus_failed:
            return None
        cfg = self.config.milvus
        if not cfg.enabled:
            self._milvus_failed = True
            return None
        try:
            from pymilvus import MilvusClient  # type: ignore
        except Exception:
            logger.warning("[LongTerm] pymilvus not installed, using local store")
            self._milvus_failed = True
            return None
        try:
            client = MilvusClient(uri=cfg.uri, token=cfg.token or None)
            if not client.has_collection(cfg.collection):
                client.create_collection(
                    collection_name=cfg.collection,
                    dimension=cfg.dim,
                    primary_field_name="id",
                    id_type="string",
                    vector_field_name="vector",
                    metric_type="COSINE",
                    auto_id=False,
                    max_length=64,
                    enable_dynamic_field=True,
                )
            self._milvus = client
            self._milvus_ready = True
            logger.info(f"[LongTerm] Milvus ready: {cfg.uri}/{cfg.collection}")
            return client
        except Exception as exc:
            logger.warning(f"[LongTerm] Milvus unavailable ({exc}), using local store")
            self._milvus_failed = True
            return None

    # ------------------------------------------------------------------- write
    async def remember(self, record: MemoryRecord) -> bool:
        if record.id in self._seen:
            return False
        await asyncio.to_thread(self._append_local, record)

        if not self.config.milvus.enabled:
            return True
        vector = await embed_one(record.text)
        if vector is None:
            return True

        def _insert() -> bool:
            client = self._ensure_milvus()
            if client is None:
                return False
            try:
                client.insert(
                    collection_name=self.config.milvus.collection,
                    data=[
                        {
                            "id": record.id,
                            "vector": vector,
                            "text": record.text,
                            "kind": record.kind,
                            "tags": json.dumps(record.tags, ensure_ascii=False),
                            "run_id": record.run_id or "",
                            "user_id": int(record.user_id or 0),
                            "created_at": record.created_at.isoformat(),
                            "metadata": json.dumps(record.metadata, ensure_ascii=False, default=str),
                        }
                    ],
                )
                return True
            except Exception as exc:
                logger.warning(f"[LongTerm] Milvus insert failed: {exc}")
                return False

        await asyncio.to_thread(_insert)
        return True

    # -------------------------------------------------------------------- read
    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        kind: str | None = None,
        user_id: int | None = None,
    ) -> list[MemoryRecord]:
        top_k = top_k or self.config.memory.long_term_top_k
        results: list[MemoryRecord] = []

        if self.config.milvus.enabled:
            vector = await embed_one(query)
            if vector is not None:
                results = await asyncio.to_thread(
                    self._search_milvus, vector, top_k, kind, user_id
                )

        if not results:
            results = await asyncio.to_thread(self._search_local, query, top_k, kind, user_id)
        return results

    def _search_milvus(
        self,
        vector: list[float],
        top_k: int,
        kind: str | None,
        user_id: int | None,
    ) -> list[MemoryRecord]:
        client = self._ensure_milvus()
        if client is None:
            return []
        filters = []
        if kind:
            safe_kind = kind.replace('"', "")
            filters.append(f'kind == "{safe_kind}"')
        if user_id:
            filters.append(f"user_id == {int(user_id)}")
        expr = " and ".join(filters) if filters else ""
        try:
            raw = client.search(
                collection_name=self.config.milvus.collection,
                data=[vector],
                limit=top_k,
                output_fields=[
                    "text", "kind", "tags", "run_id", "user_id", "created_at", "metadata"
                ],
                filter=expr or None,
            )
        except Exception as exc:
            logger.warning(f"[LongTerm] Milvus search failed: {exc}")
            return []
        records: list[MemoryRecord] = []
        for hit in (raw[0] if raw else []):
            entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
            try:
                records.append(
                    MemoryRecord(
                        id=str(hit.get("id", "")) or entity.get("id", ""),
                        kind=entity.get("kind", "experience"),
                        text=entity.get("text", ""),
                        tags=json.loads(entity.get("tags", "[]")),
                        run_id=entity.get("run_id") or None,
                        user_id=int(entity.get("user_id", 0)) or None,
                        score=float(hit.get("distance", 0.0)),
                        created_at=datetime.fromisoformat(entity["created_at"])
                        if entity.get("created_at")
                        else datetime.now(timezone.utc),
                        metadata=json.loads(entity.get("metadata", "{}")),
                    )
                )
            except Exception:
                continue
        return records

    def _search_local(
        self,
        query: str,
        top_k: int,
        kind: str | None,
        user_id: int | None,
    ) -> list[MemoryRecord]:
        scored: list[tuple[float, MemoryRecord]] = []
        for record in self._read_local():
            if kind and record.kind != kind:
                continue
            if user_id and record.user_id not in (None, user_id):
                continue
            score = _keyword_score(query, record)
            if score > 0:
                record.score = round(score, 4)
                scored.append((score, record))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [record for _, record in scored[:top_k]]

    async def recent(self, limit: int = 20, kind: str | None = None) -> list[MemoryRecord]:
        records = await asyncio.to_thread(self._read_local)
        if kind:
            records = [r for r in records if r.kind == kind]
        return list(reversed(records[-limit:]))

    def status(self) -> dict:
        return {
            "milvus_enabled": self.config.milvus.enabled,
            "milvus_ready": self._milvus_ready,
            "degraded": self._milvus_failed,
            "collection": self.config.milvus.collection,
            "fallback_file": str(self._fallback_path),
            "local_records": len(self._seen),
        }


long_term = LongTermMemory()
