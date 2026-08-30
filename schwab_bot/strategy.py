"""Toy signal logic — replace with your real edge before live trading."""

from __future__ import annotations

from typing import Any, Literal

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
    """Simple SMA crossover on close prices.

    BUY when fast SMA > slow SMA, SELL when fast < slow, else HOLD.
    """
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
