"""OAuth token helper for the Schwab Trader API.

Access tokens last ~30 minutes. Refresh tokens last ~7 days; after that
you must complete the browser authorization flow again.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Optional

import requests

TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
TRADER_BASE = "https://api.schwabapi.com/trader/v1"
MARKET_BASE = "https://api.schwabapi.com/marketdata/v1"


class SchwabAuth:
    """Refresh-token based auth with optional on-disk token persistence."""

    def __init__(
        self,
        app_key: str,
        secret: str,
        refresh_token: str,
        token_path: Optional[str | Path] = None,
    ) -> None:
        self.app_key = app_key
        self.secret = secret
        self.refresh_token = refresh_token
        self.access_token: Optional[str] = None
        self.expires_at: float = 0.0
        self.token_path = Path(token_path) if token_path else None

        if self.token_path and self.token_path.exists():
            self._load_tokens()

    def get_access_token(self) -> str:
        if self.access_token and time.time() < self.expires_at - 60:
            return self.access_token
        return self.refresh()

    def refresh(self) -> str:
        basic = base64.b64encode(f"{self.app_key}:{self.secret}".encode()).decode()
        response = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        self.access_token = data["access_token"]
        self.expires_at = time.time() + int(data.get("expires_in", 1800))
        if "refresh_token" in data:
            self.refresh_token = data["refresh_token"]
        self._save_tokens()
        return self.access_token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_access_token()}"}

    def _save_tokens(self) -> None:
        if not self.token_path:
            return
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "refresh_token": self.refresh_token,
            "access_token": self.access_token,
            "expires_at": self.expires_at,
        }
        self.token_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load_tokens(self) -> None:
        if not self.token_path:
            return
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.access_token = data.get("access_token")
        self.expires_at = float(data.get("expires_at") or 0)


def auth_from_env(token_path: Optional[str] = None) -> SchwabAuth:
    """Build SchwabAuth from SCHWAB_* environment variables."""
    app_key = os.environ["SCHWAB_APP_KEY"]
    secret = os.environ["SCHWAB_SECRET"]
    refresh_token = os.environ["SCHWAB_REFRESH_TOKEN"]
    path = token_path or os.getenv("SCHWAB_TOKEN_PATH", "schwab_bot/.tokens.json")
    return SchwabAuth(app_key, secret, refresh_token, token_path=path)
