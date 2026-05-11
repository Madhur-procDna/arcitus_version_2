"""Load .env from project root, src/, and cwd (consistent across all entrypoints)."""

from __future__ import annotations

import os
from pathlib import Path


def _force_override_enabled() -> bool:
    return (os.getenv("SDA_DOTENV_FORCE_OVERRIDE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _parse_dotenv_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines; utf-8-sig strips BOM. Values are stripped, not unescaped."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def force_apply_azure_openai_from_dotenv() -> None:
    """Set Azure OpenAI-related env vars from project .env, overriding empty/wrong OS values.

    Reads ``src/.env`` first, then ``<project>/.env`` (parent of ``src/``), then ``cwd/.env``.
    For each known alias, if the file defines a non-empty value, sets both common spellings
    in ``os.environ`` so Windows case-insensitive lookups resolve correctly.
    """
    if not _force_override_enabled():
        return
    # Prefer src/.env first so it wins over a sparse or stale repo-root .env (which
    # could set PGHOST etc. and prevent later files from filling missing PGPASSWORD).
    here = Path(__file__).resolve().parent
    candidates = [here / ".env", here.parent / ".env", Path.cwd() / ".env"]
    seen: set[Path] = set()
    pairs = [
        ("AZURE_OPENAI_KEY", "azure_openai_key"),
        ("AZURE_OPENAI_ENDPOINT", "azure_openai_endpoint"),
        ("AZURE_OPENAI_CHAT_DEPLOYMENT", "chat_deployment"),
        ("AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT", "embeddings_deployment"),
        ("AZURE_OPENAI_API_VERSION", "api_version"),
    ]
    # First file in list wins per key (src before repo root) so local secrets take precedence.
    merged: dict[str, str] = {}
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        data = _parse_dotenv_file(resolved)
        for upper, lower in pairs:
            if upper in merged:
                continue
            raw = data.get(upper) or data.get(lower)
            if raw is None or not str(raw).strip():
                continue
            merged[upper] = str(raw).strip()

    for upper, lower in pairs:
        val = merged.get(upper)
        if not val:
            continue
        os.environ[upper] = val
        os.environ[lower] = val


def force_apply_redis_from_dotenv() -> None:
    """Set ``REDIS_*`` from project ``.env``, **overriding** existing OS values.

    ``load_application_dotenv()`` only fills unset keys, so a stale ``REDIS_ENABLED=false``
    in User/System env (Windows) can block ``src/.env`` from turning Redis on. This
    applies Redis settings from ``src/.env`` first, then parent ``.env``, then ``cwd/.env``.
    """
    if not _force_override_enabled():
        return
    here = Path(__file__).resolve().parent
    redis_keys = (
        "REDIS_ENABLED",
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_DB",
        "REDIS_PASSWORD",
        "REDIS_TTL_SECONDS",
        "REDIS_URL",
        "REDIS_SOCKET_CONNECT_TIMEOUT",
        "REDIS_SOCKET_TIMEOUT",
    )
    merged: dict[str, str] = {}
    seen: set[Path] = set()
    for path in (here / ".env", here.parent / ".env", Path.cwd() / ".env"):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        data = _parse_dotenv_file(resolved)
        for k in redis_keys:
            if k in merged:
                continue
            raw = data.get(k)
            if raw is None or not str(raw).strip():
                continue
            merged[k] = str(raw).strip()
    for k, v in merged.items():
        os.environ[k] = v


def force_apply() -> None:
    """Apply selected ``.env`` keys that should override process environment."""
    force_apply_azure_openai_from_dotenv()
    force_apply_redis_from_dotenv()


def load_application_dotenv() -> None:
    """Merge ``.env`` files into ``os.environ`` (only keys that are unset or blank).

    Order: ``src/.env``, parent ``DSA/.env``, ``cwd/.env`` — so ``DSA/src/.env`` wins
    over a partial repo-root file for keys defined in both.
    """
    # Prefer src/.env first so it wins over a sparse or stale repo-root .env (which
    # could set PGHOST etc. and prevent later files from filling missing PGPASSWORD).
    here = Path(__file__).resolve().parent
    candidates = [here / ".env", here.parent / ".env", Path.cwd() / ".env"]
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        try:
            with resolved.open("r", encoding="utf-8-sig") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if not key:
                        continue
                    cur = os.environ.get(key)
                    # Treat whitespace-only as unset (Windows may alias AZURE_OPENAI_KEY / azure_openai_key).
                    if cur is None or (isinstance(cur, str) and not cur.strip()):
                        os.environ[key] = value
        except OSError:
            continue
