from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ``Backend/`` (parent of ``src/``) — used to resolve relative workbook paths.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent

_ARCETUS_FALLBACK_NAMES: tuple[str, ...] = (
    "Arcutis Dummy Data v1.xlsx",
    "Arcutis_Dummy_Data_v1.xlsx",
    "arcutis_dummy_data.xlsx",
)


def _workbook_readable_probe(p: Path) -> bool:
    """
    True when the path looks like a workbook we can actually open (not OneDrive placeholder / lock).

    ``Path.is_file()`` can be true while ``open()`` raises ``PermissionError`` on synced folders.
    """
    try:
        if not p.is_file():
            return False
        with p.open("rb") as fh:
            head = fh.read(4)
        suf = p.suffix.lower()
        if suf == ".csv":
            return len(head) > 0
        if suf in (".xlsx", ".xls"):
            return len(head) >= 2 and head[0] == 0x50 and head[1] == 0x4B  # ZIP “PK” — .xlsx container
        return bool(head)
    except OSError:
        return False


def _desktop_sql_candidates(filename: str) -> list[Path]:
    """
    Typical locations for ad-hoc analytics workbooks (e.g. ``Desktop\\sql\\*.xlsx``),
    including company **OneDrive** desktops where ``~/Desktop`` is not the real folder.
    """
    h = Path.home()
    roots: list[Path] = [h / "Desktop", h / "OneDrive" / "Desktop"]
    try:
        for od in h.glob("OneDrive*/Desktop"):
            if od not in roots:
                roots.append(od)
    except OSError:
        pass
    out: list[Path] = []
    for root in roots:
        out.append((root / "sql" / filename).resolve())
    return out


def iter_workbook_candidate_paths(raw: str) -> list[Path]:
    """Ordered search list for the Arcetus / analytics workbook (deduplicated)."""
    raw_p = Path(raw).expanduser()
    candidates: list[Path] = []
    candidates.append(raw_p)
    if not raw_p.is_absolute():
        candidates.append((_BACKEND_ROOT / raw_p).resolve())
    candidates.append((_BACKEND_ROOT / "data" / raw_p.name).resolve())
    for name in _ARCETUS_FALLBACK_NAMES:
        if name.lower() != raw_p.name.lower():
            candidates.append((_BACKEND_ROOT / "data" / name).resolve())
    names_to_try = {raw_p.name, *_ARCETUS_FALLBACK_NAMES}
    for fn in names_to_try:
        candidates.extend(_desktop_sql_candidates(fn))
    seen: set[str] = set()
    out: list[Path] = []
    for cand in candidates:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out


def resolve_workbook_path(raw: str) -> Path:
    """
    Resolve ``DATA_FILE_PATH`` / ``data_file_path`` to a workbook path the process can **open**.

    Prefers the first candidate that passes a short read probe (avoids OneDrive-locked ``Backend\\src\\*.xlsx``
    when an identical copy exists under ``Desktop\\sql\\`` or ``Backend\\data\\``).
    """
    raw_p = Path(raw).expanduser()
    for cand in iter_workbook_candidate_paths(raw):
        try:
            if _workbook_readable_probe(cand):
                return cand.resolve()
        except OSError:
            continue
    # Nothing readable — return first existing path for a clear error from ``load_file``, else default.
    for cand in iter_workbook_candidate_paths(raw):
        try:
            if cand.exists():
                return cand.resolve()
        except OSError:
            continue
    if raw_p.is_absolute():
        return raw_p.resolve()
    return (_BACKEND_ROOT / "data" / raw_p.name).resolve()


class Settings(BaseSettings):
    # Azure OpenAI — no secrets in code; same keys as ``src/.env`` and ``env_loader.force_apply_azure_openai_from_dotenv``.
    azure_openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AZURE_OPENAI_KEY",
            "AZURE_OPENAI_API_KEY",
            "azure_openai_key",
        ),
    )
    azure_openai_endpoint: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AZURE_OPENAI_ENDPOINT",
            "azure_openai_endpoint",
        ),
    )
    azure_openai_deployment: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AZURE_OPENAI_CHAT_DEPLOYMENT",
            "AZURE_OPENAI_DEPLOYMENT",
            "chat_deployment",
            "azure_openai_deployment",
        ),
    )
    azure_openai_api_version: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AZURE_OPENAI_API_VERSION",
            "api_version",
            "azure_openai_api_version",
        ),
    )
    # Set ``OPENAI_MAX_TOKENS`` in ``.env`` (SQL agent / long answers).
    openai_max_tokens: int = 1500

    # Workbook for SQLite mode (``sda_data_source=sqlite``). Override with env ``DATA_FILE_PATH``.
    # Relative paths are resolved against ``Backend/`` and ``Backend/data/`` (see ``resolve_workbook_path``).
    data_file_path: str = r"C:\Users\MadhurGauri\OneDrive - ProcDNA Analytics Pvt. Ltd\Desktop\arcetus\Backend\data\Arcutis Dummy Data v1.xlsx"

    # sqlite | postgres — sqlite loads ``data_file_path`` into in-memory SQLite (see ``data_loader``).
    sda_data_source: str = "sqlite"

    sql_validation_enabled: bool = True
    query_row_limit: int = 500

    # Set ``HISTORY_CHAR_BUDGET``, ``HISTORY_MIN_PAIRS``, ``HISTORY_MAX_ASSISTANT_CHARS`` in ``.env``.
    history_char_budget: int = 12_000
    history_min_pairs: int = 3
    history_max_assistant_chars: int = 600

    app_title: str = "Data Query API"
    app_version: str = "1.0.0"
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8501",
    ]
    fastapi_base_url: str = "http://localhost:8000"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @model_validator(mode="after")
    def _normalize_data_file_path(self) -> "Settings":
        p = resolve_workbook_path(self.data_file_path)
        self.data_file_path = str(p)
        return self


settings = Settings()
