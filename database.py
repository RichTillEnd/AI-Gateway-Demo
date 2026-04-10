"""
資料庫模型定義
這個檔案定義了所有資料表的結構
"""

from sqlalchemy import create_engine, event, Column, Integer, String, DateTime, Text, ForeignKey, Boolean, Float
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

# 優先使用 DATABASE_URL 環境變數（支援 PostgreSQL）
# 未設定時預設使用本地 SQLite
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ai_gateway_demo.db")

_is_sqlite = SQLALCHEMY_DATABASE_URL.startswith("sqlite")

# SQLite 需要 check_same_thread=False；PostgreSQL 不需要此參數
# SQLite 設定 30s busy_timeout，避免並發寫入時 "database is locked" 錯誤
_connect_args = {"check_same_thread": False, "timeout": 30} if _is_sqlite else {}

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,      # 自動偵測斷線並重連
    # PostgreSQL 連線池設定（SQLite 使用 StaticPool，忽略以下參數）
    pool_size=10,            # 常駐連線數（對應 10 位並發用戶）
    max_overflow=20,         # 尖峰時額外允許的連線數
    pool_timeout=30,         # 等待可用連線的最長秒數
    pool_recycle=1800,       # 30 分鐘後回收連線，避免 PG 端 idle timeout 切斷
) if not _is_sqlite else create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
)

# SQLite 效能優化：啟用 WAL mode（允許並發讀寫）
if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

# 建立 Session 類別（用來操作資料庫）
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 建立 Base 類別（所有模型都會繼承這個）
Base = declarative_base()


# ==================== 資料表模型 ====================

class User(Base):
    """
    用戶資料表
    儲存所有用戶的基本資訊
    """
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)  # 帳號（唯一）
    email = Column(String(100), unique=True, index=True, nullable=False)    # Email（唯一）
    hashed_password = Column(String(255), nullable=False)                   # 加密後的密碼
    full_name = Column(String(100))                                         # 全名
    department = Column(String(50))                                         # 部門
    is_active = Column(Boolean, default=False)                              # 是否啟用（預設 False，需驗證 Email）
    is_admin = Column(Boolean, default=False)                               # 是否為管理員
    created_at = Column(DateTime, default=datetime.utcnow)                  # 建立時間
    
    # Email 驗證相關
    email_verified = Column(Boolean, default=False)                         # Email 是否已驗證
    verification_token = Column(String(100), nullable=True)                 # Email 驗證 token
    verification_token_expires = Column(DateTime, nullable=True)            # 驗證 token 過期時間
    
    # 密碼重設相關
    reset_token = Column(String(100), nullable=True)                        # 重設碼（token_urlsafe(32) = 43 chars）
    reset_token_expires = Column(DateTime, nullable=True)                   # 重設碼過期時間
    
    # 密碼安全政策
    password_updated_at = Column(DateTime, default=datetime.utcnow)         # 密碼最後更新時間
    force_password_change = Column(Boolean, default=False)                  # 是否強制更換密碼

    # RAG 知識庫存取權限
    # 逗號分隔的分類名稱，例如 "hr,general"
    # None 或空字串 = 無權存取任何分類（管理員不受此限）
    allowed_rag_categories = Column(Text, nullable=True, default=None)

    # AI 記憶功能開關（預設開啟）
    memory_enabled = Column(Boolean, default=True, nullable=False)

    # 個人化偏好設定
    work_type = Column(String(50), nullable=True)      # 職業角色
    user_instructions = Column(Text, nullable=True)    # 個人指令（套用至所有對話）
    
    # 關聯：一個用戶可以有多個對話
    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")
    
    # 關聯：一個用戶可以有多個使用記錄
    usage_logs = relationship("UsageLog", back_populates="user", cascade="all, delete-orphan")

    # 關聯：一個用戶可以有多條 AI 記憶
    memories = relationship("UserMemory", back_populates="user", cascade="all, delete-orphan",
                            order_by="UserMemory.last_referenced_at.desc()")


class Project(Base):
    """
    專案資料表 - 用於將對話分組管理
    """
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(100), nullable=False)
    system_prompt = Column(Text, nullable=True)         # 專案自訂指示（注入 AI system prompt）
    rag_categories = Column(String(500), nullable=True) # 逗號分隔的知識庫分類，如 "hr,policy"
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    conversations = relationship("Conversation", back_populates="project")
    documents = relationship("ProjectDocument", back_populates="project", cascade="all, delete-orphan")


class ProjectDocument(Base):
    """
    專案私有知識庫文件 — 使用者上傳至特定專案的 RAG 文件
    ChromaDB 中以 category = 'project_{project_id}' 標記
    """
    __tablename__ = "project_documents"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    doc_id = Column(String(50), nullable=False, unique=True)   # ChromaDB doc_id
    filename = Column(String(255), nullable=False)
    doc_title = Column(String(255), nullable=False)
    chunks_count = Column(Integer, default=0)
    uploaded_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="documents")


class Conversation(Base):
    """
    對話資料表
    儲存每一次的對話（包含多個訊息）
    """
    __tablename__ = "conversations"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(200))
    is_starred = Column(Boolean, default=False)                             # 是否加星號
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)  # 所屬專案
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 關聯
    user = relationship("User", back_populates="conversations")
    project = relationship("Project", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    """
    訊息資料表
    儲存對話中的每一條訊息（用戶和 AI 的）
    """
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)  # 所屬對話
    role = Column(String(20), nullable=False)                               # 角色：user 或 assistant
    content = Column(Text, nullable=False)                                  # 訊息內容
    provider = Column(String(20))                                           # 使用的 AI 提供商（openai/gemini）
    model = Column(String(50))                                              # 使用的模型
    created_at = Column(DateTime, default=datetime.utcnow)                  # 建立時間
    
    # 關聯
    conversation = relationship("Conversation", back_populates="messages")
    attachments = relationship("Attachment", back_populates="message", cascade="all, delete-orphan")


class Attachment(Base):
    """
    附件資料表
    儲存上傳的檔案資訊
    """
    __tablename__ = "attachments"
    
    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=False)  # 所屬訊息
    filename = Column(String(255), nullable=False)                           # 原始檔名
    filepath = Column(String(500), nullable=False)                           # 儲存路徑
    file_type = Column(String(50))                                           # 檔案類型（image/document/code）
    file_size = Column(Integer)                                              # 檔案大小（bytes）
    mime_type = Column(String(100))                                          # MIME 類型
    created_at = Column(DateTime, default=datetime.utcnow)                   # 上傳時間
    
    # 關聯
    message = relationship("Message", back_populates="attachments")


class UsageLog(Base):
    """
    使用記錄資料表
    追蹤每次 API 呼叫，用於成本控制和統計
    """
    __tablename__ = "usage_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)        # 使用者
    provider = Column(String(20), nullable=False)                            # AI 提供商
    model = Column(String(50), nullable=False)                               # 使用的模型
    input_tokens = Column(Integer)                                           # 輸入 token 數
    output_tokens = Column(Integer)                                          # 輸出 token 數
    estimated_cost = Column(Float)                                           # 預估成本（美金）
    created_at = Column(DateTime, default=datetime.utcnow)                   # 使用時間
    
    # 關聯
    user = relationship("User", back_populates="usage_logs")


class UserQuota(Base):
    """
    用戶配額資料表
    管理每個用戶的使用額度
    """
    __tablename__ = "user_quotas"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)  # 用戶（唯一）
    
    # 每月配額（重置）
    monthly_token_limit = Column(Integer, default=100000)                   # 每月 token 限制
    monthly_cost_limit = Column(Float, default=10.0)                        # 每月成本限制（美金）
    
    # 當月使用量（每月重置）
    current_month_tokens = Column(Integer, default=0)                       # 本月已用 tokens
    current_month_cost = Column(Float, default=0.0)                         # 本月已用成本（合計）
    current_month_openai_cost = Column(Float, default=0.0)                  # 本月 OpenAI 成本
    current_month_gemini_cost = Column(Float, default=0.0)                  # 本月 Gemini 成本
    last_reset_date = Column(DateTime, default=datetime.utcnow)             # 上次重置時間
    
    # 總使用量（累積）
    total_tokens = Column(Integer, default=0)                               # 總 tokens
    total_cost = Column(Float, default=0.0)                                 # 總成本（合計）
    total_openai_cost = Column(Float, default=0.0)                          # 累積 OpenAI 成本
    total_gemini_cost = Column(Float, default=0.0)                          # 累積 Gemini 成本
    
    # 狀態
    is_quota_exceeded = Column(Boolean, default=False)                      # 是否超額
    quota_warning_sent = Column(Boolean, default=False)                     # 是否已發送警告
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RateLimit(Base):
    """
    速率限制記錄表
    追蹤用戶的請求次數
    """
    __tablename__ = "rate_limits"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)       # 用戶
    
    # 請求計數
    requests_last_minute = Column(Integer, default=0)                       # 最近 1 分鐘請求數
    requests_last_hour = Column(Integer, default=0)                         # 最近 1 小時請求數
    requests_today = Column(Integer, default=0)                             # 今日請求數
    
    # 時間戳記
    last_request_time = Column(DateTime, default=datetime.utcnow)           # 最後請求時間
    minute_reset_time = Column(DateTime, default=datetime.utcnow)           # 分鐘重置時間
    hour_reset_time = Column(DateTime, default=datetime.utcnow)             # 小時重置時間
    day_reset_time = Column(DateTime, default=datetime.utcnow)              # 天重置時間
    
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ErrorLog(Base):
    """
    錯誤日誌資料表
    記錄系統錯誤和異常
    """
    __tablename__ = "error_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # 錯誤資訊
    error_type = Column(String(50), nullable=False)                         # 錯誤類型（API_ERROR, AUTH_ERROR, SYSTEM_ERROR）
    error_message = Column(Text, nullable=False)                            # 錯誤訊息
    error_detail = Column(Text)                                             # 詳細錯誤
    stack_trace = Column(Text)                                              # 堆疊追蹤
    
    # 上下文
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)        # 相關用戶（可為空）
    endpoint = Column(String(200))                                          # API 端點
    method = Column(String(10))                                             # HTTP 方法
    request_data = Column(Text)                                             # 請求資料（脫敏）
    
    # 環境資訊
    ip_address = Column(String(45))                                         # IP 地址
    user_agent = Column(String(500))                                        # User Agent
    trace_id = Column(String(36))                                           # Request trace ID（UUID4）

    # 狀態
    is_resolved = Column(Boolean, default=False)                            # 是否已解決
    resolved_at = Column(DateTime)                                          # 解決時間
    resolved_by = Column(Integer)                                           # 解決人（user_id）
    
    created_at = Column(DateTime, default=datetime.utcnow, index=True)      # 發生時間


class SystemMetric(Base):
    """
    系統指標資料表
    記錄系統健康狀況
    """
    __tablename__ = "system_metrics"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # 指標類型
    metric_type = Column(String(50), nullable=False)                        # API_LATENCY, ERROR_RATE, ACTIVE_USERS
    metric_value = Column(Float, nullable=False)                            # 指標值
    
    # 詳細資料
    provider = Column(String(20))                                           # AI 提供商（如適用）
    additional_data = Column(Text)                                          # 額外資料（JSON）
    
    created_at = Column(DateTime, default=datetime.utcnow, index=True)      # 記錄時間


class RagCategory(Base):
    """
    RAG 知識庫分類資料表
    動態管理分類名稱，管理員可新增、刪除分類
    """
    __tablename__ = "rag_categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)       # 分類識別碼（英文，如 "hr"）
    label = Column(String(100), nullable=False)                  # 顯示名稱（中文，如 "人事（HR）"）
    description = Column(String(200), nullable=True)             # 說明
    is_active = Column(Boolean, default=True)                    # 是否啟用
    sort_order = Column(Integer, default=0)                      # 排序
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ApiKey(Base):
    """
    API Key 資料表
    讓程式、腳本、外部系統可以用 Key 取代 JWT 存取 Gateway
    """
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)   # 所屬用戶（管理員）
    name = Column(String(100), nullable=False)                          # 描述名稱，如 "RPA 機器人"
    key_prefix = Column(String(20), nullable=False)                     # Key 前綴，用於顯示，如 "sk-gw-ab12"
    hashed_key = Column(String(255), nullable=False, unique=True)       # bcrypt 雜湊後的完整 Key
    is_active = Column(Boolean, default=True)                           # 是否啟用
    last_used_at = Column(DateTime, nullable=True)                      # 最後使用時間
    expires_at = Column(DateTime, nullable=True)                        # 到期時間（None = 永不過期）
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RefreshToken(Base):
    """
    Refresh Token 資料表
    配合 JWT access token 實現 token 輪換與即時撤銷
    """
    __tablename__ = "refresh_tokens"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)  # SHA-256
    issued_at  = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    is_revoked = Column(Boolean, default=False)
    revoked_at = Column(DateTime, nullable=True)
    user_agent = Column(String(200), nullable=True)


class CustomQA(Base):
    """
    自訂 QA 規則資料表
    當用戶問題命中關鍵字時，直接回傳預設答案（不呼叫 AI API）
    """
    __tablename__ = "custom_qa"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)                              # 規則名稱（方便辨識）
    keywords = Column(Text, nullable=False)                                 # 關鍵字，逗號分隔，如 "模型,model id,語言模型"
    match_type = Column(String(10), default="any")                         # any=任一命中 / all=全部命中
    answer = Column(Text, nullable=False)                                   # 預設回答內容
    is_enabled = Column(Boolean, default=True)                             # 是否啟用
    hit_count = Column(Integer, default=0)                                  # 命中次數（統計用）
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)    # 建立者
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AuditLog(Base):
    """
    審計日誌資料表
    記錄所有管理員操作與重要事件，確保可追溯性
    """
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)

    # 操作者
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)       # 執行操作的用戶 ID（None = 系統）
    actor_email = Column(String(255))                                        # 快照，即使帳號刪除仍可查

    # 動作
    action = Column(String(100), nullable=False, index=True)                # 操作類型，如 USER_CREATE、QUOTA_UPDATE
    resource_type = Column(String(50), index=True)                          # 資源類型：user, quota, rag, api_key, qa
    resource_id = Column(String(100))                                       # 資源 ID（可能是整數或字串）

    # 受影響對象
    target_user_id = Column(Integer, ForeignKey("users.id"), nullable=True) # 被操作的用戶 ID
    target_user_email = Column(String(255))                                  # 快照

    # 變更細節
    details = Column(Text)                                                   # JSON 格式的變更內容

    # 請求資訊
    ip_address = Column(String(45))
    user_agent = Column(String(500))

    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class PromptTemplate(Base):
    """
    Prompt 範本資料表
    管理員建立標準化 Prompt，用戶一鍵套用
    """
    __tablename__ = "prompt_templates"

    id          = Column(Integer, primary_key=True, index=True)
    title       = Column(String(100), nullable=False)           # 範本名稱
    description = Column(String(300))                           # 簡短說明
    content     = Column(Text, nullable=False)                  # Prompt 內容（支援 {{變數}} 佔位符）
    category    = Column(String(50), index=True)                # 分類
    visibility  = Column(String(20), default="all")             # "all" | "admin_only"
    is_active   = Column(Boolean, default=True, index=True)     # 是否對用戶顯示
    sort_order  = Column(Integer, default=0)                    # 排序權重（越小越前）
    use_count   = Column(Integer, default=0)                    # 套用次數統計
    created_by  = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserMemory(Base):
    """
    用戶記憶資料表
    AI 從對話中自動擷取並長期保存的用戶個人資訊（上限 20 條）
    """
    __tablename__ = "user_memories"

    id                 = Column(Integer, primary_key=True, index=True)
    user_id            = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    content            = Column(Text, nullable=False)              # 記憶文字，如「使用者是軟體工程師」
    category           = Column(String(50), nullable=True)         # profession / habit / health / preference / life_event / other
    source_summary     = Column(Text, nullable=True)               # 擷取來源的對話摘要（除錯用）
    last_referenced_at = Column(DateTime, default=datetime.utcnow) # 最後被注入 system prompt 的時間（LRU 淘汰依據）
    created_at         = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at         = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="memories")


# ==================== 輔助函數 ====================

def init_db():
    """
    ⚠️  已由 Alembic 接管資料庫管理。
    部署或更新資料庫結構請執行：
        alembic upgrade head
    緊急回滾請執行：
        alembic downgrade -1
    """
    print("⚠️  資料庫結構由 Alembic 管理，請執行 alembic upgrade head")
    print("    如需從零初始化：alembic upgrade head")

def get_db():
    """
    獲取資料庫 session
    用於 FastAPI 的依賴注入
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


if __name__ == "__main__":
    # 如果直接執行這個檔案，就初始化資料庫
    init_db()
