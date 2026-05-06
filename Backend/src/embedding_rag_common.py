"""Azure OpenAI embeddings + shared RAG index paths.

Used by ``schema_rag`` (Postgres ERD chunks) and ``workbook_schema_rag`` (Excel→SQLite)
without importing ``pharma_schema`` — so Excel-only stacks do not load ERD metadata.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import List

from env_loader import force_apply_azure_openai_from_dotenv, load_application_dotenv
from langsmith_config import traceable

_EMBED_BATCH = 16


def rag_data_dir() -> Path:
    raw = (os.getenv("SCHEMA_RAG_DATA_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent / "data"


def _get_env(*keys: str) -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _embeddings_env_ready() -> bool:
    """True when Azure OpenAI embeddings can be called (index build + query embed)."""
    load_application_dotenv()
    force_apply_azure_openai_from_dotenv()
    return bool(
        _get_env("AZURE_OPENAI_KEY", "azure_openai_key")
        and _get_env("AZURE_OPENAI_ENDPOINT", "azure_openai_endpoint")
        and _get_env("AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT", "embeddings_deployment")
    )


def _embed_timeout_sec() -> float:
    raw = (os.getenv("AZURE_OPENAI_HTTP_TIMEOUT_SEC") or os.getenv("AZURE_OPENAI_HTTP_TIMEOUT") or "").strip()
    if not raw:
        return 120.0
    try:
        return max(15.0, float(raw))
    except ValueError:
        return 120.0


def _azure_embeddings_raw(inputs: List[str]) -> List[List[float]]:
    """Call Azure OpenAI embeddings deployment. ``inputs`` length should respect batch limits."""
    load_application_dotenv()
    force_apply_azure_openai_from_dotenv()
    api_key = _get_env("AZURE_OPENAI_KEY", "azure_openai_key")
    endpoint = _get_env("AZURE_OPENAI_ENDPOINT", "azure_openai_endpoint")
    deployment = _get_env("AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT", "embeddings_deployment")
    api_version = _get_env("AZURE_OPENAI_API_VERSION", "api_version") or "2024-08-01-preview"
    if not api_key or not endpoint or not deployment:
        raise RuntimeError(
            "Azure embeddings env missing (need AZURE_OPENAI_KEY, AZURE_OPENAI_ENDPOINT, "
            "AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT)."
        )
    endpoint = endpoint.rstrip("/")
    url = f"{endpoint}/openai/deployments/{deployment}/embeddings?api-version={api_version}"
    payload = {"input": inputs}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_embed_timeout_sec()) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Embeddings HTTP {e.code}: {err[:500]}") from e
    out_data = list(body.get("data") or [])
    out_data.sort(key=lambda x: int(x.get("index", 0)))
    vecs = [item["embedding"] for item in out_data if "embedding" in item]
    if len(vecs) != len(inputs):
        raise RuntimeError(f"Embeddings API returned {len(vecs)} vectors for {len(inputs)} inputs.")
    return vecs


@traceable(name="SDA | Azure OpenAI embeddings (batched)", run_type="tool")
def embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed many strings (batched)."""
    all_vecs: List[List[float]] = []
    batch: List[str] = []
    for t in texts:
        batch.append(t[:8000] if t else "")
        if len(batch) >= _EMBED_BATCH:
            all_vecs.extend(_azure_embeddings_raw(batch))
            batch = []
    if batch:
        all_vecs.extend(_azure_embeddings_raw(batch))
    return all_vecs
