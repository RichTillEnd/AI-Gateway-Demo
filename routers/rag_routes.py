"""
RAG 知識庫 API 路由
"""
import os
import shutil
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from database import get_db, User, RagCategory
from core import get_current_user, get_admin_user, get_rag, sanitize_filename, log_audit

router = APIRouter()

UPLOAD_DIR = "./rag_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
RAG_FILES_DIR = "./rag_files"
os.makedirs(RAG_FILES_DIR, exist_ok=True)


# ==================== RAG 知識庫 API ====================

@router.post("/rag/add-text", tags=["知識庫"], summary="直接貼上文字新增至知識庫（僅管理員）")
async def rag_add_text(
    request: dict,
    http_request: Request,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """
    管理員直接貼上文字新增到知識庫
    body: { "text": "...", "doc_title": "...", "category": "hr" }
    """
    text = request.get("text", "").strip()
    doc_title = request.get("doc_title", "").strip()
    category = request.get("category", "general")

    if not text:
        raise HTTPException(status_code=400, detail="內容不能為空")
    if not doc_title:
        raise HTTPException(status_code=400, detail="請提供文件標題")

    rag = get_rag()
    if not rag:
        raise HTTPException(status_code=500, detail="RAG 服務未啟用（請設定 OPENAI_KEY）")

    result = rag.add_text(text=text, doc_title=doc_title, category=category, uploaded_by=admin_user.id)
    if result.get("success"):
        log_audit(db, "RAG_UPLOAD", actor=admin_user,
                  resource_type="rag", resource_id=result.get("doc_id"),
                  details={"doc_title": doc_title, "category": category, "source": "text"},
                  ip_address=http_request.client.host if http_request.client else None,
                  user_agent=http_request.headers.get("user-agent"))
    return result


@router.post(
    "/rag/upload",
    tags=["知識庫"],
    summary="上傳文件至知識庫（僅管理員）",
    description="支援 PDF、DOCX、TXT、MD、Excel（xlsx/xls）。Excel 每列獨立向量化，保留結構化查詢能力。最大 20MB。",
    responses={
        400: {"description": "格式不支援或超過 20MB"},
        500: {"description": "RAG 服務未啟用（請設定 OPENAI_KEY）"},
    },
)
async def rag_upload_file(
    file: UploadFile = File(...),
    doc_title: str = Form(""),
    category: str = Form("general"),
    request: Request = None,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """管理員上傳文件（PDF/DOCX/TXT/MD/XLSX）到知識庫"""
    allowed = {"txt", "pdf", "docx", "md", "xlsx", "xls", "xlsm", "xlsb"}
    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"不支援 {ext}，請上傳 {', '.join(allowed)}")

    rag = get_rag()
    if not rag:
        raise HTTPException(status_code=500, detail="RAG 服務未啟用")

    safe_filename = sanitize_filename(file.filename)
    temp_path = os.path.join(UPLOAD_DIR, safe_filename)
    temp_path = os.path.realpath(temp_path)
    if not temp_path.startswith(os.path.realpath(UPLOAD_DIR)):
        raise HTTPException(status_code=400, detail="非法的檔案名稱")

    try:
        file_content = await file.read()
        if len(file_content) > 20 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="檔案超過 20MB 上限")
        with open(temp_path, "wb") as f:
            f.write(file_content)
        result = rag.add_document(
            file_path=temp_path,
            filename=safe_filename,
            doc_title=doc_title or file.filename,
            uploaded_by=admin_user.id,
            category=category,
            stored_path=""
        )
        if result.get("success") and result.get("doc_id"):
            doc_id = result["doc_id"]
            file_ext = safe_filename.rsplit(".", 1)[-1].lower() if "." in safe_filename else "bin"
            stored_path = os.path.join(RAG_FILES_DIR, f"{doc_id}.{file_ext}")
            shutil.copy2(temp_path, stored_path)
            rag_instance = get_rag()
            if rag_instance:
                try:
                    chunk_results = rag_instance.collection.get(where={"doc_id": doc_id})
                    if chunk_results["ids"]:
                        updated_metadatas = []
                        for meta in rag_instance.collection.get(
                            ids=chunk_results["ids"], include=["metadatas"]
                        )["metadatas"]:
                            meta["stored_path"] = stored_path
                            updated_metadatas.append(meta)
                        rag_instance.collection.update(
                            ids=chunk_results["ids"],
                            metadatas=updated_metadatas
                        )
                except Exception as _upd_e:
                    print(f"[RAG] stored_path 更新失敗（不影響功能）: {_upd_e}")
            result["stored_path"] = stored_path
        if result.get("success"):
            log_audit(db, "RAG_UPLOAD", actor=admin_user,
                      resource_type="rag", resource_id=result.get("doc_id"),
                      details={"filename": file.filename, "doc_title": doc_title or file.filename, "category": category},
                      ip_address=request.client.host if request and request.client else None,
                      user_agent=request.headers.get("user-agent") if request else None)
        return result

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"[RAG Upload Error] {traceback.format_exc()}")
        from core import log_error
        log_error("RAG_ERROR", f"RAG 上傳處理失敗", e, endpoint="/rag/upload", method="POST")
        return {"success": False, "message": f"上傳處理失敗：{str(e)}"}
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@router.get("/rag/documents", tags=["知識庫"], summary="列出知識庫所有文件（僅管理員）")
async def rag_list_documents(admin_user: User = Depends(get_admin_user)):
    """列出知識庫所有文件"""
    rag = get_rag()
    if not rag:
        return []
    return rag.list_documents()


@router.delete("/rag/documents/{doc_id}", tags=["知識庫"], summary="刪除知識庫文件（同步刪除原始檔）")
async def rag_delete_document(doc_id: str, request: Request, admin_user: User = Depends(get_admin_user), db: Session = Depends(get_db)):
    """刪除知識庫文件（同步刪除原始檔）"""
    rag = get_rag()
    if not rag:
        raise HTTPException(status_code=500, detail="RAG 服務未啟用")

    try:
        docs = rag.list_documents()
        stored_path = next((d.get("stored_path", "") for d in docs if d["doc_id"] == doc_id), "")
    except Exception:
        stored_path = ""

    doc_info = next((d for d in (rag.list_documents() if rag else []) if d["doc_id"] == doc_id), {})
    result = rag.delete_document(doc_id)

    if result.get("success") and stored_path and os.path.exists(stored_path):
        try:
            os.remove(stored_path)
        except Exception as _del_e:
            print(f"[RAG] 原始檔刪除失敗（不影響功能）: {_del_e}")

    if result.get("success"):
        log_audit(db, "RAG_DELETE", actor=admin_user,
                  resource_type="rag", resource_id=doc_id,
                  details={"doc_title": doc_info.get("doc_title"), "filename": doc_info.get("filename")},
                  ip_address=request.client.host if request.client else None,
                  user_agent=request.headers.get("user-agent"))

    return result


@router.get("/rag/documents/{doc_id}/download", tags=["知識庫"], summary="下載知識庫文件原始檔（僅管理員）")
async def rag_download_file(doc_id: str, admin_user: User = Depends(get_admin_user)):
    """下載 RAG 文件原始檔"""
    rag = get_rag()
    if not rag:
        raise HTTPException(status_code=500, detail="RAG 服務未啟用")

    docs = rag.list_documents()
    doc = next((d for d in docs if d["doc_id"] == doc_id), None)
    if not doc:
        raise HTTPException(status_code=404, detail="找不到該文件")

    stored_path = doc.get("stored_path", "")
    if not stored_path or not os.path.exists(stored_path):
        raise HTTPException(status_code=404, detail="原始檔案不存在（舊版文件尚未儲存原始檔）")

    return FileResponse(
        path=stored_path,
        filename=doc.get("filename", "document"),
        media_type="application/octet-stream"
    )


@router.get("/rag/documents/{doc_id}/preview", tags=["知識庫"], summary="預覽文件內容（從 ChromaDB 重組文字片段）")
async def rag_preview_file(doc_id: str, admin_user: User = Depends(get_admin_user)):
    """預覽 RAG 文件內容（從 ChromaDB 重組文字，適用所有文件含舊版）"""
    rag = get_rag()
    if not rag:
        raise HTTPException(status_code=500, detail="RAG 服務未啟用")

    docs = rag.list_documents()
    doc = next((d for d in docs if d["doc_id"] == doc_id), None)
    if not doc:
        raise HTTPException(status_code=404, detail="找不到該文件")

    chunks = rag.get_document_chunks(doc_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="無法取得文件內容")

    return {
        "doc_id": doc_id,
        "doc_title": doc.get("doc_title"),
        "filename": doc.get("filename"),
        "category": doc.get("category"),
        "total_chunks": len(chunks),
        "content": "\n\n---\n\n".join(chunks)
    }


@router.get("/rag/stats", tags=["知識庫"], summary="知識庫統計（文件數、向量片段總數）")
async def rag_stats(admin_user: User = Depends(get_admin_user)):
    """知識庫統計"""
    rag = get_rag()
    if not rag:
        return {"total_documents": 0, "total_chunks": 0}
    return rag.get_stats()


# ==================== RAG 分類管理 API ====================

DEFAULT_RAG_CATEGORIES = [
    {"name": "hr",        "label": "人事（HR）",   "description": "人事規定、請假辦法、薪資相關", "sort_order": 1},
    {"name": "policy",    "label": "政策規定",      "description": "公司政策、內部規範",           "sort_order": 2},
    {"name": "technical", "label": "技術文件",      "description": "技術手冊、操作說明",           "sort_order": 3},
    {"name": "general",   "label": "一般",          "description": "一般公告、其他資訊",           "sort_order": 4},
]

def ensure_default_categories(db: Session):
    """確保預設分類存在（只在資料表完全空白時才初始化，不覆蓋已刪除的分類）"""
    count = db.query(RagCategory).count()
    if count == 0:
        for cat in DEFAULT_RAG_CATEGORIES:
            db.add(RagCategory(**cat))
        db.commit()


@router.get("/admin/rag-categories", tags=["知識庫"], summary="取得所有 RAG 知識庫分類")
async def list_rag_categories(db: Session = Depends(get_db)):
    """取得所有啟用中的分類（登入用戶可呼叫，前端用來顯示分類選單）"""
    ensure_default_categories(db)
    cats = db.query(RagCategory).filter(RagCategory.is_active == True).order_by(RagCategory.sort_order).all()
    return [{"id": c.id, "name": c.name, "label": c.label, "description": c.description} for c in cats]


@router.post("/admin/rag-categories", tags=["知識庫"], summary="新增 RAG 分類（僅管理員）")
async def create_rag_category(
    request: dict,
    http_request: Request,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    name  = request.get("name", "").strip().lower()
    label = request.get("label", "").strip()
    desc  = request.get("description", "").strip()

    if not name or not label:
        raise HTTPException(status_code=400, detail="分類識別碼與顯示名稱不能為空")
    if not name.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="分類識別碼只能包含英文字母、數字、底線")
    if db.query(RagCategory).filter(RagCategory.name == name).first():
        raise HTTPException(status_code=400, detail=f"分類「{name}」已存在")

    max_order = db.query(RagCategory).count()
    cat = RagCategory(name=name, label=label, description=desc, sort_order=max_order + 1)
    db.add(cat)
    db.commit()
    db.refresh(cat)

    log_audit(db, "RAG_CATEGORY_CREATE", actor=admin_user,
              resource_type="rag_category", resource_id=cat.id,
              details={"name": name, "label": label},
              ip_address=http_request.client.host if http_request.client else None,
              user_agent=http_request.headers.get("user-agent"))

    return {"id": cat.id, "name": cat.name, "label": cat.label, "description": cat.description}


@router.delete("/admin/rag-categories/{cat_id}", tags=["知識庫"], summary="刪除 RAG 分類（僅管理員）")
async def delete_rag_category(
    cat_id: int,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    cat = db.query(RagCategory).filter(RagCategory.id == cat_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="分類不存在")

    rag = get_rag()
    if rag:
        try:
            all_docs = rag.list_documents()
            using = [d for d in all_docs if d.get("category") == cat.name]
            if using:
                names = "、".join(d.get("doc_title") or d.get("filename") or "未知" for d in using[:5])
                suffix = f" 等 {len(using)} 份" if len(using) > 5 else f"（共 {len(using)} 份）"
                raise HTTPException(
                    status_code=409,
                    detail=f"無法刪除：仍有文件使用此分類，請先刪除或重新分類這些文件：{names}{suffix}"
                )
        except HTTPException:
            raise
        except Exception as e:
            print(f"[RAG] 檢查分類使用情況失敗（允許繼續）: {e}")

    cat_name, cat_label = cat.name, cat.label
    db.delete(cat)
    db.commit()

    log_audit(db, "RAG_CATEGORY_DELETE", actor=admin_user,
              resource_type="rag_category", resource_id=cat_id,
              details={"name": cat_name, "label": cat_label},
              ip_address=request.client.host if request.client else None,
              user_agent=request.headers.get("user-agent"))

    return {"success": True, "message": f"分類「{cat_label}」已刪除"}


@router.put("/admin/users/{user_id}/rag-access", tags=["管理員"], summary="設定用戶可存取的 RAG 分類")
async def set_user_rag_access(
    user_id: int,
    request: dict,
    http_request: Request,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    """
    設定用戶可存取的知識庫分類。
    body: { "categories": ["hr", "general"] }
    傳空陣列 = 不可存取任何分類
    傳 null = 移除限制（可存取全部，僅建議給管理員）
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用戶不存在")

    categories = request.get("categories")
    if categories is None:
        user.allowed_rag_categories = None
    else:
        valid = {c.name for c in db.query(RagCategory).filter(RagCategory.is_active == True).all()}
        invalid = [c for c in categories if c not in valid]
        if invalid:
            raise HTTPException(status_code=400, detail=f"不存在的分類：{', '.join(invalid)}")
        user.allowed_rag_categories = ",".join(categories)

    db.commit()

    log_audit(db, "RAG_ACCESS_UPDATE", actor=admin_user,
              resource_type="rag_access", resource_id=user_id,
              target_user=user,
              details={"categories": categories},
              ip_address=http_request.client.host if http_request.client else None,
              user_agent=http_request.headers.get("user-agent"))

    return {
        "user_id": user_id,
        "username": user.username,
        "allowed_rag_categories": user.allowed_rag_categories
    }


@router.get("/admin/users/{user_id}/rag-access", tags=["管理員"], summary="查詢用戶的 RAG 存取權限")
async def get_user_rag_access(
    user_id: int,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用戶不存在")
    cats = [c.strip() for c in user.allowed_rag_categories.split(",") if c.strip()] \
           if user.allowed_rag_categories else []
    return {"user_id": user_id, "username": user.username, "allowed_rag_categories": cats}
