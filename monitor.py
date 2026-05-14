#!/usr/bin/env python3
"""
Aerodrome CL Position Monitor
==============================
Monitors your Aerodrome concentrated liquidity position on Base.
Sends Telegram alerts when:
  - Your position goes out of range (earning zero fees)
  - Your position value drops more than BALANCE_DROP_THRESHOLD_PCT% below initial deposit
"""

import os
import json
import math
import sys
import requests
from web3 import Web3
from datetime import datetime, timezone, timedelta

# ── Configuration (set via GitHub Secrets / Variables) ─────────────────────
RPC_URL      = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
TOKEN_ID     = int(os.environ["POSITION_TOKEN_ID"])
TG_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
DROP_PCT     = float(os.environ.get("BALANCE_DROP_THRESHOLD_PCT", "15"))
INITIAL_USD  = float(os.environ["INITIAL_VALUE_USD"])
COOLDOWN_HRS = float(os.environ.get("ALERT_COOLDOWN_HOURS", "4"))
STATE_FILE   = "state.json"

# ── Aerodrome Base Contracts ────────────────────────────────────────────────
# Double-check these at https://velodrome.finance/security or Aerodrome GitHub
# Aerodrome is a Velodrome v2 fork — CL uses Uniswap v3-style position NFTs
NFPM_ADDR    = Web3.to_checksum_address("0x827922686190790b37229fd06084350e74485b72")
FACTORY_ADDR = Web3.to_checksum_address("0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A")

# ── Minimal ABIs ────────────────────────────────────────────────────────────
NFPM_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
        "name": "positions",
        "outputs": [
            {"internalType": "uint96",  "name": "nonce",                       "type": "uint96"},
            {"internalType": "address", "name": "operator",                    "type": "address"},
            {"internalType": "address", "name": "token0",                      "type": "address"},
            {"internalType": "address", "name": "token1",                      "type": "address"},
            {"internalType": "int24",   "name": "tickSpacing",                 "type": "int24"},
            {"internalType": "int24",   "name": "tickLower",                   "type": "int24"},
            {"internalType": "int24",   "name": "tickUpper",                   "type": "int24"},
            {"internalType": "uint128", "name": "liquidity",                   "type": "uint128"},
            {"internalType": "uint256", "name": "feeGrowthInside0LastX128",    "type": "uint256"},
            {"internalType": "uint256", "name": "feeGrowthInside1LastX128",    "type": "uint256"},
            {"internalType": "uint128", "name": "tokensOwed0",                 "type": "uint128"},
            {"internalType": "uint128", "name": "tokensOwed1",                 "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

FACTORY_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "", "type": "address"},
            {"internalType": "address", "name": "", "type": "address"},
            {"internalType": "int24",   "name": "", "type": "int24"},
        ],
        "name": "getPool",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]

POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96",              "type": "uint160"},
            {"internalType": "int24",   "name": "tick",                      "type": "int24"},
            {"internalType": "uint16",  "name": "observationIndex",          "type": "uint16"},
            {"internalType": "uint16",  "name": "observationCardinality",    "type": "uint16"},
            {"internalType": "uint16",  "name": "observationCardinalityNext","type": "uint16"},
            {"internalType": "bool",    "name": "unlocked",                  "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

ERC20_ABI = [
    {"inputs": [], "name": "decimals", "outputs": [{"internalType": "uint8",  "name": "", "type": "uint8"}],  "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol",   "outputs": [{"internalType": "string", "name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
]

# ── State helpers ───────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_oor_alert": None, "last_bal_alert": None}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def should_alert(last_alert_iso: str | None, cooldown_hours: float) -> bool:
    """Returns True if we've never alerted OR cooldown has elapsed."""
    if last_alert_iso is None:
        return True
    last = datetime.fromisoformat(last_alert_iso)
    return datetime.now(timezone.utc) - last > timedelta(hours=cooldown_hours)

# ── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    if r.ok:
        print("  → Telegram alert sent ✓")
    else:
        print(f"  → Telegram error: {r.status_code} {r.text}", file=sys.stderr)

# ── Price (DeFiLlama — free, no API key) ──────────────────────────────────────
def get_price_usd(token_address: str) -> float:
    key = f"base:{token_address.lower()}"
    try:
        r = requests.get(f"https://coins.llama.fi/prices/current/{key}", timeout=10)
        return float(r.json()["coins"].get(key, {}).get("price", 0))
    except Exception as e:
        print(f"  Price fetch failed for {token_address}: {e}", file=sys.stderr)
        return 0.0

# ── Uniswap v3 liquidity math ────────────────────────────────────────────────
Q96 = 2 ** 96

def sqrtx96_to_float(sqrtx96: int) -> float:
    return sqrtx96 / Q96

def tick_to_sqrt(tick: int) -> float:
    return math.sqrt(1.0001 ** tick)

def amounts_from_liquidity(
    liquidity: int,
    sqrt_current: float,
    sqrt_lower: float,
    sqrt_upper: float,
) -> tuple[float, float]:
    """
    Returns (amount0_raw, amount1_raw) — before decimal adjustment.
    Based on Uniswap v3 whitepaper equations 6.29 & 6.30.
    """
    if liquidity == 0:
        return 0.0, 0.0
    if sqrt_current <= sqrt_lower:
        # Price below range — all token0
        amount0 = liquidity * (1 / sqrt_lower - 1 / sqrt_upper)
        amount1 = 0.0
    elif sqrt_current >= sqrt_upper:
        # Price above range — all token1
        amount0 = 0.0
        amount1 = liquidity * (sqrt_upper - sqrt_lower)
    else:
        # In range — mixed
        amount0 = liquidity * (1 / sqrt_current - 1 / sqrt_upper)
        amount1 = liquidity * (sqrt_current - sqrt_lower)
    return amount0, amount1

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}] Running Aerodrome monitor…")

    # Connect to Base
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print("ERROR: Cannot connect to Base RPC.", file=sys.stderr)
        sys.exit(1)
    print(f"  Connected to Base (block #{w3.eth.block_number:,})")

    # Fetch position NFT data
    nfpm = w3.eth.contract(address=NFPM_ADDR, abi=NFPM_ABI)
    pos  = nfpm.functions.positions(TOKEN_ID).call()
    (_, _, token0, token1, tick_spacing,
     tick_lower, tick_upper, liquidity,
     _, _, tokens_owed0, tokens_owed1) = pos

    # Token metadata
    t0   = w3.eth.contract(address=token0, abi=ERC20_ABI)
    t1   = w3.eth.contract(address=token1, abi=ERC20_ABI)
    sym0 = t0.functions.symbol().call()
    dec0 = t0.functions.decimals().call()
    sym1 = t1.functions.symbol().call()
    dec1 = t1.functions.decimals().call()
    print(f"  Position #{TOKEN_ID}: {sym0}/{sym1} | tickSpacing={tick_spacing}")

    # Resolve pool and get current tick
    factory   = w3.eth.contract(address=FACTORY_ADDR, abi=FACTORY_ABI)
    pool_addr = factory.functions.getPool(token0, token1, tick_spacing).call()
    pool      = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI)
    slot0     = pool.functions.slot0().call()
    sqrt_price_x96, current_tick = slot0[0], slot0[1]

    # In-range check
    in_range = tick_lower <= current_tick < tick_upper
    print(f"  Tick: current={current_tick:,} | range=[{tick_lower:,} → {tick_upper:,}] | in_range={in_range}")

    # Calculate token amounts from liquidity position
    sqrt_current = sqrtx96_to_float(sqrt_price_x96)
    sqrt_lower_f = tick_to_sqrt(tick_lower)
    sqrt_upper_f = tick_to_sqrt(tick_upper)
    raw0, raw1   = amounts_from_liquidity(liquidity, sqrt_current, sqrt_lower_f, sqrt_upper_f)

    # Add unclaimed fees (tokensOwed)
    raw0 += tokens_owed0
    raw1 += tokens_owed1

    # Adjust for token decimals
    amount0 = raw0 / (10 ** dec0)
    amount1 = raw1 / (10 ** dec1)

    # Fetch prices and compute USD value
    price0      = get_price_usd(token0)
    price1      = get_price_usd(token1)
    value0_usd  = amount0 * price0
    value1_usd  = amount1 * price1
    current_usd = value0_usd + value1_usd
    pnl_pct     = ((current_usd - INITIAL_USD) / INITIAL_USD * 100) if INITIAL_USD > 0 else 0
    dropped_pct = max(0.0, -pnl_pct)
    balance_alert_triggered = dropped_pct >= DROP_PCT

    print(f"  {sym0}: {amount0:.4f} (${value0_usd:.2f})")
    print(f"  {sym1}: {amount1:.4f} (${value1_usd:.2f})")
    print(f"  Total: ${current_usd:.2f} | Initial: ${INITIAL_USD:.2f} | PnL: {pnl_pct:+.2f}%")

    # ── Build Telegram message ─────────────────────────────────────────────
    range_line = "🟢 IN RANGE — earning fees" if in_range else "🔴 OUT OF RANGE — earning zero fees"
    pnl_icon   = "📈" if pnl_pct >= 0 else "📉"
    now_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    base_summary = (
        f"<b>Aerodrome CL Monitor</b>\n"
        f"Position #{TOKEN_ID}  •  {sym0} / {sym1}\n"
        f"──────────────────────\n"
        f"{range_line}\n"
        f"Tick <code>{current_tick:,}</code>  |  Range <code>[{tick_lower:,} → {tick_upper:,}]</code>\n\n"
        f"<b>Holdings</b>\n"
        f"  {amount0:.4f} {sym0}  ≈  <b>${value0_usd:,.2f}</b>\n"
        f"  {amount1:.4f} {sym1}  ≈  <b>${value1_usd:,.2f}</b>\n"
        f"  Total  →  <b>${current_usd:,.2f}</b>\n\n"
        f"<b>vs Initial ${INITIAL_USD:,.2f}</b>\n"
        f"  {pnl_icon}  {pnl_pct:+.2f}%"
        f"  ({'+' if pnl_pct >= 0 else ''}${current_usd - INITIAL_USD:,.2f})\n\n"
        f"<i>{now_str}</i>"
    )

    # ── Fire alerts ────────────────────────────────────────────────────────
    state   = load_state()
    now_iso = datetime.now(timezone.utc).isoformat()
    fired   = False

    if not in_range and should_alert(state.get("last_oor_alert"), COOLDOWN_HRS):
        msg = (
            f"⚠️ <b>OUT OF RANGE ALERT</b>\n\n"
            f"{base_summary}\n\n"
            f"Your position is outside its price range and earning <b>zero fees</b>.\n"
            f"Consider withdrawing and redeploying at the current price."
        )
        send_telegram(msg)
        state["last_oor_alert"] = now_iso
        fired = True

    if in_range:
        # Reset out-of-range cooldown so next time it goes OOR we alert immediately
        state["last_oor_alert"] = None

    if balance_alert_triggered and should_alert(state.get("last_bal_alert"), COOLDOWN_HRS):
        msg = (
            f"🚨 <b>BALANCE DROP ALERT</b>\n\n"
            f"Position value has dropped <b>{dropped_pct:.1f}%</b> "
            f"below your initial deposit of ${INITIAL_USD:,.2f}.\n\n"
            f"{base_summary}"
        )
        send_telegram(msg)
        state["last_bal_alert"] = now_iso
        fired = True

    if not fired:
        print("  No alerts triggered this run.")

    save_state(state)
    print("  State saved. Done.")

if __name__ == "__main__":
    main()
