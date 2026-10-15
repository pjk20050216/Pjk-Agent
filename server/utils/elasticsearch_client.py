"""Elasticsearch 客户端封装 —— 单例 + 延迟初始化 + 连通性探测，任一环节失败均优雅降级。

ES 作为 Chroma 语义检索之外的并行全文召回源：Chroma 治语义相似，ES 治关键词/专有名词精确匹配。
所有方法在 ES 不可用时返回安全默认值，绝不抛异常阻断主流程（与 redis_client 约定一致）。
"""
from elasticsearch import Elasticsearch, helpers

from server.config import settings
from server.utils.logger import logger

# 优选中文分词器（需 IK 插件），缺失时回退 standard，功能不受阻
_MAPPING_IK = {
    "properties": {
        "doc_id": {"type": "integer"},
        "chunk_type": {"type": "keyword"},
        "parent_id": {"type": "keyword"},
        "content": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
    }
}
_MAPPING_STANDARD = {
    "properties": {
        "doc_id": {"type": "integer"},
        "chunk_type": {"type": "keyword"},
        "parent_id": {"type": "keyword"},
        "content": {"type": "text"},
    }
}


class ElasticsearchClient:
    """ES 客户端：延迟初始化，无认证或账号密码两种连接方式，索引首次访问时自动创建。"""

    def __init__(self):
        self._client = None
        self._available = None  # None=未探测，True/False=探测结果
        self._index_ready = False

    def _check_connection(self) -> bool:
        if not settings.es_enabled:
            return False
        try:
            conn = self._build()
            return bool(conn.ping())
        except Exception:
            return False

    def _build(self) -> Elasticsearch:
        kwargs = {"hosts": [settings.es_url], "request_timeout": 10}
        if settings.es_username:
            kwargs["basic_auth"] = (settings.es_username, settings.es_password)
        return Elasticsearch(**kwargs)

    @property
    def client(self) -> Elasticsearch | None:
        if self._client is None and self._available is not False:
            if self._check_connection():
                self._client = self._build()
                self._available = True
                logger.info(f"Elasticsearch connected: {settings.es_url} (index={settings.es_index})")
            else:
                self._available = False
                logger.warning(f"Elasticsearch unavailable at {settings.es_url}, full-text retrieval disabled")
        return self._client

    def _ensure_index(self) -> bool:
        """索引不存在则创建；IK 分词器不可用时回退 standard。"""
        client = self.client
        if client is None:
            return False
        if self._index_ready:
            return True
        try:
            if not client.indices.exists(index=settings.es_index):
                try:
                    client.indices.create(index=settings.es_index, mappings=_MAPPING_IK)
                except Exception as e:
                    logger.warning(f"[ES] ik analyzer unavailable, fallback to standard: {e}")
                    client.indices.create(index=settings.es_index, mappings=_MAPPING_STANDARD)
            self._index_ready = True
            return True
        except Exception as e:
            logger.warning(f"[ES] ensure index failed: {e}")
            return False

    def index_docs(self, docs: list[dict]) -> int:
        """批量写入 chunk。docs 每项含 _id/doc_id/chunk_type/parent_id/content。"""
        if not docs or not self._ensure_index():
            return 0
        try:
            # _source 中不能包含 _id（ES 元数据字段），仅作为请求参数传入
            actions = [
                {
                    "_index": settings.es_index,
                    "_id": d["_id"],
                    "_source": {k: v for k, v in d.items() if k != "_id"},
                }
                for d in docs
            ]
            ok, errors = helpers.bulk(
                self.client, actions, refresh=True, raise_on_error=False, stats_only=False
            )
            if errors:
                logger.warning(f"[ES] bulk partial failure, {len(errors)} item(s): {errors[:2]}")
            return ok
        except Exception as e:
            logger.warning(f"[ES] bulk index failed: {e}")
            return 0

    def delete_by_doc(self, doc_id: int) -> int:
        if not self._ensure_index():
            return 0
        try:
            resp = self.client.delete_by_query(
                index=settings.es_index,
                query={"term": {"doc_id": doc_id}},
                refresh=True,
            )
            return resp.get("deleted", 0)
        except Exception as e:
            logger.warning(f"[ES] delete doc {doc_id} failed: {e}")
            return 0

    def search_children(
        self, query: str, document_ids: list[int] | None, k: int
    ) -> list[tuple[str, float]]:
        """BM25 检索子块，返回 [(parent_id, score)]，按分降序。失败返回 []。"""
        if not self._ensure_index():
            return []
        try:
            filters = [{"term": {"chunk_type": "child"}}]
            if document_ids:
                filters.append({"terms": {"doc_id": document_ids}})
            resp = self.client.search(
                index=settings.es_index,
                query={
                    "bool": {
                        "must": [{"match": {"content": query}}],
                        "filter": filters,
                    }
                },
                size=k,
                _source=["parent_id"],
            )
            pairs = []
            for hit in resp["hits"]["hits"]:
                pid = hit["_source"].get("parent_id")
                if pid:
                    pairs.append((pid, float(hit["_score"])))
            return pairs
        except Exception as e:
            logger.warning(f"[ES] search failed: {e}")
            return []

    def reset_index(self) -> None:
        """删除并重建索引（供全量重建时清空）。"""
        client = self.client
        if client is None:
            return
        try:
            if client.indices.exists(index=settings.es_index):
                client.indices.delete(index=settings.es_index)
                logger.info(f"[ES] Dropped index {settings.es_index} before reindex")
            self._index_ready = False
            self._ensure_index()
        except Exception as e:
            logger.warning(f"[ES] reset index failed: {e}")

    def close(self) -> None:
        try:
            if self._client:
                self._client.close()
                logger.info("Elasticsearch connection closed")
        except Exception:
            pass


es_client = ElasticsearchClient()