# FABRIK-9 — single-process image.
#
# The whole app is ONE FastAPI process: it serves the REST API, the WebSocket
# tick stream, AND the static frontend (mounted at "/" in app/main.py). So this
# is deliberately a single container, not a web/worker/static split — there is
# only one thing to run.
#
# main.py resolves the frontend as ../../frontend relative to backend/app/, so
# the image preserves the repo's {backend/, frontend/} sibling layout under
# /app and runs uvicorn from /app/backend.

FROM python:3.12-slim

# Flush logs immediately (the app logs to stderr) and skip .pyc writes.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Runtime deps only. requirements.txt already pulls aiosqlite (default) and
# asyncpg (used only when DATABASE_URL points at PostgreSQL). All wheels are
# prebuilt for cp312 manylinux, so no compiler/build tools are needed.
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt

# App code, preserving the {backend, frontend} sibling layout main.py expects.
WORKDIR /app
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Non-root runtime. /data holds the default SQLite file and is owned by the app
# user so a fresh named volume mounted there inherits writable ownership.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

# Default to a SQLite file on the persistent /data volume. Override DATABASE_URL
# (e.g. postgresql+asyncpg://...) to point at another database. Tables are
# created automatically on startup (main.py lifespan -> init_models), so an
# empty database needs no manual migration step.
ENV DATABASE_URL=sqlite+aiosqlite:////data/fabrik9.db

WORKDIR /app/backend
EXPOSE 8000

# Liveness against the DB-independent health endpoint (uses only in-memory
# state), via stdlib so the slim image needs no curl/wget.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
