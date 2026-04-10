"""
AI Gateway 主程式 — FastAPI 應用進入點
路由實作分散於 routers/ 目錄，共用邏輯在 core.py
"""
import os
import uuid
import logging
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from tracing import _trace_id_var, get_trace_id
from core import render_html


class _TraceIdFilter(logging.Filter):
    """將 trace_id 注入每一條 log record。"""
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id_var.get()
        return True


# 配置 root logger 格式（含 trace_id）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(trace_id)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
for _handler in logging.root.handlers:
    _handler.addFilter(_TraceIdFilter())

# ── 靜態目錄 ──────────────────────────────────────────────────
os.makedirs("./static/generated", exist_ok=True)
os.makedirs("./rag_uploads", exist_ok=True)
os.makedirs("./rag_files", exist_ok=True)

# ── FastAPI 應用 ───────────────────────────────────────────────
app = FastAPI(
    title="AI Gateway API",
    version="2.0.0",
    description="""
## 企業級 AI 中台系統

整合 OpenAI GPT 系列與 Google Gemini 系列的私有 AI Gateway，
提供完整的用戶管理、成本管控、RAG 知識庫與安全稽核能力。

### 功能模組
- **認證系統** — JWT 登入、Email 驗證、密碼重設
- **AI 對話** — 多模型串流對話、對話歷史管理
- **檔案處理** — 圖片視覺分析、文件解析（PDF/DOCX/Excel）
- **RAG 知識庫** — 向量檢索 + LLM Re-ranking 二次排序
- **圖像生成** — OpenAI gpt-image-1 / Google Imagen 4 Ultra
- **自訂 QA** — 關鍵字命中快速回覆，節省 API 費用
- **配額管理** — 每用戶月度 Token + 費用雙重上限
- **管理後台** — 用戶管理、使用統計、錯誤日誌

### 認證方式
所有 `/api/*` 端點（除登入、註冊外）需在 Header 帶入：
```
Authorization: Bearer <JWT Token>
```

### 支援模型
| 供應商 | 文字模型 | 圖像模型 |
|--------|---------|---------|
| OpenAI | GPT-4.1, GPT-4o, GPT-5.4 系列 | gpt-image-1 |
| Google | Gemini 1.5/2.0/2.5/3.x 系列 | Imagen 4.0 Ultra |
    """,
    contact={
        "name": "AI Gateway Demo",
        "email": "admin@example.com",
    },
    license_info={
        "name": "Private — Internal Use Only",
    },
    openapi_tags=[
        {"name": "認證", "description": "用戶註冊、登入、Email 驗證、密碼管理"},
        {"name": "對話", "description": "AI 多模型對話（同步 / SSE 串流）"},
        {"name": "檔案", "description": "檔案上傳與帶附件對話"},
        {"name": "圖像生成", "description": "OpenAI gpt-image-1 / Google Imagen 4 Ultra"},
        {"name": "對話管理", "description": "對話歷史、重命名、加星號、專案分組"},
        {"name": "知識庫", "description": "RAG 文件上傳、查詢、管理（僅管理員）"},
        {"name": "自訂QA", "description": "關鍵字快速回覆規則管理（僅管理員）"},
        {"name": "配額", "description": "用戶使用量與費用配額查詢"},
        {"name": "管理員", "description": "用戶管理、統計報表、錯誤日誌（僅管理員）"},
        {"name": "頁面", "description": "前端 HTML 頁面路由"},
        {"name": "系統", "description": "健康檢查、快取管理"},
        {"name": "Prompt 範本", "description": "Prompt 範本管理"},
        {"name": "用戶", "description": "用戶記憶與個人偏好"},
        {"name": "旅遊團體", "description": "旅遊團體 Excel 資料管理"},
        {"name": "API Key 管理", "description": "外部 API Key 管理"},
    ],
)

# ── 靜態檔案掛載 ───────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="./static"), name="static")


# ── Trace ID 中介層 ────────────────────────────────────────────
class TraceIdMiddleware(BaseHTTPMiddleware):
    """
    為每個 request 建立唯一 trace ID。
    優先使用 client 傳入的 X-Request-ID（方便前端端對端追蹤），
    否則自動產生 UUID4。Response header 一律回傳 X-Request-ID。
    """
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        token = _trace_id_var.set(trace_id)
        try:
            response = await call_next(request)
        finally:
            _trace_id_var.reset(token)
        response.headers["X-Request-ID"] = trace_id
        return response

app.add_middleware(TraceIdMiddleware)


# ── 上傳大小限制中介層（50MB）─────────────────────────────────
class LimitUploadSizeMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_upload_size: int = 50 * 1024 * 1024):
        super().__init__(app)
        self.max_upload_size = max_upload_size

    async def dispatch(self, request: Request, call_next):
        if request.method == "POST":
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > self.max_upload_size:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"檔案超過上限（最大 {self.max_upload_size // 1024 // 1024}MB）"}
                )
        return await call_next(request)

app.add_middleware(LimitUploadSizeMiddleware, max_upload_size=50 * 1024 * 1024)

# ── CORS 設定 ──────────────────────────────────────────────────
_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:8000")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)


# ── Deprecation 警告中介層（舊 /api/ 路徑） ───────────────────
class _DeprecationMiddleware(BaseHTTPMiddleware):
    """
    當 client 使用舊版 /api/ 路徑（非 /api/v1/）時，
    在 response header 加入 Deprecation 警告。
    /api/health* 為系統路由，排除在外。
    """
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if (path.startswith("/api/") and
                not path.startswith("/api/v1/") and
                not path.startswith("/api/health")):
            response.headers["Deprecation"] = "true"
            response.headers["Link"] = (
                f'</api/v1{path[4:]}>; rel="successor-version"'
            )
        return response

app.add_middleware(_DeprecationMiddleware)


# ── 掛載路由模組 ───────────────────────────────────────────────
from routers import auth, chat, conversations, admin, templates, rag_routes, images

_routers = [auth.router, chat.router, conversations.router,
            admin.router, templates.router, rag_routes.router, images.router]

# v1（正式版本）
for _r in _routers:
    app.include_router(_r, prefix="/api/v1")

# 向後相容（舊前端 /api/... 路徑持續可用，回傳 Deprecation header 提示）
for _r in _routers:
    app.include_router(_r, prefix="/api", include_in_schema=False)


# ── 頁面路由 ───────────────────────────────────────────────────
@app.get("/", tags=["頁面"], include_in_schema=False)
async def root():
    return render_html("chat_v4.html")

@app.get("/admin", tags=["頁面"], include_in_schema=False)
async def admin_page():
    return render_html("admin.html")

@app.get("/admin.html", tags=["頁面"], include_in_schema=False)
async def admin_page_full():
    return render_html("admin.html")

@app.get("/login", tags=["頁面"], include_in_schema=False)
async def login_page_short():
    return render_html("login.html")

@app.get("/login.html", tags=["頁面"], include_in_schema=False)
async def login_page():
    return render_html("login.html")

@app.get("/forgot-password.html", tags=["頁面"], include_in_schema=False)
async def forgot_password_page():
    return render_html("forgot-password.html")

@app.get("/reset-password.html", tags=["頁面"], include_in_schema=False)
async def reset_password_page():
    return render_html("reset-password.html")

@app.get("/chat", tags=["頁面"], include_in_schema=False)
async def chat_page():
    return render_html("chat_v4.html")


# ── 啟動事件：自動標記過期密碼 ─────────────────────────────────
@app.on_event("startup")
def _startup_mark_expired_passwords():
    from database import SessionLocal
    from password_policy import auto_mark_expired_passwords
    db = SessionLocal()
    try:
        count = auto_mark_expired_passwords(db)
        if count:
            logging.getLogger(__name__).info(f"已標記 {count} 位用戶密碼過期，需強制更換")
    finally:
        db.close()


# ── 系統路由 ───────────────────────────────────────────────────
@app.get("/api/health/live", tags=["系統"], summary="存活探測（Liveness Probe）")
async def health_live():
    """
    K8s / load balancer liveness probe。
    只要 process 還在執行就回 200。不做任何 IO 檢查。
    """
    import time
    return {"status": "ok", "timestamp": time.time()}


@app.get("/api/health/ready", tags=["系統"], summary="就緒探測（Readiness Probe）")
async def health_ready():
    """
    K8s readiness probe。
    檢查所有關鍵依賴（DB、Redis、ChromaDB）是否可用。
    任一失敗回 503，讓 load balancer 停止路由到此 instance。
    """
    import time
    from fastapi.responses import JSONResponse

    checks: dict[str, str] = {}
    all_ok = True

    # 1. Database
    try:
        _db = next(get_db())
        _db.execute(__import__("sqlalchemy").text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {e}"
        all_ok = False

    # 2. Redis
    try:
        from rate_limiter import _get_redis
        _r = _get_redis()
        if _r is not None:
            _r.ping()
            checks["redis"] = "ok"
        else:
            checks["redis"] = "not_configured"
    except Exception as e:
        checks["redis"] = f"error: {e}"
        all_ok = False

    # 3. ChromaDB
    try:
        from rag_service import get_rag
        _rag = get_rag()
        if _rag and _rag.collection:
            checks["chromadb"] = "ok"
        else:
            checks["chromadb"] = "not_initialized"
    except Exception as e:
        checks["chromadb"] = f"error: {e}"
        all_ok = False

    payload = {
        "status": "ok" if all_ok else "degraded",
        "checks": checks,
        "timestamp": time.time(),
    }
    status_code = 200 if all_ok else 503
    return JSONResponse(content=payload, status_code=status_code)


@app.get("/api/health", tags=["系統"], summary="健康檢查（無需登入）")
async def health_check():
    """回傳服務完整狀態，供 systemd / 監控工具探測用。"""
    import time
    from semantic_cache import cache_stats
    from core import _SENTRY_DSN
    from rate_limiter import _get_redis

    # DB
    db_ok = False
    try:
        _db = next(get_db())
        _db.execute(__import__("sqlalchemy").text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    # Redis
    redis_status = "disabled"
    try:
        _r = _get_redis()
        if _r is not None:
            _r.ping()
            redis_status = "ok"
        else:
            redis_status = "not_configured"
    except Exception:
        redis_status = "error"

    _cache_info = await cache_stats()
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "redis": redis_status,
        "semantic_cache": _cache_info,
        "sentry": "enabled" if _SENTRY_DSN else "disabled",
        "timestamp": time.time(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        h11_max_incomplete_event_size=50 * 1024 * 1024
    )
