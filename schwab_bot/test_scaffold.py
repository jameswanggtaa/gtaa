"""Offline unit checks for strategy/risk/order builders (no API calls)."""

from __future__ import annotations

from schwab_bot.orders import equity_limit, option_limit
from schwab_bot.risk import RiskLimits, approve
from schwab_bot.strategy import held_quantity, signal_from_bars, sma


def test_sma_and_signal() -> None:
    assert sma([1, 2, 3, 4, 5], 5) == 3.0
    assert sma([1, 2], 5) is None

    rising = [{"close": float(i)} for i in range(1, 30)]
    assert signal_from_bars(rising) == "BUY"

    falling = [{"close": float(i)} for i in range(30, 0, -1)]
    assert signal_from_bars(falling) == "SELL"


def test_risk_and_held() -> None:
    limits = RiskLimits(max_notional=1000, max_shares=10, max_open_positions=1)
    ok, _ = approve("BUY", 5, 100, open_position_count=0, held_qty=0, limits=limits)
    assert ok
    ok, reason = approve("BUY", 20, 100, open_position_count=0, held_qty=0, limits=limits)
    assert not ok and "max_shares" in reason
    ok, reason = approve("SELL", 5, 100, open_position_count=0, held_qty=0, limits=limits)
    assert not ok and "shorting" in reason

    positions = [
        {"instrument": {"symbol": "SPY", "assetType": "EQUITY"}, "longQuantity": 3},
        {"instrument": {"symbol": "AAPL", "assetType": "EQUITY"}, "longQuantity": 1},
    ]
    assert held_quantity(positions, "SPY") == 3.0
    assert held_quantity(positions, "MSFT") == 0.0


def test_order_builders() -> None:
    eq = equity_limit("SPY", "BUY", 1, 500.1)
    assert eq["orderType"] == "LIMIT"
    assert eq["orderLegCollection"][0]["instruction"] == "BUY"
    assert eq["price"] == "500.10"

    opt = option_limit("AAPL  260821C00200000", "SELL", 1, 1.25, open_position=False)
    assert opt["orderLegCollection"][0]["instruction"] == "SELL_TO_CLOSE"
    assert opt["orderLegCollection"][0]["instrument"]["assetType"] == "OPTION"


if __name__ == "__main__":
    test_sma_and_signal()
    test_risk_and_held()
    test_order_builders()
    print("ok")
