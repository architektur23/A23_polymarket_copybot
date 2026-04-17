"""
Trade record — one row per copied (or simulated) trade execution.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class TradeSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeStatus(str, Enum):
    PENDING = "pending"       # about to be placed
    FILLED = "filled"         # order matched / simulated
    PARTIAL = "partial"       # partially filled
    FAILED = "failed"         # order rejected or errored
    PAPER = "paper"           # paper-trade simulation


class Trade(SQLModel, table=True):
    """Full history of every copied trade attempt."""

    __tablename__ = "trades"

    id: Optional[int] = Field(default=None, primary_key=True)

    # ── Source trade (from target wallet) ────────────────────────────────────
    source_tx_hash: Optional[str] = Field(default=None, index=True)
    source_timestamp: float = Field(index=True)  # Unix timestamp

    # ── Market identity ───────────────────────────────────────────────────────
    condition_id: str = Field(index=True)
    market_title: str = Field(default="")
    # YES or NO token_id (the ERC-1155 asset ID)
    token_id: str = Field(default="")
    outcome: str = Field(default="")          # "Yes" / "No"

    # ── Trade details ─────────────────────────────────────────────────────────
    side: TradeSide
    # Size in shares (outcome tokens)
    size: float
    # Price per share in USDC (0–1)
    price: float
    # Total USDC cost/proceeds = size × price
    usdc_amount: float

    # ── Our order ────────────────────────────────────────────────────────────
    order_id: Optional[str] = Field(default=None)   # CLOB order UUID
    status: TradeStatus = Field(default=TradeStatus.PENDING)
    error_message: Optional[str] = Field(default=None)

    # ── PNL ──────────────────────────────────────────────────────────────────
    # Realized PNL in USDC, set when position exits or is claimed
    realized_pnl: Optional[float] = Field(default=None)

    # ── Metadata ──────────────────────────────────────────────────────────────
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    is_paper: bool = Field(default=False)
