"""Optional Redis cache for QA pipeline results (question -> SQL + answer)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, Optional

from redis_config import is_redis_enabled, make_redis_client
from retry_utils import redis_retry

logger = logging.getLogger(__name__)

_CACHE_VERSION = "v1"

# Questions matching this are not cached: answers depend on "now" and go stale quickly.
_VOLATILE_Q = re.compile(
    r"\b("
    r"today|yesterday|tomorrow|"
    r"this\s+(week|month|quarter|year)|"
    r"last\s+(week|month|quarter|year|night|\d+\s+days?)|"
    r"next\s+(week|month|quarter)|"
    r"past\s+\d+\s+days?|"
    r"recent(?:ly)?|"
    r"year[\s-]?to[\s-]?date|\bytd\b|\bmtd\b|\bqtd\b|"
    r"so\s+far|"
    r"current\s+(week|month|quarter|year)|"
    r"rolling|"
    r"latest|up\s*to\s*date|as\s+of"
    r")\b",
    re.IGNORECASE,
)


def is_time_volatile_question(question: str) -> bool:
    """True if the question is likely time-relative; caching would often return stale metrics."""
    q = _normalize_question(question)
    return bool(_VOLATILE_Q.search(q))


def _normalize_question(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _cache_key(question: str, schema: str | None) -> str:
    part = f"{_normalize_question(question)}|{schema or ''}"
    digest = hashlib.sha256(part.encode("utf-8")).hexdigest()
    return f"sda:qa:{_CACHE_VERSION}:{digest}"


def _client():
    return make_redis_client()


# ---------------------------------------------------------------------------
# Redis I/O (retried on ConnectionError / TimeoutError; failures -> None)
# ---------------------------------------------------------------------------


@redis_retry
def _redis_get_raw(client: Any, key: str) -> Optional[str]:
    """GET; returns None if key missing or Redis unavailable after retries."""
    return client.get(key)


@redis_retry
def _redis_setex_raw(client: Any, key: str, ttl: int, payload: str) -> Any:
    """SETEX; returns None if Redis unavailable after retries."""
    return client.setex(key, ttl, payload)


@redis_retry
def _redis_delete_pattern(client: Any, pattern: str) -> Optional[int]:
    """SCAN + DEL for pattern; returns None if Redis unavailable after retries."""
    keys = list(client.scan_iter(pattern, count=500))
    if not keys:
        return 0
    return int(client.delete(*keys))


def _ttl_seconds() -> int:
    # Default 15m: cached entries are for non-volatile questions; still refresh reasonably often.
    raw = os.getenv("REDIS_TTL_SECONDS") or os.getenv("redis_ttl_seconds") or "900"
    try:
        return max(0, int(raw))
    except ValueError:
        return 900


def redis_qa_cache_status() -> dict[str, Any]:
    """
    Connectivity + config for observability (e.g. GET /health?full=1).
    Does not use redis_retry — reports real connection errors.
    """
    ttl = _ttl_seconds()
    out: dict[str, Any] = {
        "enabled": is_redis_enabled(),
        "ttl_seconds": ttl,
        "qa_cache_writes_disabled": ttl <= 0,
    }
    if not is_redis_enabled():
        out["reachable"] = False
        out["detail"] = (
            "Redis is disabled (REDIS_ENABLED=0/false). QA cache and Redis-backed sessions are off; "
            "set REDIS_ENABLED=true and start redis-server to enable."
        )
        return out
    try:
        import redis as redis_lib  # noqa: F401
    except ImportError:
        out["reachable"] = False
        out["detail"] = "Install the redis package (pip install redis)."
        return out
    client = make_redis_client()
    if client is None:
        out["reachable"] = False
        out["detail"] = "make_redis_client() returned None."
        return out
    try:
        ok = bool(client.ping())
        out["reachable"] = ok
        out["detail"] = "PONG" if ok else "PING failed"
        return out
    except Exception as e:
        rh = (os.getenv("REDIS_HOST") or os.getenv("redis_host") or "127.0.0.1").strip()
        rp = (os.getenv("REDIS_PORT") or os.getenv("redis_port") or "6379").strip()
        out["reachable"] = False
        out["detail"] = (
            f"Cannot connect to Redis at {rh}:{rp} ({type(e).__name__}: {e}). "
            "Start a Redis server (e.g. `docker run -d -p 6379:6379 redis:alpine`) "
            "or point REDIS_HOST/REDIS_PORT to your instance."
        )
        return out


def get_cached_pipeline(question: str, schema: str | None = None) -> Optional[Dict[str, Any]]:
    """Return cached dict with keys sql, answer, row_count if present."""
    client = _client()
    if client is None:
        logger.debug("QA cache GET skipped: no Redis client (disabled, missing package, or import error)")
        return None
    key = _cache_key(question, schema)
    raw = _redis_get_raw(client, key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if "sql" not in data or "answer" not in data:
        return None
    return data


def set_cached_pipeline(
    question: str,
    *,
    schema: str | None = None,
    sql: str,
    answer: str,
    row_count: int,
) -> None:
    client = _client()
    if client is None:
        logger.warning(
            "QA cache SET skipped: Redis client unavailable — install `redis`, start redis-server, "
            "or check REDIS_HOST/REDIS_PORT (see GET /health?full=1)."
        )
        return
    ttl = _ttl_seconds()
    if ttl <= 0:
        logger.warning("QA cache SET skipped: REDIS_TTL_SECONDS is 0 — caching disabled.")
        return
    key = _cache_key(question, schema)
    payload = json.dumps(
        {"sql": sql, "answer": answer, "row_count": row_count},
        ensure_ascii=True,
    )
    result = _redis_setex_raw(client, key, ttl, payload)
    if result is None:
        logger.warning(
            "QA cache SET failed after retries — Redis unreachable or error (same as cache 'not working')."
        )


def invalidate_all_pipeline_cache() -> int:
    """Delete all keys for this app prefix. Returns number of keys removed, or -1 on failure."""
    client = _client()
    if client is None:
        return -1
    n = _redis_delete_pattern(client, f"sda:qa:{_CACHE_VERSION}:*")
    if n is None:
        return -1
    return n
