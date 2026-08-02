"""Thin REST client for Schwab market data and trading endpoints."""

from __future__ import annotations

from typing import Any, Optional

import requests

from schwab_bot.auth import MARKET_BASE, TRADER_BASE, SchwabAuth


class SchwabClient:
    def __init__(self, auth: SchwabAuth, account_hash: str) -> None:
        self.auth = auth
        self.account_hash = account_hash

    def quote(self, symbol: str) -> dict[str, Any]:
        response = requests.get(
            f"{MARKET_BASE}/quotes",
            params={"symbols": symbol},
            headers=self.auth.headers(),
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if symbol not in data:
            raise KeyError(f"No quote returned for {symbol}: {data}")
        return data[symbol]

    def quotes(self, symbols: list[str]) -> dict[str, Any]:
        response = requests.get(
            f"{MARKET_BASE}/quotes",
            params={"symbols": ",".join(symbols)},
            headers=self.auth.headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def price_history(
        self,
        symbol: str,
        period_type: str = "day",
        period: int = 10,
        frequency_type: str = "minute",
        frequency: int = 5,
        need_extended_hours_data: bool = False,
    ) -> dict[str, Any]:
        response = requests.get(
            f"{MARKET_BASE}/pricehistory",
            params={
                "symbol": symbol,
                "periodType": period_type,
                "period": period,
                "frequencyType": frequency_type,
                "frequency": frequency,
                "needExtendedHoursData": str(need_extended_hours_data).lower(),
            },
            headers=self.auth.headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def option_chain(
        self,
        symbol: str,
        contract_type: str = "ALL",
        strike_count: int = 10,
        include_underlying_quote: bool = True,
    ) -> dict[str, Any]:
        response = requests.get(
            f"{MARKET_BASE}/chains",
            params={
                "symbol": symbol,
                "contractType": contract_type,
                "strikeCount": strike_count,
                "includeUnderlyingQuote": str(include_underlying_quote).lower(),
            },
            headers=self.auth.headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def accounts(self) -> list[dict[str, Any]]:
        response = requests.get(
            f"{TRADER_BASE}/accounts",
            params={"fields": "positions"},
            headers=self.auth.headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def account(self) -> dict[str, Any]:
        response = requests.get(
            f"{TRADER_BASE}/accounts/{self.account_hash}",
            params={"fields": "positions"},
            headers=self.auth.headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def positions(self) -> list[dict[str, Any]]:
        acct = self.account()
        return acct.get("securitiesAccount", {}).get("positions", []) or []

    def orders(self, from_iso: str, to_iso: str, status: Optional[str] = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "fromEnteredTime": from_iso,
            "toEnteredTime": to_iso,
        }
        if status:
            params["status"] = status
        response = requests.get(
            f"{TRADER_BASE}/accounts/{self.account_hash}/orders",
            params=params,
            headers=self.auth.headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def place_order(self, order: dict[str, Any]) -> requests.Response:
        return requests.post(
            f"{TRADER_BASE}/accounts/{self.account_hash}/orders",
            json=order,
            headers={**self.auth.headers(), "Content-Type": "application/json"},
            timeout=30,
        )

    def cancel_order(self, order_id: str | int) -> requests.Response:
        return requests.delete(
            f"{TRADER_BASE}/accounts/{self.account_hash}/orders/{order_id}",
            headers=self.auth.headers(),
            timeout=30,
        )
