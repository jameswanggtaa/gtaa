"""Poll loop for equity SMA (legacy) or short OTM call writer.

Usage:
  python -m schwab_bot.bot --once
  STRATEGY=short_otm_call DRY_RUN=true python -m schwab_bot
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

from schwab_bot.auth import auth_from_env
from schwab_bot.chain import find_short_call_position
from schwab_bot.client import SchwabClient
from schwab_bot.orders import (
    close_short_call_debit,
    equity_limit,
    short_call_credit,
)
from schwab_bot.risk import (
    RiskLimits,
    approve,
    approve_cover_shares,
    approve_short_call,
)
from schwab_bot.strategy import (
    ShortCallConfig,
    decide_short_call,
    held_quantity,
    signal_from_bars,
)

LOG = logging.getLogger("schwab_bot")


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if not load_dotenv("schwab_bot/.env"):
        load_dotenv(".env")


def build_client() -> SchwabClient:
    account_hash = os.environ.get("SCHWAB_ACCOUNT_HASH", "").strip()
    if not account_hash:
        raise SystemExit(
            "SCHWAB_ACCOUNT_HASH is required. "
            "Run: python -m schwab_bot.list_accounts  (after auth is configured)"
        )
    return SchwabClient(auth_from_env(), account_hash)


def _submit(client: SchwabClient, order: dict, *, dry_run: bool, label: str) -> None:
    if dry_run:
        LOG.info("DRY_RUN %s: %s", label, order)
        return
    resp = client.place_order(order)
    location = resp.headers.get("Location", "")
    LOG.info(
        "live %s status=%s location=%s body=%s",
        label,
        resp.status_code,
        location,
        resp.text[:500],
    )
    if resp.status_code not in {200, 201}:
        resp.raise_for_status()


def _dte_window(target_dte: int, tolerance: int) -> tuple[str, str]:
    today = date.today()
    start = today + timedelta(days=max(1, target_dte - tolerance))
    end = today + timedelta(days=target_dte + tolerance)
    return start.isoformat(), end.isoformat()


def run_short_otm_call(
    client: SchwabClient,
    *,
    dry_run: bool,
    symbol: str,
    contracts: int,
    cfg: ShortCallConfig,
    limits: RiskLimits,
) -> None:
    from_date, to_date = _dte_window(cfg.target_dte, cfg.dte_tolerance)
    chain = client.option_chain(
        symbol,
        contract_type="CALL",
        strike_count=30,
        from_date=from_date,
        to_date=to_date,
        range_="OTM",
    )
    positions = client.positions()
    short_pos = find_short_call_position(positions, symbol)
    decision = decide_short_call(
        chain=chain,
        positions=positions,
        underlying=symbol,
        cfg=cfg,
        short_call_pos=short_pos,
    )
    underlying_px = float(chain.get("underlyingPrice") or 0)
    LOG.info(
        "%s action=%s underlying=%.2f held=%.0f short=%s reason=%s",
        symbol,
        decision.action,
        underlying_px,
        held_quantity(positions, symbol),
        bool(short_pos),
        decision.reason,
    )

    if decision.action == "HOLD":
        return

    if decision.action == "OPEN_SHORT_CALL":
        assert decision.contract is not None
        ok, reason = approve_short_call(decision.contract, contracts, limits)
        if not ok:
            LOG.warning("risk rejected short call: %s", reason)
            return
        credit = decision.contract.bid if decision.contract.bid > 0 else decision.contract.mid
        order = short_call_credit(decision.contract.symbol, contracts, round(credit, 2))
        _submit(client, order, dry_run=dry_run, label="SELL_TO_OPEN call")
        return

    if decision.action == "COVER_AND_ROLL":
        ok, reason = approve_cover_shares(decision.buy_shares, underlying_px, limits)
        if not ok:
            LOG.warning("risk rejected cover shares: %s", reason)
            return
        if decision.buy_shares > 0 and underlying_px > 0:
            stock_order = equity_limit(
                symbol, "BUY", decision.buy_shares, round(underlying_px, 2)
            )
            _submit(client, stock_order, dry_run=dry_run, label="BUY shares to cover")

        if decision.close_option_symbol and decision.close_option_qty > 0:
            debit = 0.0
            if decision.contract and decision.contract.ask > 0:
                debit = decision.contract.ask
            elif decision.contract:
                debit = decision.contract.mid
            if debit <= 0:
                LOG.warning("no debit price to BTC %s — skipping close", decision.close_option_symbol)
            else:
                close_order = close_short_call_debit(
                    decision.close_option_symbol,
                    decision.close_option_qty,
                    round(debit, 2),
                )
                _submit(client, close_order, dry_run=dry_run, label="BUY_TO_CLOSE call")

        if decision.roll_to is None:
            LOG.warning("cover done but no roll target")
            return
        ok, reason = approve_short_call(decision.roll_to, decision.close_option_qty or contracts, limits)
        if not ok:
            LOG.warning("risk rejected roll short call: %s", reason)
            return
        credit = decision.roll_to.bid if decision.roll_to.bid > 0 else decision.roll_to.mid
        open_order = short_call_credit(
            decision.roll_to.symbol,
            decision.close_option_qty or contracts,
            round(credit, 2),
        )
        _submit(client, open_order, dry_run=dry_run, label="SELL_TO_OPEN roll call")


def run_equity_sma(
    client: SchwabClient,
    *,
    dry_run: bool,
    symbol: str,
    qty: int,
    limits: RiskLimits,
) -> None:
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
    _submit(client, order, dry_run=dry_run, label=f"{sig} equity")


def sync_recent_orders(client: SchwabClient) -> None:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1)
    orders = client.orders(
        start.isoformat().replace("+00:00", "Z"),
        now.isoformat().replace("+00:00", "Z"),
    )
    LOG.info("orders last 24h: %d", len(orders) if isinstance(orders, list) else 0)


def _short_call_config_from_env() -> ShortCallConfig:
    return ShortCallConfig(
        target_dte=int(os.getenv("SCHWAB_TARGET_DTE", "35")),
        dte_tolerance=int(os.getenv("SCHWAB_DTE_TOLERANCE", "7")),
        max_delta=float(os.getenv("SCHWAB_MAX_DELTA", "0.30")),
        manage_delta=float(os.getenv("SCHWAB_MANAGE_DELTA", "0.40")),
        min_iv=float(os.getenv("SCHWAB_MIN_IV", "15")),
        max_iv=float(os.getenv("SCHWAB_MAX_IV", "45")),
        min_open_interest=int(os.getenv("SCHWAB_MIN_OI", "100")),
        min_volume=int(os.getenv("SCHWAB_MIN_VOLUME", "10")),
        max_spread_pct=float(os.getenv("SCHWAB_MAX_SPREAD_PCT", "0.15")),
    )


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    dry_run = _env_bool("DRY_RUN", True)
    strategy = os.getenv("STRATEGY", "short_otm_call").strip().lower()
    symbols = [
        s.strip().upper()
        for s in os.getenv("SCHWAB_SYMBOLS", os.getenv("SCHWAB_SYMBOL", "SPY")).split(",")
        if s.strip()
    ]
    qty = int(os.getenv("SCHWAB_QTY", "1"))
    contracts = int(os.getenv("SCHWAB_CONTRACTS", "1"))
    poll_sec = int(os.getenv("SCHWAB_POLL_SEC", "60"))
    once = "--once" in (argv or sys.argv[1:])

    limits = RiskLimits(
        max_notional=float(os.getenv("SCHWAB_MAX_NOTIONAL", "2000")),
        max_shares=int(os.getenv("SCHWAB_MAX_SHARES", "50")),
        max_open_positions=int(os.getenv("SCHWAB_MAX_OPEN_POSITIONS", "3")),
        allow_short=_env_bool("SCHWAB_ALLOW_SHORT", False),
        max_short_contracts=int(os.getenv("SCHWAB_MAX_SHORT_CONTRACTS", "2")),
        max_cover_notional=float(os.getenv("SCHWAB_MAX_COVER_NOTIONAL", "50000")),
    )
    cfg = _short_call_config_from_env()

    if not dry_run:
        LOG.warning("DRY_RUN=false — live orders will be submitted to Schwab")
    LOG.info("strategy=%s symbols=%s dry_run=%s", strategy, symbols, dry_run)

    client = build_client()

    while True:
        try:
            for symbol in symbols:
                if strategy in {"short_otm_call", "short_call", "otm_call"}:
                    run_short_otm_call(
                        client,
                        dry_run=dry_run,
                        symbol=symbol,
                        contracts=contracts,
                        cfg=cfg,
                        limits=limits,
                    )
                elif strategy in {"equity_sma", "sma"}:
                    run_equity_sma(
                        client,
                        dry_run=dry_run,
                        symbol=symbol,
                        qty=qty,
                        limits=limits,
                    )
                else:
                    raise SystemExit(f"unknown STRATEGY={strategy}")
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
