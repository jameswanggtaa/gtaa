"""Parse Schwab option-chain payloads into flat contract rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    underlying: str
    put_call: str
    expiration: date
    dte: int
    strike: float
    bid: float
    ask: float
    mark: float
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float
    volume: int
    open_interest: int
    underlying_price: float

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.mark if self.mark > 0 else max(self.bid, self.ask)

    @property
    def spread(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return self.ask - self.bid
        return 0.0

    @property
    def spread_pct(self) -> float:
        mid = self.mid
        return (self.spread / mid) if mid > 0 else 1.0


def _parse_expiration(key: str) -> tuple[date, int]:
    """Schwab map keys look like '2026-12-05:35' (date:dte)."""
    exp_part, _, dte_part = key.partition(":")
    exp = datetime.strptime(exp_part, "%Y-%m-%d").date()
    dte = int(dte_part) if dte_part else (exp - date.today()).days
    return exp, dte


def _f(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in row and row[key] is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return default


def _i(row: dict[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        if key in row and row[key] is not None:
            try:
                return int(float(row[key]))
            except (TypeError, ValueError):
                continue
    return default


def iter_contracts(
    chain: dict[str, Any],
    *,
    side: str = "CALL",
) -> Iterable[OptionContract]:
    """Yield contracts from a Schwab `/chains` response."""
    underlying = str(chain.get("symbol") or "")
    underlying_price = _f(chain, "underlyingPrice")
    side = side.upper()
    map_key = "callExpDateMap" if side == "CALL" else "putExpDateMap"
    exp_map = chain.get(map_key) or {}

    for exp_key, strikes in exp_map.items():
        try:
            expiration, dte = _parse_expiration(str(exp_key))
        except ValueError:
            continue
        if not isinstance(strikes, dict):
            continue
        for _strike_key, contracts in strikes.items():
            if not isinstance(contracts, list):
                continue
            for row in contracts:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol") or "")
                if not symbol:
                    continue
                bid = _f(row, "bid")
                ask = _f(row, "ask")
                mark = _f(row, "mark", "last")
                yield OptionContract(
                    symbol=symbol,
                    underlying=underlying or str(row.get("underlying") or ""),
                    put_call=side,
                    expiration=expiration,
                    dte=dte,
                    strike=_f(row, "strikePrice"),
                    bid=bid,
                    ask=ask,
                    mark=mark,
                    delta=_f(row, "delta"),
                    gamma=_f(row, "gamma"),
                    theta=_f(row, "theta"),
                    vega=_f(row, "vega"),
                    iv=_f(row, "volatility"),
                    volume=_i(row, "totalVolume", "tradeVolume"),
                    open_interest=_i(row, "openInterest"),
                    underlying_price=underlying_price or _f(row, "underlyingPrice"),
                )


def find_short_call_position(
    positions: list[dict[str, Any]],
    underlying: str,
) -> Optional[dict[str, Any]]:
    """Return the first short call option position for an underlying."""
    underlying = underlying.upper()
    for pos in positions:
        instrument = pos.get("instrument") or {}
        if instrument.get("assetType") != "OPTION":
            continue
        if str(instrument.get("putCall") or "").upper() != "CALL":
            continue
        # underlyingSymbol is usually present on option instruments
        und = str(
            instrument.get("underlyingSymbol")
            or instrument.get("underlying")
            or ""
        ).upper()
        short_qty = float(pos.get("shortQuantity") or 0)
        if und == underlying and short_qty > 0:
            return pos
    return None
