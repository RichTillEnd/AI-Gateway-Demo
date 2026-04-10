"""
Email 通知系統
支援各種 Email 通知功能
若未設定 SMTP 環境變數，所有發信函數將靜默略過（no-op）。
"""

import os
from typing import List
from pydantic import EmailStr
from jinja2 import Template
import secrets

# Email 設定（選填，未設定則停用）
_MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
_MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
_MAIL_FROM     = os.getenv("MAIL_FROM", _MAIL_USERNAME)
_MAIL_ENABLED  = bool(_MAIL_USERNAME and _MAIL_PASSWORD)

fm = None
if _MAIL_ENABLED:
    try:
        from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
        conf = ConnectionConfig(
            MAIL_USERNAME=_MAIL_USERNAME,
            MAIL_PASSWORD=_MAIL_PASSWORD,
            MAIL_FROM=_MAIL_FROM,
            MAIL_PORT=int(os.getenv("MAIL_PORT", "587")),
            MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp.gmail.com"),
            MAIL_STARTTLS=True,
            MAIL_SSL_TLS=False,
            USE_CREDENTIALS=True,
            VALIDATE_CERTS=True
        )
        fm = FastMail(conf)
    except Exception as e:
        print(f"[email_service] Email 設定失敗，停用發信功能: {e}")
        _MAIL_ENABLED = False


# ==================== Email 模板 ====================

EMAIL_VERIFICATION_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <style>
        body {
            font-family: Arial, sans-serif;
            line-height: 1.6;
            color: #333;
        }
        .container {
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
        }
        .header {
            background: #10a37f;
            color: white;
            padding: 20px;
            text-align: center;
            border-radius: 8px 8px 0 0;
        }
        .content {
            background: #f9f9f9;
            padding: 30px;
            border-radius: 0 0 8px 8px;
        }
        .button {
            display: inline-block;
            padding: 15px 40px;
            background: #10a37f;
            color: white !important;
            text-decoration: none;
            border-radius: 6px;
            margin: 20px 0;
            font-weight: bold;
        }
        .warning {
            background: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 12px;
            margin: 15px 0;
        }
        .footer {
            text-align: center;
            margin-top: 20px;
            color: #666;
            font-size: 12px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>✉️ 驗證您的 Email</h1>
        </div>
        <div class="content">
            <h2>嗨，{{ username }}！</h2>
            <p>感謝您註冊 AI Gateway 系統。</p>
            
            <p>為了確保帳號安全，請點擊下方按鈕驗證您的 Email 地址：</p>
            
            <div style="text-align: center;">
                <a href="{{ verification_url }}" class="button">驗證 Email 並啟用帳號</a>
            </div>
            
            <div class="warning">
                <strong>⚠️ 重要提醒：</strong>
                <ul style="margin: 5px 0;">
                    <li>此驗證連結將在 <strong>24 小時</strong>後失效</li>
                    <li>在驗證 Email 之前，您將無法登入系統</li>
                    <li>如果您沒有註冊此帳號，請忽略此郵件</li>
                </ul>
            </div>
            
            <p>如果上方按鈕無法點擊，請複製以下連結到瀏覽器：</p>
            <p style="word-break: break-all; color: #666; font-size: 12px;">
                {{ verification_url }}
            </p>
            
            <p style="margin-top: 30px;">
                <strong>帳號資訊：</strong><br>
                使用者名稱：{{ username }}<br>
                Email：{{ email }}<br>
                註冊時間：{{ created_at }}
            </p>
        </div>
        <div class="footer">
            <p>此為系統自動發送的郵件，請勿直接回覆。</p>
            <p>© 2026 AI Gateway. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
"""

WELCOME_EMAIL_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <style>
        body {
            font-family: Arial, sans-serif;
            line-height: 1.6;
            color: #333;
        }
        .container {
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
        }
        .header {
            background: #10a37f;
            color: white;
            padding: 20px;
            text-align: center;
            border-radius: 8px 8px 0 0;
        }
        .content {
            background: #f9f9f9;
            padding: 30px;
            border-radius: 0 0 8px 8px;
        }
        .button {
            display: inline-block;
            padding: 12px 30px;
            background: #10a37f;
            color: white;
            text-decoration: none;
            border-radius: 6px;
            margin: 20px 0;
        }
        .footer {
            text-align: center;
            margin-top: 20px;
            color: #666;
            font-size: 12px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎉 歡迎加入 AI Gateway！</h1>
        </div>
        <div class="content">
            <h2>嗨，{{ username }}！</h2>
            <p>感謝您註冊 AI Gateway。您的帳號已經成功建立！</p>
            
            <p><strong>帳號資訊：</strong></p>
            <ul>
                <li>使用者名稱：{{ username }}</li>
                <li>Email：{{ email }}</li>
                <li>註冊時間：{{ created_at }}</li>
            </ul>
            
            <p>您現在可以開始使用 AI Gateway 的所有功能：</p>
            <ul>
                <li>✓ 與 OpenAI GPT-4o 對話</li>
                <li>✓ 與 Google Gemini 對話</li>
                <li>✓ 上傳檔案分析</li>
                <li>✓ 查看對話歷史</li>
            </ul>
            
            <div style="text-align: center;">
                <a href="{{ login_url }}" class="button">立即登入</a>
            </div>
            
            <p>如果您有任何問題，歡迎隨時聯繫我們。</p>
        </div>
        <div class="footer">
            <p>此為系統自動發送的郵件，請勿直接回覆。</p>
            <p>© 2026 AI Gateway All rights reserved.</p>
        </div>
    </div>
</body>
</html>
"""

RESET_PASSWORD_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <style>
        body {
            font-family: Arial, sans-serif;
            line-height: 1.6;
            color: #333;
        }
        .container {
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
        }
        .header {
            background: #ef4444;
            color: white;
            padding: 20px;
            text-align: center;
            border-radius: 8px 8px 0 0;
        }
        .content {
            background: #f9f9f9;
            padding: 30px;
            border-radius: 0 0 8px 8px;
        }
        .button {
            display: inline-block;
            padding: 12px 30px;
            background: #ef4444;
            color: white;
            text-decoration: none;
            border-radius: 6px;
            margin: 20px 0;
        }
        .token-box {
            background: white;
            border: 2px dashed #ef4444;
            padding: 15px;
            text-align: center;
            font-size: 24px;
            font-weight: bold;
            letter-spacing: 3px;
            margin: 20px 0;
        }
        .warning {
            background: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 12px;
            margin: 15px 0;
        }
        .footer {
            text-align: center;
            margin-top: 20px;
            color: #666;
            font-size: 12px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🔐 密碼重設請求</h1>
        </div>
        <div class="content">
            <h2>嗨，{{ username }}！</h2>
            <p>我們收到了您的密碼重設請求。</p>
            
            <p>請點擊下方按鈕來重設您的密碼：</p>
            
            <div style="text-align: center;">
                <a href="{{ reset_url }}" class="button">重設密碼</a>
            </div>
            
            <div class="warning">
                <strong>⚠️ 安全提醒：</strong>
                <ul style="margin: 5px 0;">
                    <li>此重設碼將在 <strong>30 分鐘</strong>後失效</li>
                    <li>請勿將此重設碼分享給任何人</li>
                    <li>如果您沒有請求重設密碼，請忽略此郵件</li>
                </ul>
            </div>
            
            <p>如果上方按鈕無法點擊，請複製以下連結到瀏覽器：</p>
            <p style="word-break: break-all; color: #666; font-size: 12px;">
                {{ reset_url }}
            </p>
        </div>
        <div class="footer">
            <p>此為系統自動發送的郵件，請勿直接回覆。</p>
            <p>© 2026 AI Gateway All rights reserved.</p>
        </div>
    </div>
</body>
</html>
"""

QUOTA_WARNING_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <style>
        body {
            font-family: Arial, sans-serif;
            line-height: 1.6;
            color: #333;
        }
        .container {
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
        }
        .header {
            background: #f59e0b;
            color: white;
            padding: 20px;
            text-align: center;
            border-radius: 8px 8px 0 0;
        }
        .content {
            background: #f9f9f9;
            padding: 30px;
            border-radius: 0 0 8px 8px;
        }
        .stats-box {
            background: white;
            border: 1px solid #ddd;
            padding: 15px;
            margin: 15px 0;
            border-radius: 6px;
        }
        .progress-bar {
            background: #e5e5e5;
            height: 30px;
            border-radius: 15px;
            overflow: hidden;
            margin: 10px 0;
        }
        .progress-fill {
            background: #f59e0b;
            height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: bold;
        }
        .footer {
            text-align: center;
            margin-top: 20px;
            color: #666;
            font-size: 12px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>⚠️ 使用配額提醒</h1>
        </div>
        <div class="content">
            <h2>嗨，{{ username }}！</h2>
            <p>您的 AI Gateway 使用量已達到配額的 <strong>{{ usage_percent }}%</strong>。</p>
            
            <div class="stats-box">
                <h3>本月使用統計</h3>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: {{ usage_percent }}%;">
                        {{ usage_percent }}%
                    </div>
                </div>
                <ul>
                    <li>已使用：{{ used_count }} 次請求</li>
                    <li>總配額：{{ total_quota }} 次請求</li>
                    <li>剩餘：{{ remaining }} 次請求</li>
                    <li>本月成本：${{ total_cost }}</li>
                </ul>
            </div>
            
            <p>為了確保服務不中斷，請注意您的使用量。</p>
            
            <p><strong>建議：</strong></p>
            <ul>
                <li>優先使用 Gemini（成本較低）</li>
                <li>精簡提問內容</li>
                <li>避免重複問題</li>
            </ul>
            
            <p>如需增加配額，請聯繫管理員。</p>
        </div>
        <div class="footer">
            <p>此為系統自動發送的郵件，請勿直接回覆。</p>
            <p>© 2026 AI Gateway. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
"""


# ==================== Email 發送函數 ====================

async def send_verification_email(email: str, username: str, verification_url: str, created_at: str):
    """發送 Email 驗證郵件（未設定 SMTP 則略過）"""
    if not _MAIL_ENABLED or fm is None:
        return False
    from fastapi_mail import MessageSchema, MessageType
    template = Template(EMAIL_VERIFICATION_TEMPLATE)
    html = template.render(username=username, email=email,
                           verification_url=verification_url, created_at=created_at)
    message = MessageSchema(subject="✉️ AI Gateway - 請驗證您的 Email",
                            recipients=[email], body=html, subtype=MessageType.html)
    try:
        await fm.send_message(message)
        return True
    except Exception as e:
        print(f"發送驗證郵件失敗: {str(e)}")
        return False


async def send_welcome_email(email: str, username: str, created_at: str, login_url: str):
    """發送歡迎郵件（未設定 SMTP 則略過）"""
    if not _MAIL_ENABLED or fm is None:
        return False
    from fastapi_mail import MessageSchema, MessageType
    template = Template(WELCOME_EMAIL_TEMPLATE)
    html = template.render(username=username, email=email,
                           created_at=created_at, login_url=login_url)
    message = MessageSchema(subject="🎉 歡迎加入 AI Gateway！",
                            recipients=[email], body=html, subtype=MessageType.html)
    try:
        await fm.send_message(message)
        return True
    except Exception as e:
        print(f"發送歡迎郵件失敗: {str(e)}")
        return False


async def send_reset_password_email(email: str, username: str, reset_token: str, reset_url: str):
    """發送密碼重設郵件（未設定 SMTP 則略過）"""
    if not _MAIL_ENABLED or fm is None:
        return False
    from fastapi_mail import MessageSchema, MessageType
    template = Template(RESET_PASSWORD_TEMPLATE)
    html = template.render(username=username, reset_token=reset_token, reset_url=reset_url)
    message = MessageSchema(subject="🔐 AI Gateway - 密碼重設請求",
                            recipients=[email], body=html, subtype=MessageType.html)
    try:
        await fm.send_message(message)
        return True
    except Exception as e:
        print(f"發送密碼重設郵件失敗: {str(e)}")
        return False


async def send_quota_warning_email(
    email: str, 
    username: str, 
    usage_percent: int,
    used_count: int,
    total_quota: int,
    total_cost: float
):
    """
    發送配額警告郵件
    """
    remaining = total_quota - used_count
    
    template = Template(QUOTA_WARNING_TEMPLATE)
    html = template.render(
        username=username,
        usage_percent=usage_percent,
        used_count=used_count,
        total_quota=total_quota,
        remaining=remaining,
        total_cost=f"{total_cost:.2f}"
    )
    
    if not _MAIL_ENABLED or fm is None:
        return False
    from fastapi_mail import MessageSchema, MessageType
    message = MessageSchema(
        subject=f"⚠️ AI Gateway - 使用配額已達 {usage_percent}%",
        recipients=[email],
        body=html,
        subtype=MessageType.html
    )
    try:
        await fm.send_message(message)
        return True
    except Exception as e:
        print(f"發送配額警告郵件失敗: {str(e)}")
        return False


# ==================== 重設碼管理 ====================

# 儲存重設碼（實際應該用 Redis 或資料庫）
reset_tokens = {}

def generate_reset_token() -> str:
    """
    生成密碼重設 token（URL-safe，256 bits entropy）。
    取代舊的 6 位數字碼，防止暴力破解。
    """
    return secrets.token_urlsafe(32)


def store_reset_token(email: str, token: str, expires_minutes: int = 30):
    """儲存重設碼"""
    from datetime import datetime, timedelta
    expires_at = datetime.utcnow() + timedelta(minutes=expires_minutes)
    reset_tokens[email] = {
        'token': token,
        'expires_at': expires_at
    }


def verify_reset_token(email: str, token: str) -> bool:
    """驗證重設碼"""
    from datetime import datetime
    
    if email not in reset_tokens:
        return False
    
    stored = reset_tokens[email]
    
    # 檢查是否過期
    if datetime.utcnow() > stored['expires_at']:
        del reset_tokens[email]
        return False
    
    # 檢查 token 是否正確
    if stored['token'] != token:
        return False
    
    return True


def clear_reset_token(email: str):
    """清除重設碼"""
    if email in reset_tokens:
        del reset_tokens[email]


# ==================== 測試函數 ====================

async def test_email_config():
    """
    測試 Email 設定是否正確
    """
    try:
        # 發送測試郵件
        message = MessageSchema(
            subject="AI Gateway Email 測試",
            recipients=[conf.MAIL_FROM],
            body="如果您收到這封郵件，表示 Email 設定成功！",
            subtype=MessageType.plain
        )
        
        await fm.send_message(message)
        print("✅ Email 測試成功！")
        return True
    except Exception as e:
        print(f"❌ Email 測試失敗: {str(e)}")
        return False


if __name__ == "__main__":
    import asyncio
    
    print("測試 Email 設定...")
    print(f"MAIL_SERVER: {conf.MAIL_SERVER}")
    print(f"MAIL_USERNAME: {conf.MAIL_USERNAME}")
    print(f"MAIL_FROM: {conf.MAIL_FROM}")
    
    # asyncio.run(test_email_config())
