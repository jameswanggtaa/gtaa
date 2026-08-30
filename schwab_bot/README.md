# Schwab Bot Scaffold

Minimal Charles Schwab Trader API bot: **auth → quote → signal → risk → order → position sync**.

This is a starting point for automated equity (and single-leg option) trading. The included signal is a toy SMA crossover — replace it before risking capital.

## Prerequisites

1. Schwab brokerage account
2. App approved on [developer.schwab.com](https://developer.schwab.com) → **Trader API – Individual**
3. Python 3.10+

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

Paste the redirect URL after Schwab login. Copy the printed `SCHWAB_REFRESH_TOKEN` into `.env`.

### Discover account hash

```bash
python3 -m schwab_bot.list_accounts
```

Set `SCHWAB_ACCOUNT_HASH` from the printed `accountNumber` (hashed value Schwab returns).

## Run (dry-run by default)

```bash
# offline unit checks (no API)
python3 -m schwab_bot.test_scaffold

# one iteration
python3 -m schwab_bot.bot --once

# continuous poll loop
python3 -m schwab_bot
```

`DRY_RUN=true` logs order payloads without submitting. Set `DRY_RUN=false` only when you intentionally want live orders.

## Layout

| Module | Role |
|--------|------|
| `auth.py` | OAuth refresh + token cache |
| `client.py` | Quotes, history, chains, accounts, orders |
| `strategy.py` | Toy SMA signal (replace this) |
| `risk.py` | Max notional / shares / position checks |
| `orders.py` | Equity + single-leg option payloads |
| `bot.py` | Poll loop |
| `authorize.py` | Browser OAuth helper |
| `list_accounts.py` | Print account hashes |

## Important limits

- Access token ~30 minutes; refresh token ~7 days (then re-run `authorize`)
- API talks to **live** accounts; sandbox is not paper trading
- Keep `DRY_RUN=true` while validating; use tiny size if you go live
- Options need the correct options approval level on the Schwab account
- Rate limits apply (on the order of ~120 req/min) — this poll loop stays well under that

## Not financial advice

This scaffold does not guarantee profitable trading, correct order routing, or uninterrupted API access. You are responsible for risk controls, compliance, and any live orders.
