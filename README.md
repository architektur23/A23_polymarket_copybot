# Polymarket Copy Bot - Pre-Test-Phase!

A copy-trading bot for [Polymarket](https://polymarket.com).  
Monitors a target wallet in near real-time, mirrors its trades, auto-claims winnings, and tracks full PNL — with a clean dark-mode web UI on port `2301`.
Screenshots at wiki.

It has not been tested properly as of 17.04.2026, use at your own risk! Paper-Trading-Mode available!
Disclaimer: The Bot has a royalty system — 1% of net profits every 30 days will be transferred as USDC to the creator.
(not in paper mode and not if there are no profits)

---

## Features

- **Copy trading** — polls a target wallet and mirrors every trade automatically
- **Paper / Live modes** — simulate first, then enable live trading with a checkbox
- **Proportional or fixed sizing** — mirror the same % of equity as the source wallet, or use a flat USDC amount per trade
- **Risk limits** — max exposure %, per-trade USDC cap, market blacklist
- **Auto-claim** — automatically redeems winning tokens when markets resolve
- **PNL tracking** — realized + unrealized PNL per position, refreshed every 60 s
- **Web UI** — responsive dark-mode dashboard with live refresh (no React)
- **Webhook alerts** — Telegram / Discord notifications on every trade, claim, or error
- **Docker-ready** — single container, non-root user, SQLite volume

---

## Quick Start (local)

### 1. Prerequisites

- Python 3.12+
- A Polygon wallet with pUSD (for live trading)
- POL for gas fees (for live trading)

### 2. Install

```bash
git clone https://github.com/architektur23/A23_polymarket_copybot.git
cd A23_polymarket_copybot

python -m venv .venv
.venv\Scripts\activate        # Windows
# or: source .venv/bin/activate  (Linux/macOS)

pip install -r requirements.txt
```

### 3. Run

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 2301 --reload
```

Open **http://localhost:2301** in your browser and go to **Settings** to enter your credentials.

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

## First-time Setup

1. Open **Settings** and enter your **Private Key** and **Target Wallet Address**
2. Keep **Paper Trading** enabled and click **Save**
3. Click **Set Allowances** (approves pUSD + CTF tokens on-chain — one-time, requires POL gas)
4. Once confirmed, disable **Paper Trading**, save, and start the bot

---

## Docker Deployment

### Build

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
  pm-copy-bot
```

---

## Unraid Deployment

### Step 1 — Open a terminal

In the Unraid UI go to **Tools → Terminal** (or SSH into your server).

### Step 2 — Clone the repo and build the image

```bash
cd /mnt/user/appdata
git clone https://github.com/architektur23/A23_polymarket_copybot.git pm-copy-bot-src
cd pm-copy-bot-src
docker build -t pm-copy-bot .
```

The build takes a few minutes (compiling Web3 dependencies).

### Step 3 — Create the data directory

```bash
mkdir -p /mnt/user/appdata/pm-copy-bot/logs
chown -R 1000:1000 /mnt/user/appdata/pm-copy-bot
```

### Step 4 — Run the container

```bash
docker run -d \
  --name pm-copy-bot \
  --restart unless-stopped \
  -p 2301:2301 \
  -v /mnt/user/appdata/pm-copy-bot:/app/data \
  pm-copy-bot
```

### Step 5 — Open the UI

```
http://YOUR_UNRAID_IP:2301
```

Go to **Settings** to enter your Target Wallet and configure the bot.

---

### Updating after a new release

```bash
cd /mnt/user/appdata/pm-copy-bot-src
git pull
docker build -t pm-copy-bot .
docker stop pm-copy-bot && docker rm pm-copy-bot
docker run -d \
  --name pm-copy-bot \
  --restart unless-stopped \
  -p 2301:2301 \
  -v /mnt/user/appdata/pm-copy-bot:/app/data \
  pm-copy-bot
```

Your database and logs in `/mnt/user/appdata/pm-copy-bot` are preserved across updates.

---

## Configuration

All bot settings are managed through the **Settings page** in the web UI — no config files to edit.

| Setting | Description |
|---------|-------------|
| Private Key | Your Polygon wallet private key (stored locally in the database) |
| Target Wallet | The Polymarket wallet address to copy |
| Paper Trading | Simulate without placing real orders |
| Simulated Equity | USDC balance used for sizing calculations in paper mode |
| Sizing Mode | Proportional (mirrors source wallet %) or Fixed USDC per trade |
| Min Trade USDC | Minimum trade size floor |
| Fixed USDC | Flat USDC amount per trade in fixed mode |
| Poll Interval | How often to check the target wallet (min 5 s) |
| Max Exposure % | Don't deploy more than X% of balance at once |
| Per-trade Cap | Maximum USDC per single trade (0 = disabled) |
| Blacklist | Comma-separated condition IDs to skip |
| Webhook URL | Telegram / Discord URL for notifications |

The only optional config file is `.env`, used to override infrastructure defaults (RPC URL, log level, database path). See `.env.example` for available options.

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
│   ├── refresh_pnl        Every 60 s — update unrealized PNL prices
│   └── collect_royalty    Every 30 days — transfer 1% of net profit
│
└── Services
    ├── polymarket_client  py-clob-client-v2 wrapper + Web3 transfers
    ├── monitor            Data API polling for target wallet
    ├── trader             Copy-trade logic (size calc, order placement)
    ├── claimer            On-chain redeemPositions calls
    ├── pnl                Unrealized PNL refresh via midpoint prices
    └── notifier           Telegram/Discord webhook dispatch
```

---

## Security Notes

- All credentials are stored locally in the SQLite database (`data/bot.db`) and never transmitted
- All orders are signed locally — Polymarket never holds your keys
- Use a dedicated trading wallet, not your main wallet
- Restrict port 2301 to your local network — the UI has no authentication

---

## Disclaimer

This software is provided as-is for educational and personal use.  
Prediction market trading involves significant financial risk.  
Always test in paper mode before enabling live trading.
