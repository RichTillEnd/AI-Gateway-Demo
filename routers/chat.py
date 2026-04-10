"""
聊天 API 路由（同步 / 串流 / 檔案）
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import Optional, AsyncGenerator
import asyncio
import os
import base64
from datetime import datetime
from database import get_db, User, Conversation, Message, Attachment, UsageLog, Project
from file_handler import (
    validate_file, save_upload_file, process_image_for_ai,
    extract_text_from_file, get_file_info
)
from pii_detector import scan_message as pii_scan, load_config as load_pii_config
from core import (
    get_current_user, log_error, log_audit,
    count_tokens, calculate_cost,
    OPENAI_MODEL, GEMINI_MODEL,
    openai_client, async_openai_client,
    ChatRequest, ChatResponse,
    _check_rate_and_quota, _process_pii, _retrieve_rag_context, _build_system_prompt,
    smart_route, _fallback_provider, _provider_model, _is_fallback_worthy,
    retry_sync, generate_conversation_title, extract_and_save_memories,
    summarize_old_history, check_custom_qa,
    cache_get, cache_set,
    safe_create_task,
)
from google import genai
from quota_manager import update_quota
from web_search import needs_web_search, tavily_search, build_search_context, WEB_SEARCH_ENABLED

router = APIRouter()

# Module-level singleton — avoids re-creating the client on every request
_gemini_client = genai.Client()


@router.post(
    "/chat",
    response_model=ChatResponse,
    tags=["對話"],
    summary="AI 對話（同步，等待完整回覆）",
    response_description="AI 回覆內容、使用模型、RAG 來源",
    responses={
        429: {"description": "請求速率超限"},
        403: {"description": "月度配額已用完"},
        503: {"description": "AI 服務暫時無法回應（已自動重試 3 次）"},
    },
)
async def chat(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    發送聊天訊息

    步驟：
    1. 取得或建立對話
    2. 儲存用戶訊息
    3. 呼叫 AI API
    4. 儲存 AI 回覆
    5. 記錄使用量
    """
    # 速率限制 + 配額檢查
    _check_rate_and_quota(current_user, request.message, db)

    # PII 偵測
    _safe_message = _process_pii(request.message, current_user)

    # ── 智慧路由：provider == "auto" 時自動決定 ──────────────
    _auto_reason = None
    if request.provider == "auto":
        request.provider, _auto_reason = smart_route(_safe_message)
        print(f"[SmartRoute] /api/chat → {request.provider}（{_auto_reason}）")

    # 1. 取得或建立對話
    if request.conversation_id:
        conversation = db.query(Conversation).filter(
            Conversation.id == request.conversation_id,
            Conversation.user_id == current_user.id
        ).first()

        if not conversation:
            raise HTTPException(status_code=404, detail="對話不存在")
    else:
        # 建立新對話（若有 project_id 則直接關聯）
        proj_id = request.project_id if request.project_id else None
        conversation = Conversation(
            user_id=current_user.id,
            title=_safe_message[:50],
            project_id=proj_id,
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    # 載入所屬專案（優先用 conversation.project_id，其次 request.project_id）
    _project_id = conversation.project_id or request.project_id
    project = db.query(Project).filter(Project.id == _project_id).first() if _project_id else None

    # RAG 知識庫查詢（專案知識庫設定優先）
    rag_system_prompt, rag_sources = _retrieve_rag_context(_safe_message, current_user, request.use_rag, project=project, db=db)

    # 組合 system prompt（含專案指示）
    system_prompt = _build_system_prompt(rag_system_prompt, current_user, db, project=project)

    # 網路搜尋（OpenAI path：Tavily 關鍵字觸發；Gemini path：grounding tool 由 AI 自動決定）
    _web_search_cost = 0.0
    if request.provider != "gemini" and needs_web_search(_safe_message):
        _sr = tavily_search(_safe_message)
        _web_ctx = build_search_context(_sr)
        if _web_ctx:
            system_prompt = _web_ctx + "\n\n" + system_prompt
            _web_search_cost = _sr.get("cost", 0.0)
            print(f"[WebSearch] Tavily 搜尋完成，注入 {len(_web_ctx)} 字元 context")
        elif _sr.get("error"):
            print(f"[WebSearch] Tavily 失敗：{_sr['error']}")

    # 2. 儲存用戶訊息（儲存遮蔽後的版本）
    user_message = Message(
        conversation_id=conversation.id,
        role="user",
        content=_safe_message
    )
    db.add(user_message)
    db.commit()

    # 載入對話歷史（與 stream 端點一致）
    _SYNC_HISTORY_LIMIT = 20
    _sync_history_msgs = db.query(Message).filter(
        Message.conversation_id == conversation.id,
        Message.id != user_message.id
    ).order_by(Message.created_at.asc()).all()
    _sync_history_msgs = _sync_history_msgs[-_SYNC_HISTORY_LIMIT:]
    chat_history = [{"role": m.role, "content": m.content} for m in _sync_history_msgs]
    chat_history.append({"role": "user", "content": _safe_message})

    # 3. 語意快取查詢（只在不使用 RAG 時啟用，RAG 結果受知識庫動態影響）
    _cache_hit = False
    if not request.use_rag:
        _cached = await cache_get(_safe_message)
        if _cached:
            _cache_hit = True
            response_text = _cached
            actual_provider = request.provider
            model_name = _provider_model(actual_provider)
            print(f"[SemanticCache] /api/chat 快取命中，跳過 AI 呼叫")

    # 4. 呼叫 AI API（含跨供應商自動 Fallback）
    actual_provider = request.provider
    model_name = _provider_model(actual_provider)
    if not _cache_hit:
        response_text = None
    ai_response = None
    _last_exc = None

    for _try_provider in [request.provider, _fallback_provider(request.provider)] if not _cache_hit else []:
        try:
            if _try_provider == "gemini":
                from google.genai import types
                _client = _gemini_client
                _model = _provider_model("gemini")
                _gemini_tools = [types.Tool(google_search=types.GoogleSearch())] if WEB_SEARCH_ENABLED else []
                gemini_contents = []
                for msg in chat_history:
                    g_role = "user" if msg["role"] == "user" else "model"
                    gemini_contents.append(
                        types.Content(role=g_role, parts=[types.Part(text=msg["content"])])
                    )
                _resp = await asyncio.to_thread(
                    retry_sync,
                    _client.models.generate_content,
                    model=_model,
                    contents=gemini_contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        tools=_gemini_tools,
                    ),
                    max_retries=3
                )
                response_text = _resp.text
                ai_response = _resp
            else:  # openai
                _model = _provider_model("openai")
                _resp = await asyncio.to_thread(
                    retry_sync,
                    openai_client.chat.completions.create,
                    model=_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        *chat_history
                    ],
                    max_retries=3
                )
                response_text = _resp.choices[0].message.content
                ai_response = _resp

            # 成功
            if _try_provider != request.provider:
                actual_provider = _try_provider
                model_name = _model
                print(f"[Fallback] /api/chat：{request.provider} → {actual_provider}")
                log_error("AI_FALLBACK",
                          f"Provider {request.provider} 不可用，已自動切換至 {actual_provider}",
                          _last_exc, endpoint="/chat", method="POST")
            else:
                model_name = _model
            break

        except Exception as e:
            _last_exc = e
            if _try_provider != request.provider:
                # 兩個供應商都失敗
                log_error("AI_API_ERROR", "AI API 呼叫失敗（含 Fallback 後仍失敗）", e, endpoint="/chat", method="POST")
                err = str(e).lower()
                if "429" in err or "rate_limit" in err:
                    raise HTTPException(status_code=503, detail="AI 服務請求量過高，請稍後約 1 分鐘再試")
                elif "401" in err or "invalid_api_key" in err:
                    raise HTTPException(status_code=500, detail="AI 服務金鑰錯誤，請聯絡系統管理員")
                elif "context_length" in err or "token" in err:
                    raise HTTPException(status_code=400, detail="訊息內容過長，請縮短問題或開啟新對話")
                else:
                    raise HTTPException(status_code=503, detail="AI 服務暫時無法回應，請稍後再試")
            if not _is_fallback_worthy(e):
                # 不值得 fallback（auth / context 錯誤），直接拋出
                log_error("AI_API_ERROR", "AI API 呼叫失敗", e, endpoint="/chat", method="POST")
                err = str(e).lower()
                if "429" in err or "rate_limit" in err:
                    raise HTTPException(status_code=503, detail="AI 服務請求量過高，請稍後約 1 分鐘再試")
                elif "401" in err or "invalid_api_key" in err:
                    raise HTTPException(status_code=500, detail="AI 服務金鑰錯誤，請聯絡系統管理員")
                elif "context_length" in err or "token" in err:
                    raise HTTPException(status_code=400, detail="訊息內容過長，請縮短問題或開啟新對話")
                else:
                    raise HTTPException(status_code=503, detail="AI 服務暫時無法回應，請稍後再試")
            # 值得 fallback，繼續嘗試下一個供應商
            print(f"[Fallback] {_try_provider} 失敗（{str(e)[:80]}），嘗試切換...")

    # 寫入語意快取（僅限新 AI 回應，快取命中不重複存）
    if not _cache_hit and response_text and not request.use_rag:
        safe_create_task(cache_set(_safe_message, response_text))

    # 計算實際 token 數與費用
    if _cache_hit:
        # 快取命中：以本地 tiktoken 估算，不計入 AI API 費用
        actual_input_tokens = count_tokens(_safe_message)
        actual_output_tokens = count_tokens(response_text)
    elif actual_provider == "gemini":
        # Gemini 不回傳 usage，用 tiktoken 估算（含 system prompt + 完整歷史）
        actual_input_tokens = count_tokens(system_prompt) + sum(
            count_tokens(m["content"]) for m in chat_history
        )
        actual_output_tokens = count_tokens(response_text)
    else:
        actual_input_tokens = ai_response.usage.prompt_tokens
        actual_output_tokens = ai_response.usage.completion_tokens
    total_tokens = actual_input_tokens + actual_output_tokens
    actual_cost = 0.0 if _cache_hit else calculate_cost(actual_input_tokens, actual_output_tokens, model_name)
    actual_cost += _web_search_cost

    # 更新配額
    try:
        update_quota(current_user, total_tokens, actual_cost, db, provider=actual_provider)
    except Exception as e:
        print(f"更新配額失敗: {e}")

    # 4. 儲存 AI 回覆
    ai_message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=response_text,
        provider=actual_provider,
        model=model_name
    )
    db.add(ai_message)
    db.commit()
    db.refresh(ai_message)

    # 5. 記錄使用量
    usage_log = UsageLog(
        user_id=current_user.id,
        provider=actual_provider,
        model=model_name,
        input_tokens=actual_input_tokens,
        output_tokens=actual_output_tokens,
        estimated_cost=actual_cost
    )
    db.add(usage_log)
    db.commit()

    # 更新對話時間
    conversation.updated_at = datetime.utcnow()
    db.commit()

    # 新對話才自動命名（舊對話標題已由前次生成，不覆蓋）
    if not request.conversation_id:
        safe_create_task(generate_conversation_title(
            conversation.id, _safe_message, response_text
        ))
    safe_create_task(extract_and_save_memories(current_user.id, _safe_message, response_text))

    return ChatResponse(
        message_id=ai_message.id,
        conversation_id=conversation.id,
        response=response_text,
        provider=actual_provider,
        model=model_name,
        rag_sources=rag_sources,
        auto_routed=_auto_reason
    )


# ==================== 檔案上傳 API ====================

@router.post(
    "/upload",
    tags=["檔案"],
    summary="上傳檔案（圖片 / 文件 / 程式碼）",
    response_description="檔案資訊與解析內容（base64 圖片或萃取文字）",
    responses={
        400: {"description": "檔案格式不支援或超過 50MB 限制"},
    },
)
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    上傳檔案

    步驟:
    1. 驗證檔案
    2. 儲存檔案
    3. 記錄到資料庫（暫時記錄，等發送訊息時關聯）
    4. 如果是圖片，處理成 base64
    5. 如果是文件，提取文字
    """

    # 讀取檔案內容
    file_content = await file.read()
    file_size = len(file_content)

    # 1. 驗證檔案
    valid, error_msg = validate_file(file.filename, file_size)
    if not valid:
        raise HTTPException(status_code=400, detail=error_msg)

    # 2. 儲存檔案
    try:
        filepath = save_upload_file(file_content, file.filename, current_user.id)
    except OSError as e:
        log_error("FILE_ERROR", f"儲存檔案失敗", e, endpoint="/upload", method="POST")
        raise HTTPException(status_code=500, detail=f"儲存檔案失敗: {str(e)}")

    # 3. 獲取檔案資訊
    file_info = get_file_info(filepath)

    # 4. 根據檔案類型處理
    response_data = {
        "filename": file.filename,
        "filepath": filepath,
        "file_size": file_size,
        "file_type": file_info['file_type'],
        "mime_type": file_info['mime_type']
    }

    # 如果是圖片，轉換成 base64
    if file_info['file_type'] == 'image':
        try:
            base64_image = process_image_for_ai(filepath)
            response_data['base64'] = base64_image
            response_data['preview_available'] = True
        except Exception as e:
            print(f"圖片處理錯誤: {str(e)}")
            response_data['preview_available'] = False

    # 如果是文件，提取文字
    elif file_info['file_type'] in ['document', 'code']:
        try:
            extracted_text = extract_text_from_file(filepath)
            if extracted_text:
                response_data['extracted_text'] = extracted_text
                response_data['text_preview'] = extracted_text[:500]  # 前 500 字預覽
        except Exception as e:
            print(f"文字提取錯誤: {str(e)}")

    return response_data


@router.post(
    "/chat-with-file",
    response_model=ChatResponse,
    tags=["檔案"],
    summary="帶附件的 AI 對話（圖片視覺分析 / 文件解析）",
    responses={
        400: {"description": "檔案格式不支援"},
        503: {"description": "AI 服務暫時無法回應"},
    },
)
async def chat_with_file(
    message: str = Form(...),
    provider: str = Form(...),
    conversation_id: Optional[int] = Form(None),
    file: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    發送帶有附件的聊天訊息

    這個 API 接受 multipart/form-data，可以同時上傳檔案和訊息
    """

    # 1. 處理檔案（如果有）
    file_data = None
    attachment_record = None

    if file:
        # 讀取並驗證檔案
        file_content = await file.read()
        file_size = len(file_content)

        valid, error_msg = validate_file(file.filename, file_size)
        if not valid:
            raise HTTPException(status_code=400, detail=error_msg)

        # 儲存檔案
        filepath = save_upload_file(file_content, file.filename, current_user.id)
        file_info = get_file_info(filepath)

        # 根據檔案類型處理
        if file_info['file_type'] == 'image':
            # 圖片：轉換成 base64 供 AI 使用
            base64_image = process_image_for_ai(filepath)
            file_data = {
                'type': 'image',
                'base64': base64_image,
                'filename': file.filename
            }
        elif file_info['file_type'] in ['document', 'code']:
            # 文件：提取文字
            extracted_text = extract_text_from_file(filepath)
            if extracted_text:
                file_data = {
                    'type': 'text',
                    'content': extracted_text,
                    'filename': file.filename
                }

        # 先暫存檔案資訊，等訊息建立後再關聯
        attachment_record = {
            'filename': file.filename,
            'filepath': filepath,
            'file_type': file_info['file_type'],
            'file_size': file_size,
            'mime_type': file_info['mime_type']
        }

    # 1b. 速率限制 + 配額檢查 + PII 偵測
    _check_rate_and_quota(current_user, message, db)
    safe_message = _process_pii(message, current_user)

    # 2. 取得或建立對話
    if conversation_id:
        conversation = db.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        ).first()
        if not conversation:
            raise HTTPException(status_code=404, detail="對話不存在")
    else:
        conversation = Conversation(
            user_id=current_user.id,
            title=safe_message[:50]
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    # 載入所屬專案
    _project_id = conversation.project_id
    project = db.query(Project).filter(Project.id == _project_id).first() if _project_id else None

    # 3. 儲存用戶訊息（儲存遮蔽後的版本）
    user_message = Message(
        conversation_id=conversation.id,
        role="user",
        content=safe_message
    )
    db.add(user_message)
    db.commit()
    db.refresh(user_message)

    # 4. 如果有附件，關聯到訊息
    if attachment_record:
        attachment = Attachment(
            message_id=user_message.id,
            **attachment_record
        )
        db.add(attachment)
        db.commit()

    # 5. 準備發送給 AI 的內容
    ai_content = safe_message

    # 如果有檔案，加入檔案內容
    if file_data:
        if file_data['type'] == 'text':
            ai_content = f"{safe_message}\n\n[附件: {file_data['filename']}]\n\n{file_data['content']}"
        elif file_data['type'] == 'image':
            ai_content = f"{safe_message}\n\n[附件: 圖片 {file_data['filename']}]"

    # 6. RAG 知識庫查詢（使用統一函數，含分類權限控管）
    rag_system_prompt, rag_sources = _retrieve_rag_context(safe_message, current_user, True, project=project, db=db)

    # 組合 system prompt（含專案指示 + RAG + AI 記憶 + 用戶偏好）
    system_prompt = _build_system_prompt(rag_system_prompt, current_user, db, project=project)

    # 載入對話歷史
    _CWF_HISTORY_LIMIT = 10
    history_messages = db.query(Message).filter(
        Message.conversation_id == conversation.id,
        Message.id != user_message.id
    ).order_by(Message.created_at.asc()).all()
    history_messages = history_messages[-_CWF_HISTORY_LIMIT:]
    chat_history = [{"role": m.role, "content": m.content} for m in history_messages]
    chat_history.append({"role": "user", "content": ai_content})

    # ── 智慧路由：provider == "auto" 時自動決定（有附件一律 openai）──
    _auto_reason = None
    if provider == "auto":
        provider, _auto_reason = smart_route(message, has_file=bool(file_data))
        print(f"[SmartRoute] /api/chat-with-file → {provider}（{_auto_reason}）")

    # ── 呼叫 AI（含 Fallback）──────────────────────────────────
    actual_provider = provider
    model_name = _provider_model(actual_provider)
    response_text = None
    _cwf_last_exc = None

    for _try_provider in [provider, _fallback_provider(provider)]:
        try:
            if _try_provider == "gemini":
                from google import genai as _genai_cwf
                from google.genai import types as _types_cwf
                _client_cwf = _genai_cwf.Client()
                _model_cwf = _provider_model("gemini")
                if file_data and file_data['type'] == 'image':
                    import PIL.Image, io
                    img_data = base64.b64decode(file_data['base64'])
                    img = PIL.Image.open(io.BytesIO(img_data))
                    gemini_contents = [safe_message, img]
                    _resp = await asyncio.to_thread(
                        _client_cwf.models.generate_content,
                        model=_model_cwf,
                        contents=gemini_contents,
                        config=_types_cwf.GenerateContentConfig(system_instruction=system_prompt)
                    )
                else:
                    gemini_contents = []
                    for msg in chat_history:
                        g_role = "user" if msg["role"] == "user" else "model"
                        gemini_contents.append(
                            _types_cwf.Content(role=g_role, parts=[_types_cwf.Part(text=msg["content"])])
                        )
                    _resp = await asyncio.to_thread(
                        _client_cwf.models.generate_content,
                        model=_model_cwf,
                        contents=gemini_contents,
                        config=_types_cwf.GenerateContentConfig(system_instruction=system_prompt)
                    )
                response_text = _resp.text

            else:  # openai
                _model_cwf = _provider_model("openai")
                if file_data and file_data['type'] == 'image':
                    _resp = await asyncio.to_thread(
                        openai_client.chat.completions.create,
                        model=_model_cwf,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            *chat_history[:-1],
                            {"role": "user", "content": [
                                {"type": "text", "text": ai_content},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{file_data['base64']}"}}
                            ]}
                        ]
                    )
                else:
                    _resp = await asyncio.to_thread(
                        openai_client.chat.completions.create,
                        model=_model_cwf,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            *chat_history
                        ]
                    )
                response_text = _resp.choices[0].message.content

            # 成功
            if _try_provider != provider:
                actual_provider = _try_provider
                model_name = _model_cwf
                print(f"[Fallback] /api/chat-with-file：{provider} → {actual_provider}")
                log_error("AI_FALLBACK",
                          f"Provider {provider} 不可用，已自動切換至 {actual_provider}（chat-with-file）",
                          _cwf_last_exc, endpoint="/chat-with-file", method="POST")
            else:
                model_name = _model_cwf
            break

        except Exception as e:
            _cwf_last_exc = e
            if _try_provider != provider or not _is_fallback_worthy(e):
                log_error("AI_API_ERROR", "AI API 呼叫失敗（含檔案）", e, endpoint="/chat-with-file", method="POST")
                raise HTTPException(status_code=500, detail=f"AI API 錯誤: {str(e)}")
            print(f"[Fallback] {_try_provider} 失敗（含檔案），嘗試切換...")

    # 7. 儲存 AI 回覆
    ai_message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=response_text,
        provider=actual_provider,
        model=model_name
    )
    db.add(ai_message)
    db.commit()
    db.refresh(ai_message)

    # 8. 記錄使用量（含 system prompt + 歷史）
    cwf_input_tokens = count_tokens(system_prompt) + sum(
        count_tokens(m["content"]) for m in chat_history
    )
    cwf_output_tokens = count_tokens(response_text)
    cwf_cost = calculate_cost(cwf_input_tokens, cwf_output_tokens, model_name)
    usage_log = UsageLog(
        user_id=current_user.id,
        provider=actual_provider,
        model=model_name,
        input_tokens=cwf_input_tokens,
        output_tokens=cwf_output_tokens,
        estimated_cost=cwf_cost
    )
    db.add(usage_log)
    db.commit()

    # 更新配額（含分項成本）
    try:
        cwf_tokens = cwf_input_tokens + cwf_output_tokens
        update_quota(current_user, cwf_tokens, cwf_cost, db, provider=actual_provider)
    except Exception as e:
        print(f"更新配額失敗: {e}")

    # 更新對話時間
    conversation.updated_at = datetime.utcnow()
    db.commit()

    # 新對話才自動命名
    if not conversation_id:
        safe_create_task(generate_conversation_title(
            conversation.id, safe_message, response_text
        ))
    safe_create_task(extract_and_save_memories(current_user.id, safe_message, response_text))

    return ChatResponse(
        message_id=ai_message.id,
        conversation_id=conversation.id,
        response=response_text,
        provider=actual_provider,
        model=model_name,
        rag_sources=rag_sources,
        auto_routed=_auto_reason
    )


async def _stream_ai_to_queue(
    chunk_queue: asyncio.Queue,
    provider: str,
    system_prompt: str,
    chat_history: list,
    conversation_id: int,
    user_id: int,
    safe_message: str,
    request_conversation_id: int | None,
    web_search_cost: float,
    rag_sources: list,
    auto_reason: str | None,
) -> None:
    """
    Background generation task — runs independently of the SSE connection.
    Puts SSE event dicts into chunk_queue; the SSE generator reads from it.
    When the client disconnects mid-stream, this task keeps running, finishes
    generating the full response, and saves it to DB (same behaviour as
    ChatGPT / Claude.ai "background generation").

    Uses its own DB session so it is not affected by the request lifecycle.
    queue.put_nowait() silently drops chunks when nobody is reading
    (client already disconnected), but full_response continues to accumulate.
    """
    from database import SessionLocal, Message, UsageLog, Conversation, User
    from quota_manager import update_quota as _update_quota

    db = SessionLocal()
    full_response = ""
    actual_provider = provider
    model_name = _provider_model(provider)

    async def _put(item) -> None:
        """Non-blocking put. Silently discards when queue is full (client gone)."""
        try:
            chunk_queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    try:
        _stream_last_exc = None
        _stream_success = False

        for _try_provider in [provider, _fallback_provider(provider)]:
            _try_model = _provider_model(_try_provider)
            _used_fallback = _try_provider != provider
            full_response = ""

            try:
                meta = {
                    "type": "meta",
                    "conversation_id": conversation_id,
                    "model": _try_model,
                    "provider": _try_provider,
                    "rag_sources": rag_sources,
                }
                if _used_fallback:
                    meta["fallback_from"] = provider
                if auto_reason:
                    meta["auto_routed"] = auto_reason
                await _put(meta)

                if _try_provider == "gemini":
                    from google.genai import types
                    _async_client = _gemini_client
                    gemini_contents = []
                    for msg in chat_history:
                        g_role = "user" if msg["role"] == "user" else "model"
                        gemini_contents.append(
                            types.Content(role=g_role, parts=[types.Part(text=msg["content"])])
                        )
                    _gemini_tools_bg = [types.Tool(google_search=types.GoogleSearch())] if WEB_SEARCH_ENABLED else []
                    stream = await _async_client.aio.models.generate_content_stream(
                        model=_try_model,
                        contents=gemini_contents,
                        config=types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            tools=_gemini_tools_bg,
                        )
                    )

                    async def _consume_gemini():
                        nonlocal full_response
                        async for chunk in stream:
                            if chunk.text:
                                full_response += chunk.text
                                await _put({"type": "chunk", "text": chunk.text})

                    await asyncio.wait_for(_consume_gemini(), timeout=300.0)

                else:  # openai
                    stream = await async_openai_client.chat.completions.create(
                        model=_try_model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            *chat_history,
                        ],
                        stream=True,
                    )

                    async def _consume_openai():
                        nonlocal full_response
                        async for chunk in stream:
                            delta = chunk.choices[0].delta.content if chunk.choices else None
                            if delta:
                                full_response += delta
                                await _put({"type": "chunk", "text": delta})

                    await asyncio.wait_for(_consume_openai(), timeout=300.0)

                if _used_fallback:
                    print(f"[Fallback] /api/chat/stream：{provider} → {_try_provider}")
                    log_error(
                        "AI_FALLBACK",
                        f"Provider {provider} 不可用，已自動切換至 {_try_provider}（Streaming）",
                        _stream_last_exc, endpoint="/chat/stream", method="POST",
                    )

                _stream_success = True
                actual_provider = _try_provider
                model_name = _try_model
                break

            except asyncio.TimeoutError:
                log_error("AI_API_TIMEOUT", "Streaming 背景任務超時 (300s)", None, endpoint="/chat/stream", method="POST")
                await _put({"type": "error", "message": "AI 生成超時（超過 5 分鐘），請稍後再試"})
                return
            except Exception as e:
                _stream_last_exc = e
                if _used_fallback or not _is_fallback_worthy(e):
                    log_error("AI_API_ERROR", "Streaming 失敗", e, endpoint="/chat/stream", method="POST")
                    err = str(e).lower()
                    if "429" in err or "rate_limit" in err:
                        user_msg = "AI 服務請求量過高，請稍後約 1 分鐘再試"
                    elif "401" in err or "invalid_api_key" in err:
                        user_msg = "AI 服務金鑰錯誤，請聯絡系統管理員"
                    elif "context_length" in err or "maximum context" in err:
                        user_msg = "訊息內容過長，請縮短問題或開啟新對話"
                    else:
                        user_msg = "AI 服務暫時無法回應，請稍後再試"
                    await _put({"type": "error", "message": user_msg})
                    return
                print(f"[Fallback] {_try_provider} 串流失敗（{str(e)[:80]}），嘗試切換...")
                await _put({"type": "clear"})
                await _put({"type": "notice", "text": f"⚠️ {_try_provider} 暫時無法連線，自動切換備援..."})
                continue

        if not _stream_success:
            return

        # ── 存入 DB（正常完成 或 客戶端已斷線 都執行）─────────────────
        _save_content = full_response if full_response else "（串流中斷，無回覆內容）"
        ai_message = Message(
            conversation_id=conversation_id,
            role="assistant",
            content=_save_content,
            provider=actual_provider,
            model=model_name,
        )
        db.add(ai_message)
        db.commit()
        db.refresh(ai_message)

        actual_input_tokens = count_tokens(system_prompt) + sum(
            count_tokens(m["content"]) for m in chat_history
        )
        actual_output_tokens = count_tokens(full_response) if full_response else 0
        total_tokens = actual_input_tokens + actual_output_tokens
        actual_cost = calculate_cost(actual_input_tokens, actual_output_tokens, model_name)
        actual_cost += web_search_cost

        _bg_user = db.query(User).filter(User.id == user_id).first()
        if _bg_user:
            try:
                _update_quota(_bg_user, total_tokens, actual_cost, db, provider=actual_provider)
            except Exception as _qe:
                print(f"[StreamBG] 更新配額失敗: {_qe}")
                db.rollback()  # 清除 dirty session，讓後續 commit 可以成功

        usage_log = UsageLog(
            user_id=user_id,
            provider=actual_provider,
            model=model_name,
            input_tokens=actual_input_tokens,
            output_tokens=actual_output_tokens,
            estimated_cost=actual_cost,
        )
        db.add(usage_log)
        _bg_conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if _bg_conv:
            _bg_conv.updated_at = datetime.utcnow()
        db.commit()

        if not request_conversation_id:
            safe_create_task(generate_conversation_title(
                conversation_id, safe_message, full_response or safe_message
            ))
        if full_response:
            safe_create_task(extract_and_save_memories(user_id, safe_message, full_response))

        await _put({"type": "done", "message_id": ai_message.id})

    except asyncio.CancelledError:
        # 伺服器 shutdown — 盡量把已有的回覆存下來
        if full_response:
            try:
                ai_message = Message(
                    conversation_id=conversation_id, role="assistant",
                    content=full_response, provider=actual_provider, model=model_name,
                )
                db.add(ai_message)
                db.commit()
            except Exception:
                pass
        print(f"[StreamBG] 被取消（shutdown？），已存 {len(full_response)} 字元")
    except Exception as _gen_err:
        print(f"[StreamBG] 生成失敗: {_gen_err}")
        await _put({"type": "error", "message": "AI 生成失敗，請稍後再試"})
    finally:
        await _put(None)  # sentinel — event_generator stops reading
        db.close()


@router.post(
    "/chat/stream",
    tags=["對話"],
    summary="AI 對話（SSE 串流，即時逐字輸出）",
    description="""
回應格式為 Server-Sent Events（SSE）：
```
data: {"type": "meta", "conversation_id": 1, "model": "gpt-5.4", "provider": "openai", "rag_sources": [...]}
data: {"type": "chunk", "text": "你好"}
data: {"type": "chunk", "text": "，有什麼"}
data: {"type": "done", "message_id": 42}
data: [DONE]
```
支援 Custom QA 攔截（命中關鍵字時直接回覆，不消耗 AI Token）。
    """,
    responses={
        429: {"description": "請求速率超限"},
        403: {"description": "月度配額已用完"},
    },
)
async def chat_stream(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Streaming 聊天 API（SSE 格式）

    回應格式（Server-Sent Events）：
      data: {"type": "meta", "conversation_id": 1, "model": "gpt-4.1-mini", "provider": "openai", "rag_sources": [...]}
      data: {"type": "chunk", "text": "你好"}
      data: {"type": "chunk", "text": "，有什麼"}
      ...
      data: {"type": "done", "message_id": 42}
      data: [DONE]
    """
    # 速率限制 + 配額檢查
    _check_rate_and_quota(current_user, request.message, db)

    # PII 偵測
    _safe_message = _process_pii(request.message, current_user)

    # 取得或建立對話（先取得對話才能載入專案設定）
    if request.conversation_id:
        conversation = db.query(Conversation).filter(
            Conversation.id == request.conversation_id,
            Conversation.user_id == current_user.id
        ).first()
        if not conversation:
            raise HTTPException(status_code=404, detail="對話不存在")
    else:
        proj_id = request.project_id if request.project_id else None
        conversation = Conversation(
            user_id=current_user.id,
            title=_safe_message[:50],
            project_id=proj_id,
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    # 載入所屬專案（優先用 conversation.project_id，其次 request.project_id）
    _project_id = conversation.project_id or request.project_id
    project = db.query(Project).filter(Project.id == _project_id).first() if _project_id else None

    # RAG 查詢（串流前先做好，來源資訊要隨第一個 chunk 傳出去；專案知識庫設定優先）
    rag_system_prompt, rag_sources = _retrieve_rag_context(
        _safe_message, current_user, request.use_rag, summarize_list=True, project=project, db=db
    )

    # 組合 system prompt（含專案指示）
    system_prompt = _build_system_prompt(rag_system_prompt, current_user, db, project=project)

    # 網路搜尋（OpenAI path：Tavily 關鍵字觸發；Gemini path：grounding tool 由 AI 自動決定）
    _stream_web_search_cost = 0.0
    if request.provider != "gemini" and needs_web_search(_safe_message):
        _sr = tavily_search(_safe_message)
        _web_ctx = build_search_context(_sr)
        if _web_ctx:
            system_prompt = _web_ctx + "\n\n" + system_prompt
            _stream_web_search_cost = _sr.get("cost", 0.0)
            print(f"[WebSearch/Stream] Tavily 搜尋完成，注入 {len(_web_ctx)} 字元 context")
        elif _sr.get("error"):
            print(f"[WebSearch/Stream] Tavily 失敗：{_sr['error']}")

    # 儲存用戶訊息（儲存遮蔽後的版本）
    user_message = Message(
        conversation_id=conversation.id,
        role="user",
        content=_safe_message
    )
    db.add(user_message)
    db.commit()

    # 載入對話歷史（Session Memory + 自動摘要壓縮）
    HISTORY_LIMIT = 20       # 資料庫最多取幾條歷史
    SUMMARY_THRESHOLD = 10   # 超過幾條時，把最舊那半批壓縮成摘要
    RECENT_KEEP = 6          # 壓縮後，保留最近幾條原始訊息不壓縮

    history_messages = db.query(Message).filter(
        Message.conversation_id == conversation.id,
        Message.id != user_message.id
    ).order_by(Message.created_at.asc()).all()
    history_messages = history_messages[-HISTORY_LIMIT:]

    raw_history = [{"role": m.role, "content": m.content} for m in history_messages]

    # ── 自動摘要壓縮：超過門檻才啟用 ────────────────────────────
    if len(raw_history) > SUMMARY_THRESHOLD:
        # 把較舊的那批壓縮，保留最近 RECENT_KEEP 條原始訊息
        to_summarize = raw_history[:-RECENT_KEEP]
        recent_raw   = raw_history[-RECENT_KEEP:]

        summary_text = await summarize_old_history(to_summarize)

        if summary_text:
            # 用一條 system 角色訊息代替舊歷史，節省大量 Token
            summary_msg = {
                "role": "system",
                "content": f"【對話摘要（前 {len(to_summarize)} 條訊息的重點）】\n{summary_text}"
            }
            chat_history = [summary_msg] + recent_raw
            print(f"[摘要] 歷史壓縮：{len(raw_history)} 條 → 摘要 + {len(recent_raw)} 條")
        else:
            # 摘要失敗，退回使用原始歷史
            chat_history = raw_history
    else:
        chat_history = raw_history

    chat_history.append({"role": "user", "content": _safe_message})

    # ── 智慧路由：provider == "auto" 時自動決定 ──────────────
    _auto_reason = None
    if request.provider == "auto":
        request.provider, _auto_reason = smart_route(_safe_message)
        print(f"[SmartRoute] /api/chat/stream → {request.provider}（{_auto_reason}）")

    # 決定 model name
    model_name = GEMINI_MODEL if request.provider == "gemini" else OPENAI_MODEL

    async def event_generator() -> AsyncGenerator[str, None]:
        import json

        full_response = ""

        # ── Custom QA 攔截：命中則直接回傳，不消耗 AI Token ──────────
        qa_result = check_custom_qa(_safe_message, db)
        if qa_result:
            custom_answer, rule_id = qa_result
            try:
                meta = {
                    "type": "meta",
                    "conversation_id": conversation.id,
                    "model": "custom_qa",
                    "provider": "system",
                    "rag_sources": []
                }
                yield f"data: {json.dumps(meta, ensure_ascii=False)}\n\n"

                # 逐字輸出，維持打字機 UX
                for char in custom_answer:
                    payload = {"type": "chunk", "text": char}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.015)

                # 存入資料庫
                ai_message = Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content=custom_answer,
                    provider="system",
                    model="custom_qa"
                )
                db.add(ai_message)

                # 更新命中次數
                from database import CustomQA
                rule = db.query(CustomQA).filter(CustomQA.id == rule_id).first()
                if rule:
                    rule.hit_count = (rule.hit_count or 0) + 1

                conversation.updated_at = datetime.utcnow()
                db.commit()
                db.refresh(ai_message)

                done = {"type": "done", "message_id": ai_message.id}
                yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

                # 新對話自動命名
                if not request.conversation_id:
                    safe_create_task(generate_conversation_title(
                        conversation.id, _safe_message, custom_answer
                    ))
                safe_create_task(extract_and_save_memories(current_user.id, _safe_message, custom_answer))
            except Exception as e:
                error = {"type": "error", "message": str(e)}
                yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            return  # 直接結束，不進入 AI 邏輯
        # ── Custom QA 結束，以下為正常 AI 流程 ──────────────────────

        # ── Text-to-SQL 攔截（資料查詢意圖偵測）────────────────────
        try:
            from text_to_sql import TextToSQLService
            _tsql = TextToSQLService(openai_api_key=os.getenv("OPENAI_KEY", ""))
            if _tsql.is_sql_query_intent(_safe_message):
                _tsql_result = await _tsql.process(
                    _safe_message, current_user.is_admin, current_user.id
                )
                _tsql_text = (
                    _tsql_result["formatted_text"]
                    if _tsql_result["success"]
                    else f"⚠️ 查詢失敗：{_tsql_result['error']}\n\n請嘗試換個方式描述您的問題。"
                )

                _tsql_meta = {
                    "type": "meta",
                    "conversation_id": conversation.id,
                    "model": "資料查詢",
                    "provider": "🗃️",
                    "rag_sources": [],
                    "is_sql_result": True,
                }
                yield f"data: {json.dumps(_tsql_meta, ensure_ascii=False)}\n\n"

                # 逐字輸出（打字機效果）
                for _char in _tsql_text:
                    yield f"data: {json.dumps({'type': 'chunk', 'text': _char}, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.006)

                # 儲存到資料庫
                _tsql_msg = Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content=_tsql_text,
                    provider="system",
                    model="text-to-sql",
                )
                db.add(_tsql_msg)
                conversation.updated_at = datetime.utcnow()
                db.commit()
                db.refresh(_tsql_msg)

                _tsql_done = {
                    "type": "done",
                    "message_id": _tsql_msg.id,
                    "generated_sql": _tsql_result.get("sql", "") if _tsql_result["success"] else "",
                }
                yield f"data: {json.dumps(_tsql_done, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

                if not request.conversation_id:
                    safe_create_task(generate_conversation_title(
                        conversation.id, _safe_message, _tsql_text
                    ))
                safe_create_task(extract_and_save_memories(current_user.id, _safe_message, _tsql_text))
                return  # 不進入 AI 流程
        except Exception as _tsql_err:
            print(f"[Text-to-SQL] 初始化或意圖偵測失敗，退回 AI 模式: {_tsql_err}")
        # ── Text-to-SQL 結束 ──────────────────────────────────────

        # ── 背景生成 + Queue 轉發 ────────────────────────────────────
        # 生成在獨立 task 進行，與 SSE 連線完全解耦。
        # 客戶端切換對話串 → GeneratorExit → 只停止轉發，背景 task 繼續生成完整回覆並存入 DB。
        _chunk_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        safe_create_task(_stream_ai_to_queue(
            chunk_queue=_chunk_queue,
            provider=request.provider,
            system_prompt=system_prompt,
            chat_history=chat_history,
            conversation_id=conversation.id,
            user_id=current_user.id,
            safe_message=_safe_message,
            request_conversation_id=request.conversation_id,
            web_search_cost=_stream_web_search_cost,
            rag_sources=rag_sources,
            auto_reason=_auto_reason,
        ))

        try:
            while True:
                try:
                    item = await asyncio.wait_for(_chunk_queue.get(), timeout=120.0)
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'error', 'message': 'AI 生成超時，請稍後再試'}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                if item is None:  # sentinel — background task finished
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                if item.get("type") == "error":
                    yield "data: [DONE]\n\n"
                    return
        except (GeneratorExit, asyncio.CancelledError):
            # 客戶端斷線（切換對話串）— 背景 task 繼續生成完整回覆並存入 DB
            print("[Stream] 客戶端斷線，背景繼續生成完整回覆並存入 DB...")
            return

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"   # 告訴 nginx 不要緩衝，立即轉發
        }
    )
