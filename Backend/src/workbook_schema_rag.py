"""Embedding + cosine similarity over **loaded workbook** table metadata (Arcetus sample / Excel → SQLite).

Unlike ``schema_rag`` (ERD.md chunks), every chunk here is built from ``DatabaseState`` — the same
file the API loaded. At query time the user question is embedded once; top-k chunks are prepended
as **RETRIEVAL** hints for the SQL steward.

Requires the same Azure embeddings variables as ``schema_rag`` (see ``_embeddings_env_ready`` there).

- ``SDA_DISABLE_WORKBOOK_RAG=1`` — skip workbook RAG entirely.
- ``SDA_WORKBOOK_RAG_REBUILD=1`` — force re-embed after workbook reload.
- ``SDA_WORKBOOK_RAG_TOP_K`` — top chunks (default 8).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

from data_loader import DatabaseState
from embedding_rag_common import _embeddings_env_ready, embed_texts, rag_data_dir
from langsmith_config import traceable

logger = logging.getLogger(__name__)

_INDEX_NAME = "workbook_rag_index.json"
_DEFAULT_TOP_K = 8
_MAX_CHUNK_CHARS = 4500
_MAX_RETRIEVAL_OUT = int(os.getenv("SDA_WORKBOOK_RAG_MAX_CHARS", "16000"))


def _disabled() -> bool:
    return (os.getenv("SDA_DISABLE_WORKBOOK_RAG") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _index_path() -> Path:
    return rag_data_dir() / _INDEX_NAME


def _workbook_fingerprint(db: DatabaseState) -> str:
    payload = {
        "file_path": db.file_path,
        "loaded_at": db.loaded_at,
        "tables": {
            k: {
                "cols": [c.name for c in v.columns],
                "row_count": v.row_count,
            }
            for k, v in sorted(db.tables.items(), key=lambda x: x[0].lower())
        },
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _table_chunk_text(tname: str, meta: Any) -> str:
    lines = [
        f"Table `{tname}` (source sheet: {meta.original_name}), approximately {meta.row_count} rows.",
        "Columns:",
    ]
    col_parts = [f"  - {c.name} ({c.dtype})" for c in meta.columns]
    body = "\n".join(lines + col_parts)
    if len(body) > _MAX_CHUNK_CHARS:
        body = body[: _MAX_CHUNK_CHARS - 40] + "\n... [column list truncated]"
    return body


def _build_chunks(db: DatabaseState) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for tname, meta in sorted(db.tables.items(), key=lambda x: x[0].lower()):
        text = _table_chunk_text(tname, meta)
        out.append(
            {
                "id": f"wb:{tname}",
                "text": text,
                "tables": [str(tname).lower()],
            }
        )
    return out


def _save_index(path: Path, fp: str, chunks: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = [
        {"id": c["id"], "text": c["text"], "tables": c.get("tables") or [], "embedding": c.get("embedding")}
        for c in chunks
    ]
    payload = {"version": 1, "workbook_sha256": fp, "chunks": serializable}
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


def _l2_normalize(vec: List[float]) -> List[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _cosine(a: List[float], b: List[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def ensure_workbook_rag_index(db: DatabaseState, *, force: bool = False) -> None:
    """Build or refresh the workbook embedding index when the loaded file / schema changes."""
    if _disabled():
        return
    if not _embeddings_env_ready():
        logger.debug("workbook_schema_rag: embeddings env not set — skip index build")
        return
    fp = _workbook_fingerprint(db)
    idx_path = _index_path()
    rebuild = (os.getenv("SDA_WORKBOOK_RAG_REBUILD") or "").strip().lower() in ("1", "true", "yes", "on")
    existing = _load_index(idx_path)
    if not force and not rebuild and existing and existing.get("workbook_sha256") == fp:
        ch = existing.get("chunks") or []
        if ch and all(c.get("embedding") for c in ch):
            return

    chunks = _build_chunks(db)
    if not chunks:
        return
    texts = [c["text"] for c in chunks]
    try:
        vectors = embed_texts(texts)
    except Exception as e:
        logger.warning("workbook_schema_rag: embed index failed (%s)", e)
        raise
    for c, v in zip(chunks, vectors):
        c["embedding"] = v
    _save_index(idx_path, fp, chunks)
    logger.info("workbook_schema_rag: wrote %s chunks to %s", len(chunks), idx_path)


@traceable(name="SDA | workbook schema RAG retrieval", run_type="chain")
def workbook_retrieval_context(question: str, db: DatabaseState) -> str:
    """
    Embed ``question``, score workbook chunks by cosine similarity, return a prompt block.

    **No** Postgres ERD join graph — joins must be inferred from shared column names in the
    live schema + these snippets.
    """
    if _disabled() or not question.strip():
        return ""
    if not _embeddings_env_ready():
        return ""
    try:
        ensure_workbook_rag_index(db)
    except Exception:
        return ""

    data = _load_index(_index_path())
    if not data or data.get("workbook_sha256") != _workbook_fingerprint(db):
        try:
            ensure_workbook_rag_index(db, force=True)
            data = _load_index(_index_path())
        except Exception:
            return ""
    if not data:
        return ""

    chunks_in = data.get("chunks") or []
    chunks: List[Dict[str, Any]] = [c for c in chunks_in if c.get("embedding") and c.get("text")]
    if not chunks:
        return ""

    try:
        qvec = embed_texts([question[:8000]])[0]
    except Exception as e:
        logger.warning("workbook_schema_rag: query embed failed (%s)", e)
        return ""
    qn = _l2_normalize(qvec)

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for c in chunks:
        emb = c.get("embedding") or []
        if not emb:
            continue
        scored.append((_cosine(qn, _l2_normalize(emb)), c))
    scored.sort(key=lambda x: -x[0])

    top_k = int(os.getenv("SDA_WORKBOOK_RAG_TOP_K") or str(_DEFAULT_TOP_K))
    top_k = max(1, min(24, top_k))
    top_pairs = scored[:top_k]

    parts: List[str] = [
        "**RETRIEVAL (workbook-only, embedding similarity)** — snippets describe **tables actually loaded** "
        f"from `{db.file_name}`. Use them to pick tables/columns; **join keys must exist in the live schema** "
        "(e.g. shared `NPI`, `ZIP`, `Territory`, `Month`). Do not assume Postgres ERD FKs.",
    ]
    for i, (score, c) in enumerate(top_pairs, 1):
        tabs = ", ".join(c.get("tables") or []) or "(table)"
        parts.append(f"--- Snippet {i} (similarity={score:.4f}) — **{tabs}** ---\n{c.get('text', '').strip()}")

    out = "\n\n".join(parts).strip()
    if len(out) > _MAX_RETRIEVAL_OUT:
        out = out[: _MAX_RETRIEVAL_OUT - 20] + "\n... [truncated]"
    return out
