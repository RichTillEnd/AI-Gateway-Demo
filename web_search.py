"""
網路即時搜尋服務

- Tavily API：用於 OpenAI path（關鍵字觸發）
- Google Search Grounding：由 Gemini API 直接啟用（Gemini 自動決定是否搜尋）

環境變數：
    TAVILY_API_KEY      Tavily API 金鑰（取得：https://tavily.com）
    WEB_SEARCH_ENABLED  "true" / "false"，預設 true
"""

import os

TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
WEB_SEARCH_ENABLED: bool = os.getenv("WEB_SEARCH_ENABLED", "true").lower() == "true"

# ── 觸發即時搜尋的關鍵詞 ──────────────────────────────────────────
# 出現這些詞代表用戶需要即時／最新資訊，Tavily 才值得呼叫
_REALTIME_KEYWORDS = [
    # 時間詞
    "今天", "今日", "昨天", "明天", "後天", "現在", "目前", "當前",
    "最新", "最近", "近期", "本週", "本月", "今年", "今晚", "今早",
    "幾點", "幾度",
    # 即時資訊類
    "天氣", "氣溫", "颱風", "地震",
    "股價", "股市", "匯率", "幣值", "油價", "金價", "加密貨幣",
    "新聞", "最新消息", "最新發展", "最新情況", "最新公告",
    "即時", "實時",
    # 範例搜尋情境
    "機票", "航班", "班機", "飛機票", "訂票",
    "飯店", "住宿", "旅館", "民宿", "酒店", "房價",
    "簽證", "入境", "出入境規定",
    "匯率", "當地時間",
    # 英文
    "today", "yesterday", "tomorrow", "now", "current", "latest", "recent",
    "news", "weather", "stock", "price", "flight", "hotel",
]


def needs_web_search(message: str) -> bool:
    """
    偵測訊息是否需要即時網路資訊（用於 OpenAI path Tavily 觸發判斷）。
    Gemini path 不需要此函數——grounding tool 啟用後由 Gemini 自行判斷。
    """
    if not WEB_SEARCH_ENABLED or not TAVILY_API_KEY:
        return False
    msg_lower = message.lower()
    return any(kw in msg_lower for kw in _REALTIME_KEYWORDS)


def tavily_search(query: str, max_results: int = 5) -> dict:
    """
    呼叫 Tavily API 搜尋即時資訊。

    Returns:
        {
            "success": bool,
            "answer": str,          # Tavily 直接摘要（常為空）
            "results": [...],       # 各搜尋結果 {title, content, url, score}
            "cost": float,          # 計費用（$0.01/次）
            "error": str | None,
        }
    """
    if not TAVILY_API_KEY:
        return {"success": False, "results": [], "answer": "", "cost": 0.0,
                "error": "未設定 TAVILY_API_KEY，無法執行網路搜尋"}
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=TAVILY_API_KEY)
        resp = client.search(
            query=query,
            max_results=max_results,
            include_answer=True,
            search_depth="basic",
        )
        return {
            "success": True,
            "answer": resp.get("answer") or "",
            "results": resp.get("results", []),
            "cost": 0.01,
            "error": None,
        }
    except ImportError:
        return {"success": False, "results": [], "answer": "", "cost": 0.0,
                "error": "套件未安裝，請執行：pip install tavily-python"}
    except Exception as e:
        return {"success": False, "results": [], "answer": "", "cost": 0.0,
                "error": str(e)}


def build_search_context(search_result: dict, max_content_len: int = 400) -> str:
    """
    將 Tavily 搜尋結果格式化為可注入 system prompt 的文字。
    格式與現有 RAG context 一致，方便 AI 理解。
    """
    if not search_result.get("success") or not search_result.get("results"):
        return ""

    lines = [
        "【即時網路搜尋結果】",
        "以下是針對用戶問題取得的最新網路資訊，請優先參考並標示來源 URL：",
        "",
    ]

    if search_result.get("answer"):
        lines.append(f"搜尋摘要：{search_result['answer']}")
        lines.append("")

    for i, r in enumerate(search_result["results"][:5], 1):
        title = r.get("title", "（無標題）")
        content = (r.get("content") or "")[:max_content_len]
        url = r.get("url", "")
        lines.append(f"{i}. **{title}**")
        if content:
            lines.append(f"   {content}")
        if url:
            lines.append(f"   來源：{url}")
        lines.append("")

    return "\n".join(lines)
