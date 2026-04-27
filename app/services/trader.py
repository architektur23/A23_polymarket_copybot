"""
Copy-trade execution engine.

For each new trade detected on the target wallet:
  1. Validate against risk limits and blacklist.
  2. Calculate our scaled position size.
  3. Paper mode  → save a PAPER trade record, no order sent.
     Live mode   → place order via PolymarketClient, save result.
  4. Update Position record (create or update).
  5. Fire webhook notification.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.position import Position
from app.models.settings import BotSettings, SizingMode
from app.models.trade import Trade, TradeSide, TradeStatus
from app.services import notifier
from app.services.polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Size calculation
# ─────────────────────────────────────────────────────────────────────────────

def calculate_copy_size(
    source_usdc: float,
    source_price: float,
    settings: BotSettings,
    our_balance_usdc: float,
    source_equity_usdc: float = 0.0,
) -> float:
    """
    Determine how many USDC to deploy (then convert to shares).

    PROPORTIONAL mode:
        Detects what fraction of the source wallet's equity the trade used,
        then applies that same fraction to our equity.
        Example: source has $1 000 equity, trades $20 → 2%.
                 We have $500 equity → we trade $10.
        Falls back to min_trade_usdc if source equity is unknown.

    FIXED mode:
        Always trade a flat USDC amount regardless of source size.

    Returns size in shares (outcome tokens), or 0.0 if skipped.
    """
    if source_price <= 0:
        return 0.0

    if settings.sizing_mode == SizingMode.PROPORTIONAL:
        if source_equity_usdc > 0:
            ratio       = source_usdc / source_equity_usdc   # e.g. 0.02 = 2%
            target_usdc = ratio * our_balance_usdc
            if target_usdc > our_balance_usdc:
                # ratio > 100% — source equity is underestimated (e.g. active trader
                # with mostly resolved short-term positions). Fall back to minimum.
                logger.warning(
                    "Proportional ratio %.0f%% exceeds 100%% — source equity likely "
                    "underestimated (equity=%.2f, trade=%.2f). Using min_trade_usdc.",
                    ratio * 100, source_equity_usdc, source_usdc,
                )
                target_usdc = settings.min_trade_usdc
            else:
                target_usdc = max(target_usdc, settings.min_trade_usdc)
        else:
            # Source equity unknown — fall back to minimum
            logger.warning("Source equity unknown, using min_trade_usdc as fallback")
            target_usdc = settings.min_trade_usdc
    else:  # FIXED
        target_usdc = settings.fixed_trade_usdc

    # Per-trade hard cap
    if settings.max_position_usdc > 0:
        target_usdc = min(target_usdc, settings.max_position_usdc)

    return round(target_usdc / source_price, 4)


# ─────────────────────────────────────────────────────────────────────────────
# Position upsert helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _upsert_position(
    session: AsyncSession,
    condition_id: str,
    token_id: str,
    market_title: str,
    outcome: str,
    side: str,
    size: float,
    price: float,
    is_paper: bool,
) -> float | None:
    """Create or update the Position row for this market.

    Returns the realized PNL for this trade if it was a SELL, else None.
    """
    from sqlmodel import select

    result = await session.exec(
        select(Position).where(
            Position.condition_id == condition_id,
            Position.outcome == outcome,
        )
    )
    pos = result.first()

    if pos is None:
        pos = Position(
            condition_id=condition_id,
            market_title=market_title,
            token_id=token_id,
            outcome=outcome,
            size=0.0,
            avg_entry_price=0.0,
            total_cost=0.0,
            is_paper=is_paper,
        )
        session.add(pos)

    trade_realized_pnl: float | None = None

    if side.upper() == "BUY":
        new_cost  = pos.total_cost + (size * price)
        new_size  = pos.size + size
        pos.avg_entry_price = new_cost / new_size if new_size > 0 else price
        pos.total_cost      = new_cost
        pos.size            = new_size
    else:  # SELL
        # Reduce position; credit proportional cost basis
        sell_fraction      = size / pos.size if pos.size > 0 else 1.0
        cost_released      = pos.total_cost * sell_fraction
        proceeds           = size * price
        trade_realized_pnl = proceeds - cost_released
        pos.realized_pnl  += trade_realized_pnl
        pos.total_cost    -= cost_released
        pos.size           = max(0.0, pos.size - size)
        if pos.size < 0.001:
            pos.size       = 0.0
            pos.total_cost = 0.0

    pos.updated_at = datetime.utcnow()
    session.add(pos)
    return trade_realized_pnl


# ─────────────────────────────────────────────────────────────────────────────
# Main copy-trade function
# ─────────────────────────────────────────────────────────────────────────────

async def copy_trade(
    raw_trade: dict[str, Any],
    settings: BotSettings,
    poly_client: PolymarketClient,
    session: AsyncSession,
) -> Trade | None:
    """
    Process one raw trade dict from the Data API and mirror it.

    Returns the saved Trade object, or None if the trade was skipped.
    """
    condition_id = raw_trade.get("conditionId", "")
    token_id     = raw_trade.get("asset", "")
    side         = raw_trade.get("side", "BUY").upper()
    source_size  = float(raw_trade.get("size", 0))
    source_price = float(raw_trade.get("price", 0))
    market_title = raw_trade.get("title", "Unknown Market")
    outcome      = raw_trade.get("outcome", "")
    source_ts    = float(raw_trade.get("timestamp", 0))
    tx_hash      = raw_trade.get("transactionHash", "")

    # ── Guards ────────────────────────────────────────────────────────────────
    if condition_id in settings.blacklist_set():
        logger.info("Skipping blacklisted market %s", condition_id)
        return None

    if source_size <= 0 or source_price <= 0:
        logger.warning("Skipping trade with zero size/price: %s", raw_trade)
        return None

    # Skip SELL if we have no open position for this market — prevents fake PNL
    if side.upper() == "SELL":
        from sqlmodel import select as _select
        _chk = await session.exec(
            _select(Position).where(
                Position.condition_id == condition_id,
                Position.outcome == outcome,
            )
        )
        _existing = _chk.first()
        if _existing is None or _existing.size <= 0:
            logger.info("Skipping SELL — no open %s position for market %s", outcome, condition_id)
            return None

    # ── Max trades per market check ───────────────────────────────────────────
    if side.upper() == "BUY" and settings.max_trades_per_market > 0:
        from sqlmodel import func, select as _sel
        count_res = await session.exec(
            _sel(func.count(Trade.id)).where(
                Trade.condition_id == condition_id,
                Trade.side == TradeSide.BUY,
                Trade.is_paper == settings.paper_trading,
                Trade.status.in_([TradeStatus.FILLED, TradeStatus.PAPER]),
            )
        )
        trade_count = count_res.one() or 0
        if trade_count >= settings.max_trades_per_market:
            logger.info(
                "Skipping trade — market %s already has %d/%d copied trades",
                condition_id, trade_count, settings.max_trades_per_market,
            )
            return None

    # ── Size calculation ──────────────────────────────────────────────────────
    from app.services.monitor import fetch_wallet_equity
    from sqlmodel import func, select as _sel

    source_usdc = source_size * source_price

    if settings.paper_trading:
        _spent_res = await session.exec(
            _sel(func.sum(Trade.usdc_amount)).where(
                Trade.is_paper == True,         # noqa: E712
                Trade.side == TradeSide.BUY,
                Trade.status == TradeStatus.PAPER,
            )
        )
        _recv_res = await session.exec(
            _sel(func.sum(Trade.usdc_amount)).where(
                Trade.is_paper == True,         # noqa: E712
                Trade.side == TradeSide.SELL,
                Trade.status == TradeStatus.PAPER,
            )
        )
        _spent    = _spent_res.one() or 0.0
        _received = _recv_res.one() or 0.0
        balance   = max(0.0, settings.paper_balance_usdc - _spent + _received)
        if balance <= 0:
            logger.info("Paper balance exhausted (%.2f), skipping trade", balance)
            return None
    else:
        balance = await poly_client.get_usdc_balance()

    # For proportional mode, fetch the source wallet's current equity
    source_equity = 0.0
    if settings.sizing_mode.value == "proportional" and settings.target_wallet:
        source_equity = await fetch_wallet_equity(settings.target_wallet)

    copy_size = calculate_copy_size(
        source_usdc=source_usdc,
        source_price=source_price,
        settings=settings,
        our_balance_usdc=balance,
        source_equity_usdc=source_equity,
    )

    if copy_size <= 0:
        logger.info("Calculated copy size = 0, skipping trade %s", tx_hash)
        return None

    # ── Max exposure check ────────────────────────────────────────────────────
    if settings.max_exposure_pct > 0:
        trade_usdc = copy_size * source_price
        if balance > 0 and (trade_usdc / balance * 100) > settings.max_exposure_pct:
            logger.warning(
                "Trade %.2f USDC would exceed max exposure %.1f%% (balance %.2f)",
                trade_usdc, settings.max_exposure_pct, balance,
            )
            return None

    # ── Build Trade record ────────────────────────────────────────────────────
    trade = Trade(
        source_tx_hash=tx_hash,
        source_timestamp=source_ts,
        condition_id=condition_id,
        market_title=market_title,
        token_id=token_id,
        outcome=outcome,
        side=TradeSide(side),
        size=copy_size,
        price=source_price,
        usdc_amount=copy_size * source_price,
        status=TradeStatus.PENDING,
        is_paper=settings.paper_trading,
    )

    # ── Execute ───────────────────────────────────────────────────────────────
    if settings.paper_trading:
        trade.status = TradeStatus.PAPER
        logger.info(
            "[PAPER] %s %s %.4f @ %.4f | %s",
            side, outcome, copy_size, source_price, market_title,
        )
    else:
        try:
            resp = await poly_client.place_order(
                token_id=token_id,
                side=side,
                size=copy_size,
                price=source_price,
                order_type="FOK",
            )
            if resp.get("success") or resp.get("status") in ("matched", "live"):
                trade.order_id = resp.get("orderID")
                trade.status   = TradeStatus.FILLED
                logger.info(
                    "[LIVE] %s %s %.4f @ %.4f → order %s",
                    side, outcome, copy_size, source_price, trade.order_id,
                )
            else:
                trade.status        = TradeStatus.FAILED
                trade.error_message = str(resp)
                logger.warning("Order failed: %s", resp)
        except Exception as exc:
            trade.status        = TradeStatus.FAILED
            trade.error_message = str(exc)
            logger.error("Order placement error: %s", exc, exc_info=True)

    # ── Persist ───────────────────────────────────────────────────────────────
    session.add(trade)
    if trade.status in (TradeStatus.FILLED, TradeStatus.PAPER):
        realized = await _upsert_position(
            session=session,
            condition_id=condition_id,
            token_id=token_id,
            market_title=market_title,
            outcome=outcome,
            side=side,
            size=copy_size,
            price=source_price,
            is_paper=settings.paper_trading,
        )
        if realized is not None:
            trade.realized_pnl = round(realized, 6)
            trade.updated_at   = datetime.utcnow()
            session.add(trade)
    await session.commit()

    # ── Notify ────────────────────────────────────────────────────────────────
    if settings.webhook_url:
        await notifier.send_trade_notification(settings.webhook_url, trade)

    return trade
