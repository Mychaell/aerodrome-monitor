# Aerodrome CL Position Monitor

Monitors your [Aerodrome Finance](https://aerodrome.finance) concentrated liquidity position on Base and pings you on Telegram when things go wrong.

**Alerts you when:**
- 🔴 Your position goes **out of range** (earning zero fees)
- 🚨 Your position value **drops below a threshold** vs your initial deposit

**Runs entirely free** via GitHub Actions on a 10-minute schedule. No servers, no hosting.

---

## Setup (15 minutes)

### Step 1 — Fork / create the repo

Create a **public** GitHub repo and push these files into it.

> ⚠️ Use a **public** repo. Private repos have a 2,000 min/month limit on the free GitHub plan, which this will exceed. Your secrets (API keys) are always encrypted regardless of repo visibility.

---

### Step 2 — Create your Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy the **bot token** it gives you (looks like `7123456789:AAF...`)
4. Start a chat with your new bot (search its username and hit Start)
5. Get your **chat ID** by visiting this URL in a browser (replace `YOUR_TOKEN`):
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
   Send your bot any message first, then open that URL. Look for `"chat":{"id": 123456789}` — that number is your chat ID.

---

### Step 3 — Get a Base RPC URL (free)

Sign up at [Alchemy](https://alchemy.com), create a new app on **Base Mainnet**, and copy the HTTPS URL. Alternatively, use the public Base RPC: `https://mainnet.base.org` (less reliable but free with no signup).

---

### Step 4 — Find your Position Token ID

1. Go to [aerodrome.finance](https://aerodrome.finance) and connect your wallet
2. Navigate to your liquidity position
3. The URL or position details will show your **NFT token ID** (a number like `12345`)

---

### Step 5 — Set GitHub Secrets and Variables

In your repo, go to **Settings → Secrets and variables → Actions**.

#### Secrets (encrypted — for sensitive values)

| Name | Value |
|------|-------|
| `BASE_RPC_URL` | Your Alchemy Base RPC URL |
| `TELEGRAM_BOT_TOKEN` | Your bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Your numeric chat ID |

#### Variables (plain text — for config values)

| Name | Example | Description |
|------|---------|-------------|
| `POSITION_TOKEN_ID` | `12345` | Your Aerodrome CL NFT token ID |
| `INITIAL_VALUE_USD` | `500` | How much USD you deposited initially |
| `BALANCE_DROP_THRESHOLD_PCT` | `15` | Alert if value drops this % below initial |
| `ALERT_COOLDOWN_HOURS` | `4` | Hours between repeat alerts for the same issue |

---

### Step 6 — Verify contract addresses

Before running, confirm the Aerodrome CL contract addresses in `monitor.py` match what's listed at:
- https://aerodrome.finance/security
- https://github.com/aerodrome-finance

The addresses in `monitor.py` are:
```
NFPM_ADDR    = 0x827922686190790b37229fd06084350E74485b6
FACTORY_ADDR = 0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A
```

If Aerodrome has updated their contracts, update these lines in `monitor.py`.

---

### Step 7 — Run it manually to test

Go to **Actions → 🔍 Aerodrome Position Monitor → Run workflow**. Check the logs. If everything is configured correctly, you'll see your position details printed and receive a Telegram message if any alert condition is triggered.

---

## How it works

```
GitHub Actions (cron every 10 min)
        │
        ▼
monitor.py
  ├── Reads your CL position NFT from Aerodrome's NonfungiblePositionManager
  ├── Gets current pool tick from the pool contract (slot0)
  ├── Checks: is current tick inside [tickLower, tickUpper]?
  ├── Calculates token amounts using Uniswap v3 liquidity math
  ├── Adds unclaimed fees (tokensOwed0, tokensOwed1)
  ├── Fetches USD prices from DeFiLlama (free, no key needed)
  ├── Compares current value vs INITIAL_VALUE_USD
  └── Fires Telegram alerts if conditions are met
        │
        ▼
state.json (committed back to repo)
  Tracks last alert times to prevent spam
  (re-alerts every ALERT_COOLDOWN_HOURS if condition persists)
```

---

## Notes

- **Out-of-range cooldown resets** when your position comes back in range, so you'll get alerted immediately the next time it goes out.
- **Unclaimed fees** are included in the balance calculation.
- The script uses [DeFiLlama's price API](https://defillama.com/docs/api) — completely free, no API key.
- The `[skip ci]` tag on state commits prevents the commit from triggering another workflow run.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `KeyError: POSITION_TOKEN_ID` | Make sure it's set under Variables (not Secrets) |
| `Cannot connect to Base RPC` | Try replacing with `https://mainnet.base.org` |
| No Telegram message | Verify bot token and chat ID; make sure you messaged the bot first |
| Wrong token amounts | Verify NFPM contract address against Aerodrome's current docs |
