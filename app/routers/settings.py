"""
Settings routes.

GET  /settings        → settings page
POST /settings        → save settings
POST /settings/start  → start bot
POST /settings/stop   → stop bot
POST /settings/approve-allowances → trigger on-chain approval setup
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_db
from app.models.settings import BotSettings, SizingMode
from app.scheduler import reschedule_poll

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["settings"])


def _templates(request: Request):
    return request.app.state.templates


# ─────────────────────────────────────────────────────────────────────────────
# GET settings page
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    session: AsyncSession = Depends(get_db),
    saved: bool = False,
) -> HTMLResponse:
    result = await session.exec(select(BotSettings).where(BotSettings.id == 1))
    s = result.first() or BotSettings(id=1)
    return _templates(request).TemplateResponse(
        "settings.html",
        {"request": request, "settings": s, "saved": saved},
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST save settings
# ─────────────────────────────────────────────────────────────────────────────

@router.post("", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    session: AsyncSession = Depends(get_db),
    # ── Credentials ──────────────────────────────────────────────────────────
    poly_private_key:       Annotated[str,   Form()] = "",
    poly_funder_address:    Annotated[str,   Form()] = "",
    poly_signature_type:    Annotated[int,   Form()] = 0,
    poly_api_key:           Annotated[str,   Form()] = "",
    poly_api_secret:        Annotated[str,   Form()] = "",
    poly_api_passphrase:    Annotated[str,   Form()] = "",
    # ── Display ──────────────────────────────────────────────────────────────
    bot_name:               Annotated[str,   Form()] = "PM Copy",
    # ── Copy target ──────────────────────────────────────────────────────────
    target_wallet:          Annotated[str,   Form()] = "",
    paper_trading:          Annotated[str,   Form()] = "off",
    paper_balance_usdc:     Annotated[float, Form()] = 1000.0,
    # ── Sizing ───────────────────────────────────────────────────────────────
    sizing_mode:            Annotated[str,   Form()] = "proportional",
    min_trade_usdc:         Annotated[float, Form()] = 1.0,
    fixed_trade_usdc:       Annotated[float, Form()] = 10.0,
    # ── Polling ──────────────────────────────────────────────────────────────
    poll_interval_seconds:  Annotated[int,   Form()] = 10,
    # ── Risk ─────────────────────────────────────────────────────────────────
    max_exposure_pct:       Annotated[float, Form()] = 80.0,
    max_position_usdc:      Annotated[float, Form()] = 0.0,
    blacklisted_markets:    Annotated[str,   Form()] = "",
    # ── Notifications ────────────────────────────────────────────────────────
    webhook_url:            Annotated[str,   Form()] = "",
) -> HTMLResponse:
    result = await session.exec(select(BotSettings).where(BotSettings.id == 1))
    s = result.first() or BotSettings(id=1)

    # ── Credentials ──────────────────────────────────────────────────────────
    # Only overwrite the stored key if a non-empty value was submitted.
    # This lets the form show a masked placeholder without clearing the key
    # when the user saves other settings without re-entering it.
    new_key = poly_private_key.strip()
    if new_key:
        # Normalise: ensure 0x prefix
        if not new_key.startswith("0x"):
            new_key = "0x" + new_key
        s.poly_private_key = new_key

    s.poly_funder_address = poly_funder_address.strip()
    s.poly_signature_type = poly_signature_type
    # Only overwrite API creds if a non-empty value was submitted
    if poly_api_key.strip():
        s.poly_api_key = poly_api_key.strip()
    if poly_api_secret.strip():
        s.poly_api_secret = poly_api_secret.strip()
    if poly_api_passphrase.strip():
        s.poly_api_passphrase = poly_api_passphrase.strip()

    # ── Bot config ────────────────────────────────────────────────────────────
    s.bot_name              = bot_name.strip() or "PM Copy"
    s.target_wallet         = target_wallet.strip().lower()
    s.paper_trading         = paper_trading == "on"
    s.paper_balance_usdc    = max(1.0, min(paper_balance_usdc, 10_000_000.0))
    s.sizing_mode           = SizingMode(sizing_mode)
    s.min_trade_usdc        = max(0.0, min_trade_usdc)
    s.fixed_trade_usdc      = max(0.0, fixed_trade_usdc)
    s.poll_interval_seconds = max(5, poll_interval_seconds)
    s.max_exposure_pct      = max(0.0, min(max_exposure_pct, 100.0))
    s.max_position_usdc     = max(0.0, max_position_usdc)
    s.blacklisted_markets   = blacklisted_markets.strip()
    s.webhook_url           = webhook_url.strip() or None

    session.add(s)
    await session.commit()

    # ── Re-init Polymarket client if a key is now set ─────────────────────────
    if s.poly_private_key:
        try:
            from app.services.polymarket_client import get_poly_client
            await get_poly_client().reinitialise(
                key=s.poly_private_key,
                funder=s.poly_funder_address,
                sig_type=s.poly_signature_type,
                api_key=s.poly_api_key,
                api_secret=s.poly_api_secret,
                api_passphrase=s.poly_api_passphrase,
            )
        except Exception as exc:
            logger.warning("Client re-init after settings save failed: %s", exc)

    # Refresh the Jinja2 global so the nav reflects the new name immediately
    request.app.state.templates.env.globals["bot_name"] = s.bot_name

    # Update scheduler poll interval live
    await reschedule_poll(s.poll_interval_seconds)

    logger.info("Settings saved")
    return RedirectResponse(url="/settings?saved=1", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Start / Stop bot
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/start", response_class=HTMLResponse)
async def start_bot(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    result = await session.exec(select(BotSettings).where(BotSettings.id == 1))
    s = result.first()
    if s:
        s.is_running = True
        session.add(s)
        await session.commit()
        logger.info("Bot started")
    return RedirectResponse(url="/", status_code=303)


@router.post("/stop", response_class=HTMLResponse)
async def stop_bot(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    result = await session.exec(select(BotSettings).where(BotSettings.id == 1))
    s = result.first()
    if s:
        s.is_running = False
        session.add(s)
        await session.commit()
        logger.info("Bot stopped")
    return RedirectResponse(url="/", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# On-chain allowance setup
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/approve-allowances", response_class=HTMLResponse)
async def approve_allowances(request: Request) -> HTMLResponse:
    """
    Trigger the one-time on-chain approval transactions.
    Returns an HTMX fragment with the result.
    """
    try:
        from app.services.polymarket_client import get_poly_client
        results = await get_poly_client().setup_allowances()
        msg = f"Allowances set: {len(results)} transaction(s). Bot is ready to trade."
        ok  = True
    except Exception as exc:
        msg = f"Allowance setup failed: {exc}"
        ok  = False
        logger.error(msg)

    return _templates(request).TemplateResponse(
        "partials/allowance_result.html",
        {"request": request, "message": msg, "ok": ok},
    )
