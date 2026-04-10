"""
圖像生成 API 路由
"""
import os
import asyncio
from typing import Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from database import get_db, User, Conversation, Message, UsageLog
from core import (
    get_current_user,
    OPENAI_IMAGE_MODEL, GOOGLE_IMAGE_MODEL,
    OPENAI_IMAGE_PROVIDER, GOOGLE_IMAGE_PROVIDER,
    SUPPORTED_IMAGE_MODELS, IMAGEN_ASPECT_RATIOS,
    calculate_image_cost, log_error,
    openai_client,
)
from quota_manager import check_quota, update_quota
from rate_limiter import check_rate_limit

router = APIRouter()

GENERATED_IMG_DIR = "./static/generated"
os.makedirs(GENERATED_IMG_DIR, exist_ok=True)


class ImageGenRequest(BaseModel):
    """圖像生成請求"""
    prompt: str
    model: str = OPENAI_IMAGE_MODEL
    quality: str = "medium"
    size: str = "1024x1024"
    n: int = 1
    conversation_id: Optional[int] = None


@router.post(
    "/image/generate",
    tags=["圖像生成"],
    summary="AI 圖像生成",
    description="""
支援兩種引擎：
- **OpenAI gpt-image-1**：低 / 中 / 高品質，3 種尺寸，依品質 × 尺寸計費（$0.011～$0.25/張）
- **Google Imagen 4.0 Ultra**：5 種比例，固定 $0.08/張

生成圖片永久儲存於伺服器，對話重載後仍可檢視。
    """,
    responses={
        400: {"description": "模型或尺寸參數不正確"},
        403: {"description": "費用配額不足"},
        429: {"description": "請求速率超限"},
        502: {"description": "AI 圖像生成失敗（可能觸發安全過濾）"},
    },
)
async def generate_image(
    request: ImageGenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    import openai as _openai_module
    try:
        from google.api_core import exceptions as _google_exc
    except ImportError:
        _google_exc = None

    # ── 基本驗證 ─────────────────────────────────────────────
    if request.model not in SUPPORTED_IMAGE_MODELS:
        raise HTTPException(status_code=400,
            detail=f"不支援的圖像模型：{request.model}。目前支援：{', '.join(SUPPORTED_IMAGE_MODELS)}")

    is_imagen = (request.model == GOOGLE_IMAGE_MODEL)

    if not is_imagen:
        VALID_QUALITIES = {"low", "medium", "high"}
        VALID_SIZES = {"1024x1024", "1024x1536", "1536x1024"}
        if request.quality not in VALID_QUALITIES:
            raise HTTPException(status_code=400, detail=f"quality 必須是 {VALID_QUALITIES} 其中之一")
        if request.size not in VALID_SIZES:
            raise HTTPException(status_code=400, detail=f"size 必須是 {VALID_SIZES} 其中之一")
    else:
        request.quality = "standard"
        VALID_IMAGEN_SIZES = set(IMAGEN_ASPECT_RATIOS.keys())
        if request.size not in VALID_IMAGEN_SIZES:
            raise HTTPException(status_code=400,
                detail=f"Imagen size 必須是 {VALID_IMAGEN_SIZES} 其中之一")

    n = max(1, min(request.n, 4))

    # ── 速率限制 & 配額預檢 ────────────────────────────────
    can_proceed, error_msg = check_rate_limit(current_user, db)
    if not can_proceed:
        raise HTTPException(status_code=429, detail=error_msg)

    estimated_cost = calculate_image_cost(request.model, request.quality, request.size, n)
    can_proceed, error_msg = check_quota(current_user, 0, estimated_cost, db)
    if not can_proceed:
        raise HTTPException(status_code=403, detail=error_msg)

    # ── 呼叫 AI API ───────────────────────────────────────
    images_b64: list[str] = []
    revised_prompt: str = request.prompt
    provider_label: str = ""
    image_urls: list[str] = []

    if not is_imagen:
        # ── OpenAI gpt-image-1 ────────────────────────────
        provider_label = OPENAI_IMAGE_PROVIDER
        try:
            api_response = await asyncio.to_thread(
                lambda: openai_client.images.generate(
                    model=request.model,
                    prompt=request.prompt,
                    quality=request.quality,
                    size=request.size,
                    n=n,
                )
            )
        except _openai_module.RateLimitError:
            raise HTTPException(status_code=429, detail="OpenAI 請求頻率超限，請稍後再試")
        except _openai_module.AuthenticationError:
            raise HTTPException(status_code=500, detail="OpenAI API Key 設定錯誤，請聯絡管理員")
        except _openai_module.BadRequestError as e:
            raise HTTPException(status_code=400, detail=f"圖像生成請求被拒絕（可能觸發內容政策）：{e}")
        except _openai_module.OpenAIError as e:
            log_error("AI_API_ERROR", f"{OPENAI_IMAGE_MODEL} 生成失敗", e,
                      user_id=current_user.id, endpoint="/image/generate", method="POST")
            raise HTTPException(status_code=502, detail=f"圖像生成失敗：{str(e)}")

        import base64 as _b64, uuid as _uuid
        for item in api_response.data:
            if not item.b64_json:
                continue
            fname = f"{_uuid.uuid4().hex}.png"
            fpath = os.path.join(GENERATED_IMG_DIR, fname)
            with open(fpath, "wb") as _f:
                _f.write(_b64.b64decode(item.b64_json))
            images_b64.append(f"data:image/png;base64,{item.b64_json}")
            image_urls.append(f"/static/generated/{fname}")
        if not images_b64:
            raise HTTPException(status_code=502, detail=f"{OPENAI_IMAGE_MODEL} 回傳空結果，請稍後重試")

        revised_prompt = getattr(api_response.data[0], "revised_prompt", request.prompt) or request.prompt

    else:
        # ── Google Imagen 4 Ultra ─────────────────────────
        provider_label = GOOGLE_IMAGE_PROVIDER
        gemini_key = os.getenv("GEMINI_API_KEY")
        if not gemini_key:
            raise HTTPException(status_code=500, detail="未設定 GEMINI_API_KEY，無法使用 Imagen")

        aspect_ratio = IMAGEN_ASPECT_RATIOS.get(request.size, "1:1")

        try:
            from google import genai as google_genai
            from google.genai import types as genai_types
            import base64 as _base64
            import uuid as _uuid

            imagen_client = google_genai.Client(api_key=gemini_key)

            def _call_imagen():
                return imagen_client.models.generate_images(
                    model=request.model,
                    prompt=request.prompt,
                    config=genai_types.GenerateImagesConfig(
                        number_of_images=n,
                        aspect_ratio=aspect_ratio,
                    ),
                )

            img_response = await asyncio.to_thread(_call_imagen)

            for gen_img in img_response.generated_images:
                raw_bytes = gen_img.image.image_bytes
                b64_str = _base64.b64encode(raw_bytes).decode("utf-8")
                images_b64.append(f"data:image/png;base64,{b64_str}")
                fname = f"{_uuid.uuid4().hex}.png"
                fpath = os.path.join(GENERATED_IMG_DIR, fname)
                with open(fpath, "wb") as _f:
                    _f.write(raw_bytes)
                image_urls.append(f"/static/generated/{fname}")

            if not images_b64:
                raise HTTPException(status_code=502,
                    detail="Imagen 回傳空結果，可能觸發安全過濾，請調整 prompt")

        except HTTPException:
            raise
        except Exception as e:
            if _google_exc:
                if isinstance(e, _google_exc.ResourceExhausted):
                    raise HTTPException(status_code=429, detail="Gemini API 配額已用盡，請稍後再試")
                if isinstance(e, _google_exc.PermissionDenied):
                    raise HTTPException(status_code=500, detail="Gemini API Key 無權限，請聯絡管理員")
                if isinstance(e, _google_exc.InvalidArgument):
                    raise HTTPException(status_code=400, detail=f"圖像生成請求無效（可能觸發內容政策）：{e}")
            log_error("AI_API_ERROR", "Imagen 4 生成失敗", e,
                      user_id=current_user.id, endpoint="/image/generate", method="POST")
            raise HTTPException(status_code=502, detail=f"Imagen 生成失敗：{str(e)}")

    # ── 記錄對話 & UsageLog ───────────────────────────────
    import json as _json
    actual_cost = calculate_image_cost(request.model, request.quality, request.size, n)

    if request.conversation_id:
        conversation = db.query(Conversation).filter(
            Conversation.id == request.conversation_id,
            Conversation.user_id == current_user.id
        ).first()
        if not conversation:
            raise HTTPException(status_code=404, detail="對話不存在")
    else:
        conversation = Conversation(
            user_id=current_user.id,
            title=f"🎨 {request.prompt[:40]}"
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    user_msg = Message(
        conversation_id=conversation.id,
        role="user",
        content=f"[圖像生成] {request.prompt}"
    )
    db.add(user_msg)

    ai_msg_content = _json.dumps({
        "type": "image_result",
        "image_urls": image_urls,
        "revised_prompt": revised_prompt,
        "model": request.model,
        "quality": request.quality,
        "size": request.size,
        "cost": round(calculate_image_cost(request.model, request.quality, request.size, n), 4),
        "count": len(images_b64),
    }, ensure_ascii=False)
    ai_msg = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=ai_msg_content,
        provider=provider_label,
        model=request.model
    )
    db.add(ai_msg)

    usage_log = UsageLog(
        user_id=current_user.id,
        provider=provider_label,
        model=request.model,
        input_tokens=0,
        output_tokens=0,
        estimated_cost=actual_cost
    )
    db.add(usage_log)
    conversation.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(ai_msg)

    try:
        update_quota(current_user, 0, actual_cost, db, provider=provider_label)
    except Exception as e:
        print(f"圖像配額更新失敗（不影響結果）: {e}")

    return {
        "images": images_b64,
        "revised_prompt": revised_prompt,
        "model": request.model,
        "quality": request.quality,
        "size": request.size,
        "cost": round(actual_cost, 4),
        "conversation_id": conversation.id,
        "message_id": ai_msg.id
    }
