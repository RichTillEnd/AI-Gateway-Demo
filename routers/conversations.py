"""
對話管理 API 路由
"""
import os
import tempfile
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from typing import Optional
from database import get_db, User, Conversation, Message, Project, RagCategory, ProjectDocument
from core import get_current_user, get_rag, log_audit
from datetime import datetime

router = APIRouter()


def _project_dict(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "system_prompt": p.system_prompt or "",
        "rag_categories": [c.strip() for c in p.rag_categories.split(",") if c.strip()] if p.rag_categories else [],
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


@router.get("/conversations", tags=["對話管理"], summary="取得目前用戶的所有對話列表")
async def get_conversations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    search: Optional[str] = None
):
    query = db.query(Conversation).filter(Conversation.user_id == current_user.id)
    if search and search.strip():
        query = query.filter(Conversation.title.ilike(f"%{search.strip()}%"))
    conversations = query.order_by(Conversation.updated_at.desc()).all()
    return [
        {
            "id": c.id,
            "title": c.title,
            "is_starred": c.is_starred,
            "project_id": c.project_id,
            "updated_at": c.updated_at,
            "created_at": c.created_at,
        }
        for c in conversations
    ]


@router.get("/conversations/{conversation_id}/messages", tags=["對話管理"], summary="取得指定對話的所有訊息")
async def get_conversation_messages(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="對話不存在")
    messages = db.query(Message).filter(
        Message.conversation_id == conversation_id
    ).order_by(Message.created_at.asc()).all()
    return messages


@router.get("/conversations/search", tags=["對話管理"], summary="搜尋對話標題與訊息內文")
async def search_conversations(
    q: str,
    limit: int = 20,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    keyword = q.strip()
    if not keyword:
        return []

    like = f"%{keyword}%"

    title_matches = db.query(Conversation).filter(
        Conversation.user_id == current_user.id,
        Conversation.title.ilike(like)
    ).order_by(Conversation.updated_at.desc()).limit(limit).all()

    msg_matches = (
        db.query(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(
            Conversation.user_id == current_user.id,
            Message.content.ilike(like)
        )
        .order_by(Conversation.updated_at.desc(), Message.created_at.asc())
        .all()
    )

    seen = {}
    results = []

    for c in title_matches:
        if c.id not in seen:
            seen[c.id] = True
            results.append({
                "id":         c.id,
                "title":      c.title or "新對話",
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                "match_type": "title",
                "snippet":    None,
            })

    for m in msg_matches:
        if m.conversation_id not in seen:
            seen[m.conversation_id] = True
            conv = db.query(Conversation).filter(Conversation.id == m.conversation_id).first()
            content = m.content or ""
            idx = content.lower().find(keyword.lower())
            start = max(0, idx - 40)
            end   = min(len(content), idx + len(keyword) + 40)
            snippet = ("…" if start > 0 else "") + content[start:end] + ("…" if end < len(content) else "")
            results.append({
                "id":         m.conversation_id,
                "title":      conv.title if conv else "新對話",
                "updated_at": conv.updated_at.isoformat() if conv and conv.updated_at else None,
                "match_type": "message",
                "snippet":    snippet,
            })

    return results[:limit]


@router.delete("/conversations/{conversation_id}", tags=["對話管理"], summary="刪除指定對話（含所有訊息）")
async def delete_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="對話不存在")
    conv_title = conversation.title
    db.delete(conversation)
    db.commit()
    log_audit(db, "CONVERSATION_DELETE", actor=current_user,
              resource_type="conversation", resource_id=conversation_id,
              details={"title": conv_title})
    return {"message": "對話已刪除"}


# ==================== 專案 API ====================

@router.get("/projects", tags=["對話管理"], summary="取得目前用戶的所有專案")
async def get_projects(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    projects = db.query(Project).filter(Project.user_id == current_user.id).order_by(Project.updated_at.desc()).all()
    return [_project_dict(p) for p in projects]


@router.post("/projects", tags=["對話管理"], summary="建立新專案")
async def create_project(
    data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="專案名稱不得為空")
    project = Project(user_id=current_user.id, name=name)
    db.add(project)
    db.commit()
    db.refresh(project)
    return _project_dict(project)


@router.put("/projects/{project_id}", tags=["對話管理"], summary="重新命名專案")
async def rename_project(
    project_id: int,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    project = db.query(Project).filter(Project.id == project_id, Project.user_id == current_user.id).first()
    if not project:
        raise HTTPException(status_code=404, detail="專案不存在")
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="專案名稱不得為空")
    project.name = name
    db.commit()
    return _project_dict(project)


@router.put("/projects/{project_id}/settings", tags=["對話管理"], summary="更新專案指示與知識庫設定")
async def update_project_settings(
    project_id: int,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    更新專案的 AI 設定：
    - system_prompt: 專案自訂指示（注入 AI system prompt）
    - rag_categories: 知識庫分類清單，如 ["hr", "policy"]
    """
    project = db.query(Project).filter(Project.id == project_id, Project.user_id == current_user.id).first()
    if not project:
        raise HTTPException(status_code=404, detail="專案不存在")

    if "system_prompt" in data:
        project.system_prompt = (data["system_prompt"] or "").strip() or None

    if "rag_categories" in data:
        cats = data["rag_categories"]
        if cats is None:
            project.rag_categories = None
        else:
            # 驗證分類是否存在
            valid = {c.name for c in db.query(RagCategory).filter(RagCategory.is_active == True).all()}
            invalid = [c for c in cats if c not in valid]
            if invalid:
                raise HTTPException(status_code=400, detail=f"不存在的分類：{', '.join(invalid)}")
            project.rag_categories = ",".join(cats) if cats else None

    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return _project_dict(project)


@router.delete("/projects/{project_id}", tags=["對話管理"], summary="刪除專案（對話不會被刪除）")
async def delete_project(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    project = db.query(Project).filter(Project.id == project_id, Project.user_id == current_user.id).first()
    if not project:
        raise HTTPException(status_code=404, detail="專案不存在")
    # 清除 ChromaDB 中的專案私有文件
    rag = get_rag()
    if rag:
        proj_docs = db.query(ProjectDocument).filter(ProjectDocument.project_id == project_id).all()
        for doc in proj_docs:
            try:
                rag.delete_document(doc.doc_id)
            except Exception:
                pass
    db.query(Conversation).filter(Conversation.project_id == project_id).update({"project_id": None})
    db.delete(project)  # cascade 會刪除 project_documents 記錄
    db.commit()
    return {"message": "專案已刪除"}


# ==================== 專案私有知識庫文件 ====================

_ALLOWED_DOC_EXTS = {"txt", "pdf", "docx", "md", "xlsx", "csv"}
_MAX_DOC_SIZE = 20 * 1024 * 1024  # 20 MB


@router.get("/projects/{project_id}/documents", tags=["對話管理"], summary="列出專案私有知識庫文件")
async def list_project_documents(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    project = db.query(Project).filter(Project.id == project_id, Project.user_id == current_user.id).first()
    if not project:
        raise HTTPException(status_code=404, detail="專案不存在")
    docs = (
        db.query(ProjectDocument)
        .filter(ProjectDocument.project_id == project_id)
        .order_by(ProjectDocument.uploaded_at.desc())
        .all()
    )
    return [
        {
            "id": d.id,
            "doc_id": d.doc_id,
            "filename": d.filename,
            "doc_title": d.doc_title,
            "chunks_count": d.chunks_count,
            "uploaded_at": d.uploaded_at.isoformat(),
        }
        for d in docs
    ]


@router.post("/projects/{project_id}/documents", tags=["對話管理"], summary="上傳文件至專案私有知識庫")
async def upload_project_document(
    project_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    project = db.query(Project).filter(Project.id == project_id, Project.user_id == current_user.id).first()
    if not project:
        raise HTTPException(status_code=404, detail="專案不存在")

    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in _ALLOWED_DOC_EXTS:
        raise HTTPException(status_code=400, detail=f"不支援的格式：{ext}，可接受：{', '.join(sorted(_ALLOWED_DOC_EXTS))}")

    content = await file.read()
    if len(content) > _MAX_DOC_SIZE:
        raise HTTPException(status_code=400, detail="檔案超過 20MB 上限")

    rag = get_rag()
    if not rag:
        raise HTTPException(status_code=503, detail="RAG 服務未啟用（請確認 OPENAI_KEY）")

    # 寫入暫存檔後送 RAG
    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        category = f"project_{project_id}"
        result = rag.add_document(
            file_path=tmp_path,
            filename=file.filename,
            doc_title=file.filename,
            uploaded_by=current_user.id,
            category=category,
        )
    finally:
        os.unlink(tmp_path)

    if not result.get("success"):
        raise HTTPException(status_code=422, detail=result.get("message", "文件處理失敗"))

    proj_doc = ProjectDocument(
        project_id=project_id,
        user_id=current_user.id,
        doc_id=result["doc_id"],
        filename=file.filename,
        doc_title=file.filename,
        chunks_count=result.get("chunks_count", 0),
    )
    db.add(proj_doc)
    db.commit()
    db.refresh(proj_doc)

    return {
        "id": proj_doc.id,
        "doc_id": proj_doc.doc_id,
        "filename": proj_doc.filename,
        "chunks_count": proj_doc.chunks_count,
        "uploaded_at": proj_doc.uploaded_at.isoformat(),
    }


@router.delete("/projects/{project_id}/documents/{doc_id}", tags=["對話管理"], summary="刪除專案私有知識庫文件")
async def delete_project_document(
    project_id: int,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    project = db.query(Project).filter(Project.id == project_id, Project.user_id == current_user.id).first()
    if not project:
        raise HTTPException(status_code=404, detail="專案不存在")

    proj_doc = db.query(ProjectDocument).filter(
        ProjectDocument.project_id == project_id,
        ProjectDocument.doc_id == doc_id,
    ).first()
    if not proj_doc:
        raise HTTPException(status_code=404, detail="文件不存在")

    rag = get_rag()
    if rag:
        rag.delete_document(doc_id)

    db.delete(proj_doc)
    db.commit()
    return {"message": "文件已刪除"}


@router.patch("/conversations/{conversation_id}/star", tags=["對話管理"], summary="切換對話星號標記")
async def toggle_star(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    conv = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="對話不存在")
    conv.is_starred = not conv.is_starred
    db.commit()
    return {"id": conv.id, "is_starred": conv.is_starred}


@router.patch("/conversations/{conversation_id}/title", tags=["對話管理"], summary="重新命名對話標題")
async def rename_conversation(
    conversation_id: int,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    conv = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="對話不存在")
    title = (data.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="標題不得為空")
    conv.title = title[:200]
    db.commit()
    return {"id": conv.id, "title": conv.title}


@router.patch("/conversations/{conversation_id}/project", tags=["對話管理"], summary="將對話移入指定專案")
async def set_conversation_project(
    conversation_id: int,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    conv = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="對話不存在")
    project_id = data.get("project_id")
    if project_id is not None:
        project = db.query(Project).filter(Project.id == project_id, Project.user_id == current_user.id).first()
        if not project:
            raise HTTPException(status_code=404, detail="專案不存在")
    conv.project_id = project_id
    db.commit()
    return {"id": conv.id, "project_id": conv.project_id}
