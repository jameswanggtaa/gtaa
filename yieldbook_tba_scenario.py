"""
Yield Book REST API: TBA price (PY) and shock scenario example.

This script follows the official notebooks you provided:
- Auth via https://www.yieldbook.com/x/oauth/api-token
- Base URL: https://api.yieldbook.com/analytics/v2
- Price via /bond/py
- Scenario via /bond/scenario-calc

Credentials:
- Either create yb_api_key.txt with: "<CLIENT_ID> <CLIENT_SECRET>" on one line
- Or set env vars YB_API_ID and YB_API_KEY.
"""

import os
import time
import json
import requests as rq
from typing import Optional, Dict, Any, List


AUTH_URL = "https://www.yieldbook.com/x/oauth/api-token"
API_BASE_URL = "https://api.yieldbook.com/analytics/v2"


def _load_api_credentials() -> Dict[str, str]:
    """
    Load API client_id and client_secret.

    For now we hard-code your values here so you don't
    need to manage files or environment variables.
    """
    api_id = "zwang@mtb.com-api"
    api_key = "557ee405-5bc7-f273-5ec4-d9ff91697656"
    return {"client_id": api_id, "client_secret": api_key}


def get_access_token() -> str:
    """Get JWT access token using client_credentials (same pattern as notebooks)."""
    creds = _load_api_credentials()
    auth_config = {
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "grant_type": "client_credentials",
        "audience": "API2-PROD",
    }
    resp = rq.post(AUTH_URL, params=auth_config)
    if resp:
        token = resp.json().get("accessToken")
        if token:
            return token
    raise RuntimeError(f"Error retrieving token: {resp.status_code} {resp.text[:500]}")


def api_url(endpoint: str, mode: Optional[str] = None) -> str:
    """
    Build full API URL, matching the notebooks' helper.
    mode examples:
    - None   -> https://api.yieldbook.com/analytics/v2/bond/py
    - "sync" -> https://api.yieldbook.com/analytics/v2/sync/bond/py
    - "req"  -> https://api.yieldbook.com/analytics/v2/req/bond/py
    """
    if not mode:
        return "/".join([API_BASE_URL.strip("/"), endpoint.strip("/")])
    return "/".join([API_BASE_URL.strip("/"), mode.strip("/"), endpoint.strip("/")])


def api_headers(access_token: str) -> Dict[str, str]:
    return {
        "Accept": "*/*",
        "Authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }


def get_tba_price_py(
    access_token: str,
    identifier: str,
    id_type: str = "securityIDEntry",
    level: str = "100",
    curve_type: str = "SWAP_RFR",
) -> Dict[str, Any]:
    """
    Call /bond/py in sync mode for a TBA/MBS identifier and return the full JSON.

    You can then pull price via result["data"][0]["py"]["price"] or similar,
    depending on your contract fields. This mirrors the PY examples in the demo notebook.
    """
    endpoint = "/bond/py"
    url = api_url(endpoint, mode="sync")
    body = {
        "globalSettings": {
            # adjust if you want a specific date
            "usePreviousClose": True,
        },
        "input": [
            {
                "identifier": identifier,
                "idType": id_type,  # often "securityIDEntry" or "cusip"
                "level": level,
                "curve": {
                    "curveType": curve_type,
                },
                "prepaySettings": {
                    "type": "Model",
                    "rate": 100,
                },
                "volatility": {
                    "type": "Default",
                },
                "extraSettings": {
                    "optionModel": "OASEDUR",
                },
            }
        ],
    }
    resp = rq.post(url, headers=api_headers(access_token), json=body)
    resp.raise_for_status()
    return resp.json()


def run_shock_scenario(
    access_token: str,
    identifier: str,
    id_type: str = "securityIDEntry",
    level: float = 100.0,
    curve_type: str = "GVT",
    currency: str = "USD",
    prepay_rate: float = 100.0,
    horizon_days: int = 30,
    curve_shifts_bps: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """
    Run a shock scenario via /bond/scenario-calc in async mode, mirroring the
    example in the demo notebook. Returns the final DONE result JSON.

    curve_shifts_bps: list of parallel shifts (in bps) to apply, e.g. [400, 300, 200, 100, 50, 0]
    """
    if curve_shifts_bps is None:
        curve_shifts_bps = [100, 0]  # simple +100bp and base case

    # Build scenarios list
    scenarios = []
    for i, shift in enumerate(curve_shifts_bps, start=1):
        scenarios.append(
            {
                "scenarioID": f"scen{i}",
                "timing": "Gradual",
                "reinvestmentRate": "default",
                "definition": {
                    "userScenario": {
                        "shiftType": "Par",
                        "interpolationType": "Years",
                        "swapSpreadConst": False,
                        "curveShifts": [
                            {
                                "year": 0.25,
                                "value": shift,
                            }
                        ],
                    }
                },
            }
        )

    # Horizon info for each scenario (same pattern as notebook)
    horizon_info = []
    for scen in scenarios:
        horizon_info.append(
            {
                "scenarioID": scen["scenarioID"],
                "level": 0,
                "prepay": {
                    "rate": prepay_rate,
                },
            }
        )

    endpoint = "/bond/scenario-calc"
    url = api_url(endpoint, mode="req")
    body = {
        "globalSettings": {
            # you can add pricingDate here if desired
            "pricingDate": os.getenv("YB_PRICING_DATE", "2025-01-31"),
            "horizonDays": horizon_days,
        },
        "scenarios": scenarios,
        "input": [
            {
                "userTag": "1",
                "identifier": identifier,
                "idType": id_type,
                "curve": {
                    "curveType": curve_type,
                    "currency": currency,
                },
                "settlementInfo": {
                    "level": level,
                    "settlementType": "CUSTOM",
                    # use env or default; must be a valid date for your setup
                    "settlementDate": os.getenv("YB_SETTLE_DATE", "2025-01-31"),
                    "prepay": {
                        "type": "Model",
                        "rate": prepay_rate,
                    },
                },
                "horizonInfo": horizon_info,
                "assumeCall": False,
                "horizonPYMethod": "OAS Change",
            }
        ],
    }

    resp = rq.post(url, headers=api_headers(access_token), json=body)
    resp.raise_for_status()
    data = resp.json()
    request_id = data.get("requestId")
    if not request_id:
        raise RuntimeError(f"No requestId returned from scenario-calc: {data}")

    # Poll /results/{requestId} until status == DONE.
    # The official demo notebook uses the base tree (no mode) for results.
    results_endpoint = "/results/{requestId}"
    results_url = api_url(results_endpoint.format(requestId=request_id), mode=None)

    # Retry loop with simple handling for 404 (result not ready/visible yet).
    max_wait_seconds = 120
    interval = 5
    waited = 0

    while waited <= max_wait_seconds:
        res = rq.get(results_url, headers=api_headers(access_token))
        if res.status_code == 404:
            time.sleep(interval)
            waited += interval
            continue

        res.raise_for_status()
        res_json = res.json()
        status = res_json.get("meta", {}).get("status")
        if status == "DONE":
            return res_json

        time.sleep(interval)
        waited += interval

    raise RuntimeError(f"Timed out waiting for scenario results at {results_url}")


def main() -> None:
    """
    Example flow:
    - Get token
    - Get TBA/bond PY (price)
    - Run a shock scenario on the same identifier
    """
    print("Getting access token...")
    token = get_access_token()
    print("Access token OK.\n")

    # Example identifier:
    # - For MBS/TBA, you can use a CUSIP or other ID your setup supports.
    #   Replace this with your TBA ID.
    identifier = os.getenv("YB_TBA_ID", "3138EHXE")
    id_type = os.getenv("YB_ID_TYPE", "securityIDEntry")  # e.g. "cusip"

    print(f"Running PY for {identifier} ({id_type})...")
    py_result = get_tba_price_py(token, identifier=identifier, id_type=id_type)
    print("PY result:")
    print(json.dumps(py_result, indent=2))

    print("\nRunning shock scenario...")
    scenario_result = run_shock_scenario(
        token,
        identifier=identifier,
        id_type=id_type,
        curve_shifts_bps=[100, 0, -100],
    )
    print("Scenario result:")
    print(json.dumps(scenario_result, indent=2))


if __name__ == "__main__":
    main()

