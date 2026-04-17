"""
Async SQLite database engine and session factory (SQLModel / SQLAlchemy).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import get_settings

settings = get_settings()

# StaticPool keeps a single connection — fine for SQLite in a single process.
# WAL journal mode is set at connection time for better concurrency.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


async def init_db() -> None:
    """Create all tables (idempotent). Called on app startup."""
    # Enable WAL mode for better read/write concurrency
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await conn.execute(
            __import__("sqlalchemy").text("PRAGMA journal_mode=WAL")
        )


async def migrate_add_columns() -> None:
    """
    Idempotently add columns introduced after the initial schema creation.
    Safe to run on both fresh installs and existing databases.
    """
    import sqlalchemy

    stmts = [
        "ALTER TABLE bot_settings ADD COLUMN paper_balance_usdc REAL NOT NULL DEFAULT 1000.0",
    ]
    async with engine.begin() as conn:
        for stmt in stmts:
            try:
                await conn.execute(sqlalchemy.text(stmt))
            except Exception:
                pass  # column already exists — ignore


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context-manager session for use outside of FastAPI DI."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a session and auto-commits on success."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
