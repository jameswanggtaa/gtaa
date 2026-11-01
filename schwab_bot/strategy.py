"""Strategy helpers: toy SMA (legacy) + short OTM call writer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from schwab_bot.chain import OptionContract, iter_contracts

Signal = Literal["BUY", "SELL", "HOLD"]


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window or window <= 0:
        return None
    return sum(values[-window:]) / window


def signal_from_bars(
    candles: list[dict[str, Any]],
    fast: int = 5,
    slow: int = 20,
) -> Signal:
    """Legacy SMA crossover — kept for reference / equity dry-runs."""
    closes = [float(c["close"]) for c in candles if "close" in c]
    fast_ma = sma(closes, fast)
    slow_ma = sma(closes, slow)
    if fast_ma is None or slow_ma is None:
        return "HOLD"
    if fast_ma > slow_ma:
        return "BUY"
    if fast_ma < slow_ma:
        return "SELL"
    return "HOLD"


def held_quantity(positions: list[dict[str, Any]], symbol: str) -> float:
    """Return long quantity for an equity symbol from Schwab positions payload."""
    for pos in positions:
        instrument = pos.get("instrument") or {}
        if instrument.get("symbol") == symbol and instrument.get("assetType") == "EQUITY":
            return float(pos.get("longQuantity") or 0)
    return 0.0


@dataclass(frozen=True)
class ShortCallConfig:
    """Rules for writing ~5-week OTM calls and repairing when challenged."""

    target_dte: int = 35
    dte_tolerance: int = 7
    max_delta: float = 0.30
    # When short-call delta rises above this, buy stock (if needed) and roll.
    manage_delta: float = 0.40
    min_iv: float = 15.0
    max_iv: float = 45.0
    min_open_interest: int = 100
    min_volume: int = 10
    max_spread_pct: float = 0.15
    prefer_highest_delta_under_cap: bool = True


@dataclass(frozen=True)
class ShortCallDecision:
    action: Literal["HOLD", "OPEN_SHORT_CALL", "COVER_AND_ROLL"]
    reason: str
    contract: Optional[OptionContract] = None
    roll_to: Optional[OptionContract] = None
    buy_shares: int = 0
    close_option_symbol: Optional[str] = None
    close_option_qty: int = 0


def select_short_otm_call(
    chain: dict[str, Any],
    cfg: ShortCallConfig,
) -> Optional[OptionContract]:
    """Pick an OTM call near target DTE with delta <= max_delta and liquidity filters.

    Prefers the call closest to (but not above) max_delta among liquid names —
    that usually maximizes premium while staying OTM / delta-capped.
    """
    candidates: list[OptionContract] = []
    for c in iter_contracts(chain, side="CALL"):
        if abs(c.dte - cfg.target_dte) > cfg.dte_tolerance:
            continue
        if c.underlying_price <= 0 or c.strike <= c.underlying_price:
            continue  # require OTM
        if c.delta <= 0 or c.delta > cfg.max_delta:
            continue
        if not (cfg.min_iv <= c.iv <= cfg.max_iv):
            continue
        if c.open_interest < cfg.min_open_interest:
            continue
        if c.volume < cfg.min_volume:
            continue
        if c.mid <= 0 or c.spread_pct > cfg.max_spread_pct:
            continue
        candidates.append(c)

    if not candidates:
        return None

    if cfg.prefer_highest_delta_under_cap:
        candidates.sort(key=lambda x: (x.delta, -abs(x.dte - cfg.target_dte)), reverse=True)
    else:
        candidates.sort(key=lambda x: (abs(x.dte - cfg.target_dte), -x.delta))
    return candidates[0]


def decide_short_call(
    *,
    chain: dict[str, Any],
    positions: list[dict[str, Any]],
    underlying: str,
    cfg: ShortCallConfig,
    short_call_pos: Optional[dict[str, Any]] = None,
) -> ShortCallDecision:
    """Entry + management decision for the short OTM call strategy.

    Entry: no short call → open SELL_TO_OPEN on selected contract.
    Manage: short call delta >= manage_delta (or ITM) → buy 100 shares per
    short contract if not covered, close the short call, open a fresh OTM call.
    """
    underlying = underlying.upper()
    held = held_quantity(positions, underlying)
    selected = select_short_otm_call(chain, cfg)

    if short_call_pos is None:
        if selected is None:
            return ShortCallDecision("HOLD", "no liquid OTM call matches filters")
        return ShortCallDecision(
            "OPEN_SHORT_CALL",
            (
                f"sell OTM call {selected.symbol} strike={selected.strike} "
                f"dte={selected.dte} delta={selected.delta:.2f} mid={selected.mid:.2f}"
            ),
            contract=selected,
        )

    instrument = short_call_pos.get("instrument") or {}
    short_qty = int(float(short_call_pos.get("shortQuantity") or 0))
    if short_qty <= 0:
        return ShortCallDecision("HOLD", "short call quantity is zero")

    occ = str(instrument.get("symbol") or "")
    strike = float(instrument.get("strikePrice") or 0)
    # Prefer live chain greeks for the open short; fall back to strike/ITM check.
    live = _match_contract(chain, occ)
    underlying_price = (
        live.underlying_price
        if live
        else float((chain.get("underlyingPrice") or 0) or 0)
    )
    delta = abs(live.delta) if live else 0.0
    challenged = False
    reasons: list[str] = []
    if live and delta >= cfg.manage_delta:
        challenged = True
        reasons.append(f"delta {delta:.2f} >= manage_delta {cfg.manage_delta}")
    if underlying_price > 0 and strike > 0 and underlying_price >= strike:
        challenged = True
        reasons.append(f"underlying {underlying_price:.2f} >= strike {strike:.2f}")

    if not challenged:
        detail = f"delta={delta:.2f}" if live else "no live greeks"
        return ShortCallDecision("HOLD", f"short call intact ({detail})")

    shares_needed = max(0, short_qty * 100 - int(held))
    if selected is None:
        return ShortCallDecision(
            "HOLD",
            "challenged but no roll candidate matches filters; manual review",
            close_option_symbol=occ,
            close_option_qty=short_qty,
            buy_shares=shares_needed,
        )

    return ShortCallDecision(
        "COVER_AND_ROLL",
        "; ".join(reasons) + f" → buy {shares_needed} shares and roll to {selected.symbol}",
        contract=live,
        roll_to=selected,
        buy_shares=shares_needed,
        close_option_symbol=occ,
        close_option_qty=short_qty,
    )


def _match_contract(chain: dict[str, Any], occ_symbol: str) -> Optional[OptionContract]:
    if not occ_symbol:
        return None
    for c in iter_contracts(chain, side="CALL"):
        if c.symbol == occ_symbol:
            return c
    return None
