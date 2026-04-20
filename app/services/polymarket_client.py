"""
Polymarket service layer.

Wraps py-clob-client initialisation, credential derivation, allowance
setup, and all order-placement helpers behind a clean async-friendly API.

The ClobClient itself is synchronous (it uses requests internally), so we
run every blocking call in a thread-pool executor to keep FastAPI's async
event-loop free.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# ── Lazy imports for py-clob-client (only available at runtime) ──────────────
# We guard these so that unit-tests or UI-only runs don't hard-fail.
try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import (
        ApiCreds,
        MarketOrderArgs,
        OrderArgs,
        OrderType,
        TradeParams,
    )
    from py_clob_client_v2.constants import AMOY, POLYGON
    from py_clob_client_v2.order_builder.constants import BUY, SELL
    _CLOB_AVAILABLE = True
except ImportError:
    _CLOB_AVAILABLE = False
    logger.warning("py-clob-client-v2 not installed — live trading disabled")

# ── Web3 for on-chain allowance setup ────────────────────────────────────────
try:
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    _WEB3_AVAILABLE = True
except ImportError:
    _WEB3_AVAILABLE = False

# ── Contract constants ────────────────────────────────────────────────────────
CTF_EXCHANGE          = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE     = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER      = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CTF_EXCHANGE_V2       = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2  = "0xe2222d279d744050d28e00520010520000310F59"
CTF_ADDRESS           = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS          = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # pUSD (replaces USDC.e)

MAX_INT = 2**256 - 1  # unlimited allowance

# Minimal ABIs for approval transactions
ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "transfer",
        "type": "function",
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "amount",    "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]
ERC1155_ABI = [
    {
        "name": "setApprovalForAll",
        "type": "function",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
    },
    {
        "name": "isApprovedForAll",
        "type": "function",
        "inputs": [
            {"name": "account",  "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]


class PolymarketClient:
    """
    Thread-safe wrapper around ClobClient.

    Usage:
        client = PolymarketClient(settings)
        await client.initialise()          # derives creds, sets up client
        order_resp = await client.place_order(...)
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._clob: Optional[Any] = None  # ClobClient instance
        self._ready = False

    # ─────────────────────────────────────────────────────────────────────────
    # Initialisation
    # ─────────────────────────────────────────────────────────────────────────

    async def initialise(
        self,
        key: str = "",
        funder: str = "",
        sig_type: int = 0,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
    ) -> None:
        """
        Build the ClobClient and derive L2 API credentials.
        Credentials are passed in explicitly (read from BotSettings by the caller).
        Runs the blocking SDK calls in a thread-pool executor.
        """
        if not _CLOB_AVAILABLE:
            logger.warning("py-clob-client unavailable — running in paper-only mode")
            return

        # Normalise key: accept with or without 0x prefix
        if key and not key.startswith("0x"):
            key = "0x" + key

        if not key:
            logger.warning("No private key configured — running in paper-only mode. "
                           "Enter your key on the Settings page.")
            return

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                partial(self._sync_initialise, key, funder or None, sig_type,
                        api_key, api_secret, api_passphrase),
            )
            self._ready = True
            logger.info("PolymarketClient initialised (funder: %s)", self._funder)
        except Exception as exc:
            self._ready = False
            logger.warning(
                "PolymarketClient init failed (%s) — running in paper-only mode. "
                "Check that your private key is a 64-hex-char value, not a wallet address.",
                exc,
            )

    async def reinitialise(
        self,
        key: str,
        funder: str = "",
        sig_type: int = 0,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
    ) -> None:
        """Re-initialise after credentials are saved via the Settings UI."""
        self._ready = False
        self._clob  = None
        await self.initialise(
            key=key, funder=funder, sig_type=sig_type,
            api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase,
        )

    def _sync_initialise(
        self,
        key: str,
        funder: str | None,
        sig_type: int,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
    ) -> None:
        """Synchronous init — called in executor."""
        self._clob = ClobClient(
            self._settings.clob_host,
            key=key,
            chain_id=self._settings.chain_id,
            signature_type=sig_type,
            funder=funder,
        )

        # Use manually-provided L2 API creds if all three fields are set;
        # otherwise auto-derive them from the private key (deterministic).
        if api_key and api_secret and api_passphrase:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )
            logger.info("Using manually-provided L2 API credentials")
        else:
            creds = self._clob.create_or_derive_api_key()
            logger.info("L2 API credentials auto-derived from private key")

        self._clob.set_api_creds(creds)

        # Cache the funder address (displayed in UI)
        self._funder = self._clob.get_address()

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def funder_address(self) -> str:
        return getattr(self, "_funder", "")

    # ─────────────────────────────────────────────────────────────────────────
    # Allowance setup (one-time, called from settings page)
    # ─────────────────────────────────────────────────────────────────────────

    async def setup_allowances(self) -> dict[str, str]:
        """
        Approve pUSD + CTF tokens for all exchange contracts (V1 + V2).
        Returns a dict of contract → tx_hash.
        """
        if not _WEB3_AVAILABLE:
            raise RuntimeError("web3 not installed")

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_setup_allowances)

    def _sync_setup_allowances(self) -> dict[str, str]:
        if not self._clob:
            raise RuntimeError("Client not initialised — save your private key in Settings first")
        w3 = Web3(Web3.HTTPProvider(self._settings.polygon_rpc))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        # Re-derive the key from the signer stored in the CLOB client
        raw_key = self._clob.signer.private_key.to_hex()
        account = w3.eth.account.from_key(raw_key)
        addr    = account.address
        results: dict[str, str] = {}

        usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
        ctf  = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS),  abi=ERC1155_ABI)

        targets = [CTF_EXCHANGE, NEG_RISK_EXCHANGE, NEG_RISK_ADAPTER, CTF_EXCHANGE_V2, NEG_RISK_EXCHANGE_V2]

        for target in targets:
            cs_target = Web3.to_checksum_address(target)
            nonce = w3.eth.get_transaction_count(addr)

            # 1. ERC-20 approve USDC.e
            current_allowance = usdc.functions.allowance(addr, cs_target).call()
            if current_allowance < MAX_INT // 2:
                tx = usdc.functions.approve(cs_target, MAX_INT).build_transaction(
                    {"from": addr, "nonce": nonce, "gas": 100_000}
                )
                signed = w3.eth.account.sign_transaction(tx, raw_key)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                w3.eth.wait_for_transaction_receipt(tx_hash)
                results[f"usdc_approve_{target[:8]}"] = tx_hash.hex()
                nonce += 1

            # 2. ERC-1155 setApprovalForAll CTF tokens
            already_approved = ctf.functions.isApprovedForAll(addr, cs_target).call()
            if not already_approved:
                tx = ctf.functions.setApprovalForAll(cs_target, True).build_transaction(
                    {"from": addr, "nonce": nonce, "gas": 100_000}
                )
                signed = w3.eth.account.sign_transaction(tx, raw_key)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                w3.eth.wait_for_transaction_receipt(tx_hash)
                results[f"ctf_approve_{target[:8]}"] = tx_hash.hex()

        logger.info("Allowances set: %s", results)
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # Order placement
    # ─────────────────────────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def place_order(
        self,
        token_id: str,
        side: str,          # "BUY" or "SELL"
        size: float,        # shares
        price: float,       # 0–1 USDC per share
        order_type: str = "FOK",
    ) -> dict[str, Any]:
        """
        Place a limit order via the CLOB.
        Returns the raw response dict from post_order.
        Retries up to 3× with exponential back-off on transient errors.
        """
        if not self._ready:
            raise RuntimeError("Client not initialised — check POLY_PRIVATE_KEY")

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            partial(self._sync_place_order, token_id, side, size, price, order_type),
        )

    def _sync_place_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        order_type: str,
    ) -> dict[str, Any]:
        _side = BUY if side.upper() == "BUY" else SELL
        _type = getattr(OrderType, order_type.upper(), OrderType.FOK)

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=_side,
        )
        signed = self._clob.create_order(order_args)
        response = self._clob.post_order(signed, _type)
        return response  # type: ignore[return-value]

    # ─────────────────────────────────────────────────────────────────────────
    # Market data helpers (async HTTP via httpx)
    # ─────────────────────────────────────────────────────────────────────────

    async def get_market_by_condition_id(self, condition_id: str) -> Optional[dict]:
        """Fetch market metadata from Gamma API."""
        url = f"{self._settings.gamma_api_host}/markets"
        params = {"conditionIds": condition_id, "limit": 1}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
        return None

    async def get_midpoint_price(self, token_id: str) -> Optional[float]:
        """
        Get the current mid-point price for a token from the CLOB API.
        Returns a float in [0, 1] or None if unavailable.
        """
        url = f"{self._settings.clob_host}/midpoint"
        params = {"token_id": token_id}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                return float(data.get("mid", 0)) or None
        except Exception as exc:
            logger.debug("Midpoint fetch failed for %s: %s", token_id, exc)
            return None

    async def get_usdc_balance(self) -> float:
        """
        Return the pUSD balance (human-readable) for our funder address.
        Uses a direct JSON-RPC call to the ERC-20 balanceOf method.
        """
        if not self._ready or not _WEB3_AVAILABLE:
            return 0.0
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_usdc_balance)

    def _sync_usdc_balance(self) -> float:
        w3 = Web3(Web3.HTTPProvider(self._settings.polygon_rpc))
        abi = [{"name": "balanceOf", "type": "function",
                "inputs": [{"name": "account", "type": "address"}],
                "outputs": [{"name": "", "type": "uint256"}]}]
        usdc = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS), abi=abi
        )
        raw = usdc.functions.balanceOf(
            Web3.to_checksum_address(self._funder)
        ).call()
        return raw / 1_000_000  # 6 decimals

    # ─────────────────────────────────────────────────────────────────────────
    # USDC transfer (used by royalty collection job)
    # ─────────────────────────────────────────────────────────────────────────

    async def transfer_usdc(self, recipient: str, amount_usdc: float) -> str:
        """
        Transfer amount_usdc pUSD to recipient on Polygon.
        Returns the transaction hash as a hex string.
        Raises on failure.
        """
        if not self._ready or not _WEB3_AVAILABLE:
            raise RuntimeError("PolymarketClient not ready — check private key in Settings")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            partial(self._sync_transfer_usdc, recipient, amount_usdc),
        )

    def _sync_transfer_usdc(self, recipient: str, amount_usdc: float) -> str:
        """Synchronous USDC transfer — runs in thread-pool executor."""
        w3 = Web3(Web3.HTTPProvider(self._settings.polygon_rpc))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        raw_key = self._clob.signer.private_key.to_hex()
        account  = w3.eth.account.from_key(raw_key)
        addr     = account.address

        usdc = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI
        )

        # pUSD uses 6 decimal places
        amount_raw = int(amount_usdc * 1_000_000)

        nonce = w3.eth.get_transaction_count(addr)
        tx = usdc.functions.transfer(
            Web3.to_checksum_address(recipient), amount_raw
        ).build_transaction({"from": addr, "nonce": nonce, "gas": 100_000})

        signed  = w3.eth.account.sign_transaction(tx, raw_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status != 1:
            raise RuntimeError(f"USDC transfer reverted — tx: {tx_hash.hex()}")

        logger.info(
            "USDC transfer: %.6f USDC → %s | tx: %s",
            amount_usdc, recipient, tx_hash.hex(),
        )
        return tx_hash.hex()


# ── Module-level singleton, initialised by the app lifespan ──────────────────
_client_instance: Optional[PolymarketClient] = None


def get_poly_client() -> PolymarketClient:
    """FastAPI dependency / global accessor."""
    global _client_instance
    if _client_instance is None:
        raise RuntimeError("PolymarketClient not initialised yet")
    return _client_instance


async def init_poly_client(config: Any, bot_settings: Any = None) -> PolymarketClient:
    """
    Called once at app startup.
    config        = app.config.Settings (infrastructure: RPC, chain_id, hosts)
    bot_settings  = app.models.settings.BotSettings (credentials from DB)
    """
    global _client_instance
    _client_instance = PolymarketClient(config)
    key        = getattr(bot_settings, "poly_private_key",    "") if bot_settings else ""
    funder     = getattr(bot_settings, "poly_funder_address", "") if bot_settings else ""
    sig_type   = getattr(bot_settings, "poly_signature_type",  0) if bot_settings else 0
    api_key    = getattr(bot_settings, "poly_api_key",        "") if bot_settings else ""
    api_secret = getattr(bot_settings, "poly_api_secret",     "") if bot_settings else ""
    api_pass   = getattr(bot_settings, "poly_api_passphrase", "") if bot_settings else ""
    await _client_instance.initialise(
        key=key, funder=funder, sig_type=sig_type,
        api_key=api_key, api_secret=api_secret, api_passphrase=api_pass,
    )
    return _client_instance
