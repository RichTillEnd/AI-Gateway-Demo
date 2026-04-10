"""
core.py — 共用常數、AI 客戶端、Helper 函數、Pydantic 模型、FastAPI 依賴注入

所有 router 從此模組 import；此模組不 import 任何 router 或 main.py。
"""
from fastapi import Depends, HTTPException, status, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from typing import Optional, List, AsyncGenerator
import asyncio
import time
import hashlib
import hmac as _hmac
import os
import base64
import re
import traceback as _traceback
from tracing import get_trace_id as _get_trace_id
import json as _json
from pydantic import BaseModel, field_validator
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime
from openai import OpenAI, AsyncOpenAI
import openai as _openai_module
from google import genai
import google.api_core.exceptions as _google_exc

from database import (
    get_db, SessionLocal,
    User, Conversation, Message, Attachment, UsageLog, ErrorLog,
    Project, UserQuota, CustomQA, RagCategory, ApiKey, AuditLog,
    PromptTemplate, UserMemory, RateLimit
)
from auth import (
    hash_password, verify_password, create_access_token, verify_token,
    decode_access_token, is_token_revoked,
    UserCreate, UserLogin, Token, UserResponse,
    validate_password_strength, validate_username, validate_email_domain
)
from rate_limiter import check_rate_limit, get_rate_limit_status
from quota_manager import check_quota, update_quota, get_quota_status
from rag_service import RAGService, build_rag_prompt
from pii_detector import scan_message as pii_scan, load_config as load_pii_config, save_config as save_pii_config
from semantic_cache import cache_get, cache_set, cache_clear, cache_stats

# ── Sentry 錯誤追蹤（需設定 SENTRY_DSN 環境變數才會啟用）──
import sentry_sdk
_SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if _SENTRY_DSN:
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_RATE", "0.1")),
        environment=os.getenv("ENVIRONMENT", "production"),
        release=f"ai-gateway@2.0.0",
        send_default_pii=False,
    )
    print(f"[Sentry] 已啟用（environment={os.getenv('ENVIRONMENT', 'production')}）")

# ── Background task 安全管理（防止 GC 提前回收）──
_background_tasks: set = set()

def safe_create_task(coro) -> asyncio.Task:
    """建立 background task 並保持 reference 直到完成，防止 GC 回收。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ── API Key HMAC pepper ──
_API_KEY_PEPPER = os.getenv("API_KEY_PEPPER", "")

def _hash_api_key(raw_key: str) -> str:
    """HMAC-SHA256 with server-side pepper（有設定 pepper 時），否則 fallback 到 SHA-256"""
    if _API_KEY_PEPPER:
        return _hmac.new(_API_KEY_PEPPER.encode(), raw_key.encode(), hashlib.sha256).hexdigest()
    return hashlib.sha256(raw_key.encode()).hexdigest()

# ── 初始化 AI 客戶端 ──
openai_client = OpenAI(api_key=os.getenv("OPENAI_KEY"))
async_openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_KEY"))

# ── HTTP Bearer / API Key 認證 ──
security = HTTPBearer()
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# ── RAG 服務初始化 ──
def get_rag():
    api_key = os.getenv("OPENAI_KEY")
    if api_key:
        return RAGService(openai_api_key=api_key)
    return None

def sanitize_filename(filename: str) -> str:
    """防止 Path Traversal 攻擊的檔名清理（含 Windows 反斜線路徑）"""
    filename = filename.replace("\\", "/")
    filename = os.path.basename(filename)
    filename = re.sub(r'[^\w\u4e00-\u9fff\-\.]', '_', filename)
    filename = filename.lstrip('.')
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:196] + ext
    return filename or 'upload'

# ╔══════════════════════════════════════════════════════════╗
# ║            AI 模型設定（統一在這裡修改）                 ║
# ║  可透過環境變數覆寫，無需修改程式碼：                    ║
# ║    OPENAI_MODEL=gpt-4o                                   ║
# ║    GEMINI_MODEL=gemini-2.0-flash                         ║
# ║    OPENAI_IMAGE_MODEL=gpt-image-1                        ║
# ║    GOOGLE_IMAGE_MODEL=imagen-3.0-generate-001            ║
# ╚══════════════════════════════════════════════════════════╝

OPENAI_MODEL       = os.getenv("OPENAI_MODEL",       "gpt-5.4")
GEMINI_MODEL       = os.getenv("GEMINI_MODEL",       "gemini-3.1-flash-lite-preview")

OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1.5")
GOOGLE_IMAGE_MODEL = os.getenv("GOOGLE_IMAGE_MODEL", "imagen-4.0-ultra-generate-001")

OPENAI_IMAGE_PROVIDER = "openai"
GOOGLE_IMAGE_PROVIDER = "gemini"

SUPPORTED_IMAGE_MODELS = {OPENAI_IMAGE_MODEL, GOOGLE_IMAGE_MODEL}

MODEL_PRICING = {
    "gpt-4.1":            {"input": 2.00,  "output": 8.00},
    "gpt-4.1-mini":       {"input": 0.40,  "output": 1.60},
    "gpt-4.1-nano":       {"input": 0.10,  "output": 0.40},
    "gpt-4o":             {"input": 2.50,  "output": 10.00},
    "gpt-4o-mini":        {"input": 0.15,  "output": 0.60},
    "gpt-5-mini":         {"input": 0.25,  "output": 2.00},
    "gpt-5.1":            {"input": 1.25,  "output": 10.00},
    "gpt-5.2":            {"input": 1.75,  "output": 14.00},
    "gpt-5.3-chat-latest":{"input": 1.75,  "output": 14.00},
    "gpt-5.4":            {"input": 2.50,  "output": 15.00},
    "gemini-3.1-pro-preview":       {"input": 2.00,  "output": 12.00},
    "gemini-3.1-flash-lite-preview":{"input": 0.25,  "output": 1.50},
    "gemini-3-flash-preview":       {"input": 0.50,  "output": 3.00},
    "gemini-2.5-flash":             {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash":             {"input": 0.10,  "output": 0.40},
    "gemini-1.5-flash":             {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro":               {"input": 1.25,  "output": 5.00},
}

IMAGE_PRICING = {
    OPENAI_IMAGE_MODEL: {
        ("low",    "1024x1024"): 0.011,
        ("low",    "1024x1792"): 0.016,
        ("low",    "1792x1024"): 0.016,
        ("medium", "1024x1024"): 0.042,
        ("medium", "1024x1792"): 0.063,
        ("medium", "1792x1024"): 0.063,
        ("high",   "1024x1024"): 0.167,
        ("high",   "1024x1792"): 0.250,
        ("high",   "1792x1024"): 0.250,
    },
    GOOGLE_IMAGE_MODEL: {
        ("standard", "1024x1024"): 0.08,
        ("standard", "896x1280"):  0.08,
        ("standard", "1280x896"):  0.08,
        ("standard", "1408x896"):  0.08,
        ("standard", "896x1408"):  0.08,
    },
}

IMAGEN_ASPECT_RATIOS = {
    "1024x1024": "1:1",
    "896x1280":  "3:4",
    "1280x896":  "4:3",
    "1408x896":  "16:9",
    "896x1408":  "9:16",
}

def calculate_image_cost(model: str, quality: str, size: str, n: int = 1) -> float:
    pricing = IMAGE_PRICING.get(model, {})
    per_image = pricing.get((quality, size), 0.04)
    return per_image * n


def _is_retryable(e: Exception) -> bool:
    """判斷 exception 是否值得重試（基於 exception type，不靠字串匹配）"""
    # OpenAI SDK exceptions
    if isinstance(e, _openai_module.RateLimitError):
        return True
    if isinstance(e, _openai_module.InternalServerError):
        return True
    if isinstance(e, (_openai_module.APIConnectionError, _openai_module.APITimeoutError)):
        return True
    # OpenAI — 不可重試
    if isinstance(e, (_openai_module.AuthenticationError, _openai_module.BadRequestError)):
        return False
    # Google API exceptions
    if isinstance(e, (_google_exc.TooManyRequests, _google_exc.ResourceExhausted)):
        return True
    if isinstance(e, (_google_exc.ServiceUnavailable, _google_exc.InternalServerError, _google_exc.DeadlineExceeded)):
        return True
    # Google — 不可重試
    if isinstance(e, (_google_exc.Unauthenticated, _google_exc.InvalidArgument, _google_exc.PermissionDenied)):
        return False
    # 未知 exception：fallback 到字串匹配（相容第三方）
    err = str(e).lower()
    if any(x in err for x in ["timeout", "connection", "unavailable"]):
        return True
    return False


def _is_fallback_worthy(e: Exception) -> bool:
    """判斷是否值得切換備援供應商（auth / context length 錯誤不切）"""
    # 明確不切的情況
    if isinstance(e, (_openai_module.AuthenticationError, _openai_module.BadRequestError)):
        return False
    if isinstance(e, (_google_exc.Unauthenticated, _google_exc.InvalidArgument, _google_exc.PermissionDenied)):
        return False
    # 字串 fallback（context_length 等 BadRequest 子情境）
    err = str(e).lower()
    if "context_length" in err or "maximum context" in err:
        return False
    return _is_retryable(e)

def _fallback_provider(provider: str) -> str:
    return "gemini" if provider == "openai" else "openai"

def _provider_model(provider: str) -> str:
    return GEMINI_MODEL if provider == "gemini" else OPENAI_MODEL


def smart_route(message: str, has_file: bool = False) -> tuple[str, str]:
    msg_len = len(message)
    if has_file:
        return "openai", "附件分析"
    if msg_len > 4000:
        return "gemini", "長文本"
    _creative = ['寫作', '創意', '故事', '詩', '劇本', '文案', '行銷', '廣告', '標語', '修辭']
    if any(kw in message for kw in _creative):
        return "openai", "創意寫作"
    _analysis = ['分析', '整理', '列出', '比較', '統計', '摘要', '總結', '翻譯', '表格', '條列']
    if any(kw in message for kw in _analysis):
        return "gemini", "資料整理"
    if msg_len < 50:
        return "gemini", "簡短問題"
    return "openai", "預設"


def retry_sync(func, *args, max_retries: int = 3, base_delay: float = 1.0, **kwargs):
    last_exc = None
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if not _is_retryable(e) or attempt == max_retries - 1:
                raise
            wait = base_delay * (2 ** attempt)
            print(f"[Retry] 第 {attempt + 1} 次失敗，{wait:.0f}s 後重試... ({str(e)[:80]})")
            time.sleep(wait)
    raise last_exc


def count_tokens(text: str) -> int:
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except ImportError:
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other_chars = len(text) - chinese_chars
        return int(chinese_chars * 1.5 + other_chars * 0.3)

def calculate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        return (input_tokens + output_tokens) * 0.000001
    return (
        input_tokens  * pricing["input"]  / 1_000_000 +
        output_tokens * pricing["output"] / 1_000_000
    )


def log_error(
    error_type: str,
    error_message: str,
    e: Exception = None,
    *,
    user_id: int = None,
    endpoint: str = None,
    method: str = None,
    request_data: str = None,
    ip_address: str = None,
    user_agent: str = None,
    db_session=None
) -> None:
    try:
        own_session = db_session is None
        db = db_session or SessionLocal()
        try:
            entry = ErrorLog(
                error_type=error_type,
                error_message=str(error_message)[:1000],
                error_detail=str(e) if e else None,
                stack_trace=_traceback.format_exc() if e else None,
                user_id=user_id,
                endpoint=endpoint,
                method=method,
                request_data=str(request_data)[:500] if request_data else None,
                ip_address=ip_address,
                user_agent=user_agent,
                trace_id=_get_trace_id(),
            )
            db.add(entry)
            db.commit()
        finally:
            if own_session:
                db.close()
        if e and _SENTRY_DSN:
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("error_type", error_type)
                if user_id:
                    scope.set_user({"id": user_id})
                if endpoint:
                    scope.set_tag("endpoint", endpoint)
                sentry_sdk.capture_exception(e)
    except Exception as _log_exc:
        print(f"[ErrorLog] 寫入失敗: {_log_exc}")


def log_audit(
    db,
    action: str,
    actor: "User" = None,
    *,
    resource_type: str = None,
    resource_id: str = None,
    target_user: "User" = None,
    details: dict = None,
    ip_address: str = None,
    user_agent: str = None,
) -> None:
    try:
        entry = AuditLog(
            actor_id=actor.id if actor else None,
            actor_email=actor.email if actor else None,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            target_user_id=target_user.id if target_user else None,
            target_user_email=target_user.email if target_user else None,
            details=_json.dumps(details, ensure_ascii=False) if details else None,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.add(entry)
        db.commit()
    except Exception as _audit_exc:
        print(f"[AuditLog] 寫入失敗: {_audit_exc}")


_summary_cache: dict[str, str] = {}   # hash → summary text
_SUMMARY_CACHE_MAX = 200

async def summarize_old_history(messages: list[dict]) -> str:
    if not messages:
        return ""

    # 用歷史內容 hash 做快取 key，同一段歷史不重複呼叫 AI
    history_text = "\n".join(
        f"{'用戶' if m['role'] == 'user' else 'AI'}：{m['content'][:300]}"
        for m in messages
    )
    cache_key = hashlib.md5(history_text.encode()).hexdigest()
    if cache_key in _summary_cache:
        print(f"[摘要] 快取命中（{len(messages)} 條歷史）")
        return _summary_cache[cache_key]

    try:
        prompt = f"""請將以下對話歷史壓縮成一段簡短摘要（繁體中文，150字以內）。
保留關鍵資訊、重要結論、用戶提到的具體數據或名詞。
只回傳摘要內容，不要任何前言或說明。

對話歷史：
{history_text}"""
        response = await asyncio.to_thread(
            lambda: openai_client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=200,
                temperature=0.3
            )
        )
        summary = response.choices[0].message.content.strip()
        print(f"[摘要] 壓縮 {len(messages)} 條歷史 → {len(summary)} 字摘要")

        # 存入快取（LRU：超限時清除最早的一半）
        if len(_summary_cache) >= _SUMMARY_CACHE_MAX:
            keys_to_remove = list(_summary_cache.keys())[:_SUMMARY_CACHE_MAX // 2]
            for k in keys_to_remove:
                _summary_cache.pop(k, None)
        _summary_cache[cache_key] = summary

        return summary
    except Exception as e:
        print(f"[摘要] 生成失敗，退回原始歷史: {e}")
        return ""


async def generate_conversation_title(
    conversation_id: int,
    user_message: str,
    ai_response: str,
) -> None:
    try:
        prompt = f"""根據以下對話，生成一個簡潔的繁體中文標題（6~12字，不加引號、不加標點符號結尾）：

用戶：{user_message[:200]}
AI：{ai_response[:200]}

只回傳標題文字，不要任何其他說明。"""
        response = await asyncio.to_thread(
            lambda: openai_client.chat.completions.create(
                model="gpt-5.2",
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=30,
                temperature=0.3
            )
        )
        title = response.choices[0].message.content.strip()
        title = title.strip('"\'「」').strip()
        if len(title) > 30:
            title = title[:30]
        if title:
            from database import SessionLocal
            async_db = SessionLocal()
            try:
                conv = async_db.query(Conversation).filter(
                    Conversation.id == conversation_id
                ).first()
                if conv:
                    conv.title = title
                    async_db.commit()
                    print(f"[標題] 對話 {conversation_id}：{title}")
            finally:
                async_db.close()
    except Exception as e:
        print(f"[標題] 生成失敗（不影響對話）: {e}")


MEMORY_LIMIT = 20

_WORK_TYPE_LABELS = {
    "software_engineer": "軟體工程師/開發者",
    "manager":           "管理職/主管",
    "sales":             "業務/銷售",
    "designer":          "設計師",
    "analyst":           "資料分析師",
    "hr":                "人資/行政",
    "marketing":         "行銷",
    "finance":           "財務/會計",
    "operations":        "營運",
    "other":             "其他",
}

_WORK_TYPE_BEHAVIOR = {
    "software_engineer": (
        "用戶是軟體工程師/開發者。請遵守以下行為準則：\n"
        "- 可直接使用技術術語，無需解釋基礎概念\n"
        "- 程式碼範例請給完整、可直接執行的版本，並標注語言\n"
        "- 回答簡潔直接，省略鋪陳與勵志語句\n"
        "- 遇到架構或設計問題時，主動列出優缺點與取捨\n"
        "- 指令操作請用 code block 格式呈現"
    ),
    "manager": (
        "用戶是管理職/主管。請遵守以下行為準則：\n"
        "- 優先給出結論與建議，細節放在後面\n"
        "- 使用條列式或摘要結構，方便快速閱讀\n"
        "- 避免過度技術細節，聚焦在影響、風險與決策面\n"
        "- 涉及跨部門協作、時程、優先順序時主動提出考量\n"
        "- 語氣專業但不過度正式"
    ),
    "sales": (
        "用戶是業務/銷售人員。請遵守以下行為準則：\n"
        "- 回答以實用性為優先，貼近實際情境\n"
        "- 客戶溝通、話術、提案相關問題請給可直接套用的範本\n"
        "- 語氣親切有說服力，避免冷冰冰的技術語言\n"
        "- 數字與成效要具體，避免模糊描述\n"
        "- 主動考量客戶心理與異議處理"
    ),
    "designer": (
        "用戶是設計師。請遵守以下行為準則：\n"
        "- 視覺、UX、品牌相關問題請提供具體且有邏輯的建議\n"
        "- 可使用設計術語（版面、字重、留白、hierarchy 等）\n"
        "- 回答時考量視覺美感與使用者體驗的平衡\n"
        "- 提供靈感或方向時，可適當舉例說明風格參考\n"
        "- 避免過度技術性的工程說明"
    ),
    "analyst": (
        "用戶是資料分析師。請遵守以下行為準則：\n"
        "- 可直接使用統計、資料科學術語\n"
        "- SQL、Python（pandas/numpy）相關問題請給完整可執行範例\n"
        "- 回答時注重數據準確性與方法論的嚴謹\n"
        "- 涉及圖表或視覺化時主動建議適合的呈現方式\n"
        "- 結論要有數據支撐，避免空泛描述"
    ),
    "hr": (
        "用戶是人資/行政人員。請遵守以下行為準則：\n"
        "- 涉及勞動法規、制度設計請以台灣法規為預設\n"
        "- 員工溝通、面談、文件範本等請給可直接使用的草稿\n"
        "- 語氣溫和、中立，考量不同立場的感受\n"
        "- 流程與規範說明請條列清楚，方便製作 SOP\n"
        "- 敏感人事議題請注重保密性與合規性"
    ),
    "marketing": (
        "用戶是行銷人員。請遵守以下行為準則：\n"
        "- 文案、廣告、社群貼文相關問題請直接給可用的版本\n"
        "- 語氣生動、有創意，符合品牌溝通風格\n"
        "- 策略建議要結合目標受眾與渠道特性\n"
        "- 數據分析面向（CTR、ROAS、轉換率等）可直接使用術語\n"
        "- 主動考量競品差異化與市場定位"
    ),
    "finance": (
        "用戶是財務/會計人員。請遵守以下行為準則：\n"
        "- 財務術語、會計科目可直接使用，無需額外解釋\n"
        "- 計算與數字請確保精確，並說明假設條件\n"
        "- 涉及台灣稅務、會計準則請以本地規範為預設\n"
        "- 報表、Excel 公式等請給具體可操作的步驟\n"
        "- 風險與合規性考量要主動提出"
    ),
    "operations": (
        "用戶是營運人員。請遵守以下行為準則：\n"
        "- 流程優化、SOP、問題排查請給具體可執行的步驟\n"
        "- 回答要考量跨部門協作與資源限制的現實\n"
        "- 效率、成本、品質的三角取捨要主動說明\n"
        "- 數字與 KPI 要具體，避免模糊的「提升效率」等說法\n"
        "- 語氣務實，聚焦在解決問題而非理論"
    ),
    "other": (
        "用戶已設定職業為「其他」。請根據對話內容自動判斷其背景與需求，\n"
        "適時調整回答的深度、語氣與術語選擇。"
    ),
}

def get_user_preferences_for_prompt(user: User) -> str:
    parts = []
    if user.work_type:
        behavior = _WORK_TYPE_BEHAVIOR.get(user.work_type)
        if behavior:
            parts.append(behavior)
        else:
            label = _WORK_TYPE_LABELS.get(user.work_type, user.work_type)
            parts.append(f"用戶職業角色：{label}")
    if user.user_instructions:
        parts.append(f"用戶個人指令（請嚴格遵守）：{user.user_instructions}")
    if not parts:
        return ""
    return "【用戶偏好設定】\n" + "\n".join(parts)


def get_user_memories_for_prompt(user_id: int, db) -> str:
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.memory_enabled:
        return ""
    memories = db.query(UserMemory).filter(
        UserMemory.user_id == user_id
    ).order_by(UserMemory.last_referenced_at.desc()).all()
    if not memories:
        return ""
    lines = [f"- {m.content}" for m in memories]
    return "【關於此用戶的已知資訊】\n" + "\n".join(lines)


async def extract_and_save_memories(user_id: int, user_message: str, ai_response: str) -> None:
    try:
        async_db = SessionLocal()
        try:
            _user = async_db.query(User).filter(User.id == user_id).first()
            if not _user or not _user.memory_enabled:
                return

            existing = async_db.query(UserMemory).filter(
                UserMemory.user_id == user_id
            ).order_by(UserMemory.last_referenced_at.desc()).all()

            existing_list = "\n".join(
                [f"[id={m.id}] {m.content}" for m in existing]
            ) if existing else "（目前無記憶）"

            prompt = f"""你是一個記憶擷取助理。請分析以下對話，找出值得長期記住的用戶個人資訊。
可記憶的類型：職業/身份(profession)、生活習慣(habit)、健康狀況(health)、偏好(preference)、重要事件(life_event)、其他(other)。

現有記憶：
{existing_list}

新對話：
用戶：{user_message[:500]}
AI：{ai_response[:300]}

請回傳 JSON，格式如下（若無可記憶資訊則 memories 為空陣列）：
{{
  "memories": [
    {{
      "content": "用戶是軟體工程師",
      "category": "profession",
      "action": "new",
      "update_id": null
    }}
  ]
}}

規則：
- action 只能是 "new"（新增）、"update"（更新/取代現有記憶，需填 update_id）、"delete"（刪除現有記憶，需填 update_id）、"skip"（忽略）
- 若與現有記憶重複或矛盾，用 "update" 取代舊的，不要新增重複項目
- 【重要】若用戶說「請記住 XXX」、「記住我 XXX」等明確要求記憶的話，必須用 "new" 新增，即使不是典型的個人背景資訊
- 【重要】若用戶說「請忘記 XXX」、「不要記住 XXX」、「刪除記憶 XXX」等，找到現有記憶中最吻合的條目，用 "delete" 刪除（填入該條目的 update_id）
- 每次最多回傳 3 條，只記錄明確、具體的事實
- 不要記錄當下問題本身，只記錄關於用戶的長期背景資訊"""

            response = await asyncio.to_thread(
                lambda: openai_client.chat.completions.create(
                    model="gpt-5.2",
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    max_completion_tokens=300,
                    temperature=0.1
                )
            )

            import json
            result = json.loads(response.choices[0].message.content)
            new_memories = result.get("memories", [])

            current_count = async_db.query(UserMemory).filter(
                UserMemory.user_id == user_id
            ).count()

            for item in new_memories:
                action = item.get("action", "skip")
                content = item.get("content", "").strip()
                category = item.get("category", "other")
                if not content or action == "skip":
                    continue

                if action == "delete":
                    delete_id = item.get("update_id")
                    if delete_id:
                        target = async_db.query(UserMemory).filter(
                            UserMemory.id == delete_id,
                            UserMemory.user_id == user_id
                        ).first()
                        if target:
                            print(f"[記憶] 刪除 user={user_id}: {target.content}")
                            async_db.delete(target)
                            current_count -= 1
                        else:
                            print(f"[記憶] delete 指向不存在的 id={delete_id}，略過")
                    else:
                        print(f"[記憶] delete 缺少 update_id，略過 user={user_id}")
                elif action == "update":
                    update_id = item.get("update_id")
                    if update_id:
                        target = async_db.query(UserMemory).filter(
                            UserMemory.id == update_id,
                            UserMemory.user_id == user_id
                        ).first()
                        if target:
                            target.content = content
                            target.category = category
                            target.updated_at = datetime.utcnow()
                            print(f"[記憶] 更新 user={user_id}: {content}")
                        else:
                            # update_id 指向不存在的記憶，fallback 新增
                            action = "new"
                    else:
                        # AI 回傳 update 但沒給 id，fallback 新增
                        print(f"[記憶] update 缺少 update_id，改為新增 user={user_id}: {content}")
                        action = "new"
                    if action == "new":
                        if current_count >= MEMORY_LIMIT:
                            lru = async_db.query(UserMemory).filter(
                                UserMemory.user_id == user_id
                            ).order_by(UserMemory.last_referenced_at.asc()).first()
                            if lru:
                                print(f"[記憶] LRU 淘汰 user={user_id}: {lru.content}")
                                async_db.delete(lru)
                        new_mem = UserMemory(
                            user_id=user_id,
                            content=content,
                            category=category,
                            source_summary=f"U: {user_message[:100]}"
                        )
                        async_db.add(new_mem)
                        if current_count < MEMORY_LIMIT:
                            current_count += 1
                        print(f"[記憶] 新增（update fallback）user={user_id}: {content}")
                elif action == "new":
                    if current_count >= MEMORY_LIMIT:
                        lru = async_db.query(UserMemory).filter(
                            UserMemory.user_id == user_id
                        ).order_by(UserMemory.last_referenced_at.asc()).first()
                        if lru:
                            print(f"[記憶] LRU 淘汰 user={user_id}: {lru.content}")
                            async_db.delete(lru)

                    new_mem = UserMemory(
                        user_id=user_id,
                        content=content,
                        category=category,
                        source_summary=f"U: {user_message[:100]}"
                    )
                    async_db.add(new_mem)
                    if current_count < MEMORY_LIMIT:
                        current_count += 1
                    print(f"[記憶] 新增 user={user_id}: {content}")

            async_db.commit()

            # 並發保護：commit 後再次確認總數，若超限則刪除最舊的直到符合上限
            overflow = async_db.query(UserMemory).filter(
                UserMemory.user_id == user_id
            ).count() - MEMORY_LIMIT
            if overflow > 0:
                oldest = async_db.query(UserMemory).filter(
                    UserMemory.user_id == user_id
                ).order_by(UserMemory.last_referenced_at.asc()).limit(overflow).all()
                for m in oldest:
                    print(f"[記憶] 並發超限清理 user={user_id}: {m.content}")
                    async_db.delete(m)
                async_db.commit()

        finally:
            async_db.close()

    except Exception as e:
        print(f"[記憶] 擷取失敗（不影響對話）: {e}")


def render_html(filename: str) -> HTMLResponse:
    """讀取 HTML 並注入 .env 變數與模型設定常數"""
    domain = os.getenv("DOMAIN_ENTRY", "")
    with open(filename, "r", encoding="utf-8") as f:
        html = f.read()
    inject = (
        f'<script>\n'
        f'  const API_URL            = "{domain}";\n'
        f'  const OPENAI_IMAGE_MODEL = "{OPENAI_IMAGE_MODEL}";\n'
        f'  const GOOGLE_IMAGE_MODEL = "{GOOGLE_IMAGE_MODEL}";\n'
        f'</script>\n'
    )
    html = html.replace("</head>", inject + "</head>", 1)
    return HTMLResponse(html)


# ==================== Pydantic 模型 ====================

PROVIDER_ALIASES = {"google": "gemini"}

class ChatRequest(BaseModel):
    """聊天請求"""
    message: str
    provider: str
    conversation_id: Optional[int] = None
    project_id: Optional[int] = None   # 新對話時指定所屬專案
    use_rag: bool = True

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, v: str) -> str:
        return PROVIDER_ALIASES.get(v.lower(), v.lower())


class ChatResponse(BaseModel):
    """聊天回應"""
    message_id: int
    conversation_id: int
    response: str
    provider: str
    model: str
    rag_sources: List[dict] = []
    auto_routed: Optional[str] = None


class CustomQACreate(BaseModel):
    name: str
    keywords: str
    match_type: str = "any"
    answer: str
    is_enabled: bool = True

class CustomQAUpdate(BaseModel):
    name: Optional[str] = None
    keywords: Optional[str] = None
    match_type: Optional[str] = None
    answer: Optional[str] = None
    is_enabled: Optional[bool] = None


def check_custom_qa(message: str, db: Session) -> Optional[tuple]:
    try:
        rules = db.query(CustomQA).filter(CustomQA.is_enabled == True).all()
        msg_lower = message.lower()
        for rule in rules:
            keywords = [kw.strip().lower() for kw in rule.keywords.split(",") if kw.strip()]
            if not keywords:
                continue
            if rule.match_type == "all":
                hit = all(kw in msg_lower for kw in keywords)
            else:
                hit = any(kw in msg_lower for kw in keywords)
            if hit:
                return (rule.answer, rule.id)
    except Exception as e:
        print(f"[CustomQA] 比對失敗（不影響對話）: {e}")
    return None


# ==================== 認證依賴注入 ====================

async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
    api_key: Optional[str] = Depends(api_key_header),
    db: Session = Depends(get_db)
) -> User:
    """驗證當前用戶：支援 JWT Bearer Token 與 X-API-Key 兩種方式"""
    if api_key:
        hashed = _hash_api_key(api_key)
        key_obj = db.query(ApiKey).filter(
            ApiKey.hashed_key == hashed,
            ApiKey.is_active == True
        ).first()

        if not key_obj and _API_KEY_PEPPER:
            old_hash = hashlib.sha256(api_key.encode()).hexdigest()
            key_obj = db.query(ApiKey).filter(
                ApiKey.hashed_key == old_hash,
                ApiKey.is_active == True
            ).first()
            if key_obj:
                key_obj.hashed_key = hashed
                db.commit()

        if not key_obj:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="無效或已停用的 API Key"
            )

        if key_obj.expires_at and key_obj.expires_at < datetime.utcnow():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API Key 已過期"
            )

        key_obj.last_used_at = datetime.utcnow()
        db.commit()

        user = db.query(User).filter(User.id == key_obj.user_id).first()
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="用戶不存在或已被停用"
            )
        return user

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="請提供認證憑證（Bearer Token 或 X-API-Key）",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="無效的認證憑證",
            headers={"WWW-Authenticate": "Bearer"},
        )

    jti = payload.get("jti")
    if jti and is_token_revoked(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 已撤銷，請重新登入",
            headers={"WWW-Authenticate": "Bearer"},
        )

    username = payload.get("sub")

    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用戶不存在"
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="用戶已被停用"
        )

    return user


async def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理員權限"
        )
    return current_user


# ==================== Chat 共用前置邏輯 ====================

def _check_rate_and_quota(current_user: User, message: str, db: Session):
    """速率限制 + 配額檢查，失敗直接 raise HTTPException"""
    can_proceed, error_msg = check_rate_limit(current_user, db)
    if not can_proceed:
        raise HTTPException(status_code=429, detail=error_msg)

    estimated_tokens = count_tokens(message) + 200
    estimated_cost = calculate_cost(estimated_tokens, estimated_tokens // 2, OPENAI_MODEL)
    can_proceed, error_msg = check_quota(current_user, estimated_tokens, estimated_cost, db)
    if not can_proceed:
        raise HTTPException(status_code=403, detail=error_msg)


def _process_pii(message: str, current_user: User) -> str:
    """PII 偵測，回傳處理後的安全訊息；偵測到 block 則 raise HTTPException"""
    _pii_cfg = load_pii_config()
    _pii_result = pii_scan(message, _pii_cfg)
    if _pii_result["action"] == "block":
        if _pii_cfg.get("log_detections"):
            log_error("PII_BLOCKED", f"user={current_user.email} detections={_pii_result['detections']}", user_id=current_user.id)
        raise HTTPException(status_code=400, detail=_pii_cfg.get("block_message", "訊息含個資，已被攔截"))
    if _pii_result["action"] == "mask" and _pii_cfg.get("log_detections"):
        log_error("PII_MASKED", f"user={current_user.email} detections={_pii_result['detections']}", user_id=current_user.id)
    return _pii_result["text"]


def _retrieve_rag_context(message: str, current_user: User, use_rag: bool, summarize_list: bool = False, project=None, db=None):
    """
    RAG 知識庫查詢，回傳 (rag_system_prompt, rag_sources)。
    summarize_list=True 時（streaming 模式），list_query 會先摘要再送 AI。
    db 用於管理員無 project 情境時，限定只搜管理員定義分類，不碰 project_* 私有文件。
    """
    if not use_rag:
        return "", []

    import re as _re
    rag_system_prompt = ""
    rag_sources = []
    try:
        rag = get_rag()
        if not rag:
            return "", []

        # 建立分類白名單：管理員分類 + 專案私有分類（project_{id}）
        if project:
            # 從管理員分類出發
            if getattr(project, "rag_categories", None):
                allowed_cats = [c.strip() for c in project.rag_categories.split(",") if c.strip()]
            else:
                allowed_cats = []
            # 永遠附加專案私有分類（即使沒文件也只是空結果，不會出錯）
            allowed_cats.append(f"project_{project.id}")
        elif not current_user.is_admin:
            if current_user.allowed_rag_categories is None:
                return "", []
            allowed_cats = [c.strip() for c in current_user.allowed_rag_categories.split(",") if c.strip()]
            if not allowed_cats:
                return "", []
        else:
            # 管理員在無 project 情境：只搜管理員定義的公開分類，不搜 project_* 私有文件
            if db is not None:
                admin_cats = [c.name for c in db.query(RagCategory).filter(RagCategory.is_active == True).all()]
                allowed_cats = admin_cats if admin_cats else None
            else:
                allowed_cats = None

        date_keyword = None
        m = _re.search(r'(\d{4})[/-](\d{1,2})', message)
        if m:
            y, mo = m.group(1), m.group(2).zfill(2)
            date_keyword = f"{y}/{mo}"

        is_list_query = date_keyword and any(
            kw in message for kw in ['所有', '全部', '列出', '有哪些', '幾個', '幾筆', '幾團', '告訴我']
        )

        if is_list_query:
            from rag_service import get_collection
            col = get_collection()
            all_docs = col.get(include=['documents', 'metadatas'])
            matched = [
                {"text": doc, "doc_title": meta.get("doc_title", ""), "score": 1.0}
                for doc, meta in zip(all_docs['documents'], all_docs['metadatas'])
                if date_keyword in doc
                and (allowed_cats is None or meta.get("category") in allowed_cats)
            ]
            print(f"[RAG 關鍵字過濾] '{date_keyword}' 找到 {len(matched)} 筆（分類限制：{allowed_cats}）")

            if summarize_list:
                count = len(matched)
                summary_text = f"根據知識庫資料，{date_keyword} 出發的團共有 {count} 筆紀錄。"
                if 0 < count <= 50:
                    summary_text += "\n\n明細如下：\n" + "\n".join(m["text"] for m in matched[:50])
                contexts = [{"text": summary_text, "doc_title": matched[0]["doc_title"] if matched else "", "score": 1.0}]
            else:
                contexts = matched
        else:
            if allowed_cats:
                all_contexts = []
                for cat in allowed_cats:
                    c = rag.search(query=message, top_k=5, category=cat, use_rerank=False)
                    all_contexts.extend(c)
                contexts = sorted(all_contexts, key=lambda x: x["score"], reverse=True)[:10]
            else:
                contexts = rag.search(query=message, top_k=10, use_rerank=False)

        if contexts:
            rag_system_prompt = build_rag_prompt(message, contexts)
            rag_sources = [{"title": c["doc_title"], "score": c["score"]} for c in contexts]

    except Exception as e:
        print(f"RAG 查詢失敗（不影響對話）: {e}")

    return rag_system_prompt, rag_sources


def _build_system_prompt(rag_system_prompt: str, current_user: User, db: Session, project=None) -> str:
    """組合最終 system prompt：專案指示 + RAG context + 記憶 + 偏好設定"""
    system_prompt = rag_system_prompt
    _memory_fragment = get_user_memories_for_prompt(current_user.id, db)
    if _memory_fragment:
        system_prompt = _memory_fragment + "\n\n" + system_prompt if system_prompt else _memory_fragment
    _pref_fragment = get_user_preferences_for_prompt(current_user)
    if _pref_fragment:
        system_prompt = _pref_fragment + "\n\n" + system_prompt if system_prompt else _pref_fragment
    # 專案指示放最前面（優先權最高）
    if project and getattr(project, "system_prompt", None):
        project_fragment = f"【專案指示】\n{project.system_prompt}"
        system_prompt = project_fragment + "\n\n" + system_prompt if system_prompt else project_fragment
    return system_prompt
