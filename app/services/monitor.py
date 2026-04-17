"""
Target-wallet monitor.

Polls data-api.polymarket.com/trades for the configured target wallet and
yields any trade that occurred after the last known timestamp.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Optional

import httpx

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"


async def fetch_recent_trades(
    target_wallet: str,
    since_ts: Optional[float] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Return trades for *target_wallet* newer than *since_ts* (Unix seconds).

    The Data API returns trades in descending timestamp order by default.
    We fetch the first page (up to *limit*) and filter locally.

    Each trade dict contains at minimum:
        proxyWallet, side, asset (token_id), conditionId,
        size, price, timestamp, title, outcome, outcomeIndex,
        transactionHash
    """
    url = f"{DATA_API}/trades"
    params: dict[str, Any] = {
        "user":      target_wallet.lower(),
        "limit":     limit,
        "takerOnly": "false",  # include both maker and taker fills
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            trades: list[dict] = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("Data API HTTP error %s: %s", exc.response.status_code, exc)
        return []
    except Exception as exc:
        logger.error("Data API fetch error: %s", exc)
        return []

    if since_ts is None:
        return trades

    # Keep only trades strictly newer than our last-seen timestamp
    new_trades = [
        t for t in trades
        if float(t.get("timestamp", 0)) > since_ts
    ]
    return new_trades


async def fetch_target_positions(
    wallet: str,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """
    Return all current open positions for *wallet* via Data API.
    Useful for reconciling our mirrored state.
    """
    url = f"{DATA_API}/positions"
    params: dict[str, Any] = {
        "user":      wallet.lower(),
        "limit":     limit,
        "sizeThreshold": 0.01,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.error("Positions fetch error for %s: %s", wallet, exc)
        return []


async def fetch_wallet_equity(wallet: str) -> float:
    """
    Estimate a wallet's total invested equity by summing the currentValue
    of all open positions via the Data API.

    This is used for proportional sizing: if the source wallet deployed
    2% of their equity on a trade, we deploy the same 2% of ours.

    Note: this reflects *invested* capital only (open positions).
    Uninvested USDC sitting idle is not included, so the ratio may
    slightly over-size our trades — still a sound conservative proxy.
    """
    url = f"{DATA_API}/positions"
    params: dict[str, Any] = {
        "user":          wallet.lower(),
        "limit":         500,
        "sizeThreshold": 0.01,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            positions = resp.json()
        total = sum(float(p.get("currentValue", 0)) for p in positions)
        return total if total > 0 else 0.0
    except Exception as exc:
        logger.warning("Could not fetch equity for %s: %s", wallet, exc)
        return 0.0


def extract_new_trades(
    fetched: list[dict[str, Any]],
    seen_tx_hashes: set[str],
) -> list[dict[str, Any]]:
    """
    Filter out trades we have already processed (by transaction hash).
    Returns only genuinely new trades, oldest-first so we process in order.
    """
    new = [
        t for t in fetched
        if t.get("transactionHash") not in seen_tx_hashes
    ]
    # Reverse so we process oldest-first (API returns newest-first)
    return list(reversed(new))


def latest_trade_timestamp(trades: list[dict[str, Any]]) -> Optional[float]:
    """Return the maximum timestamp from a list of trade dicts."""
    if not trades:
        return None
    return max(float(t.get("timestamp", 0)) for t in trades)


def now_utc_ts() -> float:
    """Current UTC time as a Unix timestamp float."""
    return datetime.now(timezone.utc).timestamp()
