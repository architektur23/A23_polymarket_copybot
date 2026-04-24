"""
Bot settings persisted in the database.
Credentials (private key, funder address) are stored here so they can
be entered via the web UI without touching any .env file.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class SizingMode(str, Enum):
    PROPORTIONAL = "proportional"  # mirror source wallet's equity % on my equity
    FIXED        = "fixed"         # fixed USDC per trade


class BotSettings(SQLModel, table=True):
    """Single-row settings table (id always 1)."""

    __tablename__ = "bot_settings"

    id: int = Field(default=1, primary_key=True)

    # ── Polymarket credentials (entered via UI) ───────────────────────────────
    poly_private_key:    str = Field(default="")
    poly_funder_address: str = Field(default="")
    # 0 = EOA (standard wallet), 1 = POLY_PROXY (Magic/email), 2 = GNOSIS_SAFE
    poly_signature_type: int = Field(default=0)
    # L2 API credentials — auto-derived from private key if left blank.
    poly_api_key:        str = Field(default="")
    poly_api_secret:     str = Field(default="")
    poly_api_passphrase: str = Field(default="")

    # ── Display ──────────────────────────────────────────────────────────────
    bot_name: str = Field(default="PM Copy")

    # ── Copying target ────────────────────────────────────────────────────────
    target_wallet: str = Field(default="")

    # ── Mode ─────────────────────────────────────────────────────────────────
    paper_trading: bool = Field(default=True)

    # Simulated equity used for position sizing in paper mode.
    # In live mode the real USDC wallet balance is used instead.
    paper_balance_usdc: float = Field(default=1000.0)

    # ── Position sizing ───────────────────────────────────────────────────────
    sizing_mode: SizingMode = Field(default=SizingMode.PROPORTIONAL)

    # Proportional mode: bot detects what % of their equity the source wallet
    # used and applies the same % to your equity.
    # min_trade_usdc acts as a floor for both modes.
    min_trade_usdc: float = Field(default=1.0)

    # Fixed mode: exact USDC amount per trade regardless of source size
    fixed_trade_usdc: float = Field(default=10.0)

    # ── Polling ──────────────────────────────────────────────────────────────
    poll_interval_seconds: int = Field(default=10, ge=5)

    # ── Risk limits ───────────────────────────────────────────────────────────
    max_exposure_pct: float = Field(
        default=80.0,
        description="Max % of balance deployed at once (0 = disabled)",
    )
    max_position_usdc: float = Field(
        default=0.0,
        description="Per-trade cap in USDC (0 = disabled)",
    )
    blacklisted_markets: str = Field(default="")

    # ── Notifications ─────────────────────────────────────────────────────────
    webhook_url: Optional[str] = Field(default=None)

    # ── Royalty ───────────────────────────────────────────────────────────────
    royalty_pct: float = Field(default=1.0)

    # ── Bot state ────────────────────────────────────────────────────────────
    is_running:           bool           = Field(default=False)
    last_poll_at:         Optional[str]  = Field(default=None)
    last_seen_trade_ts:   Optional[float]= Field(default=None)

    def blacklist_set(self) -> set[str]:
        if not self.blacklisted_markets:
            return set()
        return {c.strip() for c in self.blacklisted_markets.split(",") if c.strip()}
