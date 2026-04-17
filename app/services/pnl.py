"""
PNL calculation engine.

Refreshes unrealized PNL for all open positions by fetching current
mid-point prices from the CLOB API, then writes the results back to
the Position table.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.position import Position
from app.services.polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)


async def refresh_unrealized_pnl(
    session: AsyncSession,
    poly_client: PolymarketClient,
) -> dict[str, Any]:
    """
    For every open Position (size > 0), fetch the current mid-price and
    update current_value / unrealized_pnl / unrealized_pnl_pct.

    Returns a summary dict with aggregate stats for the dashboard.
    """
    result = await session.exec(select(Position).where(Position.size > 0))
    positions = result.all()

    total_unrealized  = 0.0
    total_realized    = 0.0
    total_cost_basis  = 0.0
    total_value       = 0.0

    # Fetch all prices concurrently
    async def fetch_and_update(pos: Position) -> None:
        nonlocal total_unrealized, total_realized, total_cost_basis, total_value

        price = await poly_client.get_midpoint_price(pos.token_id)
        if price is None:
            # Fall back to last known price or entry price
            price = pos.current_price or pos.avg_entry_price

        cur_value      = pos.size * price
        unrealized     = cur_value - pos.total_cost
        unrealized_pct = (unrealized / pos.total_cost * 100) if pos.total_cost else 0.0

        pos.current_price      = price
        pos.current_value      = cur_value
        pos.unrealized_pnl     = unrealized
        pos.unrealized_pnl_pct = unrealized_pct
        pos.updated_at         = datetime.utcnow()
        session.add(pos)

        total_unrealized  += unrealized
        total_realized    += pos.realized_pnl
        total_cost_basis  += pos.total_cost
        total_value       += cur_value

    await asyncio.gather(*[fetch_and_update(p) for p in positions])
    await session.commit()

    return {
        "total_unrealized_pnl": round(total_unrealized, 4),
        "total_realized_pnl":   round(total_realized,   4),
        "total_pnl":            round(total_unrealized + total_realized, 4),
        "total_cost_basis":     round(total_cost_basis, 4),
        "total_current_value":  round(total_value, 4),
        "open_positions":       len(positions),
    }


async def get_portfolio_summary(session: AsyncSession) -> dict[str, Any]:
    """
    Return aggregate PNL stats directly from DB (no price refresh).
    Used by the dashboard for fast reads between refresh cycles.
    """
    result = await session.exec(select(Position))
    all_positions = result.all()

    open_pos   = [p for p in all_positions if p.size > 0]
    closed_pos = [p for p in all_positions if p.size <= 0]

    total_unrealized  = sum(p.unrealized_pnl or 0.0 for p in open_pos)
    total_realized    = sum(p.realized_pnl        for p in all_positions)
    total_exposure    = sum(p.total_cost           for p in open_pos)
    total_value       = sum(p.current_value or 0.0 for p in open_pos)

    return {
        "total_unrealized_pnl": round(total_unrealized, 4),
        "total_realized_pnl":   round(total_realized,   4),
        "total_pnl":            round(total_unrealized + total_realized, 4),
        "total_exposure_usdc":  round(total_exposure,   4),
        "total_current_value":  round(total_value,      4),
        "open_position_count":  len(open_pos),
        "closed_position_count": len(closed_pos),
    }
