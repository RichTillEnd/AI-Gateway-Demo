"""
速率限制模組 - 滑動窗口計數
防止 API 濫用

優先使用 Redis（原子性 sliding window，支援多 instance 部署）。
若 REDIS_URL 未設定或 Redis 不可用，自動 fallback 到 DB 實作。
"""

import os
import time
import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from database import RateLimit

logger = logging.getLogger(__name__)

RATE_LIMITS = {
    "requests_per_minute": 5,
    "requests_per_hour":   60,
    "requests_per_day":    100,
}

ADMIN_UNLIMITED = True

# ── Redis 初始化 ──────────────────────────────────────────────────────────────

_redis_client = None

def _get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    redis_url = os.getenv("REDIS_URL", "")
    if not redis_url:
        return None
    try:
        import redis
        client = redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2)
        client.ping()
        _redis_client = client
        logger.info("Rate limiter: Redis connected (%s)", redis_url)
        return _redis_client
    except Exception as e:
        logger.warning("Rate limiter: Redis unavailable (%s), falling back to DB", e)
        return None


# ── Redis sliding window (Lua script，原子操作) ──────────────────────────────

# KEYS[1] = redis key, ARGV[1] = window seconds, ARGV[2] = limit, ARGV[3] = now_ms
_SLIDING_WINDOW_LUA = """
local key    = KEYS[1]
local window = tonumber(ARGV[1])
local limit  = tonumber(ARGV[2])
local now    = tonumber(ARGV[3])
local cutoff = now - window * 1000

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)
if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local reset_at = 0
    if #oldest > 0 then
        reset_at = math.ceil((tonumber(oldest[2]) + window * 1000) / 1000)
    end
    return {0, count, reset_at}
end
redis.call('ZADD', key, now, now)
redis.call('EXPIRE', key, window + 1)
return {1, count + 1, 0}
"""

def _redis_check(r, user_id: int) -> tuple[bool, str]:
    """
    用 Redis 執行三層 sliding window 檢查（分鐘/小時/日）。
    回傳 (can_proceed, error_msg)
    """
    now_ms = int(time.time() * 1000)
    prefix = f"rl:{user_id}"
    windows = [
        (60,       RATE_LIMITS["requests_per_minute"], "分鐘", f"{prefix}:min"),
        (3600,     RATE_LIMITS["requests_per_hour"],   "小時", f"{prefix}:hr"),
        (86400,    RATE_LIMITS["requests_per_day"],    "日",   f"{prefix}:day"),
    ]

    results = []
    try:
        pipe = r.pipeline()
        script = r.register_script(_SLIDING_WINDOW_LUA)
        for win_sec, limit, _label, key in windows:
            script(keys=[key], args=[win_sec, limit, now_ms], client=pipe)
        results = pipe.execute()
    except Exception as e:
        logger.warning("Rate limiter Redis error: %s", e)
        return None, ""  # signal fallback

    labels = ["分鐘", "小時", "日"]
    limits = [
        RATE_LIMITS["requests_per_minute"],
        RATE_LIMITS["requests_per_hour"],
        RATE_LIMITS["requests_per_day"],
    ]
    units = ["秒", "分鐘", "小時"]
    divisors = [1, 60, 3600]

    for i, res in enumerate(results):
        allowed, _count, reset_ts = int(res[0]), int(res[1]), int(res[2])
        if not allowed:
            wait_raw = max(1, reset_ts - int(time.time()))
            wait = max(1, wait_raw // divisors[i])
            return False, f"請求太頻繁，請等待 {wait} {units[i]}後再試（每{labels[i]}上限 {limits[i]} 次）"

    return True, ""


def _redis_status(r, user_id: int) -> dict | None:
    """取得 Redis 中三層計數的當前狀態。"""
    now_ms = int(time.time() * 1000)
    prefix = f"rl:{user_id}"
    keys_windows = [
        (f"{prefix}:min",  60),
        (f"{prefix}:hr",   3600),
        (f"{prefix}:day",  86400),
    ]
    try:
        pipe = r.pipeline()
        for key, win_sec in keys_windows:
            cutoff = now_ms - win_sec * 1000
            pipe.zremrangebyscore(key, "-inf", cutoff)
            pipe.zcard(key)
        raw = pipe.execute()
        counts = [raw[i * 2 + 1] for i in range(3)]
        min_used, hr_used, day_used = counts
        return {
            "is_admin": False,
            "requests_last_minute": min_used,
            "requests_last_hour":   hr_used,
            "requests_today":       day_used,
            "limits": RATE_LIMITS,
            "remaining": {
                "per_minute": max(0, RATE_LIMITS["requests_per_minute"] - min_used),
                "per_hour":   max(0, RATE_LIMITS["requests_per_hour"]   - hr_used),
                "per_day":    max(0, RATE_LIMITS["requests_per_day"]    - day_used),
            },
            "backend": "redis",
        }
    except Exception as e:
        logger.warning("Rate limiter Redis status error: %s", e)
        return None


# ── DB fallback（原實作） ────────────────────────────────────────────────────

def _get_or_create_rate_limit(user, db: Session):
    from database import RateLimit
    rl = db.query(RateLimit).filter(RateLimit.user_id == user.id).first()
    if not rl:
        now = datetime.utcnow()
        rl = RateLimit(
            user_id=user.id,
            requests_last_minute=0,
            requests_last_hour=0,
            requests_today=0,
            last_request_time=now,
            minute_reset_time=now + timedelta(minutes=1),
            hour_reset_time=now + timedelta(hours=1),
            day_reset_time=now + timedelta(days=1),
        )
        db.add(rl)
        db.commit()
        db.refresh(rl)
    return rl


def _db_check(user, db: Session) -> tuple[bool, str]:
    rl = _get_or_create_rate_limit(user, db)
    now = datetime.utcnow()

    if now >= rl.minute_reset_time:
        rl.requests_last_minute = 0
        rl.minute_reset_time    = now + timedelta(minutes=1)
    if now >= rl.hour_reset_time:
        rl.requests_last_hour = 0
        rl.hour_reset_time    = now + timedelta(hours=1)
    if now >= rl.day_reset_time:
        rl.requests_today = 0
        rl.day_reset_time = now + timedelta(days=1)

    if rl.requests_last_minute >= RATE_LIMITS["requests_per_minute"]:
        wait = int((rl.minute_reset_time - now).total_seconds()) + 1
        return False, f"請求太頻繁，請等待 {wait} 秒後再試"
    if rl.requests_last_hour >= RATE_LIMITS["requests_per_hour"]:
        wait_min = int((rl.hour_reset_time - now).total_seconds() / 60) + 1
        return False, f"每小時請求次數已達上限（{RATE_LIMITS['requests_per_hour']} 次），請等待 {wait_min} 分鐘"
    if rl.requests_today >= RATE_LIMITS["requests_per_day"]:
        wait_hr = int((rl.day_reset_time - now).total_seconds() / 3600) + 1
        return False, f"今日請求次數已達上限（{RATE_LIMITS['requests_per_day']} 次），請等待 {wait_hr} 小時"

    rl.requests_last_minute += 1
    rl.requests_last_hour   += 1
    rl.requests_today       += 1
    rl.last_request_time    = now
    db.commit()
    return True, ""


def _db_status(user, db: Session) -> dict:
    rl = _get_or_create_rate_limit(user, db)
    now = datetime.utcnow()
    min_used  = rl.requests_last_minute if now < rl.minute_reset_time else 0
    hour_used = rl.requests_last_hour   if now < rl.hour_reset_time   else 0
    day_used  = rl.requests_today       if now < rl.day_reset_time    else 0
    return {
        "is_admin": False,
        "requests_last_minute": min_used,
        "requests_last_hour":   hour_used,
        "requests_today":       day_used,
        "limits": RATE_LIMITS,
        "remaining": {
            "per_minute": max(0, RATE_LIMITS["requests_per_minute"] - min_used),
            "per_hour":   max(0, RATE_LIMITS["requests_per_hour"]   - hour_used),
            "per_day":    max(0, RATE_LIMITS["requests_per_day"]    - day_used),
        },
        "backend": "db",
    }


# ── Public API（呼叫端不需修改） ─────────────────────────────────────────────

def check_rate_limit(user, db: Session) -> tuple[bool, str]:
    """
    滑動窗口速率限制。
    優先 Redis；Redis 不可用時 fallback 到 DB。
    回傳: (can_proceed: bool, error_msg: str)
    """
    if ADMIN_UNLIMITED and user.is_admin:
        return True, ""

    r = _get_redis()
    if r is not None:
        result, msg = _redis_check(r, user.id)
        if result is not None:          # None = Redis error, fallback
            return result, msg

    return _db_check(user, db)


def get_rate_limit_status(user, db: Session) -> dict:
    if ADMIN_UNLIMITED and user.is_admin:
        return {"is_admin": True, "requests_last_minute": 0,
                "requests_last_hour": 0, "requests_today": 0}

    r = _get_redis()
    if r is not None:
        status = _redis_status(r, user.id)
        if status is not None:
            return status

    return _db_status(user, db)
