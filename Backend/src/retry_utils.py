"""Retry utilities for SDA pipeline.

Provides a reusable exponential-backoff decorator used by:
  - Azure OpenAI HTTP calls  (transient 429 / 503 / URLError)
  - Postgres run_query()     (transient OperationalError / connection reset)
  - Redis cache operations   (connection refused / timeout)

Usage
-----
    from retry_utils import with_retry

    @with_retry(retries=3, backoff_base=2.0, retriable_exceptions=(RuntimeError,))
    def my_flaky_call():
        ...

Environment overrides
---------------------
    RETRY_MAX_ATTEMPTS   – default 3  (applies to Azure + Postgres retries)
    RETRY_BACKOFF_BASE   – default 2.0 seconds (multiplied by attempt number)
    RETRY_MAX_WAIT       – default 30 seconds (cap per sleep)
"""

from __future__ import annotations

import functools
import logging
import os
import re
import time
import urllib.error
from typing import Callable, Tuple, Type

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def default_max_attempts() -> int:
    return _env_int("RETRY_MAX_ATTEMPTS", 3)


def default_backoff_base() -> float:
    return _env_float("RETRY_BACKOFF_BASE", 2.0)


def default_max_wait() -> float:
    return _env_float("RETRY_MAX_WAIT", 30.0)


# ---------------------------------------------------------------------------
# Core decorator
# ---------------------------------------------------------------------------


def with_retry(
    *,
    retries: int | None = None,
    backoff_base: float | None = None,
    max_wait: float | None = None,
    retriable_exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    retriable_http_codes: Tuple[int, ...] = (429, 500, 502, 503, 504),
) -> Callable:
    """Exponential-backoff retry decorator.

    Args:
        retries: Max retry attempts (not counting the first call).
                 Defaults to RETRY_MAX_ATTEMPTS env var (3).
        backoff_base: Base sleep seconds; sleep = backoff_base * (2 ** attempt).
                      Defaults to RETRY_BACKOFF_BASE env var (2.0).
        max_wait: Cap on sleep per attempt in seconds.
                  Defaults to RETRY_MAX_WAIT env var (30.0).
        retriable_exceptions: Exception types that should trigger a retry.
        retriable_http_codes: HTTP status codes (int) that should trigger retry
                              when embedded in the exception message.
    """
    max_attempts = retries if retries is not None else default_max_attempts()
    base = backoff_base if backoff_base is not None else default_backoff_base()
    cap = max_wait if max_wait is not None else default_max_wait()

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc: BaseException | None = None
            for attempt in range(max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retriable_exceptions as exc:
                    last_exc = exc
                    # Non-retriable HTTP codes: fail fast (e.g. 400 Bad Request, 401 Unauth)
                    if isinstance(exc, urllib.error.HTTPError):
                        if exc.code not in retriable_http_codes:
                            logger.error(
                                "[retry] %s — HTTP %d is not retriable; failing immediately.",
                                func.__name__,
                                exc.code,
                            )
                            raise
                    msg = str(exc)
                    m_az = re.search(r"Azure OpenAI HTTP error (\d{3})", msg)
                    if m_az:
                        code = int(m_az.group(1))
                        if code not in retriable_http_codes:
                            logger.error(
                                "[retry] %s — HTTP %d is not retriable; failing immediately.",
                                func.__name__,
                                code,
                            )
                            raise
                    if "HTTP error:" in msg or "Azure OpenAI HTTP error:" in msg:
                        for segment in msg.split():
                            try:
                                code = int(segment.rstrip(".,:;"))
                                if code not in retriable_http_codes:
                                    logger.error(
                                        "[retry] %s — HTTP %d is not retriable; failing immediately.",
                                        func.__name__,
                                        code,
                                    )
                                    raise
                            except ValueError:
                                continue

                    if attempt == max_attempts:
                        logger.error(
                            "[retry] %s — all %d attempts exhausted. Last error: %s",
                            func.__name__,
                            max_attempts + 1,
                            exc,
                        )
                        raise

                    sleep_time = min(base * (2**attempt), cap)
                    logger.warning(
                        "[retry] %s — attempt %d/%d failed (%s). Retrying in %.1fs.",
                        func.__name__,
                        attempt + 1,
                        max_attempts + 1,
                        exc,
                        sleep_time,
                    )
                    time.sleep(sleep_time)

            # Should never reach here, but satisfy type checkers
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Pre-configured retry profiles
# ---------------------------------------------------------------------------


def azure_retry(func: Callable) -> Callable:
    """Retry profile for Azure OpenAI HTTP calls.

    Retries on: RuntimeError (wraps HTTPError / URLError), urllib.error.*
    Non-retriable: HTTP 400, 401, 403 (bad request / auth — fix the call, not retry)
    Retriable:     HTTP 429 (rate limit), 500/502/503/504 (server errors), URLError (network)
    """
    import urllib.error

    return with_retry(
        retries=default_max_attempts(),
        backoff_base=default_backoff_base(),
        max_wait=default_max_wait(),
        retriable_exceptions=(RuntimeError, urllib.error.URLError, urllib.error.HTTPError),
        retriable_http_codes=(429, 500, 502, 503, 504),
    )(func)


def postgres_retry(func: Callable) -> Callable:
    """Retry profile for Postgres psycopg2 calls.

    Retries on: OperationalError (connection reset, server gone away),
                InterfaceError (connection closed unexpectedly).
    Does NOT retry on: ProgrammingError, DataError (bad SQL — fix the query).
    Falls back gracefully to re-raising after exhausting attempts.
    """
    try:
        import psycopg2

        retriable = (psycopg2.OperationalError, psycopg2.InterfaceError)
    except ImportError:
        retriable = (Exception,)  # type: ignore[assignment]

    return with_retry(
        retries=default_max_attempts(),
        backoff_base=default_backoff_base(),
        max_wait=default_max_wait(),
        retriable_exceptions=retriable,
    )(func)


def redis_retry(func: Callable) -> Callable:
    """Retry profile for Redis cache calls.

    Redis down should NEVER crash the pipeline — after exhausting retries
    the wrapper degrades gracefully (returns None) instead of raising.
    This is applied inside redis_cache.py around get/set operations.
    """
    try:
        import redis as redis_lib

        retriable = (redis_lib.exceptions.ConnectionError, redis_lib.exceptions.TimeoutError)
    except ImportError:
        retriable = (Exception,)  # type: ignore[assignment]

    base_decorator = with_retry(
        retries=2,  # Fail fast for cache — 3 total attempts
        backoff_base=1.0,  # Short waits for cache ops
        max_wait=5.0,
        retriable_exceptions=retriable,
    )

    @functools.wraps(func)
    def graceful_wrapper(*args, **kwargs):
        try:
            return base_decorator(func)(*args, **kwargs)
        except Exception as exc:
            logger.warning(
                "[redis_retry] %s — cache unavailable after retries (%s). "
                "Degrading gracefully to no-cache mode.",
                func.__name__,
                exc,
            )
            return None  # Caller must treat None as cache miss

    return graceful_wrapper
