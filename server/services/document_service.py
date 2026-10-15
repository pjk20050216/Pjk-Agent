"""文档业务逻辑 —— 文件上传校验、存储、RAG 索引，以及文档删除（含向量存储清理）。"""
import asyncio
import uuid
from pathlib import Path
from sqlalchemy.orm import Session
from fastapi import HTTPException, UploadFile
from server.config import settings
from server.models.document import Document
from server.utils.file_handler import get_file_sha256, validate_file_type
from server.utils.redis_client import redis_client
from server.utils.logger import logger

MAX_UPLOAD_SIZE = settings.max_upload_size_mb * 1024 * 1024


def list_documents(db: Session, user_id: int) -> list[Document]:
    """获取当前用户上传的所有文档，按时间降序。"""
    return (
        db.query(Document)
        .filter(Document.user_id == user_id)
        .order_by(Document.created_at.desc())
        .all()
    )


async def upload_document(db: Session, user_id: int, file: UploadFile) -> Document:
    """
    上传文档：校验类型/大小 → 存盘 → 计算 SHA-256 → 创建数据库记录 → RAG 向量化索引。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file selected")

    if not validate_file_type(file.filename, settings.allowed_file_types):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {', '.join(settings.allowed_file_types)}",
        )

    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail=f"File too large. Max: {settings.max_upload_size_mb}MB")

    upload_dir = Path(settings.upload_dir)
    # 确保储存的目录存在，不存在则自动创建
    upload_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(file.filename).suffix.lower()
    stored_name = f"{uuid.uuid4().hex}{ext}"
    file_path = upload_dir / stored_name

    # 以二进制写入模式（wb）打开目标路径
    with open(file_path, "wb") as f:
        # 将内存中的文件内容写入磁盘
        f.write(content)

    file_hash = get_file_sha256(file_path)
    file_type = ext.lstrip(".")

    doc = Document(
        user_id=user_id,
        filename=stored_name,
        original_filename=file.filename,
        file_type=file_type,
        file_size=len(content),
        file_hash=file_hash,
        status="processing",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    try:
        from server.services.rag_service import rag_service
        chunk_count = rag_service.index_document(str(file_path), doc.id)
        doc.chunk_count = chunk_count
        doc.status = "ready"
        # asyncio.create_task 异步触发一个后台任务，清理 Redis 中与 RAG 相关的缓存（devflow:rag:*），确保下次查询时能获取最新数据。
        asyncio.create_task(redis_client.delete_pattern("devflow:rag:*"))
    except Exception as e:
        logger.error(f"Failed to index document {doc.id}: {e}")
        doc.status = "error"
        doc.chunk_count = 0

    db.commit()
    db.refresh(doc)
    return doc


def delete_document(db: Session, document_id: int, user_id: int) -> None:
    """删除文档：清理向量存储中的分块 → 删除磁盘文件 → 删除数据库记录。"""
    doc = db.query(Document).filter(
        Document.id == document_id,
        Document.user_id == user_id,
    ).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Remove from vector store
    try:
        from server.services.rag_service import rag_service
        rag_service.delete_document_chunks(doc.id)
    except Exception as e:
        logger.warning(f"Failed to delete RAG chunks for document {doc.id}: {e}")

    # Remove from ES full-text index
    try:
        from server.services.es_service import es_service
        es_service.delete_document_chunks(doc.id)
    except Exception as e:
        logger.warning(f"Failed to delete ES chunks for document {doc.id}: {e}")

    # 清理 RAG 缓存
    try:
        redis_client.delete_pattern_sync("devflow:rag:*")
    except Exception:
        pass

    # Remove file
    file_path = Path(settings.upload_dir) / doc.filename
    if file_path.exists():
        file_path.unlink()

    db.delete(doc)
    db.commit()
