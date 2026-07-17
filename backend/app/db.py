"""Async database setup. SQLite by default (zero-config dev);
set DATABASE_URL=postgresql+asyncpg://user:pass@host/db for production."""

import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./fabrik9.db")

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_models():
    from . import models
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
