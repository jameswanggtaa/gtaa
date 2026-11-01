"""Offline unit checks (no API calls)."""

from __future__ import annotations

from schwab_bot.chain import iter_contracts
from schwab_bot.orders import equity_limit, option_limit, short_call_credit
from schwab_bot.risk import RiskLimits, approve, approve_cover_shares, approve_short_call
from schwab_bot.strategy import (
    ShortCallConfig,
    decide_short_call,
    held_quantity,
    select_short_otm_call,
    signal_from_bars,
    sma,
)


def _sample_chain(underlying_price: float = 100.0) -> dict:
    """Synthetic Schwab-shaped chain for unit tests."""
    return {
        "symbol": "AAA",
        "underlyingPrice": underlying_price,
        "callExpDateMap": {
            "2026-12-04:35": {
                "105.0": [
                    {
                        "symbol": "AAA  261204C00105000",
                        "strikePrice": 105,
                        "bid": 1.20,
                        "ask": 1.30,
                        "mark": 1.25,
                        "delta": 0.28,
                        "gamma": 0.04,
                        "theta": -0.03,
                        "vega": 0.10,
                        "volatility": 22.0,
                        "totalVolume": 50,
                        "openInterest": 500,
                    }
                ],
                "110.0": [
                    {
                        "symbol": "AAA  261204C00110000",
                        "strikePrice": 110,
                        "bid": 0.55,
                        "ask": 0.65,
                        "mark": 0.60,
                        "delta": 0.18,
                        "gamma": 0.03,
                        "theta": -0.02,
                        "vega": 0.08,
                        "volatility": 22.0,
                        "totalVolume": 40,
                        "openInterest": 400,
                    }
                ],
                "102.0": [
                    {
                        "symbol": "AAA  261204C00102000",
                        "strikePrice": 102,
                        "bid": 2.00,
                        "ask": 2.20,
                        "mark": 2.10,
                        "delta": 0.42,  # above max_delta — should be excluded on entry
                        "gamma": 0.05,
                        "theta": -0.04,
                        "vega": 0.11,
                        "volatility": 22.0,
                        "totalVolume": 80,
                        "openInterest": 800,
                    }
                ],
            }
        },
    }


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

    opt = option_limit("AAPL  260821C00200000", "SELL", 1, 1.25, open_position=True)
    assert opt["orderLegCollection"][0]["instruction"] == "SELL_TO_OPEN"
    credit = short_call_credit("AAPL  260821C00200000", 1, 1.25)
    assert credit["orderLegCollection"][0]["instruction"] == "SELL_TO_OPEN"


def test_select_short_otm_call() -> None:
    cfg = ShortCallConfig(target_dte=35, dte_tolerance=7, max_delta=0.30)
    chain = _sample_chain(100)
    contracts = list(iter_contracts(chain, side="CALL"))
    assert len(contracts) == 3

    chosen = select_short_otm_call(chain, cfg)
    assert chosen is not None
    # Highest delta under 0.30 among OTM liquid calls → 105 strike @ 0.28
    assert chosen.strike == 105.0
    assert chosen.delta <= 0.30

    decision = decide_short_call(
        chain=chain,
        positions=[],
        underlying="AAA",
        cfg=cfg,
        short_call_pos=None,
    )
    assert decision.action == "OPEN_SHORT_CALL"
    assert decision.contract is not None
    ok, _ = approve_short_call(decision.contract, 1, RiskLimits())
    assert ok


def test_cover_and_roll_when_challenged() -> None:
    cfg = ShortCallConfig(target_dte=35, max_delta=0.30, manage_delta=0.40)
    # Price rallied; short 102 call is ITM / high delta in chain
    chain = _sample_chain(underlying_price=103.0)
    # Inject high delta on the open short symbol
    chain["callExpDateMap"]["2026-12-04:35"]["102.0"][0]["delta"] = 0.55
    chain["callExpDateMap"]["2026-12-04:35"]["102.0"][0]["ask"] = 3.50

    short_pos = {
        "shortQuantity": 1,
        "instrument": {
            "assetType": "OPTION",
            "putCall": "CALL",
            "symbol": "AAA  261204C00102000",
            "underlyingSymbol": "AAA",
            "strikePrice": 102,
        },
    }
    decision = decide_short_call(
        chain=chain,
        positions=[],  # not covered yet
        underlying="AAA",
        cfg=cfg,
        short_call_pos=short_pos,
    )
    assert decision.action == "COVER_AND_ROLL"
    assert decision.buy_shares == 100
    assert decision.roll_to is not None
    assert decision.roll_to.delta <= 0.30
    assert decision.roll_to.strike > 103.0

    ok, _ = approve_cover_shares(100, 103.0, RiskLimits(max_cover_notional=50_000))
    assert ok


if __name__ == "__main__":
    test_sma_and_signal()
    test_risk_and_held()
    test_order_builders()
    test_select_short_otm_call()
    test_cover_and_roll_when_challenged()
    print("ok")
