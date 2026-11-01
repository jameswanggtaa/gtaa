"""Poll loop: quote → signal → risk → (dry-run) order → position sync.

Usage:
  export $(grep -v '^#' schwab_bot/.env | xargs)   # or use python-dotenv
  python -m schwab_bot.bot
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from schwab_bot.auth import auth_from_env
from schwab_bot.client import SchwabClient
from schwab_bot.orders import equity_limit
from schwab_bot.risk import RiskLimits, approve
from schwab_bot.strategy import held_quantity, signal_from_bars

LOG = logging.getLogger("schwab_bot")


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _load_dotenv() -> None:
    """Load schwab_bot/.env if python-dotenv is installed; otherwise no-op."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # Prefer package-local .env, fall back to repo root .env
    loaded = load_dotenv("schwab_bot/.env")
    if not loaded:
        load_dotenv(".env")


def build_client() -> SchwabClient:
    account_hash = os.environ.get("SCHWAB_ACCOUNT_HASH", "").strip()
    if not account_hash:
        raise SystemExit(
            "SCHWAB_ACCOUNT_HASH is required. "
            "Run: python -m schwab_bot.list_accounts  (after auth is configured)"
        )
    return SchwabClient(auth_from_env(), account_hash)


def run_once(client: SchwabClient, *, dry_run: bool, symbol: str, qty: int, limits: RiskLimits) -> None:
    hist = client.price_history(symbol)
    candles = hist.get("candles") or []
    sig = signal_from_bars(candles)

    quote = client.quote(symbol)
    last = float(quote.get("quote", {}).get("lastPrice") or 0)
    positions = client.positions()
    held = held_quantity(positions, symbol)
    open_count = len(positions)

    LOG.info(
        "%s last=%.4f signal=%s held=%.0f positions=%d dry_run=%s",
        symbol,
        last,
        sig,
        held,
        open_count,
        dry_run,
    )

    if sig not in {"BUY", "SELL"} or last <= 0:
        return

    # Avoid churning: only BUY if flat, only SELL if long (unless shorts allowed)
    if sig == "BUY" and held > 0:
        LOG.info("skip BUY; already long %s", symbol)
        return
    if sig == "SELL" and held <= 0 and not limits.allow_short:
        LOG.info("skip SELL; not long %s", symbol)
        return

    ok, reason = approve(sig, qty, last, open_count, held, limits)
    if not ok:
        LOG.warning("risk rejected %s %s x%s @ %.2f: %s", sig, symbol, qty, last, reason)
        return

    order = equity_limit(symbol, sig, qty, round(last, 2))
    if dry_run:
        LOG.info("DRY_RUN order payload: %s", order)
        return

    resp = client.place_order(order)
    location = resp.headers.get("Location", "")
    LOG.info("live order status=%s location=%s body=%s", resp.status_code, location, resp.text[:500])
    if resp.status_code not in {200, 201}:
        resp.raise_for_status()


def sync_recent_orders(client: SchwabClient) -> None:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1)
    orders = client.orders(start.isoformat().replace("+00:00", "Z"), now.isoformat().replace("+00:00", "Z"))
    LOG.info("orders last 24h: %d", len(orders) if isinstance(orders, list) else 0)


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    dry_run = _env_bool("DRY_RUN", True)
    symbol = os.getenv("SCHWAB_SYMBOL", "SPY").upper()
    qty = int(os.getenv("SCHWAB_QTY", "1"))
    poll_sec = int(os.getenv("SCHWAB_POLL_SEC", "60"))
    once = "--once" in (argv or sys.argv[1:])

    limits = RiskLimits(
        max_notional=float(os.getenv("SCHWAB_MAX_NOTIONAL", "2000")),
        max_shares=int(os.getenv("SCHWAB_MAX_SHARES", "50")),
        max_open_positions=int(os.getenv("SCHWAB_MAX_OPEN_POSITIONS", "3")),
        allow_short=_env_bool("SCHWAB_ALLOW_SHORT", False),
    )

    if not dry_run:
        LOG.warning("DRY_RUN=false — live orders will be submitted to Schwab")

    client = build_client()

    while True:
        try:
            run_once(client, dry_run=dry_run, symbol=symbol, qty=qty, limits=limits)
            sync_recent_orders(client)
        except Exception:
            LOG.exception("loop iteration failed")
            if once:
                return 1
        if once:
            return 0
        time.sleep(poll_sec)


if __name__ == "__main__":
    raise SystemExit(main())
