"""RAG 服务 —— Parent-Child Chunking：子 chunk 检索（精度高）+ 父 chunk 阅读（上下文完整）。

混合检索：Chroma 向量语义召回 与 Elasticsearch BM25 全文召回并行，RRF 融合后统一回表父块。
ES 不可用或关闭时自动退化为纯 Chroma 语义检索，对外契约不变。
"""
import asyncio
import hashlib
import json
import threading
import uuid
from pathlib import Path
import chromadb
from langchain_chroma import Chroma
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from langchain_core.documents import Document
from server.config import settings
from server.llm.factory import provider_factory
from server.services.es_service import es_service
from server.utils.redis_client import redis_client
from server.utils.logger import logger

RAG_CACHE_PREFIX = "devflow:rag:"


class RAGService:
    def __init__(self):
        self._embeddings = None
        self._embedding_model = None
        self._vector_store = None
        self._reindex_lock = threading.Lock()

    @property
    def embeddings(self):
        provider = provider_factory.get_active()
        model = provider.config.embedding_model
        if self._embeddings is None or self._embedding_model != model:
            if self._embeddings is not None:
                logger.warning(
                    f"[RAG] embedding model changed ({self._embedding_model} -> {model}), "
                    "请重建向量索引以保证检索一致性"
                )
            self._embeddings = OpenAIEmbeddings(
                openai_api_base=provider.config.embedding_base_url,
                openai_api_key=provider.config.api_key,
                model=model,
                # 关闭 tiktoken 预分词：开启时 langchain 会发送「整数 token 数组」，
                # DashScope 兼容模式会拒绝该形式（400 InvalidParameter:
                # contents is neither str nor list of str.），导致文档索引全部失败。
                check_embedding_ctx_length=provider.config.embedding_check_ctx_length,
            )
            self._embedding_model = model
        return self._embeddings

    def _collection_name(self) -> str:
        """按 embedding 模型区分 collection，切换 embedding 后自动使用独立索引。"""
        model = provider_factory.get_active().config.embedding_model
        digest = hashlib.md5(model.encode("utf-8")).hexdigest()[:8]
        return f"{settings.chroma_collection_name}_{digest}"

    @property
    def vector_store(self):
        if self._vector_store is None:
            self._vector_store = Chroma(
                collection_name=self._collection_name(),
                embedding_function=self.embeddings,
                persist_directory=settings.chroma_persist_dir,
            )
            logger.info(f"Chroma vector store initialized: {settings.chroma_persist_dir}")
        return self._vector_store

    @property
    def _parent_splitter(self):
        """父块分割器：大粒度，保证上下文完整。"""
        return RecursiveCharacterTextSplitter(
            chunk_size=600,
            chunk_overlap=100,
            separators=["\n\n", "\n", ".", "!", "?", "。", "", " "],
        )

    @property
    def _child_splitter(self):
        """子块分割器：小粒度，用于向量检索精确匹配。"""
        return RecursiveCharacterTextSplitter(
            chunk_size=200,
            chunk_overlap=50,
            separators=["\n\n", "\n", ".", "!", "?", "。", "", " "],
        )

    # 索引

    def index_document(self, file_path: str, doc_id: int) -> int:
        """
        加载文档 → 父块切分 → 每个父块再切子块 → 仅子块建向量索引（父块存全文）。
        返回：子块总数（即向量索引条数）。

        幂等：索引前先清除该 doc_id 的旧 chunk。否则重复上传同一文档（或
        rebuild_index）会不断累加，检索时同一段内容被返回多次并挤占 top_k。
        ES 侧在 es_service.index_document 中已有等价的 delete_by_doc。
        """
        self.delete_document_chunks(doc_id)

        path = Path(file_path)
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            loader = PyPDFLoader(str(path))
        elif suffix == ".txt":
            loader = TextLoader(str(path), encoding="utf-8")
        else:
            raise ValueError(f"Unsupported file type: {suffix}")

        documents = loader.load()
        if not documents:
            return 0

        # 第一层：父块切分
        parents = self._parent_splitter.split_documents(documents)
        logger.info(f"Document {doc_id}: {len(parents)} parent chunks")

        children_all = []
        parents_for_storage = []

        for i, parent in enumerate(parents):
            parent_id = f"doc_{doc_id}_p_{i}_{uuid.uuid4().hex[:6]}"

            # 第二层：子块切分
            children = self._child_splitter.split_documents([parent])
            for child in children:
                child.metadata["doc_id"] = doc_id
                child.metadata["chunk_type"] = "child"
                child.metadata["parent_id"] = parent_id
            children_all.extend(children)

            # 父块存入 metadata（不参与向量检索，但可作为回退阅读源）
            parent.metadata["doc_id"] = doc_id
            parent.metadata["chunk_type"] = "parent"
            parent.metadata["parent_id"] = parent_id
            parents_for_storage.append(parent)

        # 子块入 Chroma（建向量索引，用于检索匹配）
        if children_all:
            self.vector_store.add_documents(children_all)

        # 父块入 Chroma（只存文本和 metadata，embedding 计算开销可控，便于按 parent_id 查全）
        if parents_for_storage:
            self.vector_store.add_documents(parents_for_storage)

        # 双写 ES 全文索引（复用同一份 chunk，失败不影响 Chroma）
        try:
            es_service.index_document(children_all, parents_for_storage, doc_id)
        except Exception as e:
            logger.warning(f"[ES] index doc {doc_id} failed: {e}")

        logger.info(
            f"Indexed doc {doc_id}: {len(parents)} parents + {len(children_all)} children"
        )
        return len(children_all)

    # 检索

    def _retrieve_cache_key(
        self, query: str, document_ids: list[int] | None, k: int, threshold: float
    ) -> str:
        payload = json.dumps({
            "query": query,
            "document_ids": sorted(document_ids) if document_ids else None,
            "k": k,
            "threshold": threshold,
        }, sort_keys=True, ensure_ascii=False)
        return RAG_CACHE_PREFIX + hashlib.md5(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize_parents(parents: list[Document]) -> str:
        return json.dumps([
            {"page_content": p.page_content, "metadata": p.metadata}
            for p in parents
        ], ensure_ascii=False)

    @staticmethod
    def _deserialize_parents(data: str) -> list[Document]:
        items = json.loads(data)
        return [Document(page_content=item["page_content"], metadata=item["metadata"]) for item in items]

    @staticmethod
    def _annotate_chunks(chunks: list[Document]) -> list[Document]:
        """给父块标注来源序号和相似度分数，供 LLM 引用。"""
        for i, chunk in enumerate(chunks):
            score = chunk.metadata.get("score", 1.0)
            source = chunk.metadata.get("doc_id", "unknown")
            chunk.page_content = (
                f"[来源 {i + 1} · 文档 #{source} · 相关度 {score:.2f}]\n"
                f"{chunk.page_content}"
            )
        return chunks

    async def retrieve(
        self,
        query: str,
        document_ids: list[int] | None = None,
        k: int | None = None,
        score_threshold: float | None = None,
    ) -> list[Document]:
        """
        Parent-Child 混合检索：
        1. 子 chunk 双路召回 —— Chroma 向量语义 + ES BM25 全文（并行）
        2. RRF 融合两路排名（ES 不可用时退化为纯 Chroma）
        3. 命中后按 parent_id 回表取父 chunk（上下文完整）
        4. 父 chunk 去重 + 来源标注
        """
        k = k or settings.rag_top_k
        score_threshold = score_threshold or settings.rag_relevance_threshold

        cache_key = self._retrieve_cache_key(query, document_ids, k, score_threshold)
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                logger.debug(f"RAG cache HIT: {cache_key[:48]}...")
                return self._deserialize_parents(cached)
        except Exception:
            pass

        # Step 1：并行双召回 —— Chroma 语义（子块）+ ES 全文（BM25 子块）
        # Chroma 是同步阻塞调用，移入线程避免阻塞事件循环；ES 不可用时返回 []，不影响主流程
        chroma_pairs, es_pairs = await asyncio.gather(
            asyncio.to_thread(
                self._chroma_search_children, query, document_ids, k, score_threshold
            ),
            self._es_search_children(query, document_ids, settings.rag_es_top_k),
        )

        if not chroma_pairs and not es_pairs:
            logger.info(f"RAG: 0 chunks matched (threshold={score_threshold}, hybrid={settings.rag_hybrid_enabled})")
            return []

        # Step 2：融合 —— 混合模式用 RRF 决定「排序」，关闭混合时退化为纯 Chroma 排序
        if settings.rag_hybrid_enabled and es_pairs:
            ranked = self._rrf_fuse(chroma_pairs, es_pairs, settings.rag_rrf_k, k)
            # RRF 分数（1/(k+rank) 累加）与真实相似度无关，直接归一化会让每个来源
            # 都显示成「相关度 1.00」，与 system_prompt 中「低于 0.5 需谨慎引用」
            # 的防幻觉规则冲突。故 RRF 只用于排序，展示分数回填 Chroma 的真实
            # 相关度；仅被 ES 命中（Chroma 未召回）的父块才退化为按排名折算。
            chroma_score_map = dict(chroma_pairs)
            total = max(len(ranked), 1)
            fused = [
                (
                    pid,
                    chroma_score_map[pid]
                    if pid in chroma_score_map
                    else max(0.0, 1.0 - rank / total),
                )
                for rank, (pid, _) in enumerate(ranked)
            ]
        else:
            fused = chroma_pairs[:k]

        # Step 3：按 parent_id 获取父块全文（Chroma 存储父块）
        parent_scores = dict(fused)
        parents = self._fetch_parents([pid for pid, _ in fused], parent_scores)

        # Step 4：来源标注
        parents = self._annotate_chunks(parents)

        logger.info(
            f"RAG: chroma={len(chroma_pairs)} es={len(es_pairs)} → "
            f"{len(parents)} unique parents (hybrid={settings.rag_hybrid_enabled})"
        )

        try:
            serialized = self._serialize_parents(parents)
            await redis_client.set(cache_key, serialized, ttl=settings.redis_ttl_rag)
        except Exception:
            pass

        return parents

    def _chroma_search_children(
        self, query: str, document_ids: list[int] | None, k: int, score_threshold: float
    ) -> list[tuple[str, float]]:
        """Chroma 子块语义检索，返回 [(parent_id, 最高相似度)]，按分降序。

        直接调用 vector_store.similarity_search_with_relevance_scores 而非
        as_retriever()：后者在 similarity_score_threshold 模式下会丢弃相似度
        （docs = [doc for doc, _ in ...]），且不写回 metadata["score"]，
        导致上游读到的分数恒为 0、来源标注全变成「相关度 0.00」。
        """
        child_filter: dict = {"chunk_type": "child"}
        if document_ids:
            # Chroma 要求 where 顶层只能有一个操作符，多个条件必须用 $and 包裹；
            # 直接写 {"chunk_type": ..., "doc_id": ...} 会抛
            # ValueError: Expected where to have exactly one operator
            child_filter = {
                "$and": [
                    {"chunk_type": "child"},
                    {"doc_id": {"$in": list(document_ids)}},
                ]
            }

        pairs = self.vector_store.similarity_search_with_relevance_scores(
            query,
            k=k,
            score_threshold=score_threshold,
            filter=child_filter,
        )

        parent_scores: dict[str, float] = {}
        for child, score in pairs:
            pid = child.metadata.get("parent_id")
            if pid and (pid not in parent_scores or score > parent_scores[pid]):
                parent_scores[pid] = score
        return sorted(parent_scores.items(), key=lambda x: x[1], reverse=True)

    async def _es_search_children(
        self, query: str, document_ids: list[int] | None, k: int
    ) -> list[tuple[str, float]]:
        """ES 子块全文检索。未开启混合检索或 ES 异常时返回 []（降级）。"""
        if not settings.rag_hybrid_enabled:
            return []
        try:
            return await es_service.search_children(query, document_ids, k)
        except Exception as e:
            logger.warning(f"[ES] search_children failed: {e}")
            return []

    @staticmethod
    def _rrf_fuse(
        chroma_pairs: list[tuple[str, float]],
        es_pairs: list[tuple[str, float]],
        rrf_k: int,
        top: int,
    ) -> list[tuple[str, float]]:
        """倒数排名融合：Chroma 相似度与 ES BM25 量纲不同，只按排名累加最稳。"""
        scores: dict[str, float] = {}
        for rank, (pid, _) in enumerate(chroma_pairs):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (rrf_k + rank + 1)
        for rank, (pid, _) in enumerate(es_pairs):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (rrf_k + rank + 1)
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top]

    def _fetch_parents(
        self, parent_ids: list[str], scores: dict[str, float]
    ) -> list[Document]:
        """从 Chroma 中按 parent_id 列表获取父块全文。"""
        try:
            collection = self.vector_store._collection
            result = collection.get(
                where={"parent_id": {"$in": parent_ids}},
                include=["documents", "metadatas"],
            )
        except Exception as e:
            logger.warning(f"Chroma batch get failed, fallback to single fetch: {e}")
            return self._fetch_parents_one_by_one(parent_ids, scores)

        parents = []
        for content, meta in zip(result.get("documents", []), result.get("metadatas", [])):
            if meta.get("chunk_type") == "parent":
                # 注入子块命中的最高相似度分数
                meta["score"] = scores.get(meta.get("parent_id", ""), 0)
                parents.append(Document(page_content=content, metadata=meta))

        parents.sort(key=lambda p: p.metadata.get("score", 0), reverse=True)
        return parents

    def _fetch_parents_one_by_one(
        self, parent_ids: list[str], scores: dict[str, float]
    ) -> list[Document]:
        """回退方案：逐个获取父块（兼容 Chroma 不支持 $in 的场景）。"""
        parents = []
        collection = self.vector_store._collection
        for pid in parent_ids:
            try:
                result = collection.get(
                    where={"parent_id": pid},
                    include=["documents", "metadatas"],
                )
                for content, meta in zip(result.get("documents", []), result.get("metadatas", [])):
                    if meta.get("chunk_type") == "parent":
                        meta["score"] = scores.get(meta.get("parent_id", ""), 0)
                        parents.append(Document(page_content=content, metadata=meta))
            except Exception:
                continue
        parents.sort(key=lambda p: p.metadata.get("score", 0), reverse=True)
        return parents

    # 删除

    def delete_document_chunks(self, doc_id: int) -> None:
        try:
            self.vector_store.delete(where={"doc_id": doc_id})
            logger.info(f"Deleted all chunks for document {doc_id}")
        except Exception as e:
            logger.warning(f"Failed to delete chunks for document {doc_id}: {e}")

    def _drop_collection(self, name: str) -> None:
        """直接删除 Chroma collection（不构造 embedding，避免受凭据/模型可用性影响）。"""
        try:
            client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
            if name in {c.name for c in client.list_collections()}:
                client.delete_collection(name)
                logger.info(f"[RAG] Dropped collection {name} before reindex")
        except Exception as e:
            logger.warning(f"[RAG] Drop collection {name} failed: {e}")

    def rebuild_index(self, db) -> int:
        """清空当前 embedding 对应的索引，用当前 embedding 重新索引所有 ready 文档。

        切换 embedding 后调用。先删除目标 collection 再全量重建，
        否则重复切换（A→B→A）会在同一个 collection 里累积重复向量。
        """
        from server.models.document import Document

        with self._reindex_lock:
            self._drop_collection(self._collection_name())

            # ES 索引一并清空重建（BM25 无 embedding 依赖，但需与文档集保持一致）
            try:
                es_service.reset_index()
            except Exception as e:
                logger.warning(f"[ES] reset index failed during reindex: {e}")

            # 重置缓存，使下次访问 vector_store 时用新 collection + 新 embedding
            self._vector_store = None
            self._embeddings = None
            self._embedding_model = None

            docs = db.query(Document).filter(Document.status == "ready").all()
            count = 0
            for doc in docs:
                file_path = Path(settings.upload_dir) / doc.filename
                if not file_path.exists():
                    logger.warning(f"[RAG] Reindex skip missing file for doc {doc.id}: {file_path}")
                    continue
                try:
                    doc.chunk_count = self.index_document(str(file_path), doc.id)
                    count += 1
                except Exception as e:
                    logger.error(f"[RAG] Reindex failed for doc {doc.id}: {e}")

            db.commit()

            try:
                redis_client.delete_pattern_sync("devflow:rag:*")
            except Exception:
                pass

            logger.info(f"[RAG] Reindex completed: {count} documents")
            return count


rag_service = RAGService()