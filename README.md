# Polymarket Copy Bot - Pre-Test-Phase!

A production-ready copy-trading bot for [Polymarket](https://polymarket.com).  
Monitors a target wallet in near real-time, mirrors its trades, auto-claims winnings, and tracks full PNL — with a clean dark-mode web UI served on `http://localhost:2301`.
Screenshots at wiki.

It has not been testet properly as of 17.04.2026, use at your own risk! Paper-Trading-Mode available!
Disclaimer: The Bot has a royalty use, 1% of pure profits each 30 days will be transfered as USDC to the creator.
(not in paper mode and not if there are no profits)

---

## Features

- **Copy trading**: Poll a target wallet and mirror every trade automatically
- **Paper / Live modes**: Simulate first, then enable live trading with a checkbox
- **Flexible sizing**: Copy as a % of source trade size, or a fixed USDC amount
- **Risk limits**: Max exposure %, per-trade USDC cap, market blacklist
- **Auto-claim**: Automatically redeems winning tokens when markets resolve
- **PNL tracking**: Realized + unrealized PNL per position, refreshed every 60 s
- **Web UI**: Responsive dark-mode dashboard with HTMX live refresh (no React)
- **Webhook alerts**: Telegram / Discord notifications on every trade, claim, or error
- **Docker-ready**: Single container, non-root user, SQLite volume

---

## Quick Start (local)

### 1. Prerequisites

- Python 3.12+
- A Polygon wallet with USDC.e (for live trading)
- POL (MATIC) for gas fees (for live trading)

### 2. Install

```bash
# Clone / copy project
cd pm_copy

# Create virtual environment
python -m venv .venv
.venv\Scripts\activate      # Windows
# or: source .venv/bin/activate  (Linux/macOS)

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure

```bash
copy .env.example .env      # Windows
# or: cp .env.example .env  (Linux/macOS)
```

Edit `.env` and set at minimum:
```
POLY_PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
```

### 4. Run

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 2301 --reload
```

Open **http://localhost:2301** in your browser.

---

## Web UI Pages

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `/` | Live positions, PNL cards, bot status |
| Trades | `/trades` | Full trade history with filters + CSV export |
| Logs | `/logs` | Live scrolling log viewer |
| Settings | `/settings` | All bot configuration |
| Health | `/health` | JSON liveness probe |

---

## First-time Live Trading Setup

1. Go to **Settings** and set your **Target Wallet Address**
2. Keep **Paper Trading** checked and hit **Save**
3. Click **Set Allowances** (approves USDC.e + CTF tokens on-chain — one-time, requires POL gas)
4. Once confirmed, uncheck **Paper Trading**, save, and start the bot

---

## Docker Deployment

### Build the image

```bash
docker build -t pm-copy-bot .
```

### Run (local)

```bash
docker run -d \
  --name pm-copy-bot \
  --restart unless-stopped \
  -p 2301:2301 \
  -v $(pwd)/data:/app/data \
  --env-file .env \
  pm-copy-bot
```

---

## Unraid Deployment

### Step 1 — Add the image

In Unraid, go to **Docker → Add Container** and fill in:

| Field | Value |
|-------|-------|
| Name | `pm-copy-bot` |
| Repository | `pm-copy-bot` (after building locally) or your registry image |
| Network Type | `bridge` |
| Port (host → container) | `2301:2301` |

### Step 2 — Map the data volume

Add a **Path** mapping:

| Field | Value |
|-------|-------|
| Container Path | `/app/data` |
| Host Path | `/mnt/user/appdata/pm-copy-bot` |
| Access Mode | Read/Write |

### Step 3 — Set environment variables

Add each variable from `.env.example` under **Environment Variables**:

```
POLY_PRIVATE_KEY  →  0xYOUR_KEY
POLY_FUNDER_ADDRESS  →  0xYOUR_ADDRESS
POLYGON_RPC  →  https://polygon-rpc.com
LOG_LEVEL  →  INFO
```

### Step 4 — Apply and open

Click **Apply**. Once running, open:
```
http://YOUR_UNRAID_IP:2301
```

### Step 5 — One-command docker run equivalent (for CLI on Unraid)

```bash
docker run -d \
  --name pm-copy-bot \
  --restart unless-stopped \
  -p 2301:2301 \
  -v /mnt/user/appdata/pm-copy-bot:/app/data \
  -e POLY_PRIVATE_KEY="0xYOUR_PRIVATE_KEY" \
  -e POLY_FUNDER_ADDRESS="0xYOUR_ADDRESS" \
  -e POLY_SIGNATURE_TYPE="0" \
  -e POLYGON_RPC="https://polygon-rpc.com" \
  -e LOG_LEVEL="INFO" \
  pm-copy-bot
```

---

## Configuration Reference

All settings are either environment variables (secrets/infrastructure) or stored in the web UI (bot behaviour).

### Environment Variables (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `POLY_PRIVATE_KEY` | — | **Required.** Your Polygon wallet private key |
| `POLY_FUNDER_ADDRESS` | auto-derived | Wallet address holding USDC.e |
| `POLY_SIGNATURE_TYPE` | `0` | `0`=EOA, `1`=Magic, `2`=Safe |
| `POLYGON_RPC` | `https://polygon-rpc.com` | Polygon JSON-RPC endpoint |
| `DATABASE_URL` | `sqlite+aiosqlite:///data/bot.db` | SQLite path |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

### UI Settings

| Setting | Description |
|---------|-------------|
| Target Wallet | The Polymarket wallet address to copy |
| Paper Trading | Simulate without placing real orders |
| Sizing Mode | Percentage or Fixed USDC per trade |
| Copy % | Percentage of source trade size to copy |
| Min Trade USDC | Minimum trade size in percentage mode |
| Fixed USDC | Fixed USDC per trade in fixed mode |
| Poll Interval | How often to check target wallet (min 5 s) |
| Max Exposure % | Don't deploy more than X% of balance |
| Per-trade Cap | Maximum USDC per single trade |
| Blacklist | Comma-separated conditionIds to skip |
| Webhook URL | Telegram/Discord URL for notifications |

---

## Architecture

```
FastAPI app (main.py)
├── Lifespan: DB init → Polymarket client init → Scheduler start
│
├── Routers
│   ├── GET /              Dashboard (full page + HTMX partials)
│   ├── GET/POST /settings Settings management
│   ├── GET /trades        Trade history + CSV export
│   ├── GET /logs          Live log viewer
│   └── GET /health        Liveness probe
│
├── Scheduler (APScheduler)
│   ├── poll_and_copy      Every N seconds — fetch + mirror trades
│   ├── auto_claim         Every 5 min — redeem resolved positions
│   └── refresh_pnl        Every 60 s — update unrealized PNL prices
│
└── Services
    ├── polymarket_client  py-clob-client wrapper + Web3 allowances
    ├── monitor            Data API polling for target wallet
    ├── trader             Copy-trade logic (size calc, order placement)
    ├── claimer            On-chain redeemPositions calls
    ├── pnl                Unrealized PNL refresh via midpoint prices
    └── notifier           Telegram/Discord webhook dispatch
```

---

## Security Notes

- The private key is **only read from `.env`** — never written to the database
- All orders are signed locally — Polymarket never holds your keys
- For production: use a hardware wallet or HSM, not a raw private key in `.env`
- Restrict port 2301 to your local network — the UI has no authentication

---

## Disclaimer

This software is provided as-is for educational and personal use.  
Prediction market trading involves significant financial risk.  
Always test in paper mode before enabling live trading.
