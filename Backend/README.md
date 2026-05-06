# Data Structured Agent (SDA)
Natural-language **question → SQL → Postgres → answer** pipeline with **Sofie ERD**–grounded text-to-SQL (`src/ERD.md`), **embedding similarity** over the ERD for table/join hints (`schema_rag.py`), optional **Redis** caching, **LangSmith** tracing, and **Azure OpenAI** for generation + embeddings.
## Features
- **Text-to-SQL** uses **`src/ERD.md`** plus **RAG**: each question is embedded; top similar ERD sections and FK join paths are passed to the model as **RETRIEVAL** (`schema_rag.py`, `text_to_sql_prompt.py`).
- **Postgres** execution with timeouts, row caps, and retries on transient connection errors (`src/postgres_runner.py`).
- **SQL safety** via SQLGlot: read-only checks, incomplete/truncated SQL guards, tokenizer errors handled cleanly (`src/sql_validate.py`).
- **Redis** (optional): end-to-end QA cache; time-relative questions skip cache by default (`src/redis_cache.py`).
- **Conversation context** for follow-ups, with optional Redis-backed history (`src/conversation_context.py`).
- **Retries** for Azure HTTP, Postgres, and Redis cache I/O (`src/retry_utils.py`).
- **LangSmith** tracing when `LANGCHAIN_API_KEY` / project are set (`src/langsmith_config.py`).
## Requirements
- Python 3.10+
- PostgreSQL reachable with credentials below
- Azure OpenAI deployments for **chat** (text-to-SQL + summarization) and **embeddings** (schema RAG; optional if `SDA_DISABLE_SCHEMA_RAG=1`)
## Setup
```bash
cd SDA
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
```
Copy environment template and fill in secrets (never commit `.env`):
```bash
copy .env.example .env          # Windows
# cp .env.example .env          # Unix
```
After **ERD** changes, edit `src/ERD.md` (or `SDA_ERD_PATH` / `ERD_PATH`). The **schema RAG** index rebuilds automatically when the ERD file hash changes (or set `SDA_SCHEMA_RAG_REBUILD=1` once). Manual rebuild:

```bash
cd src
python -c "from schema_rag import rebuild_schema_rag_index_cli; rebuild_schema_rag_index_cli()"
```

Tables in a non-`public` schema: set `PGSCHEMA=new_schema` or `SDA_PHARMA_SCHEMA=new_schema` (legacy: `SDA_TAKEDA_SCHEMA`) in `.env` so qualified names match your database.
## Environment variables
| Area | Variables |
|------|-----------|
| **Azure OpenAI** | `AZURE_OPENAI_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_CHAT_DEPLOYMENT`; `AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT` for schema RAG; `AZURE_OPENAI_API_VERSION` |
| **Schema RAG** | `SDA_DISABLE_SCHEMA_RAG`, `SDA_SCHEMA_RAG_TOP_K`, `SCHEMA_RAG_DATA_DIR`, `SDA_SCHEMA_RAG_REBUILD`, `SDA_SCHEMA_RAG_MAX_CHARS`, `SDA_SCHEMA_RAG_PROMPT_CAP` |
| **Postgres** | `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`; optional `PGPORT`, `PGSCHEMA` (or `SDA_PHARMA_SCHEMA` / `SDA_TAKEDA_SCHEMA` for qualified table names in RAG) |
| **ERD** | `SDA_ERD_PATH` or `ERD_PATH` — markdown file used as schema ground truth (default `src/ERD.md`) |
| **Redis** (optional) | `REDIS_HOST`, optional `REDIS_PORT`, `REDIS_DB`, `REDIS_PASSWORD`, `REDIS_ENABLED`, `REDIS_TTL_SECONDS` |
| **LangSmith** (optional) | `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT` |
| **SQL generation** | `TEXT_TO_SQL_MAX_TOKENS` (default `2048` for long CTE queries) |
| **Retries** | `RETRY_MAX_ATTEMPTS`, `RETRY_BACKOFF_BASE`, `RETRY_MAX_WAIT` |
Lowercase aliases (e.g. `azure_openai_key`) are supported in `.env` on Windows.
## Run
From the **`src`** directory:

```bash
cd src
python qa_pipeline.py
```
Enter questions at the prompt; an **empty line** exits. Multi-turn follow-ups use in-session (and optionally Redis) context.

### HTTP API (Next.js / frontend)

The FastAPI app in `src/api_server.py` exposes the same pipeline for the UI:

```bash
cd src
pip install -r ../requirements.txt   # includes fastapi + uvicorn
uvicorn api_server:app --reload --host 127.0.0.1 --port 8000
```

Windows (from `Backend/`):

```powershell
.\run_api.ps1
```

- **POST** `/query?question=...&session_id=...` — returns JSON `{ success, response, error?, sql?, row_count? }`.
- **GET** `/health` — liveness check.
- **CORS**: defaults to `http://localhost:3000` and `http://127.0.0.1:3000`; override with env `CORS_ALLOW_ORIGINS` (comma-separated).

Each `session_id` gets its own `ConversationBuffer` with Redis key `sda:api:session:{session_id}:turns` when Redis is enabled.

## Project layout
```
SDA/
├── .env.example          # Template only — copy to .env
├── .gitignore
├── requirements.txt
├── questions.txt         # Example prompts (not loaded by the app)
└── src/
    ├── ERD.md            # Schema ground truth for NL→SQL
    ├── qa_pipeline.py    # Entry point
    ├── api_server.py     # FastAPI HTTP API
    ├── text_to_sql_prompt.py
    ├── sql_validate.py
    ├── postgres_runner.py
    ├── redis_cache.py
    ├── redis_config.py
    ├── retry_utils.py
    ├── conversation_context.py
    ├── pharma_schema.py
    ├── schema_rag.py
    ├── env_loader.py
    └── langsmith_config.py
```
## Security
- Do **not** commit `.env` or real credentials.
- Rotate keys if they may have been exposed.

