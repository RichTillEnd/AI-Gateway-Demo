# AI Gateway — Enterprise AI Middleware Demo

An enterprise-grade AI middleware platform built with **FastAPI (Python 3.12)**. Proxies requests to OpenAI GPT and Google Gemini models with a full suite of enterprise features: user management, cost/quota control, RAG knowledge base, PII detection, rate limiting, and security auditing.

---

## Key Features

- **Streaming Chat (SSE)** — Real-time token streaming via Server-Sent Events; supports OpenAI GPT and Google Gemini
- **PII Detection** — Regex-based detection of Taiwan ID, credit card, phone numbers, email; configurable mask or block mode
- **RAG Knowledge Base** — ChromaDB vector search + GPT re-ranking for document-grounded answers
- **Semantic Cache** — Embedding-based response caching to reduce redundant API calls
- **Rate Limiting** — Sliding-window counters (5/min · 60/hr · 100/day) with Redis or in-memory fallback
- **Web Search** — Tavily-powered real-time search injected as context
- **Text-to-SQL** — Natural language queries converted to SQL against internal data
- **Image Generation** — OpenAI gpt-image-1 and Google Imagen 4
- **User Auth & Admin Panel** — JWT authentication, role-based access, usage stats, error logs

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, SQLAlchemy, Alembic |
| Database | SQLite (default) / PostgreSQL |
| Vector Store | ChromaDB |
| AI Models | OpenAI GPT-4.1/4o, Google Gemini 2.x |
| Caching | Redis (optional, falls back gracefully) |
| Frontend | Vanilla HTML/CSS/JS (no build step) |

---

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/yourusername/ai-gateway-demo
cd ai-gateway-demo

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — at minimum set OPENAI_KEY and JWT_SECRET_KEY

# 5. Initialize the database
alembic upgrade head

# 6. Run
python main.py
```

Open **http://localhost:8000** in your browser.

- **Chat UI** → `/`
- **Admin Panel** → `/admin`
- **API Docs (Swagger)** → `/docs`

---

## First Login

1. Go to `/login` and register a new account (any email accepted)
2. Log in immediately — no email verification required in demo mode
3. For admin access, set `is_admin = true` in the database or use:
   ```bash
   python -c "
   from database import SessionLocal, User
   db = SessionLocal()
   user = db.query(User).filter(User.username == 'your-username').first()
   user.is_admin = True
   db.commit()
   print('Done')
   "
   ```

---

## Architecture

```
Request → Rate Limiter → Quota Check → PII Scan
       → RAG Retrieval (optional) → Custom QA Match
       → AI API Call (w/ exponential backoff retry)
       → Token Counting → DB Persistence
       → Background: auto-title, quota warnings
```

### Key Modules

| File | Responsibility |
|------|---------------|
| `routers/chat.py` | Chat endpoint, streaming, RAG, web search |
| `routers/auth.py` | Registration, login, JWT, password reset |
| `routers/admin.py` | User management, usage stats, error logs |
| `routers/rag_routes.py` | Document upload, vector search, categories |
| `pii_detector.py` | PII detection and masking |
| `rag_service.py` | ChromaDB + embedding + re-ranking |
| `semantic_cache.py` | Embedding-based response cache |
| `rate_limiter.py` | Sliding-window rate limiting |
| `quota_manager.py` | Monthly token/cost limits per user |

---

## Environment Variables

See `.env.example` for the full list. Only two are required to get started:

```
OPENAI_KEY        # Your OpenAI API key
JWT_SECRET_KEY    # Any random 32-character string
```

Everything else (Gemini, web search, email, Redis) is optional and degrades gracefully when not configured.
