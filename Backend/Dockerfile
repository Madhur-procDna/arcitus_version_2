# ──────────────────────────────────────────────────────────────
# Backend Dockerfile — FastAPI + Uvicorn (Python 3.11)
# ──────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Install system dependencies required by psycopg2-binary and general build tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpq-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/

# The API server runs from the src/ directory
WORKDIR /app/src

EXPOSE 8001

# Env vars are injected by docker-compose (env_file or environment block).
# Uvicorn binds to 0.0.0.0 so other containers on the Docker network can reach it.
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8001"]
