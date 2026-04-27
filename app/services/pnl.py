"""
PNL calculation engine.

Refreshes unrealized PNL for all open positions by fetching current
mid-point prices from the CLOB API, then writes the results back to
the Position table.

When a market's price is unavailable (CLOB returns None), the Gamma API
is queried to check for resolution. Resolved positions are auto-settled:
  - Winning outcome: resolution_price = 1.0  → PNL = size - cost
  - Losing outcome:  resolution_price = 0.0  → PNL = -cost
A synthetic SELL trade is created for each settled position.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.position import Position
from app.services.polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)


async def _settle_resolved_position(
    session: AsyncSession,
    pos: Position,
    resolution_price: float,
) -> None:
    """
    Close a position whose market has resolved on-chain.
    Creates a settlement Trade record and zeros out the Position.
    resolution_price = 1.0 for the winning outcome, 0.0 for the losing one.
    """
    import time

    from app.models.trade import Trade, TradeSide, TradeStatus

    proceeds      = pos.size * resolution_price
    realized      = round(proceeds - pos.total_cost, 6)
    outcome_label = "WON" if resolution_price == 1.0 else "LOST"

    logger.info(
        "Market resolved — %s %s %.4f shares @ %.2f | PNL %.4f | %s",
        outcome_label, pos.outcome, pos.size, resolution_price, realized, pos.market_title,
    )

    session.add(Trade(
        source_timestamp  = time.time(),
        condition_id      = pos.condition_id,
        market_title      = pos.market_title,
        token_id          = pos.token_id,
        outcome           = pos.outcome,
        side              = TradeSide.SELL,
        size              = pos.size,
        price             = resolution_price,
        usdc_amount       = round(proceeds, 6),
        status            = TradeStatus.PAPER if pos.is_paper else TradeStatus.FILLED,
        realized_pnl      = realized,
        is_paper          = pos.is_paper,
    ))

    pos.realized_pnl      += realized
    pos.current_price      = resolution_price
    pos.current_value      = 0.0
    pos.unrealized_pnl     = 0.0
    pos.unrealized_pnl_pct = 0.0
    pos.size               = 0.0
    pos.total_cost         = 0.0
    pos.market_resolved    = True
    pos.updated_at         = datetime.utcnow()
    session.add(pos)


async def refresh_unrealized_pnl(
    session: AsyncSession,
    poly_client: PolymarketClient,
) -> dict[str, Any]:
    """
    For every open Position (size > 0), fetch the current mid-price and
    update current_value / unrealized_pnl / unrealized_pnl_pct.

    When get_midpoint_price() returns None (market no longer on the CLOB),
    the Gamma API is checked for resolution. If resolved, the position is
    auto-settled via _settle_resolved_position().

    Returns a summary dict with aggregate stats for the dashboard.
    """
    result = await session.exec(select(Position).where(Position.size > 0))
    positions = result.all()

    total_unrealized  = 0.0
    total_realized    = 0.0
    total_cost_basis  = 0.0
    total_value       = 0.0

    async def fetch_and_update(pos: Position) -> None:
        nonlocal total_unrealized, total_realized, total_cost_basis, total_value

        price = await poly_client.get_midpoint_price(pos.token_id)

        # ── Time-based expiry check ───────────────────────────────────────
        now_utc = datetime.now(timezone.utc)
        past_end_date = False
        if pos.market_end_date:
            try:
                end_dt = datetime.fromisoformat(pos.market_end_date.replace("Z", "+00:00"))
                past_end_date = now_utc > end_dt
            except ValueError:
                pass

        # Call Gamma when: no CLOB price, or past the market's scheduled end date
        if price is None or past_end_date:
            try:
                market = await poly_client.get_market_by_condition_id(pos.condition_id)
                if market:
                    # Opportunistically store end_date (one-time, prevents future calls)
                    if pos.market_end_date is None:
                        end_iso = market.get("endDateIso") or market.get("endDate")
                        if end_iso:
                            pos.market_end_date = end_iso
                            try:
                                end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                                past_end_date = now_utc > end_dt
                            except ValueError:
                                pass

                    # Only settle if we believe the market has actually closed
                    if (price is None or past_end_date) and market.get("resolved"):
                        tokens = market.get("tokens") or []
                        winning_id = next(
                            (
                                t.get("token_id") or t.get("tokenId")
                                for t in tokens
                                if t.get("winner")
                            ),
                            None,
                        )
                        resolution_price = (
                            1.0 if (winning_id and pos.token_id == winning_id) else 0.0
                        )
                        await _settle_resolved_position(session, pos, resolution_price)
                        total_realized += pos.realized_pnl
                        return
            except Exception as exc:
                logger.warning("Resolution check failed for %s: %s", pos.condition_id, exc)

            # Not resolved yet or check failed — keep last known price
            if price is None:
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

    # Lazily populate market_end_date for one position per cycle (sequential,
    # never concurrent) so the time-based resolution check can eventually fire.
    missing_end_dates = [p for p in positions if p.market_end_date is None]
    if missing_end_dates:
        candidate = missing_end_dates[0]
        try:
            market_info = await poly_client.get_market_by_condition_id(candidate.condition_id)
            if market_info:
                end_iso = market_info.get("endDateIso") or market_info.get("endDate")
                if end_iso:
                    candidate.market_end_date = end_iso
                    session.add(candidate)
                    logger.debug("Stored end_date=%s for %s", end_iso, candidate.condition_id)
        except Exception as exc:
            logger.warning("Could not fetch end_date for %s: %s", candidate.condition_id, exc)

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
    }
