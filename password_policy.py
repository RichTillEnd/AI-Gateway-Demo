"""
密碼安全政策工具
處理密碼過期檢查和強制更換
"""

from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from database import User


# 密碼有效期限（天數）
PASSWORD_EXPIRY_DAYS = 90  # 天

# 密碼即將過期提醒（天數）
PASSWORD_WARNING_DAYS = 7  # 剩 7 天時提醒


def check_password_expired(user: User) -> dict:
    """
    檢查用戶密碼是否過期
    
    返回:
    {
        "expired": bool,           # 是否已過期
        "days_until_expiry": int,  # 距離過期天數（負數表示已過期）
        "should_warn": bool,       # 是否應該警告
        "force_change": bool       # 是否強制更換
    }
    """
    # 管理員不受密碼過期限制
    if user.is_admin:
        return {
            "expired": False,
            "days_until_expiry": 999,
            "should_warn": False,
            "force_change": False
        }
    
    # 如果沒有 password_updated_at，使用 created_at
    password_updated_at = user.password_updated_at or user.created_at
    
    # 計算密碼年齡
    password_age = datetime.utcnow() - password_updated_at
    days_old = password_age.days
    
    # 計算距離過期天數
    days_until_expiry = PASSWORD_EXPIRY_DAYS - days_old
    
    # 判斷是否過期
    expired = days_old >= PASSWORD_EXPIRY_DAYS
    
    # 判斷是否應該警告（剩餘天數 <= 7）
    should_warn = 0 < days_until_expiry <= PASSWORD_WARNING_DAYS
    
    return {
        "expired": expired,
        "days_until_expiry": days_until_expiry,
        "should_warn": should_warn,
        "force_change": user.force_password_change or expired
    }


def mark_password_changed(user: User, db: Session):
    """
    標記密碼已更換
    
    更新 password_updated_at 並清除 force_password_change 標記
    """
    user.password_updated_at = datetime.utcnow()
    user.force_password_change = False
    db.commit()


def force_user_password_change(user: User, db: Session):
    """
    強制用戶下次登入時更換密碼
    """
    user.force_password_change = True
    db.commit()


def get_expiring_passwords_count(db: Session, days: int = 7) -> int:
    """
    獲取即將過期的密碼數量
    
    參數:
        days: 幾天內過期
    """
    cutoff_date = datetime.utcnow() - timedelta(days=PASSWORD_EXPIRY_DAYS - days)
    
    count = db.query(User).filter(
        User.is_admin == False,
        User.is_active == True,
        User.password_updated_at < cutoff_date
    ).count()
    
    return count


def get_users_with_expired_passwords(db: Session) -> list:
    """
    獲取所有密碼已過期的用戶
    """
    cutoff_date = datetime.utcnow() - timedelta(days=PASSWORD_EXPIRY_DAYS)
    
    users = db.query(User).filter(
        User.is_admin == False,
        User.is_active == True,
        User.password_updated_at < cutoff_date
    ).all()
    
    return users


def auto_mark_expired_passwords(db: Session):
    """
    自動標記所有過期密碼為需要強制更換
    
    應該在定時任務中執行（例如每天一次）
    """
    expired_users = get_users_with_expired_passwords(db)
    
    for user in expired_users:
        if not user.force_password_change:
            user.force_password_change = True
    
    db.commit()
    
    return len(expired_users)


def get_password_policy_info() -> dict:
    """
    獲取密碼政策資訊
    """
    return {
        "password_expiry_days": PASSWORD_EXPIRY_DAYS,
        "password_warning_days": PASSWORD_WARNING_DAYS,
        "applies_to_admins": False,
        "description": f"一般用戶密碼每 {PASSWORD_EXPIRY_DAYS} 天必須更換"
    }


if __name__ == "__main__":
    print("密碼安全政策設定：")
    print(f"- 密碼有效期：{PASSWORD_EXPIRY_DAYS} 天")
    print(f"- 過期前提醒：{PASSWORD_WARNING_DAYS} 天")
    print(f"- 適用對象：一般用戶（管理員除外）")
