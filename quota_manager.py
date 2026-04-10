"""
配額管理工具
管理用戶的 token 和成本限制
"""

from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import text
from database import User, UserQuota
import asyncio


DEFAULT_QUOTAS = {
    "monthly_token_limit": 2000000,   # 每月 50 萬 tokens
    "monthly_cost_limit": 10.0,      # 每月 $20 USD
}

QUOTA_WARNING_THRESHOLD = 0.8


def get_or_create_quota(user: User, db: Session) -> UserQuota:
    quota = db.query(UserQuota).filter(UserQuota.user_id == user.id).first()
    
    if not quota:
        quota = UserQuota(
            user_id=user.id,
            monthly_token_limit=DEFAULT_QUOTAS["monthly_token_limit"],
            monthly_cost_limit=DEFAULT_QUOTAS["monthly_cost_limit"],
            current_month_tokens=0,
            current_month_cost=0.0,
            current_month_openai_cost=0.0,
            current_month_gemini_cost=0.0,
            total_tokens=0,
            total_cost=0.0,
            total_openai_cost=0.0,
            total_gemini_cost=0.0,
            last_reset_date=datetime.utcnow()
        )
        db.add(quota)
        db.commit()
        db.refresh(quota)
    return quota


def check_quota(user: User, estimated_tokens: int, estimated_cost: float, db: Session) -> tuple[bool, str]:
    if user.is_admin:
        return True, ""
    
    quota = get_or_create_quota(user, db)
    reset_quota_if_needed(quota, db)
    
    if quota.is_quota_exceeded:
        return False, f"配額已用完，請等待下月重置"
    
    if quota.current_month_tokens + estimated_tokens > quota.monthly_token_limit:
        remaining = quota.monthly_token_limit - quota.current_month_tokens
        return False, f"Token 配額不足。剩餘：{remaining:,}，需要：{estimated_tokens:,}"
    
    if quota.current_month_cost + estimated_cost > quota.monthly_cost_limit:
        remaining = quota.monthly_cost_limit - quota.current_month_cost
        return False, f"成本配額不足。剩餘：${remaining:.2f}，需要：${estimated_cost:.2f}"
    
    return True, ""


def update_quota(user: User, tokens_used: int, cost_incurred: float, db: Session, provider: str = "openai"):
    """
    更新配額使用量。
    provider: 'openai' 或 'gemini'，用於分項計費統計。

    使用原子 SQL UPDATE 替代 ORM read-modify-write，避免並發請求時
    （TOCTOU 競態）導致計數互相覆蓋的問題。
    """
    # 確保配額列存在
    quota = get_or_create_quota(user, db)

    # ── 原子累加：直接在 DB 層做加法，不依賴 Python 讀取的舊值 ──────
    if provider == "gemini":
        db.execute(text("""
            UPDATE user_quotas SET
                current_month_tokens      = current_month_tokens + :tokens,
                current_month_cost        = current_month_cost   + :cost,
                current_month_gemini_cost = COALESCE(current_month_gemini_cost, 0) + :cost,
                total_tokens              = total_tokens + :tokens,
                total_cost                = total_cost   + :cost,
                total_gemini_cost         = COALESCE(total_gemini_cost, 0) + :cost,
                updated_at                = :now
            WHERE user_id = :uid
        """), {"tokens": tokens_used, "cost": cost_incurred,
               "now": datetime.utcnow(), "uid": user.id})
    else:
        db.execute(text("""
            UPDATE user_quotas SET
                current_month_tokens       = current_month_tokens + :tokens,
                current_month_cost         = current_month_cost   + :cost,
                current_month_openai_cost  = COALESCE(current_month_openai_cost, 0) + :cost,
                total_tokens               = total_tokens + :tokens,
                total_cost                 = total_cost   + :cost,
                total_openai_cost          = COALESCE(total_openai_cost, 0) + :cost,
                updated_at                 = :now
            WHERE user_id = :uid
        """), {"tokens": tokens_used, "cost": cost_incurred,
               "now": datetime.utcnow(), "uid": user.id})
    db.commit()

    # ── 重新讀取最新值，更新超額 / 警告 flag ──────────────────────────
    db.refresh(quota)
    token_pct = quota.current_month_tokens / max(quota.monthly_token_limit, 1)
    cost_pct  = quota.current_month_cost   / max(quota.monthly_cost_limit, 0.001)

    changed = False
    if (token_pct >= 1.0 or cost_pct >= 1.0) and not quota.is_quota_exceeded:
        quota.is_quota_exceeded = True
        changed = True
    if not quota.quota_warning_sent:
        if token_pct >= QUOTA_WARNING_THRESHOLD or cost_pct >= QUOTA_WARNING_THRESHOLD:
            quota.quota_warning_sent = True
            changed = True
    if changed:
        db.commit()


def reset_quota_if_needed(quota: UserQuota, db: Session):
    now = datetime.utcnow()
    last_reset = quota.last_reset_date
    
    if now.year > last_reset.year or (now.year == last_reset.year and now.month > last_reset.month):
        quota.current_month_tokens      = 0
        quota.current_month_cost        = 0.0
        quota.current_month_openai_cost = 0.0
        quota.current_month_gemini_cost = 0.0
        quota.is_quota_exceeded  = False
        quota.quota_warning_sent = False
        quota.last_reset_date    = now
        db.commit()
    else:
        # 同月內：若超額標記存在但實際用量在上限內，修正
        if quota.is_quota_exceeded:
            if (quota.current_month_tokens < quota.monthly_token_limit and
                    quota.current_month_cost < quota.monthly_cost_limit):
                quota.is_quota_exceeded = False
                db.commit()


def get_quota_status(user: User, db: Session) -> dict:
    if user.is_admin:
        return {"is_admin": True, "unlimited": True}
    
    quota = get_or_create_quota(user, db)
    reset_quota_if_needed(quota, db)
    
    return {
        "is_admin": False,
        "monthly_limits": {
            "tokens": quota.monthly_token_limit,
            "cost":   quota.monthly_cost_limit
        },
        "current_usage": {
            "tokens":       quota.current_month_tokens,
            "cost":         round(quota.current_month_cost, 6),
            "openai_cost":  round(quota.current_month_openai_cost or 0.0, 6),
            "gemini_cost":  round(quota.current_month_gemini_cost or 0.0, 6),
        },
        "total_usage": {
            "tokens":       quota.total_tokens,
            "cost":         round(quota.total_cost, 6),
            "openai_cost":  round(quota.total_openai_cost or 0.0, 6),
            "gemini_cost":  round(quota.total_gemini_cost or 0.0, 6),
        },
        "remaining": {
            "tokens": max(0, quota.monthly_token_limit - quota.current_month_tokens),
            "cost":   round(max(0, quota.monthly_cost_limit - quota.current_month_cost), 6)
        },
        "usage_percent": {
            "tokens": round(quota.current_month_tokens / quota.monthly_token_limit * 100, 1),
            "cost":   round(quota.current_month_cost   / quota.monthly_cost_limit   * 100, 1)
        },
        "is_exceeded": quota.is_quota_exceeded
    }
