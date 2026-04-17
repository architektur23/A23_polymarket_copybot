"""
Trades routes.

GET  /trades           → trades history page
GET  /trades/data      → HTMX partial (trades table)
GET  /trades/export    → CSV download
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_db
from app.models.trade import Trade, TradeSide, TradeStatus

router = APIRouter(prefix="/trades", tags=["trades"])


def _templates(request: Request):
    return request.app.state.templates


# ─────────────────────────────────────────────────────────────────────────────
# Full page
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def trades_page(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        "trades.html",
        {"request": request},
    )


# ─────────────────────────────────────────────────────────────────────────────
# HTMX partial — trades table
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/data", response_class=HTMLResponse)
async def trades_data(
    request: Request,
    session: AsyncSession = Depends(get_db),
    page:    int = 1,
    limit:   int = 50,
    side:    Optional[str] = None,
    status:  Optional[str] = None,
    market:  Optional[str] = None,
) -> HTMLResponse:
    stmt = select(Trade).order_by(Trade.created_at.desc())  # type: ignore[arg-type]

    if side:
        stmt = stmt.where(Trade.side == TradeSide(side.upper()))
    if status:
        stmt = stmt.where(Trade.status == TradeStatus(status.lower()))
    if market:
        stmt = stmt.where(Trade.market_title.ilike(f"%{market}%"))  # type: ignore[attr-defined]

    offset = (page - 1) * limit
    stmt   = stmt.offset(offset).limit(limit)

    result = await session.exec(stmt)
    trades = result.all()

    # Total count for pagination
    count_stmt = select(Trade)
    if side:
        count_stmt = count_stmt.where(Trade.side == TradeSide(side.upper()))
    if status:
        count_stmt = count_stmt.where(Trade.status == TradeStatus(status.lower()))
    if market:
        count_stmt = count_stmt.where(Trade.market_title.ilike(f"%{market}%"))  # type: ignore[attr-defined]
    count_result = await session.exec(count_stmt)
    total = len(count_result.all())

    return _templates(request).TemplateResponse(
        "partials/trades_table.html",
        {
            "request": request,
            "trades":  trades,
            "page":    page,
            "limit":   limit,
            "total":   total,
            "pages":   max(1, (total + limit - 1) // limit),
            "side":    side or "",
            "status":  status or "",
            "market":  market or "",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# CSV export
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/export")
async def export_csv(
    session: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    result = await session.exec(
        select(Trade).order_by(Trade.created_at.desc())  # type: ignore[arg-type]
    )
    trades = result.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "created_at", "market_title", "outcome", "side",
        "size", "price", "usdc_amount", "status", "order_id",
        "realized_pnl", "is_paper", "source_tx_hash",
    ])
    for t in trades:
        writer.writerow([
            t.id,
            t.created_at.isoformat(),
            t.market_title,
            t.outcome,
            t.side,
            t.size,
            t.price,
            t.usdc_amount,
            t.status,
            t.order_id or "",
            t.realized_pnl or "",
            t.is_paper,
            t.source_tx_hash or "",
        ])

    output.seek(0)
    filename = f"trades_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
