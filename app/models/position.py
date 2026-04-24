"""
Live position — one row per market we currently hold tokens in.
Updated after every trade + every PNL refresh cycle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class Position(SQLModel, table=True):
    """Current open positions (updated in-place)."""

    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("condition_id", "outcome"),)

    id: Optional[int] = Field(default=None, primary_key=True)

    # ── Market identity ───────────────────────────────────────────────────────
    condition_id: str = Field(index=True)
    market_title: str = Field(default="")
    token_id: str = Field(default="")
    outcome: str = Field(default="")          # "Yes" / "No"

    # ── Position state ────────────────────────────────────────────────────────
    # Total shares held
    size: float = Field(default=0.0)
    # Weighted-average entry price per share
    avg_entry_price: float = Field(default=0.0)
    # Total USDC spent acquiring this position
    total_cost: float = Field(default=0.0)

    # ── PNL (refreshed periodically) ──────────────────────────────────────────
    current_price: Optional[float] = Field(default=None)
    current_value: Optional[float] = Field(default=None)   # size × current_price
    unrealized_pnl: Optional[float] = Field(default=None)  # current_value - total_cost
    unrealized_pnl_pct: Optional[float] = Field(default=None)
    realized_pnl: float = Field(default=0.0)                # from partial exits

    # ── Market status ────────────────────────────────────────────────────────
    market_resolved: bool = Field(default=False)
    redeemable: bool = Field(default=False)
    market_end_date: Optional[str] = Field(default=None)    # ISO date string

    # ── Metadata ──────────────────────────────────────────────────────────────
    opened_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    is_paper: bool = Field(default=False)
