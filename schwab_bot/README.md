# Schwab Bot Scaffold

Minimal Charles Schwab Trader API bot with a **short OTM call** strategy:

> Write ~5-week OTM calls on liquid names (delta ≤ 0.30). If the short call is
> challenged (delta ≥ manage threshold or ITM), buy shares to cover and roll
> into a fresh OTM call.

Also includes a legacy equity SMA path (`STRATEGY=equity_sma`).

## Prerequisites

1. Schwab brokerage account with **options approval** high enough to sell calls
2. App approved on [developer.schwab.com](https://developer.schwab.com) → **Trader API – Individual**
3. Python 3.10+

## Strategy rules (defaults)

| Rule | Default | Env |
|------|---------|-----|
| Target DTE | ~35 days (~5 weeks) | `SCHWAB_TARGET_DTE` |
| DTE window | ±7 days | `SCHWAB_DTE_TOLERANCE` |
| Max entry delta | 0.30 | `SCHWAB_MAX_DELTA` |
| Manage / roll when delta ≥ | 0.40 (or ITM) | `SCHWAB_MANAGE_DELTA` |
| IV band (moderate vol) | 15–45 | `SCHWAB_MIN_IV` / `SCHWAB_MAX_IV` |
| Liquidity | OI≥100, vol≥10, spread≤15% of mid | `SCHWAB_MIN_OI`, etc. |

**Entry:** no short call → `SELL_TO_OPEN` best OTM call under delta cap  
**Manage:** challenged → buy 100 shares per short contract if uncovered → `BUY_TO_CLOSE` → `SELL_TO_OPEN` new OTM call

Keep `DRY_RUN=true` until you intentionally go live. The API submits to **live** accounts only (no paperMoney via API).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r schwab_bot/requirements.txt
cp schwab_bot/.env.example schwab_bot/.env
# edit schwab_bot/.env with APP_KEY, SECRET, CALLBACK_URL
```

### Authorize (one-time / every ~7 days)

```bash
python3 -m schwab_bot.authorize
```

### Discover account hash

```bash
python3 -m schwab_bot.list_accounts
```

Set `SCHWAB_ACCOUNT_HASH` from the printed hashed `accountNumber`.

## Run (dry-run by default)

```bash
# offline unit checks (no API)
python3 -m schwab_bot.test_scaffold

# one iteration — short OTM call strategy
python3 -m schwab_bot.bot --once

# continuous poll loop
python3 -m schwab_bot
```

## Layout

| Module | Role |
|--------|------|
| `auth.py` / `authorize.py` | OAuth refresh + browser login |
| `client.py` | Quotes, history, chains, accounts, orders |
| `chain.py` | Parse Schwab option-chain payloads |
| `strategy.py` | Short OTM call select + cover/roll decisions |
| `risk.py` | Notional / contract / cover caps |
| `orders.py` | Equity + option order payloads |
| `bot.py` | Poll loop |
| `list_accounts.py` | Print account hashes |

## Important limits

- Access token ~30 min; refresh token ~7 days (re-run `authorize`)
- API is live-only; use `DRY_RUN` + tiny size if you ever go live
- Naked / cash-secured short calls need the correct options level
- Cover-and-roll places **separate** orders (stock, BTC, STO) — not one multi-leg ticket yet
- Rate limits ~120 req/min — this poll loop stays well under that

## Not financial advice

This scaffold does not guarantee profitable trading, correct fills, or uninterrupted API access. You own risk controls, compliance, and any live orders.
