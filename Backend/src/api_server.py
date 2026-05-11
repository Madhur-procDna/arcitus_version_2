"""
HTTP API for the Arcutis Biotherapeutics Data Assistant (wraps ``run_question_pipeline_turn``).

Run from ``Backend/src``::

    uvicorn api_server:app --reload --host 127.0.0.1 --port 8000

Frontend (Next.js): set ``NEXT_PUBLIC_SERVER_URL=http://127.0.0.1:8000``.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import sys
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Dict
from uuid import uuid4

# package root = this directory
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from env_loader import (  # noqa: E402
    force_apply_azure_openai_from_dotenv,
    force_apply_redis_from_dotenv,
    load_application_dotenv,
)
from langsmith_config import init_langsmith_tracing  # noqa: E402

load_application_dotenv()
force_apply_azure_openai_from_dotenv()
force_apply_redis_from_dotenv()
init_langsmith_tracing()

from conversation_context import ConversationBuffer  # noqa: E402
from nl_row_format import normalize_chart_month_labels  # noqa: E402
from qa_pipeline import (  # noqa: E402
    purge_bad_cache_entries,
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
_backend_api_key = (os.getenv("SDA_BACKEND_API_KEY") or "").strip()
_max_sessions = max(32, int(os.getenv("SDA_MAX_SESSION_BUFFERS", "512")))

_log = logging.getLogger("sda.api")


def _strip_sources_checked_line(text: str) -> str:
    """Hard guard: remove unwanted boilerplate sections from LLM output."""
    if not text:
        return text
    # Remove "Sources checked" lines.
    out = re.sub(r"(?im)^.*sources\s*checked.*\n?", "", text)
    # Remove "Formulas used:" / "Formula used:" header + all immediately following bullet lines.
    out = re.sub(
        r"(?im)^\**\s*formulas?\s+used\s*:?\**\s*\n((?:\s*[-*•]\s+.*\n?)*)",
        "",
        out,
    )
    # Remove "ZORYVE Market Share by Segment" section (header + bullets).
    out = re.sub(
        r"(?im)^#+\s*ZORYVE\s+Market\s+Share\s+by\s+Segment\s*\n((?:\s*[-*•]\s+.*\n?)*)",
        "",
        out,
    )
    out = re.sub(
        r"(?im)^\**\s*\d+\.\s*ZORYVE\s+MARKET\s+SHARE\s+BY\s+SEGMENT\**\s*\n((?:\s*[-*•]\s+.*\n?)*)",
        "",
        out,
    )
    # Remove "Supporting Observations" section in all heading formats (###, **, plain)
    # plus every bullet / paragraph line immediately beneath it.
    out = re.sub(
        r"(?im)^(?:#{1,3}\s+|\*{1,2})?Supporting\s+Observations\*{0,2}\s*:?\s*\n"
        r"((?:[ \t]*[-*•\d.].*\n?)*)",
        "",
        out,
    )
    # Also strip any bare "- Contribution % = ..." / "- Market Share % = ..." bullet lines
    # that leaked through without a header.
    out = re.sub(
        r"(?im)^\s*[-*•]\s+(Contribution\s+%|Market\s+Share\s+%|Mis[-\s]*targeted\s+HCPs)\s*=.*\n?",
        "",
        out,
    )
    # Remove "Recommended Chart" section (header + all lines until the next blank line or header).
    out = re.sub(
        r"(?im)^(?:#{1,3}\s+|\*{1,2})?Recommended\s+Chart\*{0,2}\s*:?\s*\n"
        r"((?:[ \t]*.*\n?)*?)(?=\n(?:#{1,3}\s|\*{1,2}|\Z)|\Z)",
        "",
        out,
    )
    # Simpler fallback: remove any line starting with "Recommended Chart"
    out = re.sub(r"(?im)^(?:#{1,3}\s+|\*{1,2})?Recommended\s+Chart\b.*\n?", "", out)
    # Remove "A <type> chart ..." standalone sentence lines (chart recommendation text)
    out = re.sub(
        r"(?im)^A\s+\*{0,2}(?:bar|line|pie|donut|scatter)\s+chart\*{0,2}[^\n]*\n?",
        "",
        out,
    )
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _strip_quarter_bias(text: str) -> str:
    """Remove hardcoded Q1'26 / Q1 2026 phrasing unless user asked for that quarter."""
    if not text:
        return text
    out = re.sub(r"\s+for\s+Q1['\s]?2?6\.?", "", text, flags=re.IGNORECASE)
    out = re.sub(r"\s+for\s+Q1\s+2026\.?", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+for\s+the\s+latest\s+quarter\.?", "", out, flags=re.IGNORECASE)
    return out.strip()


_FOLLOWUP_PRESENTATION_PATTERNS = re.compile(
    r"^\s*"
    r"(?:(?:give|show|display|render|make|create|plot|draw)(?:\s+me)?\s+)?"
    r"(?:(?:the|a|an)\s+)?"
    r"(?:"
    r"table|tabular(?:\s+form)?|chart|bar\s+chart|line\s+chart|pie\s+chart|donut(?:\s+chart)?|"
    r"graph|visualization|visual|plot|export|download|csv|data|that|it|this"
    r")"
    r"\s*(?:please)?\s*[.!?]?\s*$",
    re.IGNORECASE,
)

_FOLLOWUP_CONTEXT_PATTERNS = re.compile(
    r"\b(same|that|those|them|it|this|above|previous|last|the\s+result"
    r"|their|they|these|whose|its|he|she|the\s+same|the\s+above"
    r"|for\s+them|of\s+them|of\s+these|of\s+those)\b",
    re.IGNORECASE,
)

# Short queries that are almost always attribute follow-ups on the previous result.
_ATTRIBUTE_FOLLOWUP_RE = re.compile(
    r"^\s*(?:what(?:\s+are|\s+is|\s+tell\s+me)?|show(?:\s+me)?|give\s+me|list|also\s+show|tell\s+me)?\s*"
    r"(?:their|these|those|the(?:se)?|its?)\s+\w[\w\s]*\??$",
    re.IGNORECASE,
)

# Known HCP / HCO entity-level attributes — short queries asking only for these (with no filter
# context of their own) are almost certainly attribute follow-ups on the previous result.
_ENTITY_ATTRIBUTE_RE = re.compile(
    r"\b(hco[\s_]?name|hco|hcos|specialty|specialties|decile|npi|territory|territories"
    r"|region|zip|state|address|affiliation|target[\s_]?flag|segment)\b",
    re.IGNORECASE,
)

# Chart/visual type keywords — when the user asks for one of these without any scope of
# their own, it is a presentation follow-up on the previous result.
_CHART_TYPE_RE = re.compile(
    r"\b(bar\s+chart|line\s+chart|pie\s+chart|donut(?:\s+chart)?|bar\s+graph|chart|graph|visual)\b",
    re.IGNORECASE,
)


def _is_presentation_followup(question: str) -> bool:
    """True when user only wants to reformat/extend the previous result."""
    q = question.strip()
    words = q.split()
    if _FOLLOWUP_PRESENTATION_PATTERNS.match(q):
        return True
    # Pronoun-based context reference — relax word-count gate to 15 words
    if _FOLLOWUP_CONTEXT_PATTERNS.search(q) and len(words) <= 15:
        return True
    # Short attribute queries with explicit pronouns ("their hco name", "these specialties")
    if _ATTRIBUTE_FOLLOWUP_RE.match(q) and len(words) <= 10:
        return True
    # Loose pronoun phrasing ("what tell me their X", "show their X")
    if re.search(r"\b(their|these|those)\b", q, re.IGNORECASE) and len(words) <= 10:
        return True
    # Short attribute-only queries with no filter context of their own
    # e.g. "show me hco names", "list specialties", "what are their deciles"
    if _ENTITY_ATTRIBUTE_RE.search(q) and len(words) <= 7:
        return True
    # Pure chart/visual type request with no scope ("show bar chart", "bar chart", "give me line graph")
    if _CHART_TYPE_RE.search(q) and len(words) <= 6:
        return True
    return False


def _inject_context_into_question(question: str, last_question: str) -> str:
    """Rewrite a vague follow-up into a self-contained question."""
    if not last_question:
        return question

    q = question.strip().lower()

    wants_chart = bool(
        re.search(r"\b(chart|graph|bar|line|pie|donut|plot|visual)\b", q, re.IGNORECASE)
    )
    wants_table = bool(
        re.search(r"\b(table|data|csv|download|export)\b", q, re.IGNORECASE)
    )

    chart_type = "bar chart"
    if re.search(r"\bline\b", q):
        chart_type = "line chart"
    elif re.search(r"\bpie\b|\bdonut\b", q):
        chart_type = "pie chart"

    if wants_chart:
        return (
            f"{last_question} — present the results as a {chart_type}. "
            f"Use the exact same filters, data, and scope as the previous answer."
        )
    if wants_table:
        return (
            f"{last_question} — return the results as a data table. "
            f"Use the exact same filters, data, and scope as the previous answer."
        )

    # Attribute follow-up — user asks for a specific attribute of the previous result's HCPs.
    # Handles both pronoun-based ("their hco names") and noun-only ("show me hco names").
    pronoun_re = re.compile(r"\b(their|they|these|those|them|it|this|that)\b", re.IGNORECASE)
    has_pronoun = bool(pronoun_re.search(question))
    has_entity_attr = bool(_ENTITY_ATTRIBUTE_RE.search(question))

    if has_pronoun or has_entity_attr:
        # Extract the attribute — after the pronoun if present, else the whole noun phrase
        if has_pronoun:
            attr_match = re.search(
                r"\b(?:their|they|these|those|them|its?|this|that)\b[,\s]*([\w\s]+?)[\?\.]*$",
                question, re.IGNORECASE
            )
            attr = attr_match.group(1).strip() if attr_match else question
        else:
            # Strip leading verb phrases like "show me / list / give me / what are"
            attr = re.sub(
                r"^\s*(?:show(?:\s+me)?|give\s+me|list|what\s+(?:are|is)|tell\s+me)\s+",
                "", question, flags=re.IGNORECASE
            ).strip(" ?.")
        return (
            f"For the SAME HCPs from the previous query '{last_question}', "
            f"show ONLY their {attr} as a plain table. "
            f"Do NOT re-run a new TRx analysis. "
            f"SELECT hcp_name and {attr} (or the closest matching column) for those exact HCPs."
        )

    return f"Based on the previous query '{last_question}': {question}"


def _last_context_question(
    buf: ConversationBuffer,
    previous_question: str | None,
    prior_turns: list[_ClientTurn] | None,
) -> str:
    """Best available original data question for format-only follow-ups."""
    stored = (buf.get_last_question() or "").strip()
    if stored and not _is_presentation_followup(stored):
        return stored

    last_user = (buf.last_user_question() or "").strip()
    if last_user and not _is_presentation_followup(last_user):
        return last_user

    prev = (previous_question or "").strip()
    if prev and not _is_presentation_followup(prev):
        return prev

    if prior_turns:
        for turn in reversed(prior_turns):
            user = turn.user.strip()
            if user and not _is_presentation_followup(user):
                return user
    return ""


def _strip_quarter_bias_from_response_text(text: str) -> str:
    """Apply quarter-bias strip to plain markdown or to ``answer_text`` inside JSON."""
    if not (text or "").strip():
        return text or ""
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and isinstance(obj.get("answer_text"), str):
                obj["answer_text"] = _strip_quarter_bias(obj["answer_text"])
                return json.dumps(obj, ensure_ascii=True)
        except Exception:
            pass
    return _strip_quarter_bias(text)


def _normalize_structured_response(raw_answer: str, out: Dict[str, Any]) -> Dict[str, Any]:
    """Return standardized API payload fields expected by frontend."""
    parsed: Dict[str, Any] = {}
    try:
        candidate = json.loads(raw_answer) if raw_answer else {}
        if isinstance(candidate, dict):
            parsed = candidate
    except Exception:
        parsed = {}

    answer_text = str(parsed.get("answer_text") or raw_answer or "").strip()
    # Always prefer the full un-truncated result_table from the backend over the LLM's data_table
    data_table = None
    rt = out.get("result_table")
    if isinstance(rt, dict) and isinstance(rt.get("rows"), list):
        data_table = rt.get("rows")
    if data_table is None:
        data_table = parsed.get("data_table")
    chart_recommendation = parsed.get("chart_recommendation")
    chart = out.get("chart")
    
    if chart and isinstance(chart, dict) and isinstance(chart.get("data"), list) and len(chart.get("data", [])) > 1:
        # Override LLM's chart recommendation with backend's superior heuristics
        chart_recommendation = {
            "show_chart": True,
            "chart_type": str(chart.get("kind")),
            "x_axis": "name",
            "y_axis": "value",
            "title": str(chart.get("title") or ""),
            "rationale": str(chart.get("description") or ""),
        }
    elif chart_recommendation is None:
        chart_recommendation = {
            "show_chart": False,
            "chart_type": "none",
            "x_axis": "",
            "y_axis": "",
            "title": "",
            "rationale": "",
        }
    clarification_needed = parsed.get("clarification_needed")
    return {
        "answer_text": answer_text,
        "data_table": data_table if isinstance(data_table, list) else None,
        "chart_recommendation": chart_recommendation,
        "clarification_needed": clarification_needed if isinstance(clarification_needed, str) and clarification_needed.strip() else None,
    }


def _pie_chart_from_share_text(question: str, answer_text: str) -> dict[str, Any] | None:
    """Build a small pie chart for explicit pie follow-ups when the answer only has share text."""
    if not re.search(r"\b(pie|donut)\b", question or "", re.IGNORECASE):
        return None
    pairs: list[dict[str, object]] = []
    seen: set[str] = set()
    for label, pct in re.findall(
        r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?([A-Za-z][A-Za-z /&-]{1,40}?)(?:\s+share|\s+portion)?(?:\*\*)?\s*[:=-]\s*~?(\d+(?:\.\d+)?)\s*%",
        answer_text or "",
    ):
        name = re.sub(r"\s+", " ", label).strip(" .:-").title()
        if not name or name.lower() in seen:
            continue
        value = float(pct)
        if value <= 0:
            continue
        seen.add(name.lower())
        pairs.append({"name": name, "value": value})
    if len(pairs) < 2:
        return None
    return {
        "kind": "pie",
        "data": pairs,
        "title": "Payer Mix Share",
        "description": "Share percentages from the previous answer.",
    }


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

    # Purge any rejection/fallback replies that may have been accidentally cached in a
    # previous run, so legitimate questions always get a fresh LLM response.
    purged = purge_bad_cache_entries()
    if purged:
        _log.warning("Startup: purged %d bad cache entries (canned rejection replies).", purged)
    yield


app = FastAPI(title="Arcutis Biotherapeutics Data Assistant API", version="1.0.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key"],
    expose_headers=["X-Process-Time-Ms"],
)


def _query_json(
    body: Dict[str, Any],
    *,
    duration_ms: int,
    status_code: int = 200,
    request_id: str | None = None,
) -> JSONResponse:
    out = {**body, "duration_ms": duration_ms}
    if request_id:
        out["request_id"] = request_id
    headers = {"X-Process-Time-Ms": str(duration_ms)}
    if request_id:
        headers["X-Request-Id"] = request_id
    return JSONResponse(
        status_code=status_code,
        content=out,
        headers=headers,
    )


def _is_request_authorized(request: Request) -> bool:
    """Check API key guard for server-to-server calls."""
    if not _backend_api_key:
        return True
    presented = (request.headers.get("x-api-key") or "").strip()
    return bool(presented) and presented == _backend_api_key

_buffers: "OrderedDict[str, ConversationBuffer]" = OrderedDict()
_buffer_lock = Lock()


def _get_buffer(session_id: str) -> ConversationBuffer:
    """One conversation buffer per browser session (isolated Redis list key when Redis is on)."""
    with _buffer_lock:
        if session_id not in _buffers:
            if len(_buffers) >= _max_sessions:
                _buffers.popitem(last=False)
            rkey = f"sda:api:session:{session_id}:turns"
            _buffers[session_id] = ConversationBuffer(redis_list_key=rkey)
        _buffers.move_to_end(session_id)
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
    force_refresh: bool = False  # when True, bypass cache and always run a fresh LLM call


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
        for t in prior_turns[-3:]:
            u = t.user.strip()
            a = (t.assistant or "").strip()
            if u:
                buf.append(u, "(client-history)", a if a else "(no answer)")
    return buf


@app.get("/health")
def health(
    request: Request,
    full: bool = Query(False, description="Include redis_qa_cache diagnostics"),
) -> dict[str, Any]:
    """
    Liveness: ``{"status": "ok"}``.

    With ``?full=1``, includes ``redis_qa_cache`` (reachable, TTL, connection errors).
    If ``reachable`` is false, QA result caching will not read or write — start Redis or fix REDIS_* env.
    """
    out: dict[str, Any] = {"status": "ok"}
    if full:
        if not _is_request_authorized(request):
            return {"status": "ok", "redis_qa_cache": {"authorized": False}}
        out["redis_qa_cache"] = redis_qa_cache_status()
    return out


@app.get("/ready")
def ready() -> dict[str, Any]:
    """Readiness probe: returns 200 + status so Render health checks and keep-alive pings pass."""
    missing = []
    using_azure = bool(
        os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("azure_openai_endpoint")
    )
    using_openai = bool(os.getenv("OPENAI_API_KEY"))

    if not using_azure and not using_openai:
        missing.append("AZURE_OPENAI_ENDPOINT or OPENAI_API_KEY")
    if using_azure and not (
        os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT")
        or os.getenv("AZURE_OPENAI_DEPLOYMENT")
        or os.getenv("chat_deployment")
    ):
        missing.append("AZURE_OPENAI_CHAT_DEPLOYMENT")

    if not (os.getenv("PGHOST") or os.getenv("DATABASE_URL")):
        missing.append("PGHOST / DATABASE_URL")

    if missing:
        return {"status": "degraded", "missing_config": missing}
    return {"status": "ready"}


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
    request_id = (request.headers.get("x-request-id") or str(uuid4())).strip()
    if not _is_request_authorized(request):
        return JSONResponse(
            status_code=401,
            content={"success": False, "response": "", "error": "Unauthorized request.", "request_id": request_id},
            headers={"X-Request-Id": request_id},
        )

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
                    "error": "Invalid JSON body.",
                },
                duration_ms=elapsed,
                status_code=422,
                request_id=request_id,
            )
        question = payload.question
        session_id = payload.session_id
        previous_question = payload.previous_question
        previous_sql = payload.previous_sql
        prior_turns = payload.prior_turns
        force_refresh = payload.force_refresh
    else:
        qp = request.query_params
        question = qp.get("question") or ""
        session_id = qp.get("session_id") or ""
        previous_question = qp.get("previous_question")
        previous_sql = qp.get("previous_sql")
        prior_turns = None
        force_refresh = qp.get("force_refresh", "").lower() in ("1", "true", "yes")
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
                request_id=request_id,
            )

    buf = _prepare_buffer(
        session_id,
        previous_question=previous_question,
        previous_sql=previous_sql,
        prior_turns=prior_turns,
    )
    # ── Follow-up presentation context injection ──────────────────────────
    original_question = question
    context_question = question
    if _is_presentation_followup(question):
        last_q = _last_context_question(buf, previous_question, prior_turns)
        if last_q:
            question = _inject_context_into_question(question, last_q)
            context_question = last_q
            _log.info(
                "Presentation follow-up detected. Rewrote: '%s' → '%s'",
                original_question,
                question,
            )
        if re.search(r"\b(pie|donut)\b", original_question, re.IGNORECASE):
            last_result = buf.get_last_result() or {}
            previous_answer = str(last_result.get("answer_text") or "").strip()
            if not previous_answer and prior_turns:
                previous_answer = str(prior_turns[-1].assistant or "").strip()
            pie_chart = _pie_chart_from_share_text(original_question, previous_answer)
            if pie_chart:
                elapsed = ms()
                body = {
                    "success": True,
                    "response": previous_answer,
                    "answer_text": previous_answer,
                    "data_table": pie_chart["data"],
                    "chart": pie_chart,
                    "chart_recommendation": {
                        "show_chart": True,
                        "chart_type": "pie",
                        "x_axis": "name",
                        "y_axis": "value",
                        "title": str(pie_chart["title"]),
                        "rationale": str(pie_chart["description"]),
                    },
                    "clarification_needed": None,
                    "sql": previous_sql,
                    "row_count": len(pie_chart["data"]),
                    "cache_hit": False,
                }
                return _query_json(body, duration_ms=elapsed, request_id=request_id)
    # ── End follow-up injection ───────────────────────────────────────────

    buf_out = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_out):
            out = run_question_pipeline_turn(
                question,
                conversation=buf,
                use_cache=not force_refresh,
                trace_metadata={"session_id": session_id},
            )
    except Exception as e:
        elapsed = ms()
        _log.warning("/query exception after %dms: %s", elapsed, e)
        return _query_json(
            {
                "success": True,
                "response": (
                    "I didn't quite catch that — could you rephrase your question? "
                    "I'm here to help with HCP data, ZORYVE TRx, territory performance, "
                    "payer mix, and related Arcutis insights."
                ),
                "error": None,
                "clarification_needed": (
                    "Could you try rephrasing? I want to make sure I understand "
                    "exactly what you're looking for."
                ),
                "data_table": None,
                "chart_recommendation": {"show_chart": False, "chart_type": "none"},
            },
            duration_ms=elapsed,
            request_id=request_id,
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
            request_id=request_id,
        )
    if not ans:
        elapsed = ms()
        _log.info("/query done success=False (no answer) in %dms", elapsed)
        return _query_json(
            {
                "success": True,
                "response": (
                    "I wasn't able to find data for that. Could you tell me a bit more — "
                    "for example, which region, time period, or metric you're interested in?"
                ),
                "clarification_needed": (
                    "Please provide more detail so I can find the right data for you."
                ),
                "data_table": None,
                "chart_recommendation": {"show_chart": False, "chart_type": "none"},
                "sql": out.get("sql"),
                "row_count": out.get("row_count", 0),
            },
            duration_ms=elapsed,
            request_id=request_id,
        )
    elapsed = ms()
    _log.info("/query done success=True in %dms", elapsed)
    chat_nl = strip_sql_from_nl_chat_markup(ans)
    chat_nl = _strip_sources_checked_line(chat_nl)
    chat_nl = _strip_quarter_bias_from_response_text(chat_nl)
    normalized = _normalize_structured_response(chat_nl, out)
    normalized["answer_text"] = _strip_quarter_bias(str(normalized.get("answer_text") or ""))
    fallback_pie_chart = None
    if not out.get("chart"):
        fallback_pie_chart = _pie_chart_from_share_text(original_question, normalized["answer_text"])
        if fallback_pie_chart:
            normalized["chart_recommendation"] = {
                "show_chart": True,
                "chart_type": "pie",
                "x_axis": "name",
                "y_axis": "value",
                "title": str(fallback_pie_chart["title"]),
                "rationale": str(fallback_pie_chart["description"]),
            }
            normalized["data_table"] = fallback_pie_chart["data"]
    body: Dict[str, Any] = {
        "success": True,
        "response": sanitize_user_visible_text(chat_nl) or chat_nl,
        "answer_text": normalized["answer_text"],
        "data_table": normalized["data_table"],
        "chart_recommendation": normalized["chart_recommendation"],
        "clarification_needed": normalized["clarification_needed"],
        "sql": out.get("sql"),
        "row_count": out.get("row_count", 0),
        "cache_hit": bool(out.get("cache_hit")),
    }
    if out.get("chart"):
        body["chart"] = normalize_chart_month_labels(out["chart"])
    elif fallback_pie_chart:
        body["chart"] = normalize_chart_month_labels(fallback_pie_chart)
    if out.get("result_table"):
        body["result_table"] = out["result_table"]
    if out.get("result_table_multipart_last_part"):
        body["result_table_multipart_last_part"] = True
    if out.get("sub_results"):
        body["sub_results"] = out["sub_results"]
    # Store context for next follow-up
    buf.store_last_question(context_question)
    buf.store_last_result(
        {
            "question": original_question,
            "data_table": normalized["data_table"],
            "chart_recommendation": normalized["chart_recommendation"],
            "answer_text": normalized["answer_text"],
        }
    )

    return _query_json(body, duration_ms=elapsed, request_id=request_id)
