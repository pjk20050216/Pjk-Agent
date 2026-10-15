"""ES 检索服务 —— 接收 rag_service 已切好的 chunk（不重复解析文件），提供索引 / 删除 / 检索。

与 rag_service 对称：入库、删除、重建的操作都会同时作用于 Chroma 与 ES；
检索侧只负责 BM25 子块召回，父块回表仍由 rag_service 统一处理。
"""
import asyncio

from server.utils.elasticsearch_client import es_client
from server.utils.logger import logger


class ESService:
    def index_document(self, children: list, parents: list, doc_id: int) -> int:
        """双写 ES：先按 doc_id 清理旧数据（幂等），再批量写入子块 + 父块。"""
        es_client.delete_by_doc(doc_id)

        docs = []
        for i, child in enumerate(children):
            docs.append({
                "_id": f"doc_{doc_id}_child_{i}",
                "doc_id": doc_id,
                "chunk_type": "child",
                "parent_id": child.metadata.get("parent_id", ""),
                "content": child.page_content,
            })
        for i, parent in enumerate(parents):
            docs.append({
                "_id": f"doc_{doc_id}_parent_{i}",
                "doc_id": doc_id,
                "chunk_type": "parent",
                "parent_id": parent.metadata.get("parent_id", ""),
                "content": parent.page_content,
            })

        count = es_client.index_docs(docs)
        logger.info(f"[ES] indexed {count} chunks for doc {doc_id}")
        return count

    def delete_document_chunks(self, doc_id: int) -> None:
        es_client.delete_by_doc(doc_id)

    def reset_index(self) -> None:
        es_client.reset_index()

    async def search_children(
        self, query: str, document_ids: list[int] | None, k: int
    ) -> list[tuple[str, float]]:
        """BM25 子块召回，返回 [(parent_id, score)]。ES 不可用时返回 []。"""
        return await asyncio.to_thread(es_client.search_children, query, document_ids, k)


es_service = ESService()