"""
認證 API 路由
"""
import hashlib
import time
from fastapi import APIRouter, Depends, HTTPException, status, Request, Body
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from database import get_db, User, RefreshToken
from auth import (
    hash_password, verify_password, create_access_token, decode_access_token,
    revoke_access_token_jti, create_refresh_token_db,
    UserCreate, UserLogin, Token, UserResponse,
    validate_password_strength, validate_username
)
from email_service import (
    send_welcome_email, send_reset_password_email,
    generate_reset_token
)
from password_policy import check_password_expired, mark_password_changed, force_user_password_change
from core import get_current_user, log_audit, safe_create_task
from datetime import datetime

router = APIRouter()


@router.post(
    "/auth/register",
    response_model=UserResponse,
    tags=["認證"],
    summary="用戶註冊",
    response_description="成功建立的用戶資訊（不含密碼）",
    responses={
        400: {"description": "Email 網域不符、帳號已存在、密碼強度不足"},
        201: {"description": "註冊成功，驗證信已寄出"},
    },
)
async def register(user_data: UserCreate, db: Session = Depends(get_db)):
    """
    用戶註冊

    步驟：
    1. 驗證使用者名稱和密碼格式
    2. 檢查使用者名稱和 email 是否已存在
    3. 加密密碼並建立用戶（自動啟用）
    """
    # 驗證使用者名稱
    valid, msg = validate_username(user_data.username)
    if not valid:
        raise HTTPException(status_code=400, detail=msg)

    # 驗證密碼強度
    valid, msg = validate_password_strength(user_data.password)
    if not valid:
        raise HTTPException(status_code=400, detail=msg)

    # 檢查使用者名稱是否已存在
    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(status_code=400, detail="使用者名稱已被使用")

    # 檢查 email 是否已存在
    if db.query(User).filter(User.email == user_data.email).first():
        raise HTTPException(status_code=400, detail="Email 已被使用")

    # 建立新用戶（自動啟用，無需 Email 驗證）
    new_user = User(
        username=user_data.username,
        email=user_data.email,
        hashed_password=hash_password(user_data.password),
        full_name=user_data.full_name,
        department=user_data.department,
        is_active=True,   # 自動啟用
        is_admin=False,
        email_verified=True,
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    log_audit(db, "USER_CREATE", actor=new_user,
              resource_type="user", resource_id=new_user.id,
              details={"username": new_user.username, "email": new_user.email,
                       "department": new_user.department})

    return new_user


@router.get(
    "/auth/verify-email",
    tags=["認證"],
    summary="Email 驗證",
    response_description="驗證成功後重導向至登入頁",
    responses={
        400: {"description": "驗證連結無效或已過期"},
    },
)
async def verify_email(token: str, db: Session = Depends(get_db)):
    """
    驗證 Email 並啟用帳號

    參數:
        token: 驗證 token
    """
    # 查找用戶
    user = db.query(User).filter(User.verification_token == token).first()

    if not user:
        raise HTTPException(status_code=400, detail="無效的驗證連結")

    # 檢查是否已驗證
    if user.email_verified:
        # 已驗證，直接跳轉到登入頁面
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/login.html?verified=already")

    # 檢查 token 是否過期
    if datetime.utcnow() > user.verification_token_expires:
        raise HTTPException(status_code=400, detail="驗證連結已過期，請重新註冊")

    # 啟用帳號
    user.email_verified = True
    user.is_active = True
    user.verification_token = None  # 清除 token
    user.verification_token_expires = None
    db.commit()

    # 發送歡迎郵件
    try:
        import asyncio
        login_url = "http://localhost/login.html"
        created_at = user.created_at.strftime("%Y-%m-%d %H:%M")

        safe_create_task(
            send_welcome_email(
                email=user.email,
                username=user.username,
                created_at=created_at,
                login_url=login_url
            )
        )
    except Exception as e:
        print(f"發送歡迎郵件失敗: {str(e)}")

    # 跳轉到成功頁面
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/login.html?verified=success")


@router.post(
    "/auth/login",
    response_model=Token,
    tags=["認證"],
    summary="用戶登入",
    response_description="JWT Access Token（有效期 7 天）",
    responses={
        401: {"description": "帳號或密碼錯誤"},
        403: {"description": "Email 尚未驗證或帳號未啟用"},
    },
)
async def login(user_data: UserLogin, request: Request, db: Session = Depends(get_db)):
    """
    用戶登入

    步驟：
    1. 查找用戶
    2. 驗證密碼
    3. 檢查帳號啟用狀態
    4. 檢查密碼是否過期
    5. 生成 JWT token
    """
    # 查找用戶
    user = db.query(User).filter(User.username == user_data.username).first()

    if not user:
        log_audit(db, "USER_LOGIN_FAIL", resource_type="user",
                  details={"username": user_data.username, "reason": "user_not_found"},
                  ip_address=request.client.host if request.client else None,
                  user_agent=request.headers.get("user-agent"))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="帳號或密碼錯誤"
        )

    # 驗證密碼
    if not verify_password(user_data.password, user.hashed_password):
        log_audit(db, "USER_LOGIN_FAIL", actor=user,
                  resource_type="user", resource_id=user.id,
                  details={"reason": "wrong_password"},
                  ip_address=request.client.host if request.client else None,
                  user_agent=request.headers.get("user-agent"))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="帳號或密碼錯誤"
        )

    # 檢查帳號是否啟用
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="帳號已被停用"
        )

    # 密碼過期政策（demo 版停用）
    force_change = False
    days_until_expiry = 0
    password_status = {"should_warn": False}

    # 生成 access token + refresh token
    access_token  = create_access_token(data={"sub": user.username})
    refresh_token = create_refresh_token_db(
        user_id=user.id,
        db=db,
        user_agent=request.headers.get("user-agent"),
    )

    log_audit(db, "USER_LOGIN", actor=user,
              resource_type="user", resource_id=user.id,
              ip_address=request.client.host if request.client else None,
              user_agent=request.headers.get("user-agent"))

    response = {
        "access_token":  access_token,
        "token_type":    "bearer",
        "refresh_token": refresh_token,
    }

    # 密碼即將過期提醒
    should_warn = password_status.get("should_warn", False) if not force_change else False
    if should_warn:
        response["password_warning"] = {
            "days_until_expiry": days_until_expiry,
            "message": f"您的密碼將在 {days_until_expiry} 天後過期，請儘早更換。"
        }

    return response


@router.get("/auth/me", response_model=UserResponse, tags=["認證"], summary="取得目前登入用戶資訊")
async def get_me(current_user: User = Depends(get_current_user)):
    """
    獲取當前登入用戶的資訊
    """
    return current_user


@router.post("/auth/refresh", tags=["認證"], summary="更新 Access Token（使用 Refresh Token）")
async def refresh_access_token(
    body: dict = Body(..., examples={"default": {"value": {"refresh_token": "<refresh_token>"}}}),
    db: Session = Depends(get_db),
):
    """
    使用 Refresh Token 取得新的 Access Token。
    同時執行 Token Rotation：舊 Refresh Token 作廢，回傳新的 Refresh Token。
    """
    raw_token = body.get("refresh_token")
    if not raw_token:
        raise HTTPException(status_code=400, detail="缺少 refresh_token")

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    rt = db.query(RefreshToken).filter(
        RefreshToken.token_hash == token_hash,
        RefreshToken.is_revoked == False,
    ).first()

    if not rt:
        raise HTTPException(status_code=401, detail="無效的 refresh token")

    if datetime.utcnow() > rt.expires_at:
        raise HTTPException(status_code=401, detail="Refresh token 已過期，請重新登入")

    user = db.query(User).filter(
        User.id == rt.user_id, User.is_active == True
    ).first()
    if not user:
        raise HTTPException(status_code=401, detail="用戶不存在或已停用")

    # Token Rotation：撤銷舊 refresh token，發行新的
    rt.is_revoked = True
    rt.revoked_at = datetime.utcnow()
    db.commit()

    new_access  = create_access_token(data={"sub": user.username})
    new_refresh = create_refresh_token_db(user.id, db, rt.user_agent)

    return {
        "access_token":  new_access,
        "token_type":    "bearer",
        "refresh_token": new_refresh,
    }


@router.post("/auth/logout", tags=["認證"], summary="登出（撤銷 Token）")
async def logout(
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    body: dict = Body(default={}, examples={"default": {"value": {"refresh_token": "<refresh_token>"}}}),
    db: Session = Depends(get_db),
):
    """
    登出目前用戶：
    - 將 Access Token 的 jti 加入 Redis blacklist（立即失效）
    - 若提供 refresh_token，一併撤銷
    """
    payload = decode_access_token(credentials.credentials)
    if payload:
        jti = payload.get("jti")
        exp = payload.get("exp", 0)
        if jti:
            ttl = max(1, int(exp - time.time()))
            revoke_access_token_jti(jti, ttl)

    raw_refresh = body.get("refresh_token")
    if raw_refresh:
        token_hash = hashlib.sha256(raw_refresh.encode()).hexdigest()
        rt = db.query(RefreshToken).filter(
            RefreshToken.token_hash == token_hash
        ).first()
        if rt and not rt.is_revoked:
            rt.is_revoked = True
            rt.revoked_at = datetime.utcnow()
            db.commit()

    return {"message": "已登出"}


@router.post("/auth/forgot-password", tags=["認證"], summary="忘記密碼（發送驗證碼至 Email）")
async def forgot_password(request: dict, db: Session = Depends(get_db)):
    """
    忘記密碼 - 發送重設碼到 Email

    接收 JSON body:
    {
        "email": "user@example.com"
    }
    """
    email = request.get("email")

    if not email:
        raise HTTPException(status_code=400, detail="請提供 Email")

    # 查找用戶
    user = db.query(User).filter(User.email == email).first()

    # 即使用戶不存在也返回成功（安全考量，不洩漏用戶是否存在）
    if not user:
        return {"message": "如果該 Email 存在，重設碼已發送"}

    # 生成重設碼
    reset_token = generate_reset_token()

    # 儲存到資料庫
    from datetime import datetime, timedelta
    user.reset_token = reset_token
    user.reset_token_expires = datetime.utcnow() + timedelta(minutes=30)
    db.commit()

    # 發送郵件
    try:
        import asyncio
        import os as _os
        _base = _os.getenv("API_URL", "http://localhost:8000").rstrip("/")
        reset_url = f"{_base}/reset-password.html?token={reset_token}&email={email}"

        safe_create_task(
            send_reset_password_email(
                email=user.email,
                username=user.username,
                reset_token=reset_token,
                reset_url=reset_url
            )
        )
    except Exception as e:
        print(f"發送密碼重設郵件失敗: {str(e)}")

    return {"message": "如果該 Email 存在，重設碼已發送"}


@router.post("/auth/reset-password", tags=["認證"], summary="重設密碼（需提供驗證碼）")
async def reset_password(
    request: dict,
    db: Session = Depends(get_db)
):
    """
    重設密碼 - 使用重設碼

    接收 JSON body:
    {
        "email": "user@example.com",
        "reset_token": "123456",
        "new_password": "NewPassword123"
    }
    """
    email = request.get("email")
    reset_token = request.get("reset_token")
    new_password = request.get("new_password")

    if not email or not reset_token or not new_password:
        raise HTTPException(status_code=400, detail="缺少必要參數")

    # 查找用戶
    user = db.query(User).filter(User.email == email).first()

    if not user:
        raise HTTPException(status_code=400, detail="無效的重設碼")

    # 驗證重設碼
    from datetime import datetime
    if not user.reset_token or user.reset_token != reset_token:
        raise HTTPException(status_code=400, detail="無效的重設碼")

    if not user.reset_token_expires or datetime.utcnow() > user.reset_token_expires:
        raise HTTPException(status_code=400, detail="重設碼已過期")

    # 驗證新密碼強度
    valid, msg = validate_password_strength(new_password)
    if not valid:
        raise HTTPException(status_code=400, detail=msg)

    # 更新密碼
    user.hashed_password = hash_password(new_password)
    user.reset_token = None
    user.reset_token_expires = None

    # 更新密碼更新時間
    mark_password_changed(user, db)

    log_audit(db, "PASSWORD_RESET", actor=user,
              resource_type="user", resource_id=user.id)

    return {"message": "密碼重設成功，請使用新密碼登入"}


@router.post("/auth/change-password", tags=["認證"], summary="修改密碼（需登入）")
async def change_password(
    request: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    更換密碼 - 已登入用戶

    接收 JSON body:
    {
        "old_password": "OldPassword123",
        "new_password": "NewPassword123"
    }
    """
    old_password = request.get("old_password")
    new_password = request.get("new_password")

    if not old_password or not new_password:
        raise HTTPException(status_code=400, detail="缺少必要參數")

    # 驗證舊密碼
    if not verify_password(old_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="舊密碼錯誤")

    # 驗證新密碼強度
    valid, msg = validate_password_strength(new_password)
    if not valid:
        raise HTTPException(status_code=400, detail=msg)

    # 檢查新密碼不能與舊密碼相同
    if verify_password(new_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="新密碼不能與舊密碼相同")

    # 更新密碼
    current_user.hashed_password = hash_password(new_password)

    # 更新密碼更新時間
    mark_password_changed(current_user, db)

    log_audit(db, "PASSWORD_CHANGE", actor=current_user,
              resource_type="user", resource_id=current_user.id)

    return {"message": "密碼更換成功"}
