"""Multi-turn conversation buffer for the SQL agent.

Provides two classes:

ConversationBuffer  — in-session buffer used by qa_pipeline.py (drop-in
                      replacement for the original commented-out version).
                      Automatically syncs every turn to Redis so context
                      survives restarts.

ConversationContext — lower-level Redis read/write used internally by
                      ConversationBuffer. Can also be used standalone.
"""

from __future__ import annotations

import json
import os
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from redis_config import make_redis_client


# ── Config ────────────────────────────────────────────────────────────────────
_CTX_KEY   = "sda:context:turns"   # Redis list — most-recent first
_MAX_TURNS = 8                      # default turns kept (multi-turn follow-ups)
_CTX_TTL   = 7200                  # 2-hour session window — supports longer 3+ turn chats
# Must match qa_pipeline anchor budget — truncating mid-SQL breaks follow-ups (e.g. "by region").
_MAX_STORED_SQL_CHARS = int(os.getenv("CONVERSATION_MAX_SQL_CHARS", "12000"))
_MAX_STORED_ANSWER_CHARS = int(os.getenv("CONVERSATION_MAX_ANSWER_CHARS", "4000"))


# ── Metric / period / entity extractors ──────────────────────────────────────

_METRIC_PATTERNS = {
    "reach":           r"\breach\b",
    "frequency":       r"\bfrequency\b",
    "call_attainment": r"\bcall.attainment\b|\battainment\b",
    "no_of_calls_tot": r"\btotal.calls?\b|\bcalls?\b",
    "no_of_calls":     r"\bactual.calls?\b",
    "call_volume":     r"\bactivity\b|\binteractions?\b",
}

_PERIOD_PATTERNS = {
    "current_month":   r"\bcurrent.month\b|\bthis.month\b",
    "last_month":      r"\blast.month\b|\bprevious.month\b",
    "last_quarter":    r"\blast.quarter\b|\bprevious.quarter\b",
    "M1": r"\bM1\b", "M2": r"\bM2\b", "M3": r"\bM3\b",
    "M4": r"\bM4\b", "M5": r"\bM5\b", "M6": r"\bM6\b",
    "CR6M": r"\bCR6M\b|\brolling.6.month\b",
}

_ENTITY_PATTERNS = {
    "territory": r"\bterrit\w+\b",
    "district":  r"\bdistrict\b",
    "region":    r"\bregion\b",
    "rep":       r"\brep\b|\brepresentative\b",
    "hcp":       r"\bhcp\b|\bphysician\b|\bdoctor\b",
    "brand":     r"\bbrand\b|\bentyvio\b|\bproduct\b",
}

_FOLLOW_UP_SIGNALS = [
    r"\bnow\b", r"\binstead\b", r"\bsame\b", r"\bthat\b",
    r"\bthose\b", r"\bcompare\b", r"\bvs\b",
    r"\bbreak.*(down|it)\b", r"\bonly\b", r"\bjust\b",
    r"\bshow.me.more\b", r"\bfilter\b", r"\bexclude\b",
]


def _extract(text: str, patterns: Dict[str, str]) -> Optional[str]:
    t = text.lower()
    for key, pat in patterns.items():
        if re.search(pat, t):
            return key
    return None


def _extract_all(text: str, patterns: Dict[str, str]) -> List[str]:
    t = text.lower()
    return [key for key, pat in patterns.items() if re.search(pat, t)]


def _parse_turn(question: str, sql: str, answer: str = "") -> Dict[str, Any]:
    combined = f"{question} {sql}"
    cap = max(600, _MAX_STORED_ANSWER_CHARS)
    ans = answer.strip()
    if len(ans) > cap:
        ans = ans[: cap - 3] + "..."
    return {
        "question":  question,
        "sql":       sql,
        "answer":    ans,
        "metric":    _extract(combined, _METRIC_PATTERNS),
        "period":    _extract(combined, _PERIOD_PATTERNS),
        "entities":  _extract_all(combined, _ENTITY_PATTERNS),
        "timestamp": datetime.utcnow().isoformat(),
    }


# ── Redis client ──────────────────────────────────────────────────────────────


def _make_client():
    return make_redis_client()


# ── ConversationContext (low-level Redis layer) ───────────────────────────────

class ConversationContext:
    """Low-level Redis read/write for conversation turns.

    Used internally by ConversationBuffer.
    Can also be used standalone if needed.
    """

    def __init__(
        self,
        max_turns: int = _MAX_TURNS,
        ttl: int = _CTX_TTL,
        *,
        redis_list_key: str | None = None,
    ):
        self.max_turns = max_turns
        self.ttl       = ttl
        self._key      = redis_list_key or _CTX_KEY
        self._client   = _make_client()
        self._memory: List[Dict] = []   # fallback when Redis is unavailable

    def save(self, question: str, sql: str, answer: str = "") -> None:
        """Persist one completed turn to Redis (or in-memory fallback)."""
        turn    = _parse_turn(question, sql, answer)
        payload = json.dumps(turn, ensure_ascii=True)

        if self._client:
            try:
                pipe = self._client.pipeline()
                pipe.lpush(self._key, payload)
                pipe.ltrim(self._key, 0, self.max_turns - 1)
                pipe.expire(self._key, self.ttl)
                pipe.execute()
                return
            except Exception:
                pass

        self._memory.insert(0, turn)
        self._memory = self._memory[: self.max_turns]

    def load(self) -> List[Dict]:
        """Return recent turns, newest first."""
        if self._client:
            try:
                raw = self._client.lrange(self._key, 0, self.max_turns - 1)
                return [json.loads(r) for r in raw if r]
            except Exception:
                pass
        return list(self._memory)

    def clear(self) -> None:
        if self._client:
            try:
                self._client.delete(self._key)
            except Exception:
                pass
        self._memory.clear()

    def build_context_block(self) -> str:
        """Build context string for LLM prompt injection."""
        turns = self.load()
        if not turns:
            return ""
        lines = ["--- CONVERSATION CONTEXT (most recent first) ---"]
        for i, t in enumerate(turns):
            lines.append(f"\nPrior turn {i + 1} ({t.get('timestamp', '')[:16]}):")
            lines.append(f"  Question : {t['question']}")
            lines.append(f"  Metric   : {t.get('metric') or 'unknown'}")
            lines.append(f"  Period   : {t.get('period') or 'unknown'}")
            lines.append(f"  Entities : {', '.join(t.get('entities') or []) or 'unknown'}")
            sql_preview = t['sql'][:300] + ('...' if len(t['sql']) > 300 else '')
            lines.append(f"  SQL      : {sql_preview}")
            ans = (t.get("answer") or "").strip()
            if ans:
                a_cap = 2000
                ap = ans[:a_cap] + ("..." if len(ans) > a_cap else "")
                lines.append(f"  Answer   : {ap}")
        lines.append(
            "\nIMPORTANT: If the current question uses pronouns or references "
            "('same', 'instead', 'now show', 'that', 'those', 'compare', 'top 2', 'top 3'), "
            "inherit the metric, period, and entity from Prior turn 1 above unless "
            "the user explicitly specifies a different value. "
            "Short asks like **top N** mean: same topic / territory / table grain as the prior answer — "
            "take the first N rows by the ranking already implied (e.g. prescription volume), do not ask to clarify."
        )
        lines.append("--- END CONTEXT ---")
        return "\n".join(lines)


# ── ConversationBuffer (drop-in for qa_pipeline.py) ──────────────────────────

@dataclass
class ConversationTurn:
    question:       str
    sql:            str
    answer_excerpt: str


class ConversationBuffer:
    """Drop-in replacement for the original ConversationBuffer.

    Keeps last N turns in a deque for in-session use AND automatically
    syncs every turn to Redis via ConversationContext so context persists
    across restarts.

    All existing qa_pipeline.py call sites work unchanged:
        buffer.append(question, sql, answer)
        buffer.format_for_prompt()
        buffer.embedding_augmentation(question)
        len(buffer)
    """

    def __init__(
        self,
        max_turns: int | None = None,
        *,
        redis_list_key: str | None = None,
    ) -> None:
        if max_turns is None:
            raw = (
                os.getenv("CONVERSATION_HISTORY_TURNS")
                or os.getenv("conversation_history_turns")
                or str(_MAX_TURNS)
            )
            try:
                max_turns = int(raw)
            except ValueError:
                max_turns = _MAX_TURNS

        self._max  = max(1, min(24, max_turns))
        self._turns: deque[ConversationTurn] = deque(maxlen=self._max)
        self._ctx  = ConversationContext(
            max_turns=self._max,
            redis_list_key=redis_list_key,
        )

        # On startup, reload any turns persisted from a previous session
        self._reload_from_redis()

    # ── Public API (unchanged from original) ─────────────────────────────────

    def append(self, question: str, sql: str, answer: str) -> None:
        """Add a completed turn. Syncs to Redis automatically."""
        excerpt = answer.strip()
        cap = max(600, _MAX_STORED_ANSWER_CHARS)
        if len(excerpt) > cap:
            excerpt = excerpt[: cap - 3] + "..."
        sq = sql.strip()
        if len(sq) > _MAX_STORED_SQL_CHARS:
            sq = sq[: _MAX_STORED_SQL_CHARS - 3] + "..."

        self._turns.append(
            ConversationTurn(question=question, sql=sq, answer_excerpt=excerpt)
        )
        # Persist to Redis for cross-session memory
        self._ctx.save(question, sq, answer)

    def __len__(self) -> int:
        return len(self._turns)

    def last_user_question(self) -> Optional[str]:
        """Most recent user message in this session (for follow-up expansion)."""
        if not self._turns:
            return None
        return self._turns[-1].question

    def last_sql(self) -> Optional[str]:
        """SQL from the most recent completed turn (for adapting filters on follow-ups)."""
        if not self._turns:
            return None
        return self._turns[-1].sql

    def format_for_prompt(self) -> str:
        """Format turns for LLM prompt.

        Prefer the in-memory deque (after ``sync_from_redis``) — it always carries
        **answer excerpts**, which the Redis-only summary block used to omit and
        which short follow-ups like **top 3** need.

        If memory is empty but Redis / fallback still has turns, use
        ``build_context_block()`` (now includes answer snippets).
        """
        if self._turns:
            lines = [
                "--- RECENT CONVERSATION (resolve short follow-ups: 'this month', "
                "'same for reps', **top 1 / top 3**, 'only the first two'; keep the same "
                "metric/tables/grain unless the user clearly changes topic) ---",
            ]
            for i, t in enumerate(self._turns, 1):
                lines.append(f"[Turn {i}] User: {t.question}")
                lines.append(f"         SQL used:\n{t.sql}")
                lines.append(f"         Answer excerpt: {t.answer_excerpt}")
            return "\n".join(lines)

        redis_block = self._ctx.build_context_block()
        return redis_block or ""

    def embedding_augmentation(self, current_question: str) -> str:
        """Combine last turn with current question for better schema retrieval."""
        # Try Redis first for cross-session last turn
        turns = self._ctx.load()
        if turns:
            last = turns[0]
            tail = last["sql"][:800]
            return (
                f"Previous user question: {last['question']}\n"
                f"SQL that answered it (excerpt): {tail}\n"
                f"Follow-up user message: {current_question}"
            )

        # Fallback: in-memory
        if not self._turns:
            return current_question
        last_mem = self._turns[-1]
        tail = last_mem.sql[:800] if len(last_mem.sql) > 800 else last_mem.sql
        return (
            f"Previous user question: {last_mem.question}\n"
            f"SQL that answered it (excerpt): {tail}\n"
            f"Follow-up user message: {current_question}"
        )

    def is_follow_up(self, question: str) -> bool:
        """Heuristic: does this question reference prior context?"""
        q = question.lower()
        return any(re.search(p, q) for p in _FOLLOW_UP_SIGNALS)

    def clear(self) -> None:
        """Wipe all context — both in-memory and Redis."""
        self._turns.clear()
        self._ctx.clear()

    def sync_from_redis(self) -> None:
        """Reload the in-memory deque from Redis (multi-worker / reconnect safety).

        When Redis has no turns, the deque is left unchanged so a process that only
        has in-memory state (Redis temporarily down) does not lose context.
        """
        turns = self._ctx.load()
        if not turns:
            return
        self._turns.clear()
        for t in reversed(turns):
            self._turns.append(
                ConversationTurn(
                    question=t["question"],
                    sql=t.get("sql") or "",
                    answer_excerpt=t.get("answer", ""),
                )
            )

    # ── Internal ─────────────────────────────────────────────────────────────

    def _reload_from_redis(self) -> None:
        """On startup, populate in-memory deque from Redis turns."""
        turns = self._ctx.load()
        if not turns:
            return
        # Turns are newest-first in Redis; reverse to fill deque oldest-first
        for t in reversed(turns):
            self._turns.append(
                ConversationTurn(
                    question=t["question"],
                    sql=t["sql"],
                    answer_excerpt=t.get("answer", ""),
                )
            )