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


async def migrate_position_unique_key() -> None:
    """
    One-time migration: change positions table unique constraint from
    condition_id alone to (condition_id, outcome).
    Idempotent — only runs if the old single-column unique index exists.
    """
    import logging
    import sqlalchemy

    _logger = logging.getLogger(__name__)

    async with engine.begin() as conn:
        result = await conn.execute(sqlalchemy.text(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='positions' AND sql LIKE '%condition_id%'"
        ))
        rows = result.fetchall()
        has_old_unique = any(
            r[0] and "unique" in r[0].lower() and "outcome" not in r[0].lower()
            for r in rows
        )
        if not has_old_unique:
            return  # already migrated or fresh install

        _logger.info("Migrating positions table: unique(condition_id) → unique(condition_id, outcome)")

        await conn.execute(sqlalchemy.text("""
            CREATE TABLE positions_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT NOT NULL,
                market_title TEXT NOT NULL DEFAULT '',
                token_id    TEXT NOT NULL DEFAULT '',
                outcome     TEXT NOT NULL DEFAULT '',
                size        REAL NOT NULL DEFAULT 0.0,
                avg_entry_price REAL NOT NULL DEFAULT 0.0,
                total_cost  REAL NOT NULL DEFAULT 0.0,
                current_price REAL,
                current_value REAL,
                unrealized_pnl REAL,
                unrealized_pnl_pct REAL,
                realized_pnl REAL NOT NULL DEFAULT 0.0,
                market_resolved INTEGER NOT NULL DEFAULT 0,
                redeemable  INTEGER NOT NULL DEFAULT 0,
                market_end_date TEXT,
                opened_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                is_paper    INTEGER NOT NULL DEFAULT 1,
                UNIQUE(condition_id, outcome)
            )
        """))
        await conn.execute(sqlalchemy.text(
            "INSERT OR IGNORE INTO positions_new "
            "SELECT id, condition_id, market_title, token_id, outcome, size, "
            "avg_entry_price, total_cost, current_price, current_value, "
            "unrealized_pnl, unrealized_pnl_pct, realized_pnl, "
            "market_resolved, redeemable, market_end_date, "
            "opened_at, updated_at, is_paper FROM positions"
        ))
        await conn.execute(sqlalchemy.text("DROP TABLE positions"))
        await conn.execute(sqlalchemy.text("ALTER TABLE positions_new RENAME TO positions"))
        _logger.info("Position table migration complete")


async def migrate_add_columns() -> None:
    """
    Idempotently add columns introduced after the initial schema creation.
    Safe to run on both fresh installs and existing databases.
    """
    import sqlalchemy

    stmts = [
        "ALTER TABLE bot_settings ADD COLUMN paper_balance_usdc REAL NOT NULL DEFAULT 1000.0",
        "ALTER TABLE bot_settings ADD COLUMN royalty_pct REAL NOT NULL DEFAULT 1.0",
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
