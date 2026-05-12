"""Embedding + similarity search over ERD chunks → table names + join paths for text-to-SQL.

Index is built from ``ERD.md`` (plus FK edges from ``pharma_schema``) and cached under ``src/data/``.
Each user question is embedded once per SQL generation; top-k chunks are concatenated with derived
join paths and passed to the LLM as ``RETRIEVAL`` context (see ``text_to_sql_prompt``).

Disable with ``SDA_DISABLE_SCHEMA_RAG=1``. If Azure embedding variables are unset, index build is skipped
(no warning per query); SQL generation still uses the full **ERD** in the prompt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from embedding_rag_common import _embeddings_env_ready, embed_texts, rag_data_dir
from langsmith_config import traceable
from pharma_schema import erd_markdown_path, get_all_tables, pharma_relationships

logger = logging.getLogger(__name__)

_RAG_EMBEDDINGS_SKIP_LOGGED = False

_INDEX_NAME = "schema_rag_index.json"
_DEFAULT_TOP_K = 6
_MAX_CHUNK_CHARS = 4500
_MAX_RETRIEVAL_OUT = int(os.getenv("SDA_SCHEMA_RAG_MAX_CHARS", "28000"))


def _index_path() -> Path:
    return rag_data_dir() / _INDEX_NAME


def _disabled() -> bool:
    return (os.getenv("SDA_DISABLE_SCHEMA_RAG") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _l2_normalize(vec: List[float]) -> List[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _cosine(a: List[float], b: List[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _tables_from_erd_section(body: str) -> List[str]:
    # ERD may use ### or #### before backticked table names, e.g. ### `REP_ACTIVITY` *(Fact Table)*
    found = re.findall(r"(?:###|####) `([A-Za-z0-9_]+)`", body)
    # de-dup preserve order
    seen: set[str] = set()
    out: List[str] = []
    for t in found:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            out.append(t)
    return out


def _fk_table_name(fqn: str) -> str:
    parts = fqn.replace('"', "").split(".")
    if len(parts) >= 3:
        return parts[1].lower()
    return ""


def _relationships_chunk() -> Dict[str, Any]:
    rels = pharma_relationships()
    lines = [
        "Schema join graph (use with ERD column detail):",
        "Known tables: " + ", ".join(get_all_tables()),
        "",
        "Join edges (left column belongs to left table, right to right table):",
    ]
    tables: set[str] = set()
    for r in rels:
        left, right = r.get("left") or "", r.get("right") or ""
        lines.append(f"- {left}  →  {right}")
        lt, rt = _fk_table_name(left), _fk_table_name(right)
        if lt:
            tables.add(lt)
        if rt:
            tables.add(rt)
    text = "\n".join(lines)
    return {"id": "meta:fk_join_graph", "text": text[:_MAX_CHUNK_CHARS], "tables": sorted(tables)}


def _split_section_on_headers(text: str, header_re: str, max_chars: int) -> List[Tuple[str, str]]:
    """Split *text* on *header_re* pattern; yield (title, body) pairs each ≤ max_chars chars."""
    text = text.strip()
    matches = list(re.finditer(header_re, text, re.MULTILINE))
    if not matches:
        # No sub-headers: yield as sliding windows if too large
        if len(text) <= max_chars:
            return [("section", text)]
        chunks: List[Tuple[str, str]] = []
        step = max_chars - 200
        for i in range(0, len(text), step):
            chunks.append(("section", text[i : i + max_chars]))
        return chunks
    out: List[Tuple[str, str]] = []
    # text before first sub-header
    pre = text[: matches[0].start()].strip()
    if pre:
        out.append(("preamble", pre[:max_chars]))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        title = body.split("\n", 1)[0].replace("#", "").strip()[:120]
        if len(body) <= max_chars:
            out.append((title, body))
        else:
            # Further split large sections into sliding windows
            step = max_chars - 200
            for j in range(0, len(body), step):
                out.append((title, body[j : j + max_chars]))
    return out


def _split_erd_into_chunks(erd_text: str) -> List[Tuple[str, str]]:
    """Split ERD markdown into fine-grained (title, body) chunks.

    Strategy (coarse → fine):
    1. Split on ``##`` top-level headers.
    2. Within each section, split again on ``###`` headers.
    3. Within each sub-section, split again on ``####`` headers.
    Each chunk is capped at ``_MAX_CHUNK_CHARS`` characters.
    """
    erd_text = erd_text.strip()
    if not erd_text:
        return [("empty", "")]

    # Step 1: split on ## headers (or treat whole text as one section)
    h2_matches = list(re.finditer(r"(?m)^## ", erd_text))
    h2_sections: List[Tuple[str, str]] = []
    if not h2_matches:
        h2_sections = [("erd_full", erd_text)]
    else:
        pre = erd_text[: h2_matches[0].start()].strip()
        if pre:
            h2_sections.append(("erd_preamble", pre))
        for i, m in enumerate(h2_matches):
            start = m.start()
            end = h2_matches[i + 1].start() if i + 1 < len(h2_matches) else len(erd_text)
            body = erd_text[start:end].strip()
            if body:
                title = body.split("\n", 1)[0].replace("#", "").strip()[:120]
                h2_sections.append((title, body))

    out: List[Tuple[str, str]] = []
    for h2_title, h2_body in h2_sections:
        if len(h2_body) <= _MAX_CHUNK_CHARS:
            out.append((h2_title, h2_body))
            continue
        # Step 2: split on ### within this section
        h3_pairs = _split_section_on_headers(h2_body, r"^### ", _MAX_CHUNK_CHARS)
        for h3_title, h3_body in h3_pairs:
            if len(h3_body) <= _MAX_CHUNK_CHARS:
                out.append((h3_title or h2_title, h3_body))
                continue
            # Step 3: split on #### within this sub-section
            h4_pairs = _split_section_on_headers(h3_body, r"^#### ", _MAX_CHUNK_CHARS)
            for h4_title, h4_body in h4_pairs:
                out.append((h4_title or h3_title or h2_title, h4_body))
    return out


def _erd_fingerprint(erd_path: Path) -> str:
    raw = erd_path.read_bytes()
    return hashlib.sha256(raw).hexdigest()


def _build_chunk_dicts(erd_path: Path) -> List[Dict[str, Any]]:
    erd_text = erd_path.read_text(encoding="utf-8", errors="replace")
    chunks: List[Dict[str, Any]] = []
    # FK / table list chunk first — stabilizes join-path retrieval
    fk = _relationships_chunk()
    fk["id"] = "meta:fk_join_graph"
    chunks.append(fk)

    for title, body in _split_erd_into_chunks(erd_text):
        if not body.strip():
            continue
        tid = re.sub(r"[^A-Za-z0-9_]+", "_", title.lower())[:60] or "section"
        cid = f"erd:{tid}"
        tabs = _tables_from_erd_section(body)
        chunks.append({"id": cid, "text": body, "tables": tabs})
    return chunks


def _save_index(path: Path, erd_fp: str, chunks: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = []
    for c in chunks:
        serializable.append(
            {
                "id": c["id"],
                "text": c["text"],
                "tables": c.get("tables") or [],
                "embedding": c.get("embedding"),
            }
        )
    payload = {"version": 1, "erd_sha256": erd_fp, "chunks": serializable}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    tmp.replace(path)


def _load_index(path: Path) -> Dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def ensure_schema_rag_index(*, force: bool = False) -> None:
    """Build or refresh the embedding index when ERD changes or index is missing."""
    global _RAG_EMBEDDINGS_SKIP_LOGGED
    if _disabled():
        return
    if not _embeddings_env_ready():
        if not _RAG_EMBEDDINGS_SKIP_LOGGED:
            _RAG_EMBEDDINGS_SKIP_LOGGED = True
            logger.info(
                "schema_rag: Azure embeddings env not set (AZURE_OPENAI_KEY, AZURE_OPENAI_ENDPOINT, "
                "AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT) — schema RAG retrieval off; text-to-SQL still uses ERD.md. "
                "Set those variables to enable RAG, or set SDA_DISABLE_SCHEMA_RAG=1 to skip RAG entirely."
            )
        return
    erd_path = erd_markdown_path()
    if not erd_path.is_file():
        logger.warning("schema_rag: ERD not found at %s — skip index build", erd_path)
        return
    fp = _erd_fingerprint(erd_path)
    idx_path = _index_path()
    rebuild_env = (os.getenv("SDA_SCHEMA_RAG_REBUILD") or "").strip().lower() in ("1", "true", "yes")
    existing = _load_index(idx_path)
    if not force and not rebuild_env and existing and existing.get("erd_sha256") == fp:
        chunks = existing.get("chunks") or []
        if chunks and all(c.get("embedding") for c in chunks):
            return

    chunks = _build_chunk_dicts(erd_path)
    texts = [c["text"] for c in chunks]
    try:
        vectors = embed_texts(texts)
    except Exception as e:
        logger.warning("schema_rag: could not embed index (%s) — RAG disabled until fixed.", e)
        raise
    for c, v in zip(chunks, vectors):
        c["embedding"] = v
    _save_index(idx_path, fp, chunks)
    logger.info("schema_rag: wrote %s chunks to %s", len(chunks), idx_path)


def _join_paths_among_tables(table_set: set[str]) -> List[str]:
    """Return FK edge lines where both endpoints' base tables are in ``table_set``."""
    rels = pharma_relationships()
    lines: List[str] = []
    for r in rels:
        left, right = r.get("left") or "", r.get("right") or ""
        lt, rt = _fk_table_name(left), _fk_table_name(right)
        if lt in table_set and rt in table_set:
            lines.append(f"{left}  →  {right}")
    return lines


@traceable(name="SDA | schema RAG retrieval (ERD embeddings)", run_type="chain")
def retrieval_context_for_nl_question(question: str) -> str:
    """
    Embed ``question``, score cached ERD chunks by cosine similarity, and return a prompt block
    with top tables + join paths. Returns empty string when RAG is disabled or unavailable.
    """
    if _disabled():
        return ""
    q = (question or "").strip()
    if not q:
        return ""
    try:
        ensure_schema_rag_index()
    except Exception:
        return ""

    idx_path = _index_path()
    data = _load_index(idx_path)
    if not data:
        return ""
    chunks_in = data.get("chunks") or []
    chunks: List[Dict[str, Any]] = [c for c in chunks_in if c.get("embedding") and c.get("text")]
    if not chunks:
        return ""

    try:
        qvec = embed_texts([q[:8000]])[0]
    except Exception as e:
        logger.warning("schema_rag: query embed failed (%s)", e)
        return ""
    qn = _l2_normalize(qvec)

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for c in chunks:
        emb = c.get("embedding") or []
        if not emb:
            continue
        scored.append((_cosine(qn, _l2_normalize(emb)), c))
    scored.sort(key=lambda x: -x[0])

    top_k = int(os.getenv("SDA_SCHEMA_RAG_TOP_K") or str(_DEFAULT_TOP_K))
    top_k = max(1, min(16, top_k))
    top_pairs = scored[:top_k]
    top_chunks = [c for _, c in top_pairs]

    union_tables: set[str] = set()
    for c in top_chunks:
        for t in c.get("tables") or []:
            union_tables.add(str(t).lower())

    join_lines = _join_paths_among_tables(union_tables)
    # If very few tables, still surface cross-table edges touching any hit table
    if len(join_lines) < 3 and union_tables:
        extra: List[str] = []
        for r in pharma_relationships():
            left, right = r.get("left") or "", r.get("right") or ""
            lt, rt = _fk_table_name(left), _fk_table_name(right)
            if (lt in union_tables) ^ (rt in union_tables):
                extra.append(f"{left}  →  {right}")
        join_lines.extend(extra[:20])

    parts: List[str] = []
    parts.append(
        "The following snippets were selected by **embedding similarity** to the user question. "
        "Prefer tables and joins mentioned here when writing SQL; use **ERD_CONTEXT** below for full column lists."
    )
    for i, (score, c) in enumerate(top_pairs, 1):
        tabs = ", ".join(c.get("tables") or []) or "(see text)"
        parts.append(f"--- Snippet {i} (similarity={score:.4f}) — tables: **{tabs}** ---\n{c.get('text', '').strip()}")

    if join_lines:
        parts.append("--- **Join paths (FK-style) among tables in the snippets above** ---\n" + "\n".join(join_lines[:48]))

    out = "\n\n".join(parts).strip()
    if len(out) > _MAX_RETRIEVAL_OUT:
        out = out[: _MAX_RETRIEVAL_OUT - 20] + "\n... [truncated]"
    return out


def rebuild_schema_rag_index_cli() -> None:
    """CLI entry: ``python -c \"from schema_rag import rebuild_schema_rag_index_cli; rebuild_schema_rag_index_cli()\"``"""
    ensure_schema_rag_index(force=True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("rebuild", "--rebuild", "-f", "force"):
        rebuild_schema_rag_index_cli()
