"""
Text-to-SQL 服務
將自然語言問題轉換為 SQL 查詢，並在 SQLite 資料庫安全執行。

安全設計：
  - 只允許 SELECT 語句
  - 白名單資料表與欄位
  - 敏感欄位永遠封鎖（密碼、token 等）
  - 一般用戶自動注入 user_id 限制
  - 公開資料表（tour_groups）所有用戶皆可查，不需 user_id
  - SQLite 唯讀模式執行
"""

import re
import sqlite3
import json
from openai import OpenAI

# ──────────────────────────────────────────────────────────────────
# Schema 定義：各角色可存取的資料表與欄位
# ──────────────────────────────────────────────────────────────────

# 公開資料表：所有登入用戶皆可查，不需 user_id 過濾
PUBLIC_SCHEMA = {
    "tour_groups": {
        "columns": [
            "category_major",     # 大分類
            "category_minor",     # 小分類
            "departure_date",     # 出發日
            "group_code",         # 團體代碼
            "group_name",         # 團名
            "product_name",       # 產品名稱
            "trade_price",        # 同業價
            "direct_price",       # 直客價
            "total_seats",        # 總機位
            "reserved_seats",     # 保留
            "enrolled",           # 報名
            "available_seats",    # 可售
            "confirmed_bookings", # 收訂
            "tentative_bookings", # 略訂
            "deposit",            # 訂金
            "flight",             # 班機
            "entry_exit_point",   # 進出點
            "status",             # 狀態
            "group_type",         # 團型
            "staff_notes",        # 員工備註
        ],
        "description": (
            "旅遊團體資料表（管理員上傳的 Excel，所有用戶皆可查詢）。"
            "欄位對應：category_major=大分類, category_minor=小分類, "
            "departure_date=出發日, group_code=團體代碼, group_name=團名, "
            "product_name=產品名稱, trade_price=同業價, direct_price=直客價, "
            "total_seats=總機位, reserved_seats=保留, enrolled=報名, "
            "available_seats=可售, confirmed_bookings=收訂, tentative_bookings=略訂, "
            "deposit=訂金, flight=班機, entry_exit_point=進出點, "
            "status=狀態, group_type=團型, staff_notes=員工備註"
        ),
    },
}

# 一般用戶可查詢的資料表（包含公開資料表）
USER_SCHEMA = {
    **PUBLIC_SCHEMA,
    "conversations": {
        "columns": ["id", "title", "is_starred", "created_at", "updated_at"],
        "description": "用戶的對話記錄（每次聊天會話）",
        "user_filter": "WHERE user_id = {uid}",
    },
    "messages": {
        "columns": ["id", "conversation_id", "role", "content", "provider", "model", "created_at"],
        "description": "對話中的每條訊息（role 為 user 或 assistant）",
        "user_filter": "需 JOIN conversations ON messages.conversation_id = conversations.id WHERE conversations.user_id = {uid}",
    },
    "usage_logs": {
        "columns": ["id", "provider", "model", "input_tokens", "output_tokens", "estimated_cost", "created_at"],
        "description": "每次 AI API 呼叫的 token 用量與費用（estimated_cost 單位為美元）",
        "user_filter": "WHERE user_id = {uid}",
    },
    "user_quotas": {
        "columns": [
            "monthly_token_limit", "monthly_cost_limit",
            "current_month_tokens", "current_month_cost",
            "current_month_openai_cost", "current_month_gemini_cost",
            "total_tokens", "total_cost", "total_openai_cost", "total_gemini_cost",
            "is_quota_exceeded", "last_reset_date",
        ],
        "description": "用戶的月度配額設定與當前使用量",
        "user_filter": "WHERE user_id = {uid}",
    },
}

# 管理員額外可查詢的資料表
ADMIN_EXTRA_SCHEMA = {
    "users": {
        "columns": [
            "id", "username", "full_name", "department",
            "is_active", "is_admin", "email", "email_verified",
            "created_at", "allowed_rag_categories",
        ],
        "description": "所有用戶帳號資料（不含密碼與敏感 token）",
    },
    "error_logs": {
        "columns": [
            "id", "error_type", "error_message", "error_detail",
            "user_id", "endpoint", "is_resolved", "created_at",
        ],
        "description": "系統錯誤記錄",
    },
    "system_metrics": {
        "columns": ["id", "metric_type", "metric_value", "provider", "additional_data", "created_at"],
        "description": "系統健康指標（metric_type: API_LATENCY / ERROR_RATE / ACTIVE_USERS）",
    },
    "projects": {
        "columns": ["id", "user_id", "name", "created_at", "updated_at"],
        "description": "用戶的專案（用於分組對話）",
    },
    "rag_categories": {
        "columns": ["id", "name", "label", "description", "is_active", "sort_order"],
        "description": "RAG 知識庫分類",
    },
    "custom_qa": {
        "columns": ["id", "name", "keywords", "match_type", "is_enabled", "hit_count", "created_at"],
        "description": "自訂 QA 規則（關鍵字快速回覆），hit_count 為命中次數",
    },
}

ADMIN_SCHEMA = {**USER_SCHEMA, **ADMIN_EXTRA_SCHEMA}

# 永遠封鎖的欄位（任何角色都不能查）
BLOCKED_COLUMNS = [
    "hashed_password", "verification_token", "reset_token",
    "verification_token_expires", "reset_token_expires", "password_updated_at",
]

# 公開資料表集合（查詢這些表時不需要 user_id 過濾）
_PUBLIC_TABLES = set(PUBLIC_SCHEMA.keys())

# ──────────────────────────────────────────────────────────────────
# 意圖偵測：判斷訊息是否應觸發 Text-to-SQL
# ──────────────────────────────────────────────────────────────────

_TRIGGER_PATTERNS = [
    # 系統用量查詢
    r"用了多少|花了多少|花費|費用|成本|token用量|使用量|用量",
    r"幾個對話|幾條訊息|對話數量|對話記錄|查詢對話",
    r"排行|排名|統計.*用量|統計.*費用|最多.*用|最高.*費|平均.*費|平均.*token",
    r"配額|quota|剩餘.*token|token.*剩餘|超額",
    r"哪些用戶|哪個用戶|用戶列表|所有用戶.*用|部門.*用量|部門.*費用",
    r"本月.*費|上月.*費|本月.*token|今天.*用|最近\d+天|本週.*用",
    r"查詢.*用量|查看.*費用|列出.*對話|顯示.*用量|幫我查.*費|幫我看.*用量",
    r"錯誤日誌|error log|系統錯誤.*次|哪些錯誤.*多",
    # 旅遊團體查詢
    r"哪些團|哪個團|有哪些團|團體列表|團體資料|查詢.*團|列出.*團|顯示.*團",
    r"出發日|出發日期|幾號出發|幾月出發|哪天出發|\d+月出發|本月出發|這個月出發|場次.*最多|最多.*場次|哪天.*最多|最多.*出發|同一天.*出發|出發.*最多",
    r"機位|可售|報名|收訂|略訂|訂金|剩餘.*位|還有.*位",
    r"同業價|直客價|團費|報價",
    r"大分類|小分類|團名|產品名稱|團體代碼|團型",
    r"班機|進出點",
    r"幾個團|共有幾|多少團|幾筆.*團|團.*幾筆",
    r"員工備註|備註.*團|團.*備註",
    r"查行程|查團|找.*團|搜尋.*團|哪些行程",
    r"額滿|已滿|狀態.*團|團.*狀態",
    r"確定出發.*團|團.*確定出發|開放報名.*團|團.*開放報名|已出發.*團|團.*已出發",
    r"所有.*團|全部.*團|列出.*行程|所有行程",
    r"日本.*團|韓國.*團|泰國.*團|歐洲.*團|美洲.*團|亞洲.*團|越南.*團|法國.*團",
    r"取消.*團|候補.*團|團.*取消|團.*候補",
]

_EXCLUDE_PATTERNS = [
    r"怎麼|如何|教我|幫我寫|建議|解釋|是什麼|什麼是|為什麼|怎樣",
    r"python|javascript|寫一個.*程式|範例|code|sql語法|sql怎麼",
]

# 需要管理員權限的跨用戶查詢意圖（一般用戶應直接拒絕）
_ADMIN_REQUIRED_PATTERNS = [
    r"哪些用戶|哪個用戶|所有用戶|每個用戶|各用戶|用戶排行|用戶排名",
    r"哪個部門|各部門|部門排行|部門統計|部門用量",
    r"誰用最多|誰花最多|誰用了最多|誰最貴",
    r"所有人的|大家的.*費|全部用戶|所有帳號",
    r"比較.*用戶|用戶.*比較|用戶.*對比",
    r"比其他人|比別人|比大家|跟別人|跟其他人|和別人|和其他人",
    r"高於平均|低於平均|高於.*平均|低於.*平均|比平均",
    r"其他人.*費|其他用戶.*費|平均.*費用|費用.*平均|平均.*花費|平均.*用量|用量.*平均",
    r"比.*高嗎|比.*低嗎|算多嗎|算少嗎|正常嗎.*費|費用.*正常嗎",
]

_trigger_re = [re.compile(p, re.IGNORECASE) for p in _TRIGGER_PATTERNS]
_exclude_re = [re.compile(p, re.IGNORECASE) for p in _EXCLUDE_PATTERNS]
_admin_required_re = [re.compile(p, re.IGNORECASE) for p in _ADMIN_REQUIRED_PATTERNS]


# ──────────────────────────────────────────────────────────────────
# TextToSQLService
# ──────────────────────────────────────────────────────────────────

class TextToSQLService:
    def __init__(self, openai_api_key: str, db_path: str = "./ai_gateway.db"):
        self.client = OpenAI(api_key=openai_api_key)
        self.db_path = db_path

    # ── 意圖偵測 ──────────────────────────────────────────────────

    def is_sql_query_intent(self, message: str) -> bool:
        """偵測訊息是否為資料查詢意圖"""
        for pattern in _exclude_re:
            if pattern.search(message):
                return False
        for pattern in _trigger_re:
            if pattern.search(message):
                return True
        return False

    # ── Schema Context ────────────────────────────────────────────

    def _build_schema_context(self, is_admin: bool, user_id: int) -> str:
        schema = ADMIN_SCHEMA if is_admin else USER_SCHEMA
        lines = ["## 資料庫 Schema（SQLite）\n"]
        for table, info in schema.items():
            lines.append(f"### {table}")
            lines.append(f"說明：{info['description']}")
            lines.append(f"欄位：{', '.join(info['columns'])}")
            if table in _PUBLIC_TABLES:
                lines.append("⚠️ 公開資料表，不需 user_id 過濾，所有用戶皆可查詢全部資料")
            elif not is_admin and "user_filter" in info:
                filter_hint = info["user_filter"].replace("{uid}", str(user_id))
                lines.append(f"⚠️ 必須加入限制：{filter_hint}")
            lines.append("")
        return "\n".join(lines)

    # ── SQL 生成 ──────────────────────────────────────────────────

    def generate_sql(self, message: str, is_admin: bool, user_id: int) -> str:
        """呼叫 LLM 將自然語言轉換為 SQL"""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        role_hint = (
            "管理員（可查所有用戶資料，不需加 user_id 限制）"
            if is_admin
            else (
                f"一般用戶（user_id={user_id}，"
                "查詢個人資料時必須嚴格按 ⚠️ 限制過濾；"
                "查詢 tour_groups 時不需任何 user_id 過濾）"
            )
        )

        system_prompt = f"""你是 Text-to-SQL 轉換引擎，根據自然語言產生 SQLite SELECT 查詢。

規則：
1. 只能產生 SELECT 語句，絕對禁止 INSERT/UPDATE/DELETE/DROP/ALTER/PRAGMA
2. 只能使用 schema 列出的資料表和欄位
3. 永遠禁止查詢欄位：{', '.join(BLOCKED_COLUMNS)}
4. 今天日期：{today}（時間查詢請用 DATE('now') 或 strftime）
5. 當前用戶角色：{role_hint}
6. 結果加 LIMIT 100（除非問題明確要求全部）
7. estimated_cost 單位為美元（美金）
8. tour_groups 的 departure_date 格式為 YYYY/MM/DD（斜線），例如「2026/07/01」。
    日期比較直接用字串比對即可（如 departure_date >= '2026/07/01'），
    月份查詢用 LIKE '2026/03/%'，不要用 strftime 或 DATE() 轉換。
9. 只輸出純 SQL，不要任何說明或 markdown 包裝
10. 【重要】tour_groups 所有欄位皆為 TEXT 型別（從 Excel 匯入）。做數值比較時必須用 CAST，例如：
    - CAST(direct_price AS REAL) > 100000
    - CAST(available_seats AS INTEGER) > 20
    - CAST(total_seats AS INTEGER)
    嚴禁直接用文字比較數字欄位，否則結果完全錯誤（'45000' > '100000' 在文字比較下為真）。
11. 【重要】不可自行加入使用者未要求的隱含篩選條件，例如：
    - 不可自動加 departure_date >= date('now')（除非用戶說「未來」「即將」「尚未出發」）
    - 不可自動加 available_seats > 0（除非用戶說「有空位」「可報名」）
    - 不可自動加狀態過濾（除非用戶明確指定狀態）
    用戶說「可報名的團」只代表 status = '可報名'，不隱含任何日期限制。
12. 【重要】category_major（大分類）vs category_minor（小分類）的區別：
    - category_major 是「地區大類」，如：東南亞、海島、東北亞、東歐、西歐、中國、台灣…
    - category_minor 是「具體目的地」，如：帛琉、越南、日本、泰國、奧地利.捷克…
    用戶說「帛琉線」「帛琉的團」→ category_minor = '帛琉'（不是 category_major）
    用戶說「日本」「越南」「泰國」等具體國家/島嶼 → 查 category_minor
    用戶說「東南亞」「海島」「東歐」等大區域 → 查 category_major
    category_major 沒有「歐洲」這個值，歐洲分為多個子分類：
    東歐、西歐、南歐、北歐、歐洲遊輪、歐洲河輪。
    用戶說「歐洲線」「歐洲的團」→ 用 category_major IN ('東歐','西歐','南歐','北歐','歐洲遊輪','歐洲河輪')
    用戶說「亞洲線」→ category_major IN ('東南亞','東北亞','南亞','中東','北亞','中亞','亞洲遊輪')
    用戶說特定地區（如「東歐」）→ 直接 category_major = '東歐'
13. 【重要】空值處理：未填入的數值欄位在資料庫中為 NULL（不是空字串）。
    查詢「未填」「未定」「空白」應使用 col IS NULL，
    查詢「有填入」應使用 col IS NOT NULL AND col != ''。
14. 【重要】tour_groups 容易混淆的欄位區分：
    - deposit（訂金）= 每位旅客應繳的訂金「金額」（元）
    - confirmed_bookings（收訂）= 已確認訂位的「人數」
    - tentative_bookings（略訂）= 暫時保留但未確認的「人數」
    「沒收訂金」「訂金為0」→ 查 deposit，不是 confirmed_bookings
    「沒有收訂」「收訂為0」→ 查 confirmed_bookings
15. 【重要】tour_groups.status 欄位的實際值只有以下4種：
    「可報名」「截止可報名」「後補可報名」「不開放報名」
    - 用戶說「可以報名」「開放中」→ status = '可報名'
    - 用戶說「截止」「已截止」→ status = '截止可報名'
    - 用戶說「後補」「候補」→ status = '後補可報名'
    - 用戶說「不開放」→ status = '不開放報名'
    絕對不可使用「確定出發」「開放報名」「額滿」「取消」等不存在的狀態值。
15. 【重要】當用戶要求「列出完整資料」「順便列出」時，SELECT 應包含所有重要欄位，
    不可只用 COUNT(*)。
16. 【重要】統計「同一團名的場次數」時，只能 GROUP BY group_name，
    不可同時 GROUP BY product_name，否則同一團名下不同房型/艙等會被分開計算，導致結果錯誤。

{self._build_schema_context(is_admin, user_id)}"""

        response = self.client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_tokens=600,
        )

        sql = response.choices[0].message.content.strip()
        sql = re.sub(r"^```(?:sql)?\s*", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\s*```$", "", sql)
        return sql.strip()

    # ── SQL 驗證 ──────────────────────────────────────────────────

    def validate_sql(self, sql: str, is_admin: bool) -> tuple[bool, str]:
        """多層安全驗證，回傳 (is_valid, error_message)"""
        sql_stripped = sql.strip()
        sql_upper = sql_stripped.upper()

        # 只允許 SELECT
        if not sql_upper.startswith("SELECT"):
            return False, "只允許 SELECT 查詢"

        # 禁止危險關鍵字
        for kw in ["DROP", "DELETE", "INSERT", "UPDATE", "TRUNCATE",
                   "ALTER", "CREATE", "REPLACE", "EXEC", "EXECUTE",
                   "PRAGMA", "ATTACH", "DETACH"]:
            if re.search(r"\b" + kw + r"\b", sql_upper):
                return False, f"禁止使用 {kw}"

        # 禁止多語句
        inner = sql_stripped.rstrip(";")
        if ";" in inner:
            return False, "禁止多個 SQL 語句"

        # 禁止敏感欄位
        sql_lower = sql.lower()
        for col in BLOCKED_COLUMNS:
            if col.lower() in sql_lower:
                return False, f"禁止查詢敏感欄位：{col}"

        # 資料表白名單
        schema = ADMIN_SCHEMA if is_admin else USER_SCHEMA
        allowed = set(schema.keys())
        used_tables = set(
            t.lower()
            for t in re.findall(r"(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, re.IGNORECASE)
        )
        unauthorized = used_tables - allowed
        if unauthorized:
            return False, f"禁止查詢資料表：{', '.join(unauthorized)}"

        # 非管理員：只有查詢含 user_id 過濾需求的資料表才檢查 user_id 條件
        # 若查詢的資料表全部是公開資料表（tour_groups），不需要 user_id
        if not is_admin:
            non_public_tables = used_tables - _PUBLIC_TABLES
            if non_public_tables:
                user_id_filter_count = len(re.findall(r"\buser_id\s*=\s*\d+", sql_lower))
                if user_id_filter_count == 0:
                    return False, "查詢必須包含您自己的用戶 ID 限制，請換個方式描述問題"

                # 防止子查詢跳過過濾
                tables_with_filter = {
                    t for t, info in USER_SCHEMA.items()
                    if "user_filter" in info and t in used_tables
                }
                for table in tables_with_filter:
                    table_count = len(re.findall(r"\b" + table + r"\b", sql_lower))
                    if table_count > user_id_filter_count:
                        return False, (
                            f"資料表 '{table}' 在查詢中出現 {table_count} 次，"
                            f"但 user_id 過濾條件只有 {user_id_filter_count} 次，"
                            "可能存在未受限制的子查詢，已拒絕執行"
                        )

        return True, ""

    # ── SQL 執行 ──────────────────────────────────────────────────

    def execute_sql(self, sql: str, timeout_ms: int = 5000) -> tuple[list, list]:
        """唯讀模式執行 SQL，回傳 (columns, rows)，超過 timeout_ms 毫秒自動中斷"""
        sql = sql.rstrip(";") + ";"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA query_only = ON")

            def _interrupt_on_timeout():
                import time
                time.sleep(timeout_ms / 1000)
                conn.interrupt()

            import threading
            timer = threading.Thread(target=_interrupt_on_timeout, daemon=True)
            timer.start()

            try:
                cursor = conn.execute(sql)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = [list(row) for row in cursor.fetchall()]
                return columns, rows
            except sqlite3.OperationalError as e:
                if "interrupted" in str(e).lower():
                    raise TimeoutError(f"查詢超過 {timeout_ms}ms 已自動中止，請簡化查詢條件")
                raise
        finally:
            conn.close()

    # ── 結果格式化 ────────────────────────────────────────────────

    _COL_LABELS = {
        "category_major": "大分類", "category_minor": "小分類",
        "departure_date": "出發日", "group_code": "團體代碼",
        "group_name": "團名", "product_name": "產品名稱",
        "trade_price": "同業價", "direct_price": "直客價",
        "total_seats": "總機位", "reserved_seats": "保留",
        "enrolled": "報名", "available_seats": "可售",
        "confirmed_bookings": "收訂", "tentative_bookings": "略訂",
        "deposit": "訂金", "flight": "班機",
        "entry_exit_point": "進出點", "status": "狀態",
        "group_type": "團型", "staff_notes": "員工備註",
    }

    def _build_markdown_table(self, columns: list, rows: list) -> str:
        """將查詢結果轉為 Markdown 表格（欄位顯示中文）"""
        display_cols = [self._COL_LABELS.get(c, c) for c in columns]
        header = "| " + " | ".join(display_cols) + " |"
        separator = "| " + " | ".join(["---"] * len(columns)) + " |"
        data_rows = []
        for row in rows[:100]:
            cells = []
            for idx, val in enumerate(row):
                col = columns[idx].lower()
                if val is None:
                    cells.append("—")
                elif isinstance(val, float) and "cost" in col:
                    cells.append(f"${val:.6f}")
                elif isinstance(val, float) and "price" in col:
                    cells.append(f"{val:,.0f}")
                elif isinstance(val, float):
                    cells.append(f"{val:.2f}")
                elif isinstance(val, int) and col in ("is_admin", "is_active", "is_enabled",
                                                       "is_starred", "is_quota_exceeded",
                                                       "email_verified", "is_resolved"):
                    cells.append("✅" if val else "❌")
                else:
                    cells.append(str(val))
            data_rows.append("| " + " | ".join(cells) + " |")
        return "\n".join([header, separator] + data_rows)

    def _generate_summary(self, message: str, columns: list, rows: list) -> str:
        """用 LLM 生成自然語言摘要"""
        try:
            col_labels = [self._COL_LABELS.get(c, c) for c in columns]
            prompt = (
                f"根據以下資料查詢結果，用繁體中文寫 2~3 句摘要，說明關鍵數字與結論。"
                f"不要重複整個表格。\n\n"
                f"用戶問題：{message}\n"
                f"共 {len(rows)} 筆，欄位：{', '.join(col_labels)}\n"
                f"前幾筆：{json.dumps(rows[:5], ensure_ascii=False)}"
            )
            resp = self.client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            return f"查詢完成，共找到 **{len(rows)}** 筆資料。"

    def format_result(self, message: str, columns: list, rows: list) -> str:
        """將查詢結果格式化為自然語言摘要 + Markdown 表格"""
        if not rows:
            return "根據您的查詢，目前資料庫中沒有符合條件的資料。"

        summary = self._generate_summary(message, columns, rows)
        table = self._build_markdown_table(columns, rows)

        result = f"{summary}\n\n{table}"
        if len(rows) >= 100:
            result += "\n\n> ⚠️ 資料筆數過多，僅顯示前 100 筆"
        return result

    # ── 主流程 ────────────────────────────────────────────────────

    def requires_admin(self, message: str) -> bool:
        """偵測問題是否涉及跨用戶比較（需要管理員權限）"""
        for pattern in _admin_required_re:
            if pattern.search(message):
                return True
        return False

    async def process(self, message: str, is_admin: bool, user_id: int) -> dict:
        """
        完整 pipeline：意圖確認 → 生成 SQL → 驗證 → 執行 → 格式化
        回傳 dict: { success, sql, columns, rows, formatted_text, error }
        """
        try:
            if not is_admin and self.requires_admin(message):
                return {
                    "success": True,
                    "sql": "",
                    "columns": [],
                    "rows": [],
                    "formatted_text": (
                        "⚠️ 此查詢涉及其他用戶的資料，需要**管理員權限**才能執行。\n\n"
                        "您可以查詢**自己**的使用資料，例如：\n"
                        "- 「我這個月花了多少錢？」\n"
                        "- 「我的 token 配額還剩多少？」\n"
                        "- 「我最近有幾個對話？」\n\n"
                        "或查詢**團體資料**，例如：\n"
                        "- 「本月有哪些出發的團？」\n"
                        "- 「還有可售機位的團有哪些？」"
                    ),
                }

            sql = self.generate_sql(message, is_admin, user_id)

            valid, err = self.validate_sql(sql, is_admin)
            if not valid:
                return {"success": False, "sql": sql, "error": f"SQL 驗證失敗：{err}"}

            columns, rows = self.execute_sql(sql)
            formatted_text = self.format_result(message, columns, rows)

            return {
                "success": True,
                "sql": sql,
                "columns": columns,
                "rows": rows,
                "formatted_text": formatted_text,
            }

        except Exception as e:
            return {"success": False, "sql": "", "error": f"查詢處理失敗：{str(e)}"}
