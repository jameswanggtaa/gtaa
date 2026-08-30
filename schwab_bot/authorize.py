"""One-time browser OAuth helper to obtain a Schwab refresh token.

Schwab requires a registered callback URL (often https://127.0.0.1:8182).
This script:
  1. Prints the authorize URL
  2. Starts a tiny HTTPS or HTTP callback listener (HTTP only if your app
     callback is http — Schwab usually requires https localhost)
  3. Exchanges the auth code for tokens and prints/saves the refresh token

For many setups it is easier to:
  - Open the authorize URL manually
  - Paste the full redirected callback URL when prompted

Usage:
  python -m schwab_bot.authorize
"""

from __future__ import annotations

import base64
import os
import urllib.parse
import webbrowser

import requests

from schwab_bot.auth import TOKEN_URL, SchwabAuth

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if not load_dotenv("schwab_bot/.env"):
        load_dotenv(".env")


def exchange_code(app_key: str, secret: str, code: str, redirect_uri: str) -> dict:
    basic = base64.b64encode(f"{app_key}:{secret}".encode()).decode()
    response = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    _load_dotenv()
    app_key = os.environ["SCHWAB_APP_KEY"]
    secret = os.environ["SCHWAB_SECRET"]
    redirect_uri = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
    token_path = os.getenv("SCHWAB_TOKEN_PATH", "schwab_bot/.tokens.json")

    params = {
        "client_id": app_key,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("Open this URL, log in to Schwab, and approve the app:\n")
    print(url)
    print()
    try:
        webbrowser.open(url)
    except Exception:
        pass

    redirected = input(
        "Paste the FULL redirect URL from your browser address bar after login:\n> "
    ).strip()
    parsed = urllib.parse.urlparse(redirected)
    query = urllib.parse.parse_qs(parsed.query)
    if "code" not in query:
        raise SystemExit(f"No ?code= found in URL: {redirected}")
    code = urllib.parse.unquote(query["code"][0])

    tokens = exchange_code(app_key, secret, code, redirect_uri)
    refresh = tokens["refresh_token"]
    print("\nSuccess. Add this to schwab_bot/.env:")
    print(f"SCHWAB_REFRESH_TOKEN={refresh}")

    auth = SchwabAuth(app_key, secret, refresh, token_path=token_path)
    auth.access_token = tokens.get("access_token")
    auth.expires_at = __import__("time").time() + int(tokens.get("expires_in", 1800))
    auth._save_tokens()
    print(f"Also wrote tokens to {token_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
