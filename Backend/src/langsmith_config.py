"""Enable LangSmith tracing from environment (LangChain-compatible env names)."""

from __future__ import annotations

import os

try:
    from langsmith import traceable as traceable
except ImportError:  # pragma: no cover
    def traceable(*d_args, **d_kwargs):
        def _decorator(func):
            return func

        return _decorator


_initialized = False


def _env_truthy(key: str) -> bool:
    return (os.getenv(key) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def langsmith_tracing_disabled() -> bool:
    return _env_truthy("SDA_DISABLE_LANGSMITH")


def init_langsmith_tracing() -> bool:
    """
    If LANGCHAIN_API_KEY or LANGSMITH_API_KEY is set, turn on tracing and project.

    Call after ``_load_dotenv()`` so .env values are visible.
    Returns True when tracing export is expected to be active.
    """
    global _initialized
    if langsmith_tracing_disabled():
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        os.environ["LANGSMITH_TRACING"] = "false"
        _initialized = True
        return False

    key = (
        os.getenv("LANGCHAIN_API_KEY")
        or os.getenv("LANGSMITH_API_KEY")
        or os.getenv("langchain_api_key")
        or os.getenv("langsmith_api_key")
    )
    project = (
        os.getenv("LANGCHAIN_PROJECT")
        or os.getenv("LANGSMITH_PROJECT")
        or os.getenv("langchain_project")
        or os.getenv("langsmith_project")
    )
    endpoint = (
        os.getenv("LANGCHAIN_ENDPOINT")
        or os.getenv("LANGSMITH_ENDPOINT")
        or os.getenv("langsmith_endpoint")
    )

    if not key:
        _initialized = True
        return False

    os.environ.setdefault("LANGCHAIN_API_KEY", key)
    os.environ.setdefault("LANGSMITH_API_KEY", key)
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGSMITH_TRACING"] = "true"
    if project:
        os.environ.setdefault("LANGCHAIN_PROJECT", project)
        os.environ.setdefault("LANGSMITH_PROJECT", project)
    if endpoint:
        os.environ.setdefault("LANGCHAIN_ENDPOINT", endpoint)
        os.environ.setdefault("LANGSMITH_ENDPOINT", endpoint)

    _initialized = True
    return True


def langsmith_tracing_active() -> bool:
    return not langsmith_tracing_disabled() and bool(
        os.getenv("LANGCHAIN_API_KEY")
        or os.getenv("LANGSMITH_API_KEY")
    ) and os.getenv("LANGCHAIN_TRACING_V2", "").lower() == "true"


def flush_langsmith_traces() -> None:
    """Push buffered runs to LangSmith before exit (CLI runs can otherwise look empty)."""
    if langsmith_tracing_disabled() or not (
        os.getenv("LANGCHAIN_API_KEY") or os.getenv("LANGSMITH_API_KEY")
    ):
        return
    try:
        from langsmith import Client

        Client().flush()
    except Exception:
        pass
