"""
認證相關的工具函數
處理密碼加密、JWT token 生成等

安全性修復記錄：
  - [修復] SECRET_KEY 從環境變數讀取，不再寫死在程式碼中
  - [修復] 啟動時若未設定 SECRET_KEY 直接拒絕啟動（fail-fast）
  - [修復] ALLOWED_EMAIL_DOMAIN 從環境變數讀取
  - [新增] 密碼強度驗證補全大寫字母要求
"""

import os
import re
import uuid
import hashlib
import secrets
import bcrypt
from jose import JWTError, jwt
from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel


# ==================== JWT 設定（從環境變數讀取）====================

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
ALGORITHM  = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", str(60 * 24 * 7)))  # 預設 7 天
REFRESH_TOKEN_EXPIRE_DAYS  = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

# ── Fail-Fast：啟動時若未設定 SECRET_KEY 直接中止 ──────────────────
# 這確保生產環境絕對不會用到不安全的預設值
if not SECRET_KEY:
    raise RuntimeError(
        "\n\n"
        "══════════════════════════════════════════════════\n"
        "  錯誤：JWT_SECRET_KEY 未在環境變數中設定！\n"
        "  請在 .env 檔加入以下內容後重新啟動：\n"
        "  JWT_SECRET_KEY=<64位元以上的隨機字串>\n"
        "  產生方式：python3 -c \"import secrets; print(secrets.token_hex(64))\"\n"
        "══════════════════════════════════════════════════\n"
    )

if SECRET_KEY in (
    "your-secret-key-change-this-in-production-123456",
    "your-secret-key",
    "secret",
    "changeme",
    "password",
):
    raise RuntimeError(
        "\n\n"
        "══════════════════════════════════════════════════\n"
        "  錯誤：JWT_SECRET_KEY 使用了不安全的預設值！\n"
        "  請更換為真正隨機產生的字串。\n"
        "══════════════════════════════════════════════════\n"
    )

if len(SECRET_KEY) < 32:
    raise RuntimeError(
        f"錯誤：JWT_SECRET_KEY 長度不足（目前 {len(SECRET_KEY)} 字元，至少需要 32 字元）"
    )

# ── Email 網域設定（從環境變數讀取）────────────────────────────────
ALLOWED_EMAIL_DOMAIN = os.getenv("ALLOWED_EMAIL_DOMAIN", "")


# ==================== Pydantic 模型 ====================

class UserCreate(BaseModel):
    """註冊時需要的資料"""
    username: str
    email: str
    password: str
    full_name: Optional[str] = None
    department: Optional[str] = None


class UserLogin(BaseModel):
    """登入時需要的資料"""
    username: str
    password: str


class Token(BaseModel):
    """Token 回傳格式"""
    access_token: str
    token_type: str
    refresh_token: Optional[str] = None  # 登入時一併回傳，舊 client 忽略此欄位


class TokenData(BaseModel):
    """Token 中包含的資料"""
    username: Optional[str] = None


class UserResponse(BaseModel):
    """用戶資訊回傳格式（不包含密碼）"""
    id: int
    username: str
    email: str
    full_name: Optional[str]
    department: Optional[str]
    is_active: bool
    is_admin: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ==================== 密碼處理函數 ====================

def hash_password(password: str) -> str:
    """
    將明文密碼加密（bcrypt，work factor=12）

    work factor=12 比預設的 10 更安全，在現代硬體上仍可接受（約 250ms）。
    暴力破解時每個嘗試都要等 250ms，大幅提高攻擊成本。
    """
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """驗證密碼是否正確"""
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8"),
    )


# ==================== JWT Token 處理函數 ====================

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    建立 JWT access token

    參數:
        data: 要編碼進 token 的資料（通常含 username）
        expires_delta: token 有效期（可選，預設使用環境變數設定值）

    返回:
        JWT token 字串
    """
    to_encode = data.copy()
    expire = datetime.utcnow() + (
        expires_delta if expires_delta
        else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({
        "exp": expire,
        "iat": datetime.utcnow(),
        "jti": str(uuid.uuid4()),   # 唯一 Token ID，用於即時撤銷
    })
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """
    解碼並驗證 JWT token，回傳完整 payload。
    無效或過期時回傳 None。
    """
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def verify_token(token: str) -> Optional[str]:
    """
    驗證 JWT token 並取得 username（向後相容）。
    返回: username 字串（有效時），None（無效或過期時）
    """
    payload = decode_access_token(token)
    if payload is None:
        return None
    return payload.get("sub") or None


def is_token_revoked(jti: str) -> bool:
    """
    檢查 access token 的 jti 是否已在 Redis blacklist。
    Redis 不可用時 fail-open（回傳 False），避免影響正常服務。
    """
    if not jti:
        return False
    try:
        from rate_limiter import _get_redis
        r = _get_redis()
        if r:
            return r.exists(f"token_blacklist:{jti}") > 0
    except Exception:
        pass
    return False


def revoke_access_token_jti(jti: str, ttl_seconds: int) -> None:
    """將 jti 加入 Redis blacklist，TTL = token 剩餘有效秒數。"""
    if not jti or ttl_seconds <= 0:
        return
    try:
        from rate_limiter import _get_redis
        r = _get_redis()
        if r:
            r.setex(f"token_blacklist:{jti}", ttl_seconds, "1")
    except Exception:
        pass


def create_refresh_token_db(user_id: int, db, user_agent: str = None) -> str:
    """
    生成 Refresh Token，將 SHA-256 hash 存入 DB，回傳明文 token。
    明文 token 只在此函數回傳一次，之後無法重取（僅 hash 存在 DB）。
    """
    from database import RefreshToken
    raw = secrets.token_urlsafe(48)           # 384 bits，無法暴力破解
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    rt = RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        user_agent=(user_agent or "")[:200],
    )
    db.add(rt)
    db.commit()
    return raw


# ==================== 輸入驗證函數 ====================

def validate_password_strength(password: str) -> tuple[bool, str]:
    """
    驗證密碼強度

    規則：
      - 至少 8 個字元
      - 至少 1 個大寫字母
      - 至少 1 個小寫字母
      - 至少 1 個數字
    """
    if len(password) < 8:
        return False, "密碼長度至少要 8 個字元"

    if not any(c.isupper() for c in password):
        return False, "密碼需要包含至少一個大寫字母（A-Z）"

    if not any(c.islower() for c in password):
        return False, "密碼需要包含至少一個小寫字母（a-z）"

    if not any(c.isdigit() for c in password):
        return False, "密碼需要包含至少一個數字（0-9）"

    return True, ""


def validate_username(username: str) -> tuple[bool, str]:
    """
    驗證使用者名稱格式

    規則：
      - 3 到 20 個字元
      - 只能包含英文字母和數字
    """
    if len(username) < 3:
        return False, "使用者名稱至少要 3 個字元"

    if len(username) > 20:
        return False, "使用者名稱最多 20 個字元"

    if not username.isalnum():
        return False, "使用者名稱只能包含英文字母和數字"

    return True, ""


def validate_email_domain(email: str) -> tuple[bool, str]:
    """驗證 Email 格式（demo 版：接受任何合法 Email）"""
    if "@" not in email:
        return False, "Email 格式錯誤"

    email_pattern = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')
    if not email_pattern.match(email):
        return False, "Email 格式不正確"

    return True, ""


# ==================== 自我測試（python auth.py 時執行）====================

if __name__ == "__main__":
    print("🧪 測試密碼加密...")
    test_password = "MyPassword123"
    hashed = hash_password(test_password)
    print(f"  原始密碼 : {test_password}")
    print(f"  加密後   : {hashed}")
    print(f"  驗證正確 : {verify_password(test_password, hashed)}")
    print(f"  驗證錯誤 : {verify_password('wrong', hashed)}")

    print("\n🧪 測試 JWT Token...")
    token = create_access_token({"sub": "testuser"})
    print(f"  Token  : {token[:40]}...")
    print(f"  解碼   : {verify_token(token)}")

    print("\n🧪 測試密碼強度驗證...")
    for pw in ["short", "nouppercase1", "NOLOWERCASE1", "NoDigitsHere", "Valid1pass"]:
        ok, msg = validate_password_strength(pw)
        print(f"  '{pw}' → {'✅' if ok else '❌'} {msg}")

    print("\n🧪 測試 Email 網域驗證...")
    for mail in ["user@example.com", "user@gmail.com", "notanemail"]:
        ok, msg = validate_email_domain(mail)
        print(f"  '{mail}' → {'✅' if ok else '❌'} {msg}")

    print("\n✅ 全部測試完成")
