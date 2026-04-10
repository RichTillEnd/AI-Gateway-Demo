"""
Redis 語意快取模組
相似度 > threshold 的問題直接回傳快取答案，省下 AI API 費用。

依賴：
  redis>=7          pip install redis
  openai>=1         已安裝

環境變數：
  REDIS_URL         Redis 連線字串，預設 redis://localhost:6379/0
  SEMANTIC_CACHE_THRESHOLD   餘弦相似度門檻，預設 0.92
  SEMANTIC_CACHE_TTL         快取存活秒數，預設 86400 (1 天)
  SEMANTIC_CACHE_ENABLED     設為 "false" 可完全關閉
"""

import os
import json
import hashlib
import struct
import asyncio
from typing import Optional
import numpy as np

# ── 設定值 ─────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_THRESHOLD = float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.92"))
CACHE_TTL = int(os.getenv("SEMANTIC_CACHE_TTL", "86400"))
CACHE_ENABLED = os.getenv("SEMANTIC_CACHE_ENABLED", "true").lower() != "false"
CACHE_MAX_ENTRIES = int(os.getenv("SEMANTIC_CACHE_MAX_ENTRIES", "500"))

# Redis key 前綴
_KEY_PREFIX = "scache:"          # 每筆快取：scache:{hex_hash}
_INDEX_KEY  = "scache:__index__" # 所有快取 key 的 Redis Set

# ── Redis 連線（延遲初始化，避免啟動時 Redis 不可用造成崩潰）──
_redis_client = None

def _get_redis():
    """取得 redis.asyncio client，若 Redis 不可用回傳 None。"""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(REDIS_URL, decode_responses=False)
        return _redis_client
    except Exception as e:
        print(f"[SemanticCache] Redis 連線失敗（快取停用）: {e}")
        return None


# ── Embedding ──────────────────────────────────────────────
async def _get_embedding(text: str) -> Optional[np.ndarray]:
    """呼叫 OpenAI text-embedding-3-small 取得向量。"""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_KEY"))
        resp = await client.embeddings.create(
            model="text-embedding-3-small",
            input=text[:8000],   # 避免超過 token 限制
        )
        return np.array(resp.data[0].embedding, dtype=np.float32)
    except Exception as e:
        print(f"[SemanticCache] 取得 embedding 失敗: {e}")
        return None


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """計算兩個向量的餘弦相似度。"""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def _embed_to_bytes(vec: np.ndarray) -> bytes:
    """將 float32 陣列序列化為 bytes。"""
    return vec.astype(np.float32).tobytes()


def _bytes_to_embed(data: bytes) -> np.ndarray:
    """將 bytes 反序列化為 float32 陣列。"""
    return np.frombuffer(data, dtype=np.float32)


# ── 公開 API ───────────────────────────────────────────────

async def cache_get(message: str) -> Optional[str]:
    """
    在快取中找最相似的問題。
    回傳快取的 AI 回覆字串，若無命中回傳 None。
    """
    if not CACHE_ENABLED:
        return None

    r = _get_redis()
    if r is None:
        return None

    try:
        # 1. 取得查詢向量
        query_vec = await _get_embedding(message)
        if query_vec is None:
            return None

        # 2. 取出所有快取 key
        cache_keys = await r.smembers(_INDEX_KEY)
        if not cache_keys:
            return None

        # 3. 批次取得所有快取條目
        best_sim = -1.0
        best_response = None

        # 批次處理（每次 200 筆），找到高相似度即提前返回
        keys_list = list(cache_keys)
        for i in range(0, len(keys_list), 200):
            batch = keys_list[i:i+200]
            entries = await r.mget(batch)
            for raw in entries:
                if raw is None:
                    continue
                try:
                    entry = json.loads(raw)
                    cached_vec = _bytes_to_embed(bytes.fromhex(entry["embedding_hex"]))
                    sim = _cosine_similarity(query_vec, cached_vec)
                    if sim > best_sim:
                        best_sim = sim
                        best_response = entry["response"]
                    # 相似度 >= 0.98：幾乎完全匹配，提前返回不再比對
                    if best_sim >= 0.98:
                        break
                except Exception:
                    continue
            if best_sim >= 0.98:
                break

        if best_sim >= CACHE_THRESHOLD:
            print(f"[SemanticCache] 命中！相似度={best_sim:.4f} (門檻={CACHE_THRESHOLD})")
            return best_response

        return None

    except Exception as e:
        print(f"[SemanticCache] cache_get 失敗（忽略）: {e}")
        return None


async def cache_set(message: str, response: str, embedding: Optional[np.ndarray] = None) -> bool:
    """
    將問題與回覆寫入快取。
    embedding 可傳入已取得的向量以避免重複呼叫 API。
    """
    if not CACHE_ENABLED:
        return False

    r = _get_redis()
    if r is None:
        return False

    try:
        # 超過條目上限時不再新增（避免 cache_get 全量掃描膨脹）
        current_count = await r.scard(_INDEX_KEY)
        if current_count and current_count >= CACHE_MAX_ENTRIES:
            print(f"[SemanticCache] 已達上限 {CACHE_MAX_ENTRIES} 條，跳過寫入")
            return False

        vec = embedding if embedding is not None else await _get_embedding(message)
        if vec is None:
            return False

        # 用訊息的 SHA256 產生唯一 key（避免完全重複的問題）
        key = _KEY_PREFIX + hashlib.sha256(message.encode()).hexdigest()

        entry = {
            "message": message[:500],          # 僅存前 500 字供除錯
            "embedding_hex": _embed_to_bytes(vec).hex(),
            "response": response,
        }
        await r.set(key, json.dumps(entry, ensure_ascii=False), ex=CACHE_TTL)
        await r.sadd(_INDEX_KEY, key)

        return True

    except Exception as e:
        print(f"[SemanticCache] cache_set 失敗（忽略）: {e}")
        return False


async def cache_clear() -> int:
    """清除所有語意快取，回傳刪除筆數。"""
    r = _get_redis()
    if r is None:
        return 0
    try:
        keys = await r.smembers(_INDEX_KEY)
        if keys:
            await r.delete(*keys)
        await r.delete(_INDEX_KEY)
        return len(keys)
    except Exception as e:
        print(f"[SemanticCache] cache_clear 失敗: {e}")
        return 0


async def cache_stats() -> dict:
    """回傳快取統計資訊。"""
    r = _get_redis()
    if r is None:
        return {"enabled": CACHE_ENABLED, "redis_connected": False}
    try:
        count = await r.scard(_INDEX_KEY)
        info = await r.info("memory")
        return {
            "enabled": CACHE_ENABLED,
            "redis_connected": True,
            "cached_entries": count,
            "threshold": CACHE_THRESHOLD,
            "ttl_seconds": CACHE_TTL,
            "redis_used_memory_human": info.get("used_memory_human", "N/A"),
        }
    except Exception as e:
        return {"enabled": CACHE_ENABLED, "redis_connected": False, "error": str(e)}
