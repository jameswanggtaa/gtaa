"""Order payload builders for equities and single-leg options."""

from __future__ import annotations

from typing import Any, Literal

Side = Literal["BUY", "SELL"]


def equity_limit(symbol: str, side: Side, qty: int, price: float) -> dict[str, Any]:
    return {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "price": f"{price:.2f}",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": "BUY" if side == "BUY" else "SELL",
                "quantity": qty,
                "instrument": {"symbol": symbol, "assetType": "EQUITY"},
            }
        ],
    }


def equity_market(symbol: str, side: Side, qty: int) -> dict[str, Any]:
    return {
        "orderType": "MARKET",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": "BUY" if side == "BUY" else "SELL",
                "quantity": qty,
                "instrument": {"symbol": symbol, "assetType": "EQUITY"},
            }
        ],
    }


def option_limit(
    occ_symbol: str,
    side: Side,
    qty: int,
    price: float,
    *,
    open_position: bool = True,
) -> dict[str, Any]:
    """Build a single-leg option limit order.

    ``occ_symbol`` must be Schwab's option symbol (OCC-style).
    """
    if side == "BUY":
        instruction = "BUY_TO_OPEN" if open_position else "BUY_TO_CLOSE"
    else:
        instruction = "SELL_TO_OPEN" if open_position else "SELL_TO_CLOSE"

    return {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "price": f"{price:.2f}",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": instruction,
                "quantity": qty,
                "instrument": {"symbol": occ_symbol, "assetType": "OPTION"},
            }
        ],
    }


def short_call_credit(occ_symbol: str, qty: int, credit: float) -> dict[str, Any]:
    """Sell-to-open a call for a limit credit (use bid for conservative fills)."""
    return option_limit(occ_symbol, "SELL", qty, credit, open_position=True)


def close_short_call_debit(occ_symbol: str, qty: int, debit: float) -> dict[str, Any]:
    """Buy-to-close a short call for a limit debit (use ask for conservative fills)."""
    return option_limit(occ_symbol, "BUY", qty, debit, open_position=False)
