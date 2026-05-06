"""
HTTP API for the Takeda SDA QA pipeline (wraps ``run_question_pipeline_turn``).

Run from ``Backend/src``::

    uvicorn api_server:app --reload --host 127.0.0.1 --port 8000

Frontend (Next.js): set ``NEXT_PUBLIC_SERVER_URL=http://127.0.0.1:8000``.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Dict

# package root = this directory
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from env_loader import (  # noqa: E402
    force_apply_azure_openai_from_dotenv,
    force_apply_redis_from_dotenv,
    load_application_dotenv,
)

load_application_dotenv()
force_apply_azure_openai_from_dotenv()
force_apply_redis_from_dotenv()

from conversation_context import ConversationBuffer  # noqa: E402
from qa_pipeline import (  # noqa: E402
    run_question_pipeline_turn,
    sanitize_user_visible_text,
    strip_sql_from_nl_chat_markup,
)
from redis_cache import redis_qa_cache_status  # noqa: E402
from fastapi import FastAPI, Query, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

_origins_raw = os.getenv(
    "CORS_ALLOW_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000",
)
_origins = [o.strip() for o in _origins_raw.split(",") if o.strip()]

_log = logging.getLogger("sda.api")


def _strip_sources_checked_line(text: str) -> str:
    """Hard guard: remove any line containing 'sources checked'."""
    if not text:
        return text
    out = re.sub(r"(?im)^.*sources\s*checked.*\n?", "", text)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Load workbook into SQLite when ``SDA_DATA_SOURCE`` is sqlite (default)."""
    from db_adapter import use_sqlite_backend

    try:
        if use_sqlite_backend():
            from config import settings
            from data_loader import load_file

            _log.info("SDA_DATA_SOURCE=sqlite — loading %s", settings.data_file_path)
            load_file(settings.data_file_path)
            try:
                from data_loader import get_db
                from workbook_schema_rag import ensure_workbook_rag_index

                _db0 = get_db()
                if _db0 is not None:
                    ensure_workbook_rag_index(_db0)
                    _log.info("Workbook schema RAG index ready (or skipped if embeddings disabled).")
            except Exception as rag_exc:
                _log.warning("Workbook schema RAG index not built at startup: %s", rag_exc)
    except Exception:
        _log.exception("Startup: failed to load tabular data file")
        if use_sqlite_backend():
            _log.error(
                "SQLite mode: verify DATA_FILE_PATH in Backend\\src\\.env points to "
                "'Arcutis Dummy Data v1.xlsx', then restart."
            )
        raise
    yield


app = FastAPI(title="Takeda SDA QA API", version="1.0.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Process-Time-Ms"],
)


def _query_json(body: Dict[str, Any], *, duration_ms: int, status_code: int = 200) -> JSONResponse:
    out = {**body, "duration_ms": duration_ms}
    return JSONResponse(
        status_code=status_code,
        content=out,
        headers={"X-Process-Time-Ms": str(duration_ms)},
    )

_buffers: Dict[str, ConversationBuffer] = {}
_buffer_lock = Lock()


def _get_buffer(session_id: str) -> ConversationBuffer:
    """One conversation buffer per browser session (isolated Redis list key when Redis is on)."""
    with _buffer_lock:
        if session_id not in _buffers:
            rkey = f"sda:api:session:{session_id}:turns"
            _buffers[session_id] = ConversationBuffer(redis_list_key=rkey)
        return _buffers[session_id]


class _ClientTurn(BaseModel):
    """Last N completed Q→A pairs from the browser (survives API worker changes / empty server RAM)."""

    user: str = Field(..., min_length=1, max_length=8000)
    assistant: str = Field("", max_length=16000)


class _QueryPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    previous_question: str | None = None
    previous_sql: str | None = None
    prior_turns: list[_ClientTurn] | None = None


def _prepare_buffer(
    session_id: str,
    *,
    previous_question: str | None,
    previous_sql: str | None,
    prior_turns: list[_ClientTurn] | None = None,
) -> ConversationBuffer:
    """Sync Redis → memory, then optionally seed from client when buffer is still empty."""
    buf = _get_buffer(session_id)
    buf.sync_from_redis()
    pq = (previous_question or "").strip()
    ps = (previous_sql or "").strip()
    if len(buf) == 0 and pq and ps:
        upper = ps.upper()
        if upper.startswith("SELECT") or upper.startswith("WITH"):
            buf.append(pq, ps, "(client-resumed context)")
    if len(buf) == 0 and prior_turns:
        for t in prior_turns[-8:]:
            u = t.user.strip()
            a = (t.assistant or "").strip()
            if u:
                buf.append(u, "(client-history)", a if a else "(no answer)")
    return buf


@app.get("/health")
def health(full: bool = Query(False, description="Include redis_qa_cache diagnostics")) -> dict[str, Any]:
    """
    Liveness: ``{"status": "ok"}``.

    With ``?full=1``, includes ``redis_qa_cache`` (reachable, TTL, connection errors).
    If ``reachable`` is false, QA result caching will not read or write — start Redis or fix REDIS_* env.
    """
    out: dict[str, Any] = {"status": "ok"}
    if full:
        out["redis_qa_cache"] = redis_qa_cache_status()
    return out


@app.post("/query")
async def query(request: Request) -> JSONResponse:
    """
    Accepts either:
    - ``POST`` with ``Content-Type: application/json`` body
      ``{ "question", "session_id", "previous_question"?, "previous_sql"?, "prior_turns"? }`` — ``prior_turns`` is optional ``[{ "user", "assistant" }, ...]`` (last few turns from the UI) for multi-turn context when the server buffer is empty.
    - or legacy query string ``POST /query?question=...&session_id=...``.

    Returns ``{ success, response, error?, duration_ms }`` plus optional ``sql``, ``row_count``.
    Header ``X-Process-Time-Ms`` duplicates wall-clock pipeline time for DevTools.
    """
    t0 = time.perf_counter()

    def ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    ct = (request.headers.get("content-type") or "").lower()
    previous_question: str | None = None
    previous_sql: str | None = None
    if "application/json" in ct:
        try:
            raw = await request.json()
            payload = _QueryPayload.model_validate(raw)
        except Exception as e:
            elapsed = ms()
            return _query_json(
                {
                    "success": False,
                    "response": "",
                    "error": f"Invalid JSON body: {e}",
                },
                duration_ms=elapsed,
                status_code=422,
            )
        question = payload.question
        session_id = payload.session_id
        previous_question = payload.previous_question
        previous_sql = payload.previous_sql
        prior_turns = payload.prior_turns
    else:
        qp = request.query_params
        question = qp.get("question") or ""
        session_id = qp.get("session_id") or ""
        previous_question = qp.get("previous_question")
        previous_sql = qp.get("previous_sql")
        prior_turns = None
        if not question.strip() or not session_id.strip():
            elapsed = ms()
            return _query_json(
                {
                    "success": False,
                    "response": "",
                    "error": "Missing question or session_id.",
                },
                duration_ms=elapsed,
                status_code=400,
            )

    buf = _prepare_buffer(
        session_id,
        previous_question=previous_question,
        previous_sql=previous_sql,
        prior_turns=prior_turns,
    )
    buf_out = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_out):
            out = run_question_pipeline_turn(
                question,
                conversation=buf,
                trace_metadata={"session_id": session_id},
            )
    except Exception as e:
        elapsed = ms()
        _log.warning("/query exception after %dms: %s", elapsed, e)
        return _query_json(
            {
                "success": False,
                "response": "",
                "error": sanitize_user_visible_text(str(e)) or str(e),
            },
            duration_ms=elapsed,
        )

    err = out.get("error")
    ans = out.get("answer")
    if err:
        elapsed = ms()
        _log.info("/query done success=False (pipeline error) in %dms", elapsed)
        return _query_json(
            {
                "success": False,
                "response": (ans or "").strip(),
                "error": sanitize_user_visible_text(err) or err,
                "sql": out.get("sql"),
                "row_count": out.get("row_count", 0),
            },
            duration_ms=elapsed,
        )
    if not ans:
        elapsed = ms()
        _log.info("/query done success=False (no answer) in %dms", elapsed)
        return _query_json(
            {
                "success": False,
                "response": "",
                "error": "No answer produced",
                "sql": out.get("sql"),
                "row_count": out.get("row_count", 0),
            },
            duration_ms=elapsed,
        )
    elapsed = ms()
    _log.info("/query done success=True in %dms", elapsed)
    chat_nl = strip_sql_from_nl_chat_markup(ans)
    chat_nl = _strip_sources_checked_line(chat_nl)
    body: Dict[str, Any] = {
        "success": True,
        "response": sanitize_user_visible_text(chat_nl) or chat_nl,
        "sql": out.get("sql"),
        "row_count": out.get("row_count", 0),
        "cache_hit": bool(out.get("cache_hit")),
    }
    if out.get("chart"):
        body["chart"] = out["chart"]
    if out.get("result_table"):
        body["result_table"] = out["result_table"]
    if out.get("result_table_multipart_last_part"):
        body["result_table_multipart_last_part"] = True
    if out.get("sub_results"):
        body["sub_results"] = out["sub_results"]
    return _query_json(body, duration_ms=elapsed)
