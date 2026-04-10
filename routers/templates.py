"""
Prompt 範本 / 用戶記憶 / 個人偏好 API 路由
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime
from pydantic import BaseModel
from database import get_db, User, PromptTemplate, UserMemory
from core import get_current_user, get_admin_user, log_audit, MEMORY_LIMIT

router = APIRouter()


# ==================== Pydantic Models ====================

class PromptTemplateCreate(BaseModel):
    title:       str
    description: Optional[str] = ""
    content:     str
    category:    Optional[str] = "一般"
    visibility:  Optional[str] = "all"
    sort_order:  Optional[int] = 0

class PromptTemplateUpdate(BaseModel):
    title:       Optional[str] = None
    description: Optional[str] = None
    content:     Optional[str] = None
    category:    Optional[str] = None
    visibility:  Optional[str] = None
    sort_order:  Optional[int] = None
    is_active:   Optional[bool] = None


# ==================== Helpers ====================

def _template_dict(t: PromptTemplate) -> dict:
    return {
        "id":          t.id,
        "title":       t.title,
        "description": t.description,
        "content":     t.content,
        "category":    t.category,
        "visibility":  t.visibility,
        "is_active":   t.is_active,
        "sort_order":  t.sort_order,
        "use_count":   t.use_count,
        "created_by":  t.created_by,
        "created_at":  t.created_at.isoformat() if t.created_at else None,
        "updated_at":  t.updated_at.isoformat() if t.updated_at else None,
    }


# ==================== Prompt 範本 API ====================

@router.get("/prompt-templates", tags=["Prompt 範本"], summary="取得啟用中的 Prompt 範本（一般用戶）")
async def list_prompt_templates(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """回傳所有 is_active=True 且 visibility=all 的範本，依 sort_order 排序。"""
    query = db.query(PromptTemplate).filter(PromptTemplate.is_active == True)
    if not current_user.is_admin:
        query = query.filter(PromptTemplate.visibility == "all")
    templates = query.order_by(PromptTemplate.sort_order.asc(), PromptTemplate.id.asc()).all()
    return [_template_dict(t) for t in templates]


@router.get("/admin/prompt-templates", tags=["管理員"], summary="取得全部 Prompt 範本（管理員）")
async def admin_list_prompt_templates(
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    templates = db.query(PromptTemplate).order_by(
        PromptTemplate.sort_order.asc(), PromptTemplate.id.asc()
    ).all()
    return [_template_dict(t) for t in templates]


@router.post("/admin/prompt-templates", tags=["管理員"], summary="建立 Prompt 範本")
async def create_prompt_template(
    body: PromptTemplateCreate,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    t = PromptTemplate(
        title=body.title,
        description=body.description,
        content=body.content,
        category=body.category,
        visibility=body.visibility,
        sort_order=body.sort_order,
        created_by=admin_user.id,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    log_audit(db, "PROMPT_CREATE", actor=admin_user,
              resource_type="prompt_template", resource_id=str(t.id),
              details={"title": t.title})
    return _template_dict(t)


@router.put("/admin/prompt-templates/{template_id}", tags=["管理員"], summary="更新 Prompt 範本")
async def update_prompt_template(
    template_id: int,
    body: PromptTemplateUpdate,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    t = db.query(PromptTemplate).filter(PromptTemplate.id == template_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="範本不存在")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(t, field, value)
    t.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(t)
    log_audit(db, "PROMPT_UPDATE", actor=admin_user,
              resource_type="prompt_template", resource_id=str(t.id),
              details={"title": t.title})
    return _template_dict(t)


@router.patch("/admin/prompt-templates/{template_id}/toggle", tags=["管理員"], summary="切換範本啟用狀態")
async def toggle_prompt_template(
    template_id: int,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    t = db.query(PromptTemplate).filter(PromptTemplate.id == template_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="範本不存在")
    t.is_active = not t.is_active
    t.updated_at = datetime.utcnow()
    db.commit()
    return {"id": t.id, "is_active": t.is_active}


@router.delete("/admin/prompt-templates/{template_id}", tags=["管理員"], summary="刪除 Prompt 範本")
async def delete_prompt_template(
    template_id: int,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    t = db.query(PromptTemplate).filter(PromptTemplate.id == template_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="範本不存在")
    log_audit(db, "PROMPT_DELETE", actor=admin_user,
              resource_type="prompt_template", resource_id=str(t.id),
              details={"title": t.title})
    db.delete(t)
    db.commit()
    return {"success": True}


@router.post("/prompt-templates/{template_id}/use", tags=["Prompt 範本"], summary="記錄範本套用（增加計數）")
async def record_template_use(
    template_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    t = db.query(PromptTemplate).filter(PromptTemplate.id == template_id).first()
    if t:
        t.use_count = (t.use_count or 0) + 1
        db.commit()
    return {"success": True}


# ==================== 用戶記憶管理（管理員） ====================

@router.get("/admin/users/{user_id}/memories", tags=["管理員"], summary="查看用戶記憶列表（僅管理員）")
async def get_user_memories(
    user_id: int,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用戶不存在")
    memories = db.query(UserMemory).filter(
        UserMemory.user_id == user_id
    ).order_by(UserMemory.last_referenced_at.desc()).all()
    return [
        {
            "id": m.id,
            "content": m.content,
            "category": m.category,
            "last_referenced_at": m.last_referenced_at.isoformat() if m.last_referenced_at else None,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in memories
    ]


@router.delete("/admin/users/{user_id}/memories/{memory_id}", tags=["管理員"], summary="刪除單條用戶記憶（僅管理員）")
async def delete_user_memory(
    user_id: int,
    memory_id: int,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    memory = db.query(UserMemory).filter(
        UserMemory.id == memory_id,
        UserMemory.user_id == user_id
    ).first()
    if not memory:
        raise HTTPException(status_code=404, detail="記憶不存在")
    log_audit(db, "MEMORY_DELETE", actor=admin_user,
              resource_type="user_memory", resource_id=str(memory_id),
              details={"user_id": user_id, "content": memory.content})
    db.delete(memory)
    db.commit()
    return {"success": True}


@router.delete("/admin/users/{user_id}/memories", tags=["管理員"], summary="清除用戶全部記憶（僅管理員）")
async def delete_all_user_memories(
    user_id: int,
    admin_user: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用戶不存在")
    count = db.query(UserMemory).filter(UserMemory.user_id == user_id).delete()
    log_audit(db, "MEMORY_DELETE_ALL", actor=admin_user,
              resource_type="user_memory", resource_id=str(user_id),
              details={"deleted_count": count})
    db.commit()
    return {"success": True, "deleted_count": count}


# ==================== 用戶記憶管理（用戶自助） ====================

@router.get("/me/memories", tags=["用戶"], summary="查看自己的記憶列表")
async def get_my_memories(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not current_user.memory_enabled:
        return {"memory_enabled": False, "memories": [], "memory_limit": MEMORY_LIMIT}
    memories = db.query(UserMemory).filter(
        UserMemory.user_id == current_user.id
    ).order_by(UserMemory.last_referenced_at.desc()).all()
    return {
        "memory_enabled": True,
        "memory_limit": MEMORY_LIMIT,
        "memories": [
            {
                "id": m.id,
                "content": m.content,
                "category": m.category,
                "last_referenced_at": m.last_referenced_at.isoformat() if m.last_referenced_at else None,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in memories
        ]
    }


@router.delete("/me/memories/{memory_id}", tags=["用戶"], summary="刪除自己的某條記憶")
async def delete_my_memory(
    memory_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    memory = db.query(UserMemory).filter(
        UserMemory.id == memory_id,
        UserMemory.user_id == current_user.id
    ).first()
    if not memory:
        raise HTTPException(status_code=404, detail="記憶不存在")
    db.delete(memory)
    db.commit()
    return {"success": True}


@router.put("/me/memory-enabled", tags=["用戶"], summary="開啟或關閉 AI 記憶功能")
async def set_memory_enabled(
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    enabled = bool(payload.get("enabled", True))
    current_user.memory_enabled = enabled
    db.commit()
    return {"success": True, "memory_enabled": enabled}


@router.get("/me/preferences", tags=["用戶"], summary="取得個人化偏好設定")
async def get_preferences(
    current_user: User = Depends(get_current_user)
):
    return {
        "work_type": current_user.work_type,
        "user_instructions": current_user.user_instructions or "",
    }


@router.put("/me/preferences", tags=["用戶"], summary="更新個人化偏好設定")
async def update_preferences(
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if "work_type" in payload:
        current_user.work_type = payload["work_type"] or None
    if "user_instructions" in payload:
        val = (payload["user_instructions"] or "").strip()
        current_user.user_instructions = val or None
    db.commit()
    return {"success": True}
