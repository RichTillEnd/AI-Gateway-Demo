# AI Gateway — 企業級 AI 平台 Demo

以 **FastAPI（Python 3.12）** 建構的企業級 AI 平台。整合 OpenAI GPT 與 Google Gemini 模型。
提供完整的用戶管理、成本配額控管、RAG 知識庫、PII 個資偵測、Text-to-SQL 自然語言資料庫查詢、限流與安全稽核能力。

---

## 核心功能

- **串流對話（SSE）** — 透過 Server-Sent Events 即時輸出 Token；支援 OpenAI GPT 與 Google Gemini
- **PII 個資偵測** — 正則表達式偵測台灣身分證、信用卡、手機號碼、Email；可設定遮蔽或攔截模式
- **RAG 知識庫** — ChromaDB 向量搜尋 + GPT Re-ranking，支援文件知識問答
- **語義快取** — Embedding 相似度快取，降低重複 API 呼叫費用
- **Rate Limiting** — 滑動視窗計數（5/分 · 60/小時 · 100/天），支援 Redis 或記憶體備援
- **網路搜尋** — Tavily 即時搜尋注入對話上下文
- **Text-to-SQL** — 自然語言轉 SQL 查詢內部資料庫
- **圖像生成** — OpenAI gpt-image-1 與 Google Imagen 4
- **用戶管理與後台** — JWT 認證、角色權限、使用統計、錯誤日誌

---

## 技術架構

| 層級 | 技術 |
|------|------|
| 後端 | Python 3.12、FastAPI、SQLAlchemy、Alembic |
| 資料庫 | SQLite（預設）/ PostgreSQL |
| 向量資料庫 | ChromaDB |
| AI 模型 | OpenAI GPT-4.1/4o、Google Gemini 2.x |
| 快取 | Redis（選填，未設定時自動降級） |
| 前端 | 原生 HTML/CSS/JS（無需建置步驟） |

---

## 快速啟動

```bash
# 1. Clone 專案
git clone https://github.com/RichTillEnd/ai-gateway-demo
cd ai-gateway-demo

# 2. 建立虛擬環境
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. 安裝套件
pip install -r requirements.txt

# 4. 設定環境變數
cp .env.example .env
# 編輯 .env，至少填入 OPENAI_KEY 與 JWT_SECRET_KEY

# 5. 初始化資料庫
alembic upgrade head

# 6. 啟動服務
python main.py
```

開啟瀏覽器前往 **http://localhost:8000**

| 頁面 | 路徑 |
|------|------|
| 對話介面 | `/` |
| 管理後台 | `/admin` |
| API 文件（Swagger） | `/docs` |

---

## 初次登入

1. 前往 `/login` 註冊帳號（接受任何 Email 格式）
2. 註冊後直接登入，**無需** Email 驗證
3. 若需要管理員權限，執行以下指令：

```bash
python -c "
from database import SessionLocal, User
db = SessionLocal()
user = db.query(User).filter(User.username == '你的帳號').first()
user.is_admin = True
db.commit()
print('Done')
"
```

---

## 請求處理流程

```
請求進入
  → Rate Limit 檢查
  → 配額預檢（Token / 費用上限）
  → PII 個資掃描（block 或 mask）
  → RAG 知識庫檢索（若啟用）
  → Custom QA 關鍵字比對（提前回應）
  → AI API 呼叫（指數退避重試）
  → Token 計數 → 儲存至 DB
  → 背景任務：自動命名對話、配額警告信
```

### 主要模組說明

| 檔案 | 職責 |
|------|------|
| `routers/chat.py` | 對話端點、串流、RAG、網路搜尋 |
| `routers/auth.py` | 註冊、登入、JWT、密碼重設 |
| `routers/admin.py` | 用戶管理、使用統計、錯誤日誌 |
| `routers/rag_routes.py` | 文件上傳、向量搜尋、分類管理 |
| `pii_detector.py` | PII 偵測與遮蔽 |
| `rag_service.py` | ChromaDB + Embedding + Re-ranking |
| `semantic_cache.py` | 語義快取 |
| `rate_limiter.py` | 滑動視窗限流 |
| `quota_manager.py` | 每用戶月度 Token / 費用上限 |

---

## 環境變數

詳見 `.env.example`。只必填項即可啟動：

```
OPENAI_KEY        # OpenAI API 金鑰
GEMINI_API_KEY    # Gemini API 金鑰
TAVILY_API_KEY    # Tavily API 金鑰
JWT_SECRET_KEY    # 任意 32 字元以上的隨機字串
```

其餘（Email、Redis）皆為選填，未設定不影響核心功能。
