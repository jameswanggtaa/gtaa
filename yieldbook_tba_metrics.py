"""
Yieldbook REST API: TBA metrics and parallel yield curve shocks.

OpenAPI: https://www.yieldbook.com/s/restapi/ (./openapi/openapi.yaml)
Server: https://api.yieldbook.com/analytics/v2

Endpoints used:
- TBA Pricing:     GET /sync/tba-pricing (query: job, name, pri, tags per spec)
- PY Calculation:  POST /sync/bond/py
- Scenario Calc:   POST /req/bond/scenario-calc, then GET /results/{requestId}
- Scenario setups: GET /sync/ref-data/scenario-setups

- Uses SIFMA Class A for next settlement date (mbs_settlement).
- PrevClose from GET /sync/tba-pricing; PY and scenario-calc use that level.
- Pulls: Forwardyield, Yieldcurrentmargin, OAS, ForwardWAL, LongtermfWDCPR,
  Duration, Convexity, effectiveDuration, Effectiveconvexity.
- Runs scenario-calc for parallel shocks (bps): -300, -200, -100, -50, -25,
  -10, -5, +5, +10, +25, +50, +100, +200, +300.

TBA CUSIPs: FNM30 30yr FNMA, coupons 3.0–7.0%, 2025 (CTD).
"""

import csv
import os
import time
import requests as rq
from datetime import date
from typing import Any, Dict, List, Optional

from mbs_settlement import (
    get_last_business_day_iso,
    get_next_settlement_date,
)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

AUTH_URL = "https://www.yieldbook.com/x/oauth/api-token"
API_BASE_URL = "https://api.yieldbook.com/analytics/v2"

# TBA CUSIPs (FNM30.300.25(CTD) through FNM30.700.25(CTD))
TBA_CUSIPS = [
    "FNM30.300.25(CTD)",
    "FNM30.350.25(CTD)",
    "FNM30.400.25(CTD)",
    "FNM30.450.25(CTD)",
    "FNM30.500.25(CTD)",
    "FNM30.550.25(CTD)",
    "FNM30.600.25(CTD)",
    "FNM30.650.25(CTD)",
    "FNM30.700.25(CTD)",
]

# Map TBA CUSIP -> Yieldbook security name for price (YBTBAPRICE PrevClose).
# Settlement 4/13/2026 = April -> FNMAx.x-PROD-APR. Adjust suffix by settle month if needed.
TBA_CUSIP_TO_SECURITY_NAME_APR: Dict[str, str] = {
    "FNM30.300.25(CTD)": "FNMA3.0-PROD-APR",
    "FNM30.350.25(CTD)": "FNMA3.5-PROD-APR",
    "FNM30.400.25(CTD)": "FNMA4.0-PROD-APR",
    "FNM30.450.25(CTD)": "FNMA4.5-PROD-APR",
    "FNM30.500.25(CTD)": "FNMA5.0-PROD-APR",
    "FNM30.550.25(CTD)": "FNMA5.5-PROD-APR",
    "FNM30.600.25(CTD)": "FNMA6.0-PROD-APR",
    "FNM30.650.25(CTD)": "FNMA6.5-PROD-APR",
    "FNM30.700.25(CTD)": "FNMA7.0-PROD-APR",
}

# Parallel yield curve shocks (basis points)
SHOCKS_BPS = [-300, -200, -100, -50, -25, -10, -5, 5, 10, 25, 50, 100, 200, 300]

CURVE_TYPE = "SWAP_RFR"
PREPAY_RATE = 100
OUTPUT_CSV = "yieldbook_tba_metrics_results.csv"


def _load_api_credentials() -> Dict[str, str]:
    api_id = os.getenv("YB_API_ID", "zwang@mtb.com-api")
    api_key = os.getenv("YB_API_KEY", "557ee405-5bc7-f273-5ec4-d9ff91697656")
    return {"client_id": api_id, "client_secret": api_key}


def get_access_token() -> str:
    creds = _load_api_credentials()
    auth_config = {
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "grant_type": "client_credentials",
        "audience": "API2-PROD",
    }
    resp = rq.post(AUTH_URL, params=auth_config)
    resp.raise_for_status()
    token = resp.json().get("accessToken")
    if not token:
        raise RuntimeError(f"No accessToken in response: {resp.text[:500]}")
    return token


def api_url(endpoint: str, mode: Optional[str] = None) -> str:
    if not mode:
        return "/".join([API_BASE_URL.strip("/"), endpoint.strip("/")])
    return "/".join([API_BASE_URL.strip("/"), mode.strip("/"), endpoint.strip("/")])


def api_headers(token: str) -> Dict[str, str]:
    return {
        "Accept": "*/*",
        "Authorization": f"Bearer {token}",
        "content-type": "application/json",
    }


# -----------------------------------------------------------------------------
# YBTBAPRICE PrevClose — security name mapping and price fetch
# -----------------------------------------------------------------------------

# Settlement month (YYYY-MM) -> suffix for security name (APR, MAY, JUN, ...)
_SETTLE_MONTH_SUFFIX = {
    "01": "JAN", "02": "FEB", "03": "MAR", "04": "APR", "05": "MAY", "06": "JUN",
    "07": "JUL", "08": "AUG", "09": "SEP", "10": "OCT", "11": "NOV", "12": "DEC",
}


def get_security_name_for_tba(cusip: str, settlement_date: str) -> str:
    """
    Return Yieldbook security name for TBA price (YBTBAPRICE), e.g. FNMA3.0-PROD-APR.
    For 4/13/2026 settle use APR; for other months use PROD-{MONTH}.
    """
    if cusip in TBA_CUSIP_TO_SECURITY_NAME_APR:
        # If settlement is April, use the APR map as-is
        if settlement_date.startswith("2026-04") or settlement_date.startswith("2027-04"):
            return TBA_CUSIP_TO_SECURITY_NAME_APR[cusip]
        # Other months: derive from coupon and settle month (e.g. FNMA3.0-PROD-MAY)
        month = settlement_date[5:7] if len(settlement_date) >= 7 else "04"
        suffix = _SETTLE_MONTH_SUFFIX.get(month, "APR")
        base = TBA_CUSIP_TO_SECURITY_NAME_APR[cusip].replace("-APR", f"-{suffix}")
        return base
    return cusip


def get_prevclose_ybtbaprice(
    token: str,
    security_name: str,
    pricing_date: str,
) -> Optional[float]:
    """
    Fetch PrevClose via GET /sync/tba-pricing (OpenAPI: TBA Pricing).

    Query params only: job (required), name, pri (-10 to 10), tags.
    You must have submitted TBA pricing request(s) to a job first; then we GET
    results by job and name (name = security name e.g. FNMA3.0-PROD-APR).

    Env:
      YB_TBA_PRICING_JOB  (required) Job reference (J-number or job name).
      YB_TBA_PRICING_PRI  (optional) Priority -10 to 10.
      YB_PREVCLOSE_OVERRIDE  (optional) Use this number for all if GET fails.
    """
    job = os.getenv("YB_TBA_PRICING_JOB", "").strip()
    if not job:
        override = os.getenv("YB_PREVCLOSE_OVERRIDE")
        if override:
            try:
                return float(override)
            except ValueError:
                pass
        return None

    base_url = api_url("tba-pricing", mode="sync")
    custom = os.getenv("YB_TBAPRICE_ENDPOINT", "").strip()
    if custom and custom.startswith("http"):
        base_url = custom

    params: Dict[str, Any] = {"job": job, "name": security_name}
    pri = os.getenv("YB_TBA_PRICING_PRI", "").strip()
    if pri:
        try:
            params["pri"] = int(pri)
        except ValueError:
            pass

    def parse_price(data: Dict[str, Any]) -> Optional[float]:
        results = data.get("data") or data.get("results") or []
        if results and isinstance(results[0], dict):
            r0 = results[0]
            p = r0.get("price") or r0.get("prevClose") or r0.get("PrevClose") or r0.get("close")
            if p is not None:
                return float(p)
        for key in ("price", "prevClose", "PrevClose", "close"):
            if isinstance(data.get(key), (int, float)):
                return float(data[key])
        return None

    try:
        resp = rq.get(base_url, headers=api_headers(token), params=params, timeout=30)
        if resp.status_code == 200:
            p = parse_price(resp.json())
            if p is not None:
                return p
    except Exception:
        pass

    override = os.getenv("YB_PREVCLOSE_OVERRIDE")
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    return None


# -----------------------------------------------------------------------------
# PY (price/yield) — last business day close, next SIFMA settle
# -----------------------------------------------------------------------------


def run_py_for_tbas(
    token: str,
    pricing_date: str,
    settlement_date: str,
    cusip_to_level: Dict[str, float],
) -> List[Dict[str, Any]]:
    """
    POST /sync/bond/py (OpenAPI: PY Calculation) per TBA.
    Level = YBTBAPRICE PrevClose; scenario-calc uses the same level.
    """
    endpoint = "/bond/py"
    url = api_url(endpoint, mode="sync")
    results: List[Dict[str, Any]] = []

    for cusip in TBA_CUSIPS:
        level = cusip_to_level.get(cusip) or 100.0
        body = {
            "globalSettings": {
                "pricingDate": pricing_date,
                "retrievePPMProjection": True,
            },
            "input": [
                {
                    "identifier": cusip,
                    "idType": "securityIDEntry",
                    "userTag": cusip,
                    "level": level,
                    "curve": {"curveType": CURVE_TYPE},
                    "prepaySettings": {"type": "Model", "rate": PREPAY_RATE},
                    "volatility": {"type": "Default"},
                    "extraSettings": {"optionModel": "OASEDUR"},
                }
            ],
        }
        # If API supports settlement date in PY input, uncomment:
        # body["input"][0]["settlementDate"] = settlement_date

        resp = rq.post(url, headers=api_headers(token), json=body)
        if not resp.ok:
            print(f"PY failed for {cusip}: {resp.status_code} {resp.text[:500]}")
            resp.raise_for_status()
        data = resp.json()
        res_list = data.get("results") or data.get("data") or []
        if not res_list:
            raise RuntimeError(f"No results in PY response for {cusip}: {data}")
        res = res_list[0]
        if (res.get("py") or {}).get("diagnostic", "").startswith(
            "Single volatility is not available"
        ):
            body["input"][0]["volatility"] = {"type": "Default"}
            resp = rq.post(url, headers=api_headers(token), json=body)
            resp.raise_for_status()
            data = resp.json()
            res_list = data.get("results") or data.get("data") or []
            res = res_list[0] if res_list else res
        results.append(res)
    return results


def extract_py_metrics(py_obj: Dict[str, Any]) -> Dict[str, Any]:
    """Extract Forwardyield, Yieldcurrentmargin, OAS, ForwardWAL, LongtermfWDCPR, Duration, Convexity, effectiveDuration, Effectiveconvexity."""
    py = py_obj.get("py") or {}
    fwd = py.get("forwardMeasures") or {}
    ppm = py.get("dataPpmProjList") or []
    # Long-term fwd CPR: first CPR projection's longTerm
    lt_fwd_cpr = None
    for p in ppm:
        if p.get("prepayType") == "CPR" and "longTerm" in p:
            lt_fwd_cpr = p.get("longTerm")
            break
    if lt_fwd_cpr is None and ppm:
        lt_fwd_cpr = ppm[0].get("longTerm")

    return {
        "cusip": py.get("cusip") or py_obj.get("userTag"),
        "Forwardyield": fwd.get("yield") or py.get("forwardYield"),
        "Yieldcurrentmargin": py.get("yieldCurveMargin") or py.get("yieldCurrentMargin"),
        "OAS": py.get("oas"),
        "ForwardWAL": py.get("forwardWAL") or py.get("wal"),
        "LongtermfWDCPR": lt_fwd_cpr,
        "Duration": py.get("duration"),
        "Convexity": py.get("convexity"),
        "effectiveDuration": py.get("effectiveDuration"),
        "Effectiveconvexity": py.get("effectiveConvexity"),
        "price_last_close": py.get("pyLevel") or py.get("economicExposure"),
    }


# -----------------------------------------------------------------------------
# Scenario-calc — parallel shocks
# -----------------------------------------------------------------------------


def build_scenarios(curve_shifts_bps: List[int]) -> List[Dict[str, Any]]:
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
                        "curveShifts": [{"year": 0.25, "value": shift}],
                    }
                },
            }
        )
    return scenarios


def run_scenario_calc(
    token: str,
    pricing_date: str,
    settlement_date: str,
    cusip_to_level: Dict[str, float],
) -> Dict[str, Any]:
    """POST /req/bond/scenario-calc (OpenAPI: Scenario Calculation), then GET /results/{requestId} until DONE."""
    endpoint = "/bond/scenario-calc"
    url = api_url(endpoint, mode="req")
    scenarios = build_scenarios(SHOCKS_BPS)

    horizon_info = [
        {
            "scenarioID": f"scen{i}",
            "level": 0,
            "prepay": {"rate": PREPAY_RATE},
        }
        for i in range(1, len(SHOCKS_BPS) + 1)
    ]

    inputs = []
    for cusip in TBA_CUSIPS:
        level = cusip_to_level.get(cusip) or 100.0
        inputs.append(
            {
                "userTag": cusip,
                "identifier": cusip,
                "idType": "securityIDEntry",
                "curve": {"curveType": CURVE_TYPE, "currency": "USD"},
                "settlementInfo": {
                    "level": level,
                    "settlementType": "CUSTOM",
                    "settlementDate": settlement_date,
                    "prepay": {"type": "Model", "rate": PREPAY_RATE},
                },
                "horizonInfo": horizon_info,
                "assumeCall": False,
                "horizonPYMethod": "OAS Change",
            }
        )

    body = {
        "globalSettings": {
            "pricingDate": pricing_date,
            "horizonDays": 30,
        },
        "scenarios": scenarios,
        "input": inputs,
    }

    resp = rq.post(url, headers=api_headers(token), json=body)
    resp.raise_for_status()
    data = resp.json()
    request_id = data.get("requestId")
    if not request_id:
        raise RuntimeError(f"No requestId from scenario-calc: {data}")
    print(f"  requestId: {request_id}, polling for results (up to 10 min)...")

    results_url = api_url(f"/results/{request_id}", mode=None)
    max_wait = 600  # 10 min for 9 TBAs × 14 scenarios
    waited = 0
    interval = 10
    last_status = None
    while waited <= max_wait:
        r = rq.get(results_url, headers=api_headers(token))
        if r.status_code == 404:
            if waited % 30 == 0 and waited > 0:
                print(f"  ... waiting for results ({waited}s)")
            time.sleep(interval)
            waited += interval
            continue
        r.raise_for_status()
        j = r.json()
        status = j.get("meta", {}).get("status")
        if status == "DONE":
            return j
        if status != last_status and status:
            print(f"  status: {status}")
            last_status = status
        elif waited > 0 and waited % 30 == 0:
            print(f"  ... waiting ({waited}s)")
        time.sleep(interval)
        waited += interval
    print(f"Results URL (poll manually): {results_url}")
    raise RuntimeError(
        f"Timeout after {max_wait}s waiting for scenario results. "
        f"RequestId: {request_id}. You can poll the results URL with a valid token."
    )


def _safe(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float):
        return str(round(val, 6))
    return str(val)


def extract_scenario_columns(scen_results: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """Parse scenario-calc response: cusip -> { price_bps_XXX: val, ... }."""
    out: Dict[str, Dict[str, str]] = {}
    results = scen_results.get("results") or scen_results.get("data") or []
    for idx, item in enumerate(results):
        cusip = (
            item.get("userTag")
            or item.get("cusip")
            or (TBA_CUSIPS[idx] if idx < len(TBA_CUSIPS) else str(idx))
        )
        row: Dict[str, str] = {}
        scenario = item.get("scenario") or {}
        horizon = scenario.get("horizon") or []
        for i, h in enumerate(horizon):
            if i >= len(SHOCKS_BPS):
                break
            bps = SHOCKS_BPS[i]
            col = f"price_bps_{bps:+d}"
            p = h.get("price") or h.get("actualPrice")
            if p is not None:
                row[col] = _safe(p)
        out[cusip] = row
    for c in TBA_CUSIPS:
        if c not in out:
            out[c] = {}
    return out


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main() -> None:
    as_of = date.today()
    pricing_date = get_last_business_day_iso(as_of)
    settlement_date = get_next_settlement_date(as_of)
    print(f"Pricing date (last business day): {pricing_date}")
    print(f"Settlement date (SIFMA Class A):  {settlement_date}")
    print(f"TBAs: {len(TBA_CUSIPS)}")
    print(f"Shocks (bps): {SHOCKS_BPS}\n")

    print("Getting access token...")
    token = get_access_token()
    print("Token OK.\n")

    # Resolve TBA -> security name (e.g. FNMA3.0-PROD-APR) and fetch YBTBAPRICE PrevClose
    print("Fetching YBTBAPRICE PrevClose for each TBA (security name by settle)...")
    cusip_to_level: Dict[str, float] = {}
    for cusip in TBA_CUSIPS:
        sec_name = get_security_name_for_tba(cusip, settlement_date)
        price = get_prevclose_ybtbaprice(token, sec_name, pricing_date)
        if price is not None:
            cusip_to_level[cusip] = price
            print(f"  {cusip} -> {sec_name}: PrevClose = {price}")
        else:
            cusip_to_level[cusip] = 100.0
            print(f"  {cusip} -> {sec_name}: PrevClose not found, using level=100")
    print()

    print("Running PY for each TBA (at PrevClose level)...")
    py_results = run_py_for_tbas(token, pricing_date, settlement_date, cusip_to_level)
    metrics_list: List[Dict[str, Any]] = []
    for res in py_results:
        m = extract_py_metrics(res)
        # Use YBTBAPRICE PrevClose in output (not PY's pyLevel)
        cusip = m.get("cusip")
        if cusip and cusip in cusip_to_level:
            m["price_last_close"] = cusip_to_level[cusip]
        metrics_list.append(m)
    print(f"PY done: {len(metrics_list)} results.\n")

    print("Running scenario-calc (parallel yield curve shocks)...")
    scen_results = run_scenario_calc(
        token, pricing_date, settlement_date, cusip_to_level
    )
    scenario_by_cusip = extract_scenario_columns(scen_results)
    print("Scenario-calc done.\n")

    # Merge PY metrics + shock columns
    rows: List[Dict[str, Any]] = []
    py_cols = [
        "cusip",
        "Forwardyield",
        "Yieldcurrentmargin",
        "OAS",
        "ForwardWAL",
        "LongtermfWDCPR",
        "Duration",
        "Convexity",
        "effectiveDuration",
        "Effectiveconvexity",
        "price_last_close",
    ]
    shock_cols = [f"price_bps_{bps:+d}" for bps in SHOCKS_BPS]
    all_headers = py_cols + shock_cols

    for m in metrics_list:
        cusip = m.get("cusip")
        row = {k: _safe(m.get(k)) for k in py_cols}
        if cusip and cusip in scenario_by_cusip:
            row.update(scenario_by_cusip[cusip])
        rows.append(row)

    # Ensure all columns present
    for r in rows:
        for h in all_headers:
            if h not in r:
                r[h] = ""

    print("Summary (first row):")
    print("  " + ", ".join(f"{k}={rows[0].get(k, '')}" for k in py_cols[:5]))
    print()

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Results saved to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
