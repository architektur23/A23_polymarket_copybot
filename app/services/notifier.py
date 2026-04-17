"""
Webhook notification dispatcher.

Supports any generic webhook URL (works with Telegram bots and
Discord webhooks out of the box — the payload format adapts automatically).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _is_discord(url: str) -> bool:
    return "discord.com/api/webhooks" in url


def _is_telegram(url: str) -> bool:
    return "api.telegram.org" in url


def _build_payload(url: str, text: str) -> dict[str, Any]:
    """
    Build the correct payload shape for the target webhook type.
    Falls back to a generic {"text": ...} payload for unknown services.
    """
    if _is_discord(url):
        return {"content": text}
    if _is_telegram(url):
        # Telegram bot webhook: POST /sendMessage
        return {"text": text, "parse_mode": "HTML"}
    return {"text": text}


async def _post(url: str, payload: dict[str, Any]) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("Webhook POST failed (%s): %s", url[:40], exc)


async def send_trade_notification(webhook_url: str, trade: Any) -> None:
    """Fire a notification when a trade is copied."""
    emoji = "🟢" if trade.side == "BUY" else "🔴"
    mode  = "[PAPER]" if trade.is_paper else "[LIVE]"

    text = (
        f"{emoji} {mode} Copy Trade\n"
        f"Market: {trade.market_title}\n"
        f"Outcome: {trade.outcome}  |  Side: {trade.side}\n"
        f"Size: {trade.size:.4f} shares @ ${trade.price:.4f}\n"
        f"USDC: ${trade.usdc_amount:.2f}\n"
        f"Status: {trade.status}"
    )
    await _post(webhook_url, _build_payload(webhook_url, text))


async def send_claim_notification(webhook_url: str, claim: dict[str, Any]) -> None:
    """Fire a notification when winnings are auto-claimed."""
    text = (
        f"💰 Auto-Claim\n"
        f"Market: {claim.get('title', '')}\n"
        f"Shares redeemed: {claim.get('size', 0):.4f}\n"
        f"Tx: {claim.get('tx_hash', 'N/A')}"
    )
    await _post(webhook_url, _build_payload(webhook_url, text))


async def send_error_notification(webhook_url: str, message: str) -> None:
    """Fire an error alert."""
    text = f"⚠️ Bot Error\n{message}"
    await _post(webhook_url, _build_payload(webhook_url, text))
