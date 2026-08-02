"""Pre-trade risk checks. Keep conservative defaults for the scaffold."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskLimits:
    max_notional: float = 2_000.0
    max_shares: int = 50
    max_open_positions: int = 3
    allow_short: bool = False


def approve(
    side: str,
    qty: int,
    price: float,
    open_position_count: int,
    held_qty: float,
    limits: RiskLimits,
) -> tuple[bool, str]:
    """Return (ok, reason)."""
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
