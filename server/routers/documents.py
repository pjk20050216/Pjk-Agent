"""文档路由 —— 文档列表、上传（含 RAG 索引）、删除（含清理向量存储和文件）。"""
from fastapi import APIRouter, Depends, UploadFile, File
from sqlalchemy.orm import Session
from server.database import get_db
from server.dependencies import get_current_user
from server.models.user import User
from server.schemas.document import DocumentResponse, DocumentList
from server.services import document_service

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.get("", response_model=DocumentList)
def list_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """GET /api/documents —— 获取当前用户已上传的文档列表。"""
    docs = document_service.list_documents(db, current_user.id)
    return DocumentList(
        documents=[DocumentResponse.model_validate(d) for d in docs]
    )


@router.post("/upload", response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """POST /api/documents/upload —— 上传 PDF/TXT 文件，自动进行 RAG 向量化索引。"""
    doc = await document_service.upload_document(db, current_user.id, file)
    return DocumentResponse.model_validate(doc)


@router.delete("/{document_id}")
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """DELETE /api/documents/{id} —— 删除文档及其向量存储中的分块和磁盘文件。"""
    document_service.delete_document(db, document_id, current_user.id)
    return {"detail": "Document deleted"}
