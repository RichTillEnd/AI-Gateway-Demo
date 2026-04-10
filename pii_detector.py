"""
PII 偵測與遮蔽模組
支援台灣常見個資類型：身分證字號、信用卡號、手機/市話、Email、護照號碼、車牌
"""

import re
import json
import os
from dataclasses import dataclass, field
from typing import List, Tuple

# ── PII 類型定義 ──────────────────────────────────────────────

@dataclass
class PiiMatch:
    pii_type: str       # 類型名稱，如 "身分證字號"
    pii_key:  str       # 類型 key，如 "tw_id"
    original: str       # 原始文字
    start:    int       # 在原文中的起始位置
    end:      int       # 結束位置


# (key, label, pattern, validation_fn)
# validation_fn: 可選，對 regex 捕獲到的字串做額外驗證（降低誤判）
_PII_PATTERNS: List[Tuple[str, str, str, object]] = [

    # 台灣身分證字號 / 居留證：首字母 + 數字10碼
    # 身分證：A-Z + 1或2 + 8位數字
    # 注意：\b 在 Python Unicode 模式下會把中文字當 \w，需改用 ASCII 邊界
    (
        "tw_id",
        "身分證字號",
        r"(?<![A-Za-z0-9])[A-Z][12][0-9]{8}(?![A-Za-z0-9])",
        None,
    ),

    # 護照號碼（台灣）：1-2 大寫字母 + 7-9 位數字
    (
        "passport",
        "護照號碼",
        r"(?<![A-Za-z0-9])[A-Z]{1,2}[0-9]{7,9}(?![A-Za-z0-9])",
        None,
    ),

    # 信用卡號：4 組 4 位數字（允許空格或連字號分隔）
    (
        "credit_card",
        "信用卡號",
        r"(?<!\d)(?:\d{4}[\s\-]?){3}\d{4}(?!\d)",
        lambda s: len(re.sub(r"[\s\-]", "", s)) == 16,
    ),

    # 台灣手機號碼：09xxxxxxxx（含 +886 格式）
    (
        "tw_mobile",
        "手機號碼",
        r"(?<!\d)(?:\+886\s?|0)9\d{2}[\s\-]?\d{3}[\s\-]?\d{3}(?!\d)",
        None,
    ),

    # 台灣市話：(0x)xxxx-xxxx 或 0x-xxxx-xxxx
    (
        "tw_phone",
        "市話號碼",
        r"(?<!\d)0[2-8]\d{1}[\s\-]?\d{3,4}[\s\-]?\d{4}(?!\d)",
        None,
    ),

    # Email
    (
        "email",
        "電子郵件",
        r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.\-])",
        None,
    ),

    # 台灣車牌：新式 3碼+4碼 或 舊式 2碼+4碼
    (
        "tw_plate",
        "車牌號碼",
        r"(?<![A-Za-z0-9])[A-Z]{2,3}[\-]?\d{4}(?![A-Za-z0-9])|(?<![A-Za-z0-9])\d{3,4}[\-]?[A-Z]{2}(?![A-Za-z0-9])",
        None,
    ),

    # 銀行帳號：14-16 位純數字
    (
        "bank_account",
        "銀行帳號",
        r"(?<!\d)\d{14,16}(?!\d)",
        None,
    ),
]

# ── 設定檔 ────────────────────────────────────────────────────

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "pii_settings.json")

_DEFAULT_CONFIG = {
    "enabled": True,
    "action": "mask",          # "mask" | "block"
    "categories": [            # 啟用的類型 key 清單（空 = 全部）
        "tw_id", "credit_card", "tw_mobile", "email", "passport"
    ],
    "log_detections": True,    # 是否記錄偵測事件到 error_logs
    "block_message": "您的訊息包含敏感個人資訊，基於資安政策已被攔截。請移除個資後重新傳送。",
}


def load_config() -> dict:
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # 補上新增欄位的預設值
            for k, v in _DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ── 核心函數 ──────────────────────────────────────────────────

def detect_pii(text: str, categories: List[str] = None) -> List[PiiMatch]:
    """
    在 text 中偵測所有 PII。
    categories: 限定偵測的類型 key 清單；None 或空清單 = 全部偵測。
    回傳依出現位置排序的 PiiMatch 清單。
    """
    active = set(categories) if categories else None
    matches: List[PiiMatch] = []

    for key, label, pattern, validator in _PII_PATTERNS:
        if active and key not in active:
            continue
        for m in re.finditer(pattern, text, re.IGNORECASE):
            raw = m.group(0)
            if validator and not validator(raw):
                continue
            matches.append(PiiMatch(
                pii_type=label,
                pii_key=key,
                original=raw,
                start=m.start(),
                end=m.end(),
            ))

    # 去重：若多個 pattern 都命中同一段文字，保留較長的那個
    matches.sort(key=lambda x: (x.start, -(x.end - x.start)))
    deduped: List[PiiMatch] = []
    last_end = -1
    for m in matches:
        if m.start >= last_end:
            deduped.append(m)
            last_end = m.end

    return deduped


def mask_pii(text: str, categories: List[str] = None) -> Tuple[str, List[PiiMatch]]:
    """
    遮蔽 text 中的 PII，回傳 (遮蔽後文字, 偵測清單)。
    遮蔽格式：[身分證字號]、[信用卡號] 等。
    """
    found = detect_pii(text, categories)
    if not found:
        return text, []

    result = []
    cursor = 0
    for m in found:
        result.append(text[cursor:m.start])
        result.append(f"[{m.pii_type}]")
        cursor = m.end
    result.append(text[cursor:])

    return "".join(result), found


def scan_message(text: str, config: dict = None) -> dict:
    """
    根據設定處理一則用戶訊息。
    回傳：
      {
        "action":     "pass" | "mask" | "block",
        "text":       處理後的文字（block 時為原文，mask 時為遮蔽後文字），
        "detections": [{"type": "身分證字號", "key": "tw_id", "original": "A123456789"}, ...]
      }
    """
    if config is None:
        config = load_config()

    if not config.get("enabled", True):
        return {"action": "pass", "text": text, "detections": []}

    categories = config.get("categories") or None
    found = detect_pii(text, categories)

    if not found:
        return {"action": "pass", "text": text, "detections": []}

    detections = [{"type": m.pii_type, "key": m.pii_key, "original": m.original} for m in found]

    action = config.get("action", "mask")
    if action == "block":
        return {"action": "block", "text": text, "detections": detections}

    # mask
    masked, _ = mask_pii(text, categories)
    return {"action": "mask", "text": masked, "detections": detections}
