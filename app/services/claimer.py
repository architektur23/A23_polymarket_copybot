"""
Auto-claim service.

Periodically checks data-api.polymarket.com/positions?redeemable=true
for our wallet and redeems winning tokens on-chain via the CTF contract.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DATA_API    = "https://data-api.polymarket.com"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Minimal ABI for redeemPositions
CTF_REDEEM_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken",  "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",      "type": "bytes32"},
            {"name": "indexSets",        "type": "uint256[]"},
        ],
        "outputs": [],
    }
]

USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ZERO_BYTES32 = b"\x00" * 32


async def fetch_redeemable_positions(wallet: str) -> list[dict[str, Any]]:
    """
    Query the Data API for positions that are redeemable (market resolved,
    winning tokens held).
    """
    url = f"{DATA_API}/positions"
    params = {
        "user":       wallet.lower(),
        "redeemable": "true",
        "limit":      500,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            positions = resp.json()
            logger.debug("Found %d redeemable positions for %s", len(positions), wallet)
            return positions
    except Exception as exc:
        logger.error("Failed to fetch redeemable positions: %s", exc)
        return []


def redeem_position(
    private_key: str,
    rpc_url: str,
    condition_id_hex: str,
    index_sets: list[int],
) -> str:
    """
    Call redeemPositions on the CTF contract for one market.
    Returns the transaction hash as a hex string.

    index_sets = [1] for YES wins, [2] for NO wins (standard binary markets).
    """
    try:
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
    except ImportError:
        raise RuntimeError("web3 not installed — cannot redeem positions")

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    account    = w3.eth.account.from_key(private_key)
    ctf        = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS),
        abi=CTF_REDEEM_ABI,
    )

    # conditionId must be bytes32
    cond_bytes = bytes.fromhex(condition_id_hex.lstrip("0x").zfill(64))

    tx = ctf.functions.redeemPositions(
        Web3.to_checksum_address(USDC_ADDRESS),
        ZERO_BYTES32,
        cond_bytes,
        index_sets,
    ).build_transaction(
        {
            "from":  account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas":   200_000,
        }
    )
    signed  = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    if receipt.status != 1:
        raise RuntimeError(f"redeemPositions reverted for conditionId {condition_id_hex}")

    return tx_hash.hex()


async def auto_claim(
    wallet: str,
    private_key: str,
    rpc_url: str,
    paper_trading: bool,
    session: Any,  # AsyncSession — typed as Any to avoid circular import
    webhook_url: str | None = None,
) -> list[dict[str, Any]]:
    """
    Full auto-claim cycle:
      1. Fetch redeemable positions from Data API.
      2. For each, call redeemPositions on-chain (skip in paper mode).
      3. Update the Position row in the DB to mark as redeemed.
      4. Optionally fire webhook.

    Returns a list of claim result dicts.
    """
    from sqlmodel import select

    from app.models.position import Position
    from app.services import notifier

    positions = await fetch_redeemable_positions(wallet)
    if not positions:
        return []

    results = []
    for pos_data in positions:
        condition_id = pos_data.get("conditionId", "")
        size         = float(pos_data.get("size", 0))
        outcome_idx  = int(pos_data.get("outcomeIndex", 0))
        title        = pos_data.get("title", "")

        # index_set: outcomeIndex 0 → YES = index_set [1], NO = [2]
        # (Gnosis CTF: index_set is 1-based bitmask)
        index_set = [1 << outcome_idx] if outcome_idx >= 0 else [1]

        claim_result: dict[str, Any] = {
            "condition_id": condition_id,
            "title":        title,
            "size":         size,
            "success":      False,
            "tx_hash":      None,
            "error":        None,
        }

        if paper_trading:
            claim_result["success"] = True
            claim_result["tx_hash"] = "PAPER_CLAIM"
            logger.info("[PAPER] Auto-claim %s %.2f shares | %s", condition_id, size, title)
        else:
            try:
                tx_hash = redeem_position(
                    private_key=private_key,
                    rpc_url=rpc_url,
                    condition_id_hex=condition_id,
                    index_sets=index_set,
                )
                claim_result["success"] = True
                claim_result["tx_hash"] = tx_hash
                logger.info("Claimed %s — tx %s", condition_id, tx_hash)
            except Exception as exc:
                claim_result["error"] = str(exc)
                logger.error("Claim failed for %s: %s", condition_id, exc)

        # ── Update DB position ─────────────────────────────────────────────
        if claim_result["success"]:
            from datetime import datetime

            result = await session.exec(
                select(Position).where(Position.condition_id == condition_id)
            )
            db_pos = result.first()
            if db_pos:
                db_pos.redeemable      = False
                db_pos.market_resolved = True
                # Realized PNL: proceeds = size × $1 (winning tokens = $1)
                proceeds = size * 1.0
                db_pos.realized_pnl += proceeds - db_pos.total_cost
                db_pos.size          = 0.0
                db_pos.total_cost    = 0.0
                db_pos.updated_at    = datetime.utcnow()
                session.add(db_pos)
            await session.commit()

        # ── Webhook ────────────────────────────────────────────────────────
        if webhook_url and claim_result["success"]:
            await notifier.send_claim_notification(webhook_url, claim_result)

        results.append(claim_result)

    return results
