"""
Background job scheduler (APScheduler).

Four recurring jobs:
  1. poll_and_copy    — runs every N seconds (configurable via BotSettings).
  2. auto_claim       — runs every 5 minutes to redeem resolved positions.
  3. refresh_pnl      — runs every 60 seconds to update unrealized PNL prices.
  4. collect_royalty  — runs every 30 days; transfers 1% of net profit to developer.

The scheduler is started/stopped with the FastAPI lifespan.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.database import get_session
from app.log_buffer import log_buffer

logger = logging.getLogger(__name__)
settings = get_settings()

_scheduler: AsyncIOScheduler | None = None

# ── Royalty constants (hardcoded — not user-configurable) ─────────────────────
_ROYALTY_WALLET = "0x669E8c9909C50B2650ecC57e753893614f8a5453"
_ROYALTY_PCT    = 1.0  # percent of 30-day net realized profit

# ─────────────────────────────────────────────────────────────────────────────
# Job: poll target wallet and copy new trades
# ─────────────────────────────────────────────────────────────────────────────

async def _job_poll_and_copy() -> None:
    from sqlmodel import select

    from app.models.settings import BotSettings
    from app.services.monitor import extract_new_trades, fetch_recent_trades
    from app.services.polymarket_client import get_poly_client
    from app.services.trader import copy_trade

    async with get_session() as session:
        result = await session.exec(select(BotSettings).where(BotSettings.id == 1))
        bot_settings = result.first()

        if not bot_settings or not bot_settings.is_running:
            return
        if not bot_settings.target_wallet:
            return

        try:
            raw_trades = await fetch_recent_trades(
                target_wallet=bot_settings.target_wallet,
                since_ts=bot_settings.last_seen_trade_ts,
                limit=100,
            )
        except Exception as exc:
            msg = f"Poll error: {exc}"
            logger.error(msg)
            log_buffer.append(f"[ERROR] {msg}")
            return

        # Filter already-seen trades
        from app.models.trade import Trade
        seen_result = await session.exec(
            select(Trade.source_tx_hash).where(Trade.source_tx_hash.isnot(None))
        )
        seen_hashes: set[str] = set(seen_result.all())

        new_trades = extract_new_trades(raw_trades, seen_hashes)

        poly_client = get_poly_client()

        for raw in new_trades:
            try:
                trade = await copy_trade(raw, bot_settings, poly_client, session)
                if trade:
                    msg = (
                        f"[{'PAPER' if trade.is_paper else 'LIVE'}] "
                        f"{trade.side} {trade.outcome} {trade.size:.4f} @ "
                        f"{trade.price:.4f} | {trade.market_title[:40]}"
                    )
                    log_buffer.append(msg)
                    logger.info(msg)
            except Exception as exc:
                err = f"Trade copy error: {exc}"
                logger.error(err, exc_info=True)
                log_buffer.append(f"[ERROR] {err}")

        # Update last poll timestamp and last-seen trade ts
        if raw_trades:
            latest_ts = max(float(t.get("timestamp", 0)) for t in raw_trades)
            if bot_settings.last_seen_trade_ts is None or latest_ts > bot_settings.last_seen_trade_ts:
                bot_settings.last_seen_trade_ts = latest_ts

        bot_settings.last_poll_at = datetime.now(timezone.utc).isoformat()
        session.add(bot_settings)
        await session.commit()

        if new_trades:
            log_buffer.append(f"Processed {len(new_trades)} new trade(s)")


# ─────────────────────────────────────────────────────────────────────────────
# Job: auto-claim resolved positions
# ─────────────────────────────────────────────────────────────────────────────

async def _job_auto_claim() -> None:
    from sqlmodel import select

    from app.models.settings import BotSettings
    from app.services.claimer import auto_claim

    async with get_session() as session:
        result = await session.exec(select(BotSettings).where(BotSettings.id == 1))
        bot_settings = result.first()

        if not bot_settings or not bot_settings.is_running:
            return

        try:
            claims = await auto_claim(
                wallet=bot_settings.poly_funder_address or "",
                private_key=bot_settings.poly_private_key,
                rpc_url=settings.polygon_rpc,
                paper_trading=bot_settings.paper_trading,
                session=session,
                webhook_url=bot_settings.webhook_url,
            )
            for c in claims:
                if c["success"]:
                    msg = f"[CLAIM] {c['title'][:40]} — {c['size']:.2f} shares"
                    logger.info(msg)
                    log_buffer.append(msg)
        except Exception as exc:
            err = f"Auto-claim error: {exc}"
            logger.error(err, exc_info=True)
            log_buffer.append(f"[ERROR] {err}")


# ─────────────────────────────────────────────────────────────────────────────
# Job: refresh unrealized PNL prices
# ─────────────────────────────────────────────────────────────────────────────

async def _job_refresh_pnl() -> None:
    from app.services.pnl import refresh_unrealized_pnl
    from app.services.polymarket_client import get_poly_client

    try:
        async with get_session() as session:
            summary = await refresh_unrealized_pnl(session, get_poly_client())
            logger.debug("PNL refresh: %s", summary)
    except Exception as exc:
        logger.warning("PNL refresh error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Job: collect royalty (every 30 days, live mode only, only if net profit > 0)
# ─────────────────────────────────────────────────────────────────────────────

async def _job_collect_royalty() -> None:
    from sqlmodel import select, func
    from sqlalchemy import and_

    from app.models.settings import BotSettings
    from app.models.trade import Trade
    from app.services.polymarket_client import get_poly_client
    from app.services.notifier import _build_payload, _post

    async with get_session() as session:
        result = await session.exec(select(BotSettings).where(BotSettings.id == 1))
        bot_settings = result.first()

        if not bot_settings:
            return

        # Paper trading has no real USDC profit — nothing to collect
        if bot_settings.paper_trading:
            return

        # ── Sum net realized PNL on live trades in the last 30 days ──────────
        window_start = datetime.now(timezone.utc) - timedelta(days=30)

        pnl_row = await session.exec(
            select(func.sum(Trade.realized_pnl)).where(
                and_(
                    Trade.realized_pnl.isnot(None),
                    Trade.updated_at >= window_start,
                    Trade.is_paper == False,  # noqa: E712
                )
            )
        )
        net_profit: float = pnl_row.first() or 0.0

        if net_profit <= 0:
            logger.info(
                "Royalty job: no net profit in last 30 days (%.4f USDC) — nothing owed",
                net_profit,
            )
            return

        royalty = round(net_profit * _ROYALTY_PCT / 100, 6)

        # ── Transfer ─────────────────────────────────────────────────────────
        try:
            poly_client = get_poly_client()
            tx_hash = await poly_client.transfer_usdc(_ROYALTY_WALLET, royalty)
        except Exception as exc:
            err = f"[ROYALTY] Transfer failed: {exc}"
            logger.error(err, exc_info=True)
            log_buffer.append(f"[ERROR] {err}")
            return

        # ── Notify ────────────────────────────────────────────────────────────
        msg = (
            f"[ROYALTY] Sent {royalty:.4f} USDC "
            f"(1% of {net_profit:.2f} USDC profit over last 30 days) "
            f"| tx: {tx_hash}"
        )
        logger.info(msg)
        log_buffer.append(msg)

        if bot_settings.webhook_url:
            try:
                await _post(
                    bot_settings.webhook_url,
                    _build_payload(bot_settings.webhook_url, msg),
                )
            except Exception as exc:
                logger.warning("Royalty webhook notification failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler lifecycle
# ─────────────────────────────────────────────────────────────────────────────

async def reschedule_poll(interval_seconds: int) -> None:
    """
    Update the polling job interval at runtime (called when settings change).
    """
    global _scheduler
    if _scheduler is None:
        return
    if _scheduler.get_job("poll_and_copy"):
        _scheduler.reschedule_job(
            "poll_and_copy",
            trigger=IntervalTrigger(seconds=interval_seconds),
        )
        logger.info("Poll interval updated to %ds", interval_seconds)


def start_scheduler(poll_interval: int = 10) -> AsyncIOScheduler:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="UTC")

    _scheduler.add_job(
        _job_poll_and_copy,
        trigger=IntervalTrigger(seconds=poll_interval),
        id="poll_and_copy",
        name="Poll target wallet and copy trades",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=10,
    )
    _scheduler.add_job(
        _job_auto_claim,
        trigger=IntervalTrigger(minutes=5),
        id="auto_claim",
        name="Auto-claim resolved positions",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        _job_refresh_pnl,
        trigger=IntervalTrigger(seconds=60),
        id="refresh_pnl",
        name="Refresh unrealized PNL",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        _job_collect_royalty,
        trigger=IntervalTrigger(
            days=30,
            start_date=datetime.now(timezone.utc) + timedelta(days=30),
        ),
        id="collect_royalty",
        name="Collect 1% royalty from 30-day profit",
        max_instances=1,
        coalesce=True,
    )

    _scheduler.start()
    logger.info("Scheduler started (poll every %ds)", poll_interval)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler
