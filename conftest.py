# conftest.py — pytest 全域設定

# test_mail.py 是手動寄信腳本（含真實帳號），不納入自動測試
collect_ignore = ["test_mail.py", "test_password_expiry.py"]
