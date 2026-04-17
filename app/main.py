"""
FastAPI application entry point.

Handles:
  - App lifespan (DB init, Polymarket client init, scheduler start/stop)
  - Static files and Jinja2 template engine
  - Router registration
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.database import init_db, migrate_add_columns
from app.log_buffer import setup_logging

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    cfg = get_settings()
    setup_logging(level=cfg.log_level, log_dir=cfg.log_dir)
    logger.info("=== Polymarket Copy Bot starting ===")

    # 1. Initialise database
    await init_db()
    await migrate_add_columns()
    logger.info("Database ready")

    # 2. Seed default BotSettings row (id=1) if not present
    await _seed_settings()

    # 3. Initialise Polymarket client — reads credentials from DB
    from app.services.polymarket_client import init_poly_client
    from app.database import get_session
    from app.models.settings import BotSettings
    from sqlmodel import select

    async with get_session() as session:
        result = await session.exec(select(BotSettings).where(BotSettings.id == 1))
        bot_cfg = result.first()

    poly_client = await init_poly_client(cfg, bot_settings=bot_cfg)
    app.state.poly_client = poly_client

    # Expose bot_name as a Jinja2 global so every template can use {{ bot_name }}
    app.state.templates.env.globals["bot_name"] = (
        bot_cfg.bot_name if bot_cfg else "PM Copy"
    )

    # 4. Start background scheduler
    from app.scheduler import start_scheduler

    poll_interval = bot_cfg.poll_interval_seconds if bot_cfg else 10

    scheduler = start_scheduler(poll_interval=poll_interval)
    app.state.scheduler = scheduler
    logger.info("Scheduler started")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    from app.scheduler import stop_scheduler
    stop_scheduler()
    logger.info("=== Polymarket Copy Bot stopped ===")


async def _seed_settings() -> None:
    """Insert default BotSettings row if the table is empty."""
    from app.database import get_session
    from app.models.settings import BotSettings
    from sqlmodel import select

    async with get_session() as session:
        result = await session.exec(select(BotSettings).where(BotSettings.id == 1))
        if result.first() is None:
            session.add(BotSettings(id=1))
            await session.commit()
            logger.info("Default BotSettings seeded")


# ─────────────────────────────────────────────────────────────────────────────
# App factory
# ─────────────────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="Polymarket Copy Bot",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )

    # ── Templates ──────────────────────────────────────────────────────────────
    templates_dir = os.path.join(os.path.dirname(__file__), "templates")
    app.state.templates = Jinja2Templates(directory=templates_dir)

    # ── Routers ───────────────────────────────────────────────────────────────
    from app.routers import dashboard, health, logs, settings, trades

    app.include_router(health.router)
    app.include_router(dashboard.router)
    app.include_router(settings.router)
    app.include_router(trades.router)
    app.include_router(logs.router)

    return app


app = create_app()
