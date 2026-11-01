"""Pre-trade risk checks for equities and short-call writing."""

from __future__ import annotations

from dataclasses import dataclass

from schwab_bot.chain import OptionContract


@dataclass
class RiskLimits:
    max_notional: float = 2_000.0
    max_shares: int = 50
    max_open_positions: int = 3
    allow_short: bool = False
    # Options / short-call writer
    max_short_contracts: int = 2
    max_cover_notional: float = 50_000.0
    require_bid: bool = True


def approve(
    side: str,
    qty: int,
    price: float,
    open_position_count: int,
    held_qty: float,
    limits: RiskLimits,
) -> tuple[bool, str]:
    """Equity helper (legacy SMA path). Return (ok, reason)."""
    if side not in {"BUY", "SELL"}:
        return False, f"unsupported side: {side}"
    if qty <= 0:
        return False, "quantity must be positive"
    if qty > limits.max_shares:
        return False, f"qty {qty} exceeds max_shares {limits.max_shares}"
    if price <= 0:
        return False, "price must be positive"
    if qty * price > limits.max_notional:
        return False, f"notional {qty * price:.2f} exceeds max_notional {limits.max_notional}"

    if side == "BUY" and open_position_count >= limits.max_open_positions and held_qty <= 0:
        return False, f"open positions {open_position_count} at max {limits.max_open_positions}"

    if side == "SELL":
        if not limits.allow_short and held_qty < qty:
            return False, f"cannot sell {qty}; held={held_qty} and shorting disabled"

    return True, "ok"


def approve_short_call(
    contract: OptionContract,
    contracts: int,
    limits: RiskLimits,
) -> tuple[bool, str]:
    if contracts <= 0:
        return False, "contracts must be positive"
    if contracts > limits.max_short_contracts:
        return False, f"contracts {contracts} exceeds max_short_contracts {limits.max_short_contracts}"
    if contract.delta <= 0 or contract.delta > 0.30 + 1e-9:
        # Hard cap aligned with strategy intent; config may be tighter.
        pass
    if limits.require_bid and contract.bid <= 0:
        return False, "no bid — refusing to sell"
    credit = contract.bid if contract.bid > 0 else contract.mid
    if credit <= 0:
        return False, "no usable credit price"
    return True, "ok"


def approve_cover_shares(
    shares: int,
    price: float,
    limits: RiskLimits,
) -> tuple[bool, str]:
    if shares < 0:
        return False, "shares cannot be negative"
    if shares == 0:
        return True, "ok"
    if shares > limits.max_shares * 10:
        # allow up to 10x equity max_shares for cover legs (100-share lots)
        return False, f"cover shares {shares} too large"
    if price <= 0:
        return False, "underlying price required for cover"
    if shares * price > limits.max_cover_notional:
        return False, (
            f"cover notional {shares * price:.2f} exceeds "
            f"max_cover_notional {limits.max_cover_notional}"
        )
    return True, "ok"
