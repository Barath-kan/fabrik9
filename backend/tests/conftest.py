"""Shared test fixtures.

Isolates the database to a throwaway SQLite file *before* app.db is imported
(the engine is built from DATABASE_URL at import time), so no test ever touches
the real ./fabrik9.db. Also enables asyncio auto-mode for the server tests.
"""

import os
import sys
import tempfile

# backend/ on sys.path so `import app...` works regardless of pytest's rootdir.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Point every session at a private temp DB. Must happen before app.db import.
_db_fd, _db_path = tempfile.mkstemp(suffix=".fabrik9-test.db")
os.close(_db_fd)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + _db_path.replace("\\", "/")

import pytest_asyncio  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def _create_tables():
    """Create schema on the temp DB once per test (create_all is idempotent)."""
    from app.db import init_models
    await init_models()
    yield


def pytest_sessionfinish(session, exitstatus):
    """Best-effort cleanup of the temp DB file."""
    try:
        os.unlink(_db_path)
    except OSError:
        pass
