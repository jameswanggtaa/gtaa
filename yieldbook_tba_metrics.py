"""
Yieldbook REST API: TBA metrics and parallel yield curve shocks.

OpenAPI: https://www.yieldbook.com/s/restapi/ (./openapi/openapi.yaml)
Server: https://api.yieldbook.com/analytics/v2

Endpoints used:
- TBA Pricing:     GET /sync/tba-pricing (query: job, name, pri, tags per spec)
- PY Calculation:  POST /sync/bond/py
- Scenario Calc:   POST /sync/bond/scenario-calc (YBSCEN OAS Change)
- Scenario setups: GET /sync/ref-data/scenario-setups

- TBA settle: if as_of is less than 7 days from the closest Class A date,
  use the Class A month of (as_of + 30 days); else use the next Class A month.
  Example: 2026-08-14 is 1 day from 2026-08-13 -> 2026-09-13 -> Sept / 2026-09-14.
- PrevClose from GET /sync/tba-pricing; PY and scenario-calc use that level.
- Valuation/pricingDate is always the previous business day SWAP_RFR curve
  (never today). PY sends TBA settlementDate.
- Output matches yieldbook_tba_metrics_results sample: tba_security, cusip,
  price_last_close, Settlement_Date, PY metrics, OptionValue (YCM - OAS),
  and YBSCEN shocked prices including 0 and +/-75.

TBA CUSIPs: FNM30 30yr FNMA, coupons 3.0–7.5%, 2025 (CTD).
"""

import csv
import os
import re
import time
import requests as rq
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from mbs_settlement import (
    TBA_HORIZON_DAYS,
    TBA_NEAR_CLASS_A_DAYS,
    days_from_closest_class_a,
    get_closest_class_a_settlement_date,
    get_next_settlement_date,
    get_previous_business_day_iso,
    get_tba_horizon_date,
    get_tba_settle_contract_month,
    get_tba_settle_date,
    get_tba_settle_prod_suffix,
    tba_use_horizon_settle_month,
)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

AUTH_URL = "https://www.yieldbook.com/x/oauth/api-token"
API_BASE_URL = "https://api.yieldbook.com/analytics/v2"

# TBA CUSIPs (FNM30.300.25(CTD) through FNM30.750.25(CTD))
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
    "FNM30.750.25(CTD)",
]

# Map TBA CUSIP -> Yieldbook security name for price (YBTBAPRICE PrevClose).
# Suffix PROD-{MON} is derived from settlement month (e.g. Sept 2026 -> SEP).
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
    "FNM30.750.25(CTD)": "FNMA7.5-PROD-APR",
}

# Parallel yield curve shocks (basis points) — match sample output grid
SHOCKS_BPS = [
    -300, -200, -100, -75, -50, -25, -10, -5, 0, 5, 10, 25, 50, 75, 100, 200, 300,
]
MAX_SCENARIOS_PER_REQUEST = max(1, int(os.getenv("YB_MAX_SCENARIOS_PER_REQUEST", "7")))
MAX_WORKERS = max(1, int(os.getenv("YB_MAX_WORKERS", "6")))

CURVE_TYPE = "SWAP_RFR"
PREPAY_RATE = 100
PREPAY_MODEL = "Model"
VOLATILITY_TYPE = "LMMSOFRFlat"
YBSCEN_SCENARIO_QUERY = os.getenv(
    "YB_YBSCEN_SCENARIO_QUERY",
    "timing=Gradual&reinvestmentRate=Default&swapSpreadConst=true",
).strip()


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
    Queries Yieldbook directly by security name (synchonous endpoint).

    Env:
      YB_TBA_PRICING_PRI  (optional) Priority -10 to 10.
      YB_PREVCLOSE_OVERRIDE  (optional) Use this number for all if GET fails.
    """
    base_url = api_url("tba-pricing", mode="sync")
    custom = os.getenv("YB_TBAPRICE_ENDPOINT", "").strip()
    if custom and custom.startswith("http"):
        base_url = custom

    # Query by name directly (synchronous endpoint)
    params: Dict[str, Any] = {"name": security_name}
    pri = os.getenv("YB_TBA_PRICING_PRI", "").strip()
    if pri:
        try:
            params["pri"] = int(pri)
        except ValueError:
            pass

    try:
        resp = rq.get(base_url, headers=api_headers(token), params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            # API returns { "data": { "quotes": [...] }, "meta": {...} }
            quotes = data.get("data", {}).get("quotes", [])
            
            # Find quote matching our security name
            for quote in quotes:
                ticker = quote.get("ticker", "")
                if ticker == security_name or security_name in ticker:
                    # Use closePrice, lastPrice, or lastAskPrice
                    price = quote.get("closePrice") or quote.get("lastPrice") or quote.get("lastAskPrice")
                    if price is not None:
                        return float(price)
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
# PY (price/yield) — previous business day SWAP_RFR, TBA SIFMA settle
# -----------------------------------------------------------------------------


def _post_py(
    token: str,
    url: str,
    pricing_date: str,
    settlement_date: str,
    cusip: str,
    level: float,
) -> Dict[str, Any]:
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
                "settlementDate": settlement_date,
                "curve": {"curveType": CURVE_TYPE},
                "prepaySettings": {"type": "Model", "rate": PREPAY_RATE},
                "volatility": {"type": "Default"},
                "extraSettings": {"optionModel": "OASEDUR"},
            }
        ],
    }
    resp = rq.post(url, headers=api_headers(token), json=body, timeout=90)
    if not resp.ok:
        print(f"PY failed for {cusip}: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()
    data = resp.json()
    res_list = data.get("results") or data.get("data") or []
    if not res_list:
        raise RuntimeError(f"No results in PY response for {cusip}: {data}")
    return res_list[0]


def run_py_for_tbas(
    token: str,
    pricing_date: str,
    settlement_date: str,
    cusip_to_level: Dict[str, float],
) -> List[Dict[str, Any]]:
    """
    POST /sync/bond/py (OpenAPI: PY Calculation) per TBA.

    pricingDate is the previous business day (SWAP_RFR last close).
    settlementDate is the TBA contract month's SIFMA Class A date.
    """
    url = api_url("/bond/py", mode="sync")
    results: List[Dict[str, Any]] = []

    for cusip in TBA_CUSIPS:
        level = cusip_to_level.get(cusip) or 100.0
        res = _post_py(token, url, pricing_date, settlement_date, cusip, level)

        diag = (res.get("py") or {}).get("diagnostic") or ""
        if diag.startswith("Single volatility is not available"):
            res = _post_py(token, url, pricing_date, settlement_date, cusip, level)

        py = res.get("py") or {}
        if py.get("oas") is None and py.get("diagnostic"):
            print(f"  {cusip}: PY diagnostic={py.get('diagnostic')}")
        else:
            print(
                f"  {cusip}: OAS={py.get('oas')} Duration={py.get('duration')} "
                f"pricingDate={pricing_date} settlementDate={settlement_date}"
            )
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

    ycm = py.get("yieldCurveMargin") or py.get("yieldCurrentMargin")
    oas = py.get("oas")
    option_value = None
    if ycm is not None and oas is not None:
        try:
            option_value = float(ycm) - float(oas)
        except (TypeError, ValueError):
            option_value = None

    return {
        "cusip": py_obj.get("userTag") or py.get("userTag") or py.get("cusip"),
        "Forwardyield": fwd.get("yield") or py.get("forwardYield"),
        "Yieldcurrentmargin": ycm,
        "OAS": oas,
        "OptionValue": option_value,
        "ForwardWAL": fwd.get("wal") or py.get("forwardWAL") or py.get("wal"),
        "LongtermfWDCPR": lt_fwd_cpr,
        "Duration": py.get("duration"),
        "Convexity": py.get("convexity"),
        "effectiveDuration": py.get("effectiveDuration"),
        "Effectiveconvexity": py.get("effectiveConvexity"),
        "price_last_close": py.get("pyLevel") or py.get("economicExposure"),
    }


def _safe(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, int) and not isinstance(val, bool):
        return str(val)
    if isinstance(val, float):
        s = f"{val:.10f}".rstrip("0").rstrip(".")
        return s
    return str(val)


def shock_col(bps: int) -> str:
    """Sample uses price_bps_0 (no sign) and price_bps_+5 / price_bps_-5."""
    if bps == 0:
        return "price_bps_0"
    return f"price_bps_{bps:+d}"


def format_settlement_date_mdy(iso: str) -> str:
    """ISO YYYY-MM-DD -> M/D/YYYY like 9/14/2026."""
    d = date.fromisoformat(iso)
    return f"{d.month}/{d.day}/{d.year}"


def output_csv_path(pricing_date: str) -> str:
    """YB_TBA_MMDDYYYY.csv using previous-business-day valuation date.

    If YB_TBA_OUTPUT_DIR is set (non-empty), write into that directory
    (created if missing). Otherwise write next to the current working directory.
    """
    d = date.fromisoformat(pricing_date)
    filename = f"YB_TBA_{d.strftime('%m%d%Y')}.csv"
    out_dir = os.getenv("YB_TBA_OUTPUT_DIR", "").strip()
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        return os.path.abspath(os.path.join(out_dir, filename))
    return filename


# -----------------------------------------------------------------------------
# Scenario-calc — YBSCEN parallel Par shocks (sync) + OAS Change horizon
# -----------------------------------------------------------------------------


def _ybscen_scenario_ref(bps: int) -> Dict[str, str]:
    q = YBSCEN_SCENARIO_QUERY
    path = f"/sys/scenario/Par/{bps}"
    ref = f"{path}?{q}" if q else path
    return {"$ref": ref}


def _build_ybscen_sync_body(
    cusip: str,
    settlement_date: str,
    level_prevclose: float,
    shocks_bps: List[int],
) -> Dict[str, Any]:
    horizon_info: List[Dict[str, Any]] = []
    for bps in shocks_bps:
        horizon_info.append(
            {
                "prepay": {"rate": str(PREPAY_RATE)},
                "level": "",
                "scenarioRef": _ybscen_scenario_ref(bps),
            }
        )
    return {
        "identifier": cusip,
        "userTag": cusip,
        "idType": "securityIDEntry",
        "horizonInfo": horizon_info,
        "curve": {"curveType": CURVE_TYPE},
        "horizonPYMethod": "OAS Change",
        "settlementInfo": {
            "settlementType": "CUSTOM",
            "settlementDate": settlement_date,
            "prepay": {"rate": str(PREPAY_RATE), "type": PREPAY_MODEL},
            "level": str(level_prevclose),
        },
        "volatility": {"type": VOLATILITY_TYPE},
        "assumeCall": False,
    }


def _horizon_price(h: Any) -> Optional[float]:
    if not isinstance(h, dict):
        return None
    for key in (
        "price",
        "actualPrice",
        "fullPrice",
        "actualFullPrice",
        "underlyingPrice",
        "marketValue",
        "economicExposure",
        "pyLevel",
        "value",
    ):
        v = h.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    py = h.get("py") or {}
    if isinstance(py, dict):
        for key in (
            "price",
            "actualPrice",
            "fullPrice",
            "actualFullPrice",
            "underlyingPrice",
            "economicExposure",
            "pyLevel",
            "marketValue",
        ):
            v = py.get(key)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
    return None


def _horizon_prices_from_sync_payload(
    data: Any,
    cusip: str,
    expected_count: int,
) -> Optional[List[Optional[float]]]:
    if not isinstance(data, dict):
        return None
    results = data.get("results") or data.get("data")
    if results is None:
        return None
    if isinstance(results, dict):
        results = [results]
    if not isinstance(results, list) or not results:
        return None

    item = results[0]
    for r in results:
        if not isinstance(r, dict):
            continue
        tag = r.get("userTag") or (r.get("py") or {}).get("cusip") or r.get("identifier")
        if tag == cusip:
            item = r
            break

    scenario = item.get("scenario") or {}
    horizon = scenario.get("horizon")
    if not isinstance(horizon, list):
        return None

    out: List[Optional[float]] = []
    for i in range(expected_count):
        out.append(_horizon_price(horizon[i]) if i < len(horizon) else None)
    return out


def _resolve_scenario_payload(
    token: str,
    initial_payload: Dict[str, Any],
    max_wait_seconds: int = 180,
) -> Dict[str, Any]:
    if not isinstance(initial_payload, dict):
        return {}
    if initial_payload.get("results") is not None or initial_payload.get("data") is not None:
        return initial_payload
    request_id = initial_payload.get("requestId")
    if not request_id:
        return initial_payload
    results_url = api_url(f"/results/{request_id}", mode=None)
    waited = 0.0
    poll_seconds = 1.0
    while waited <= max_wait_seconds:
        r = rq.get(results_url, headers=api_headers(token), timeout=30)
        if r.status_code == 404:
            time.sleep(poll_seconds)
            waited += poll_seconds
            poll_seconds = min(4.0, poll_seconds * 1.5)
            continue
        if not r.ok:
            return initial_payload
        j = r.json()
        status = (j.get("meta") or {}).get("status")
        if status == "DONE" or j.get("results") is not None or j.get("data") is not None:
            return j
        time.sleep(poll_seconds)
        waited += poll_seconds
        poll_seconds = min(4.0, poll_seconds * 1.5)
    return initial_payload


def _extract_max_scenarios_from_errors(payload: Dict[str, Any]) -> Optional[int]:
    errs = payload.get("errors")
    if not isinstance(errs, list):
        return None
    for e in errs:
        if not isinstance(e, dict):
            continue
        desc = str(e.get("description") or "")
        m = re.search(r"Maximum number of scenarios is\s+(\d+)", desc)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


def run_scenario_calc(
    token: str,
    pricing_date: str,
    settlement_date: str,
    py_metrics: List[Dict[str, Any]],
) -> Dict[str, Dict[str, str]]:
    """
    Per TBA: POST /sync/bond/scenario-calc with YBSCEN Par/{bps} and
    horizonPYMethod OAS Change. Parses results[].scenario.horizon[] prices.
    """
    scenario_by_cusip: Dict[str, Dict[str, str]] = {}
    url = api_url("/bond/scenario-calc", mode="sync")
    shock_chunks = [
        SHOCKS_BPS[i : i + MAX_SCENARIOS_PER_REQUEST]
        for i in range(0, len(SHOCKS_BPS), MAX_SCENARIOS_PER_REQUEST)
    ]
    global_settings: Dict[str, Any] = {
        "usePreviousClose": True,
        "horizonDays": "0",
        "horizonMonths": "0",
        "calcHorizonEffectiveMeasures": True,
        "calcHorizonOptionMeasures": True,
        "pricingDate": pricing_date,
    }

    def _run_for_metric(m: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
        cusip = m.get("cusip")
        if not cusip:
            return "", {}
        try:
            base_price = float(m.get("price_last_close"))
        except (TypeError, ValueError):
            return "", {}

        row: Dict[str, str] = {shock_col(bps): "" for bps in SHOCKS_BPS}
        row[shock_col(0)] = _safe(base_price)

        for chunk_idx, chunk in enumerate(shock_chunks, start=1):
            body = {
                "input": [_build_ybscen_sync_body(cusip, settlement_date, base_price, chunk)],
                "globalSettings": global_settings,
            }
            resp = rq.post(url, headers=api_headers(token), json=body, timeout=120)
            prices: Optional[List[Optional[float]]] = None
            if resp.ok:
                payload = _resolve_scenario_payload(token, resp.json())
                if isinstance(payload, dict) and payload.get("errors"):
                    print(
                        f"scenario-calc returned errors for {cusip}, chunk {chunk}: "
                        f"{payload.get('errors')}"
                    )
                prices = _horizon_prices_from_sync_payload(payload, cusip, len(chunk))
                max_allowed = (
                    _extract_max_scenarios_from_errors(payload)
                    if isinstance(payload, dict)
                    else None
                )
                if max_allowed is not None and len(chunk) > max_allowed and prices is None:
                    print(
                        f"scenario-calc chunk too large ({len(chunk)} > {max_allowed}) "
                        f"for {cusip}; set YB_MAX_SCENARIOS_PER_REQUEST={max_allowed}."
                    )
            else:
                print(
                    f"scenario-calc sync failed for {cusip}, chunk {chunk}: "
                    f"{resp.status_code} {resp.text[:500]}"
                )

            for i, bps in enumerate(chunk):
                col = shock_col(bps)
                shocked = prices[i] if prices is not None and i < len(prices) else None
                if shocked is not None:
                    row[col] = _safe(shocked)

            if chunk_idx == 1:
                print(
                    f"  scenario-calc first chunk finished for {cusip} "
                    f"({len(chunk)} shocks)"
                )

        return cusip, row

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(py_metrics))) as ex:
        futs = [ex.submit(_run_for_metric, m) for m in py_metrics]
        for fut in as_completed(futs):
            cusip, row = fut.result()
            if cusip:
                scenario_by_cusip[cusip] = row
    return scenario_by_cusip


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main() -> None:
    as_of = date.today()
    pricing_date = get_previous_business_day_iso(as_of)
    closest_class_a = get_closest_class_a_settlement_date(as_of)
    days_to_class_a = days_from_closest_class_a(as_of)
    next_class_a = get_next_settlement_date(as_of)
    settlement_date = get_tba_settle_date(as_of)
    tba_ym = get_tba_settle_contract_month(as_of)
    tba_mon = get_tba_settle_prod_suffix(as_of)
    print(f"As-of (calendar):                 {as_of.isoformat()}")
    print(f"Valuation date (prev bus day SWAP_RFR): {pricing_date}")
    print(
        f"Closest Class A:                  {closest_class_a} "
        f"({days_to_class_a} day{'s' if days_to_class_a != 1 else ''} away)"
    )
    if tba_use_horizon_settle_month(as_of):
        horizon_date = get_tba_horizon_date(as_of)
        print(
            f"Settle rule:                      < {TBA_NEAR_CLASS_A_DAYS}d from closest Class A "
            f"-> month of as-of+{TBA_HORIZON_DAYS}d ({horizon_date.isoformat()})"
        )
    else:
        print(
            f"Settle rule:                      next Class A month "
            f"({next_class_a})"
        )
    print(
        f"TBA contract month:               {tba_ym} (PROD-{tba_mon}); "
        f"settlementDate (SIFMA Class A): {settlement_date}"
    )
    print(f"TBAs: {len(TBA_CUSIPS)}")
    print(f"Shocks (bps): {SHOCKS_BPS}\n")

    print("Getting access token...")
    token = get_access_token()
    print("Token OK.\n")

    # Resolve TBA -> security name (e.g. FNMA3.0-PROD-SEP) and fetch YBTBAPRICE PrevClose
    print("Fetching YBTBAPRICE PrevClose for each TBA (security name by settle)...")
    cusip_to_level: Dict[str, float] = {}
    cusip_to_security_name: Dict[str, str] = {}
    settle_display = format_settlement_date_mdy(settlement_date)
    for cusip in TBA_CUSIPS:
        sec_name = get_security_name_for_tba(cusip, settlement_date)
        cusip_to_security_name[cusip] = sec_name
        price = get_prevclose_ybtbaprice(token, sec_name, pricing_date)
        if price is not None:
            cusip_to_level[cusip] = price
            print(f"  {cusip} -> {sec_name}: PrevClose = {price}")
        else:
            cusip_to_level[cusip] = 100.0
            print(f"  {cusip} -> {sec_name}: PrevClose not found, using level=100")
    print()

    print("Running PY for each TBA (at PrevClose level, TBA settlementDate)...")
    py_results = run_py_for_tbas(
        token, pricing_date, settlement_date, cusip_to_level
    )
    metrics_list: List[Dict[str, Any]] = []
    for res in py_results:
        m = extract_py_metrics(res)
        cusip = m.get("cusip")
        if cusip and cusip in cusip_to_level:
            m["price_last_close"] = cusip_to_level[cusip]
            m["tba_security"] = cusip_to_security_name.get(cusip, "")
        m["Settlement_Date"] = settle_display
        metrics_list.append(m)
    print(f"PY done: {len(metrics_list)} results.\n")

    print("Running scenario-calc (YBSCEN OAS Change, parallel shocks)...")
    scenario_by_cusip = run_scenario_calc(
        token, pricing_date, settlement_date, metrics_list
    )
    print("Scenario-calc done.\n")

    rows: List[Dict[str, Any]] = []
    py_cols = [
        "tba_security",
        "cusip",
        "price_last_close",
        "Settlement_Date",
        "Forwardyield",
        "Yieldcurrentmargin",
        "OAS",
        "OptionValue",
        "ForwardWAL",
        "LongtermfWDCPR",
        "Duration",
        "Convexity",
        "effectiveDuration",
        "Effectiveconvexity",
    ]
    shock_cols = [shock_col(bps) for bps in SHOCKS_BPS]
    all_headers = py_cols + shock_cols

    for m in metrics_list:
        cusip = m.get("cusip")
        row = {k: _safe(m.get(k)) for k in py_cols}
        if cusip and cusip in scenario_by_cusip:
            row.update(scenario_by_cusip[cusip])
        rows.append(row)

    for r in rows:
        for h in all_headers:
            if h not in r:
                r[h] = ""

    print("Summary (first row):")
    if rows:
        print("  " + ", ".join(f"{k}={rows[0].get(k, '')}" for k in py_cols[:8]))
    print()

    out_path = output_csv_path(pricing_date)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=all_headers,
            extrasaction="ignore",
            delimiter="\t",
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Results saved to {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
