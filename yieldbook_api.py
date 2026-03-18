"""
Yieldbook REST API client: login, TBA price, and shock scenario.

Authentication: OAuth token from Yield Book (email + password).
Set YIELDBOOK_EMAIL and YIELDBOOK_PASSWORD in environment or .env.
"""

import os
import json
import requests
import datetime as _dt
from typing import Optional

# Optional: load .env if python-dotenv is installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- Configuration (override via env) ---
BASE_URL = os.getenv("YIELDBOOK_BASE_URL", "https://www.yieldbook.com")
# Yieldbook Analytics REST base (OpenAPI server in this workspace: https://api.yieldbook.com/analytics/v2)
ANALYTICS_BASE_URL = os.getenv(
    "YIELDBOOK_ANALYTICS_BASE_URL", "https://api.yieldbook.com/analytics/v2"
)
# Token endpoint (supports client_credentials grant in this workspace)
TOKEN_URL = os.getenv("YIELDBOOK_TOKEN_URL", "https://www.yieldbook.com/x/oauth/api-token")
# Alternative login (some setups use q.yieldbook.com)
ALT_LOGIN_URL = os.getenv("YIELDBOOK_ALT_LOGIN_URL", "https://q.yieldbook.com/api/login")


def get_access_token_client_credentials(
    client_id: str,
    client_secret: str,
    audience: str = "API2-PROD",
) -> str:
    """
    Obtain access token using OAuth client_credentials.
    Matches existing scripts in this workspace: POST api-token with query params.
    """
    params = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "audience": audience,
    }
    resp = requests.post(TOKEN_URL, params=params, timeout=30)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        msg = (resp.text or "")[:800]
        raise requests.HTTPError(f"{e} | body={msg}", response=resp) from e
    data = resp.json()
    token = data.get("accessToken") or data.get("access_token") or data.get("token")
    if not token:
        raise ValueError(f"Token not found in response: {list(data.keys())}")
    return token


def get_token(email: Optional[str] = None, password: Optional[str] = None) -> str:
    """
    Obtain access token from Yieldbook API.
    Uses YIELDBOOK_EMAIL and YIELDBOOK_PASSWORD if email/password not passed.
    """
    email = email or os.getenv("YIELDBOOK_EMAIL")
    password = password or os.getenv("YIELDBOOK_PASSWORD")
    if not email or not password:
        raise ValueError(
            "Provide YIELDBOOK_EMAIL and YIELDBOOK_PASSWORD in environment or as arguments."
        )

    # Try OAuth token endpoint first (per LSEG Quick Start)
    payload = {
        "username": email,
        "password": password,
    }
    headers = {"Content-Type": "application/json"}

    resp = requests.post(TOKEN_URL, json=payload, headers=headers, timeout=30)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        # Some tenants expect form-encoded (and return "No post data" for JSON)
        if resp.status_code == 400 and "No post data" in (resp.text or ""):
            resp2 = requests.post(TOKEN_URL, data=payload, timeout=30)
            try:
                resp2.raise_for_status()
                data = resp2.json()
                token = data.get("access_token") or data.get("token") or data.get("accessToken")
                if not token:
                    raise ValueError(f"Token not found in response: {list(data.keys())}")
                return token
            except Exception:
                msg = (resp2.text or "")[:800]
                raise requests.HTTPError(
                    f"{e} (retried form-encoded) | body={msg}", response=resp2
                ) from e

        msg = (resp.text or "")[:800]
        raise requests.HTTPError(f"{e} | body={msg}", response=resp) from e
    data = resp.json()

    # Common patterns: {"access_token": "..."} or {"token": "..."}
    token = data.get("access_token") or data.get("token") or data.get("accessToken")
    if not token:
        raise ValueError(f"Token not found in response: {list(data.keys())}")
    return token


def get_token_alt_login(email: Optional[str] = None, password: Optional[str] = None) -> str:
    """
    Alternative: login via q.yieldbook.com/api/login if OAuth endpoint fails.
    Returns token or session cookie value for use in Authorization header.
    """
    email = email or os.getenv("YIELDBOOK_EMAIL")
    password = password or os.getenv("YIELDBOOK_PASSWORD")
    if not email or not password:
        raise ValueError("Provide YIELDBOOK_EMAIL and YIELDBOOK_PASSWORD.")

    # This endpoint commonly expects username/password and (sometimes) environment
    payload = {
        "username": email,
        "password": password,
    }
    env = os.getenv("YIELDBOOK_ENVIRONMENT") or os.getenv("YIELDBOOK_ENV")
    if env:
        payload["environment"] = env
    headers = {"Content-Type": "application/json"}

    resp = requests.post(ALT_LOGIN_URL, json=payload, headers=headers, timeout=30)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        msg = (resp.text or "")[:800]
        raise requests.HTTPError(f"{e} | body={msg}", response=resp) from e
    data = resp.json()
    token = data.get("access_token") or data.get("token") or data.get("accessToken")
    if token:
        return token
    # Some APIs return a session ID in cookie; use that as Bearer token
    for c in resp.cookies:
        if "session" in c.name.lower() or "token" in c.name.lower():
            return c.value
    raise ValueError(f"Could not get token from alt login: {list(data.keys())}")


def login(email: Optional[str] = None, password: Optional[str] = None) -> str:
    """
    Login and return access token. Tries OAuth endpoint first, then alt login.
    """
    # Prefer client_credentials grant if we have an API client id/secret.
    client_id = os.getenv("YB_API_ID") or os.getenv("YIELDBOOK_CLIENT_ID")
    client_secret = os.getenv("YB_API_KEY") or os.getenv("YIELDBOOK_CLIENT_SECRET")
    if client_id and client_secret and not email and not password:
        return get_access_token_client_credentials(client_id, client_secret)

    if not email and not password:
        # Back-compat env names (also used as client_id/client_secret in this workspace)
        email = os.getenv("YIELDBOOK_EMAIL")
        password = os.getenv("YIELDBOOK_PASSWORD")

    # Convenience fallback: if env vars not set, try local yb_api_key.txt
    # Expected format: "<username> <secret>" on first line.
    if (not email or not password) and os.path.exists("yb_api_key.txt"):
        try:
            raw = open("yb_api_key.txt", "rb").read()
            first = None
            for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1252"):
                try:
                    first = raw.decode(enc).splitlines()[0].strip()
                    break
                except Exception:
                    continue
            if first:
                parts = first.split()
                if len(parts) >= 2 and (not email or not password):
                    email = email or parts[0]
                    password = password or parts[1]
        except Exception:
            pass

    # In this workspace, the "-api" credential pair is client_id/client_secret.
    if email and password:
        try:
            return get_access_token_client_credentials(email, password)
        except Exception:
            pass

    try:
        return get_token(email, password)
    except Exception as e:
        try:
            return get_token_alt_login(email, password)
        except Exception as e2:
            raise RuntimeError(
                f"Login failed. OAuth error: {e}. Alt login error: {e2}"
            ) from e2


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def post_analytics_sync(
    token: str,
    endpoint: str,
    body: dict,
    base_url: Optional[str] = None,
    timeout_s: int = 120,
) -> dict:
    base = base_url or os.getenv("YIELDBOOK_SYNC_BASE_URL", ANALYTICS_BASE_URL)
    url = base.rstrip("/") + "/" + endpoint.lstrip("/")
    resp = requests.post(url, json=body, headers=_headers(token), timeout=timeout_s)
    resp.raise_for_status()
    return resp.json()


def run_bond_py(
    token: str,
    pricing_date: str,
    inputs: list[dict],
    retrieve_ppm_projection: bool = True,
    base_url: Optional[str] = None,
) -> dict:
    """
    POST /sync/bond/py (matches the working approach in tba_analysis.py).
    """
    body = {
        "globalSettings": {
            "pricingDate": pricing_date,
            "retrievePPMProjection": retrieve_ppm_projection,
        },
        "input": inputs,
    }
    timeout_s = int(float(os.getenv("YIELDBOOK_PY_TIMEOUT_S", "180")))
    return post_analytics_sync(
        token, "/sync/bond/py", body, base_url=base_url, timeout_s=timeout_s
    )


def get_actual_vs_projected(
    token: str,
    identifier: str,
    base_url: Optional[str] = None,
    timeout_s: int = 120,
) -> dict:
    """
    GET /sync/bond/actual-vs-projected/{id}

    Returns a vector list with fields like actualCPR, projectedCPR, etc.
    """
    base = base_url or os.getenv("YIELDBOOK_SYNC_BASE_URL", ANALYTICS_BASE_URL)
    url = base.rstrip("/") + f"/sync/bond/actual-vs-projected/{identifier}"
    resp = requests.get(url, headers=_headers(token), timeout=timeout_s)
    resp.raise_for_status()
    return resp.json()


def post_sync(
    token: str,
    path: str,
    payload: dict,
    base_url: Optional[str] = None,
    timeout_s: int = 120,
) -> dict:
    """
    Generic helper for Yieldbook "sync" endpoints.

    Configure base_url via YIELDBOOK_SYNC_BASE_URL if needed (some tenants use q.yieldbook.com).
    """
    base = base_url or os.getenv("YIELDBOOK_SYNC_BASE_URL", ANALYTICS_BASE_URL)
    url = base.rstrip("/") + "/" + path.lstrip("/")
    resp = requests.post(url, json=payload, headers=_headers(token), timeout=timeout_s)
    resp.raise_for_status()
    return resp.json()


def get_sync(
    token: str,
    path: str,
    params: dict,
    base_url: Optional[str] = None,
    timeout_s: int = 120,
) -> dict:
    base = base_url or os.getenv("YIELDBOOK_SYNC_BASE_URL", ANALYTICS_BASE_URL)
    url = base.rstrip("/") + "/" + path.lstrip("/")
    resp = requests.get(url, params=params, headers=_headers(token), timeout=timeout_s)
    resp.raise_for_status()
    return resp.json()


def get_hist_data(
    token: str,
    cusips: list[str],
    fields: list[str],
    start_date: _dt.date,
    end_date: _dt.date,
    frequency: Optional[str] = None,
    path: Optional[str] = None,
    base_url: Optional[str] = None,
) -> dict:
    """
    Call /sync/bond/hist-data/{id} for one or more CUSIPs.

    Notes:
    - In this tenant, the endpoint is per-security and requires query params:
      startDate and keyword (and optionally endDate).
    - We call once per CUSIP and return a dict keyed by CUSIP.
    - Dates are sent as ISO yyyy-mm-dd.
    """
    # API path (api_url(mode="sync") would yield .../sync/bond/hist-data/{identifier})
    base_path = path or os.getenv("YIELDBOOK_HIST_DATA_PATH", "/sync/bond/hist-data")
    out: dict = {}
    for c in cusips:
        # Yieldbook expects comma-separated keywords (e.g. "effectiveDuration.LMM,effectiveConvexity.LMM")
        keyword = ",".join(fields) if fields else "CPR"
        params = {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "keyword": keyword,
        }
        if frequency:
            params["frequency"] = frequency
        out[c] = get_sync(token, f"{base_path.rstrip('/')}/{c}", params, base_url=base_url)
    return out


def get_tba_price(
    token: str,
    cusip_or_id: str,
    base_url: Optional[str] = None,
    price_endpoint: Optional[str] = None,
) -> dict:
    """
    Request TBA price for a given security.
    Endpoint path may vary by contract; replace with actual path from your API docs.
    """
    base = base_url or BASE_URL
    # Typical pattern: /api/v1/price or /api/analytics/price — replace with actual path
    path = price_endpoint or os.getenv("YIELDBOOK_PRICE_ENDPOINT", "/x/api/price")
    url = base.rstrip("/") + "/" + path.lstrip("/")
    if not url.startswith("http"):
        url = "https://www.yieldbook.com" + url

    payload = {"id": cusip_or_id, "securityType": "TBA"}
    resp = requests.post(
        url, json=payload, headers=_headers(token), timeout=60
    )
    resp.raise_for_status()
    return resp.json()


def get_shock_scenario(
    token: str,
    cusip_or_id: str,
    shocks: Optional[list] = None,
    base_url: Optional[str] = None,
    scenario_endpoint: Optional[str] = None,
) -> dict:
    """
    Request shock scenario analysis for a security.
    shocks: e.g. [{"curve": "treasury", "shift_bps": 100}, {"curve": "spread", "shift_bps": -50}]
    Replace scenario_endpoint with actual path from your API documentation.
    """
    base = base_url or BASE_URL
    path = scenario_endpoint or os.getenv(
        "YIELDBOOK_SCENARIO_ENDPOINT", "/x/api/scenario"
    )
    url = base.rstrip("/") + "/" + path.lstrip("/")
    if not url.startswith("http"):
        url = "https://www.yieldbook.com" + url

    if shocks is None:
        shocks = [
            {"curve": "treasury", "shift_bps": 100},
            {"curve": "treasury", "shift_bps": -100},
        ]
    payload = {
        "id": cusip_or_id,
        "securityType": "TBA",
        "shocks": shocks,
    }
    resp = requests.post(
        url, json=payload, headers=_headers(token), timeout=90
    )
    resp.raise_for_status()
    return resp.json()


def run_example():
    """Login, then call TBA price and shock scenario (example IDs)."""
    print("Logging in to Yieldbook API...")
    token = login()
    print("Login OK.\n")

    # Example TBA identifier (replace with your CUSIP or TBA contract ID)
    tba_id = os.getenv("YIELDBOOK_TBA_ID", "FNM30 2025-04")
    price_endpoint = os.getenv("YIELDBOOK_PRICE_ENDPOINT")
    scenario_endpoint = os.getenv("YIELDBOOK_SCENARIO_ENDPOINT")

    print("Fetching TBA price...")
    try:
        price_result = get_tba_price(token, tba_id, price_endpoint=price_endpoint)
        print("TBA price result:", json.dumps(price_result, indent=2))
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            print(
                "TBA price endpoint not found (404). "
                "Set YIELDBOOK_PRICE_ENDPOINT to your actual path."
            )
        else:
            print("TBA price error:", e.response.status_code, e.response.text[:500])
    except Exception as e:
        print("TBA price error:", e)

    print("\nFetching shock scenario...")
    try:
        scenario_result = get_shock_scenario(
            token, tba_id, scenario_endpoint=scenario_endpoint
        )
        print("Shock scenario result:", json.dumps(scenario_result, indent=2))
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            print(
                "Scenario endpoint not found (404). "
                "Set YIELDBOOK_SCENARIO_ENDPOINT to your actual path."
            )
        else:
            print("Scenario error:", e.response.status_code, e.response.text[:500])
    except Exception as e:
        print("Scenario error:", e)


if __name__ == "__main__":
    run_example()
