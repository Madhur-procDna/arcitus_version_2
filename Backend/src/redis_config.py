"""Shared Redis settings and client factory for conversation buffer and QA cache.

Without REDIS_HOST set, local dev used to skip Redis entirely because the old
logic required a non-empty host. When REDIS_ENABLED is true (default), we now
default the host to 127.0.0.1 so a local redis-server works with minimal .env.
Set REDIS_ENABLED=0 to disable all Redis usage.
"""

from __future__ import annotations

import os
from typing import Any, Optional


def is_redis_enabled() -> bool:
    flag = (os.getenv("REDIS_ENABLED") or os.getenv("redis_enabled") or "true").lower()
    return flag not in ("0", "false", "no", "off")


def make_redis_client() -> Optional[Any]:
    """Return a redis.Redis client, or None if Redis is disabled or redis is not installed."""
    if not is_redis_enabled():
        return None
    try:
        import redis
    except ImportError:
        return None

    host = (os.getenv("REDIS_HOST") or os.getenv("redis_host") or "127.0.0.1").strip()
    port = int(os.getenv("REDIS_PORT") or os.getenv("redis_port") or "6379")
    db = int(os.getenv("REDIS_DB") or os.getenv("redis_db") or "0")
    password = os.getenv("REDIS_PASSWORD") or os.getenv("redis_password") or None
    if password == "":
        password = None

    def _timeout(name: str, default: float) -> float:
        raw = (os.getenv(name) or "").strip()
        if not raw:
            return default
        try:
            return max(0.5, float(raw))
        except ValueError:
            return default

    connect_timeout = _timeout("REDIS_SOCKET_CONNECT_TIMEOUT", 5.0)
    socket_timeout = _timeout("REDIS_SOCKET_TIMEOUT", 5.0)

    url = (os.getenv("REDIS_URL") or os.getenv("redis_url") or "").strip()
    if url:
        return redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=connect_timeout,
            socket_timeout=socket_timeout,
        )

    return redis.Redis(
        host=host,
        port=port,
        db=db,
        password=password,
        decode_responses=True,
        socket_connect_timeout=connect_timeout,
        socket_timeout=socket_timeout,
    )
