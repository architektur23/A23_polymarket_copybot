"""
Dashboard routes.

GET /          → full dashboard page
GET /partials/positions  → HTMX partial (positions table)
GET /partials/pnl        → HTMX partial (PNL summary cards)
GET /partials/status     → HTMX partial (bot status bar)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_db
from app.models.position import Position
from app.models.settings import BotSettings
from app.models.trade import Trade
from app.services.pnl import get_portfolio_summary

router = APIRouter(tags=["dashboard"])


def _templates(request: Request):
    return request.app.state.templates


# ─────────────────────────────────────────────────────────────────────────────
# Full page
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    settings = await _get_settings(session)
    summary  = await get_portfolio_summary(session)
    balance  = await _get_balance(request)
    return _templates(request).TemplateResponse(
        "dashboard.html",
        {
            "request":  request,
            "settings": settings,
            "summary":  summary,
            "balance":  balance,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# HTMX partials (polled every 3 s by the frontend)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/partials/positions", response_class=HTMLResponse)
async def partial_positions(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    result    = await session.exec(
        select(Position)
        .where(Position.size > 0)
        .order_by(Position.updated_at.desc())  # type: ignore[arg-type]
    )
    positions = result.all()
    return _templates(request).TemplateResponse(
        "partials/positions_table.html",
        {"request": request, "positions": positions},
    )


@router.get("/partials/pnl", response_class=HTMLResponse)
async def partial_pnl(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    summary = await get_portfolio_summary(session)
    balance = await _get_balance(request)
    return _templates(request).TemplateResponse(
        "partials/pnl_summary.html",
        {"request": request, "summary": summary, "balance": balance},
    )


@router.get("/partials/status", response_class=HTMLResponse)
async def partial_status(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    settings = await _get_settings(session)
    return _templates(request).TemplateResponse(
        "partials/status_bar.html",
        {"request": request, "settings": settings},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _get_settings(session: AsyncSession) -> BotSettings:
    result = await session.exec(select(BotSettings).where(BotSettings.id == 1))
    s = result.first()
    if s is None:
        s = BotSettings(id=1)
    return s


async def _get_balance(request: Request) -> float:
    try:
        from app.services.polymarket_client import get_poly_client
        return await get_poly_client().get_usdc_balance()
    except Exception:
        return 0.0
