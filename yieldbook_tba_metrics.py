"""
Yieldbook REST API: TBA metrics and parallel yield curve shocks.

OpenAPI: https://www.yieldbook.com/s/restapi/ (./openapi/openapi.yaml)
Server: https://api.yieldbook.com/analytics/v2

Endpoints used:
- TBA Pricing:     GET /sync/tba-pricing (query: job, name, pri, tags per spec)
- PY Calculation:  POST /sync/bond/py
- Scenario Calc:   POST or GET /sync/bond/scenario-calc (sync) or
                   POST /req/bond/scenario-calc then GET /results/{requestId} (async)
- Scenario setups: GET /sync/ref-data/scenario-setups

- Uses SIFMA Class A (mbs_settlement): Yieldbook PROD-{MON} rolls
  TBA_YB_ROLL_DAYS_BEFORE_CLASS_A days before Class A; settlementDate matches that month.
- PrevClose from GET /sync/tba-pricing; PY and scenario-calc use that level.
- Pulls: Forwardyield, Yieldcurrentmargin, OAS, ForwardWAL, LongtermfWDCPR,
  Duration, Convexity, effectiveDuration, Effectiveconvexity.
- Shock scenarios: YBSCEN parallel curve shocks via POST /sync/bond/scenario-calc
  (horizonPYMethod "OAS Change", scenarioRef /sys/scenario/Par/{bps}?timing=Gradual&...).

TBA CUSIPs: FNM30 30yr FNMA, coupons 3.0–7.0%, 2025 (CTD).
"""

import csv
import os
import requests as rq
from datetime import date
from typing import Any, Dict, List, Optional

from mbs_settlement import (
    get_last_business_day_iso,
    get_latest_class_a_settlement_before,
    get_next_settlement_date,
    get_yieldbook_tba_contract_month,
    get_yieldbook_tba_prod_suffix,
    get_yieldbook_tba_settlement_date,
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

# Map TBA CUSIP -> Yieldbook coupon prefix for YBTBAPRICE (suffix PROD-{MON} from mbs_settlement).
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

# Parallel yield curve shocks (basis points)
SHOCKS_BPS = [-300, -200, -100, -50, -25, -10, -5, 5, 10, 25, 50, 100, 200, 300]
#SHOCKS_BPS = [-200, -50, 50, 200]
MAX_SCENARIOS_PER_REQUEST = 7  # Yieldbook /sync/bond/scenario-calc limit

# SOFR swap curve (scenario-calc input curveType)
CURVE_TYPE = "SWAP_RFR"
PREPAY_RATE = 100
OUTPUT_CSV = "yieldbook_tba_metrics_results.csv"
PREPAY_MODEL = "Model"
# Scenario-calc (YBSCEN): LMMSOFR flat vol per Yieldbook scenario-calc sync payload
VOLATILITY_TYPE = "LMMSOFRFlat"

# Optional query suffix for YBSCEN Par shift refs (override via env if needed)
YBSCEN_SCENARIO_QUERY = (
    os.getenv(
        "YB_YBSCEN_SCENARIO_QUERY",
        "timing=Gradual&reinvestmentRate=Default&swapSpreadConst=true",
    ).strip()
)

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


def get_security_name_for_tba(cusip: str, as_of: date) -> str:
    """
    Return Yieldbook security name for TBA price (YBTBAPRICE), e.g. FNMA3.0-PROD-MAY.

    PROD-{MON} uses get_yieldbook_tba_prod_suffix(as_of): same month as the next Class A
    delivery on or after as_of, rolling to the following month when as_of is within
    TBA_YB_ROLL_DAYS_BEFORE_CLASS_A calendar days of that settle (see mbs_settlement).
    """
    if cusip in TBA_CUSIP_TO_SECURITY_NAME_APR:
        suffix = get_yieldbook_tba_prod_suffix(as_of)
        prefix = TBA_CUSIP_TO_SECURITY_NAME_APR[cusip].rsplit("-", 1)[0]
        return f"{prefix}-{suffix}"
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
        # Keep the original input identifier for joins with scenario output.
        "cusip": py_obj.get("userTag") or py.get("cusip"),
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
# Scenario-calc — YBSCEN parallel Par shocks (sync) + OAS Change horizon
# -----------------------------------------------------------------------------


def _ybscen_scenario_ref(bps: int) -> Dict[str, str]:
    """Yieldbook YBSCEN-style scenario reference for parallel Par shift (bps)."""
    q = YBSCEN_SCENARIO_QUERY
    path = f"/sys/scenario/Par/{bps}"
    if q:
        ref = f"{path}?{q}"
    else:
        ref = path
    return {"$ref": ref}


def _build_ybscen_sync_body(
    cusip: str,
    settlement_date: str,
    level_prevclose: float,
    shocks_bps: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Single input object for POST /sync/bond/scenario-calc (YBSCEN formula)."""
    shocks = shocks_bps if shocks_bps is not None else SHOCKS_BPS
    horizon_info: List[Dict[str, Any]] = []
    for bps in shocks:
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


def _horizon_prices_from_sync_payload(
    data: Any,
    cusip: str,
    expected_count: int,
) -> Optional[List[Optional[float]]]:
    """
    Parse sync scenario-calc JSON: results[].scenario.horizon[] in request shock order.
    """
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
        if i < len(horizon):
            out.append(_horizon_price(horizon[i]))
        else:
            out.append(None)
    return out


def _safe(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float):
        return str(round(val, 6))
    return str(val)


def _horizon_price(h: Any) -> Optional[float]:
    """
    Extract numeric shocked price from a scenario-calc GET response.
    API may return shocked price at top level or under 'py' (price, economicExposure, pyLevel, marketValue).
    """
    if not isinstance(h, dict):
        return None
    for key in ("price", "marketValue", "economicExposure", "pyLevel", "value"):
        v = h.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    py = h.get("py") or {}
    if isinstance(py, dict):
        for key in ("price", "economicExposure", "pyLevel", "marketValue"):
            v = py.get(key)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
    return None


def run_scenario_calc(
    token: str,
    pricing_date: str,
    settlement_date: str,
    py_metrics: List[Dict[str, Any]],
) -> Dict[str, Dict[str, str]]:
    """
    Per TBA: POST https://api.yieldbook.com/analytics/v2/sync/bond/scenario-calc
    with YBSCEN scenarioRef (/sys/scenario/Par/{bps}?...) and horizonPYMethod
    "OAS Change". globalSettings: usePreviousClose, horizonDays/Months 0, horizon
    effective/option measures. Parses results[].scenario.horizon[] for shocked prices.
    Missing shocks (API error or empty horizon) are left blank.
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

    for m in py_metrics:
        cusip = m.get("cusip")
        if not cusip:
            continue
        try:
            base_price = float(m.get("price_last_close"))
        except (TypeError, ValueError):
            continue

        row: Dict[str, str] = {f"price_bps_{bps:+d}": "" for bps in SHOCKS_BPS}

        for chunk_idx, chunk in enumerate(shock_chunks, start=1):
            body = {
                "input": [_build_ybscen_sync_body(cusip, settlement_date, base_price, chunk)],
                "globalSettings": global_settings,
            }

            resp = rq.post(url, headers=api_headers(token), json=body, timeout=120)
            prices: Optional[List[Optional[float]]] = None
            if resp.ok:
                try:
                    prices = _horizon_prices_from_sync_payload(resp.json(), cusip, len(chunk))
                except (ValueError, TypeError):
                    prices = None
            else:
                print(
                    f"scenario-calc sync failed for {cusip}, chunk {chunk}: "
                    f"{resp.status_code} {resp.text[:500]}"
                )

            for i, bps in enumerate(chunk):
                col = f"price_bps_{bps:+d}"
                shocked: Optional[float] = None
                if prices is not None and i < len(prices):
                    shocked = prices[i]
                if shocked is not None:
                    row[col] = _safe(shocked)

            if chunk_idx == 1:
                print(
                    f"  scenario-calc first chunk finished for {cusip} "
                    f"({len(chunk)} shocks)"
                )

        for bps in SHOCKS_BPS:
            col = f"price_bps_{bps:+d}"
            if col not in row:
                row[col] = ""

        scenario_by_cusip[cusip] = row

    return scenario_by_cusip


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main() -> None:
    as_of = date.today()
    pricing_date = get_last_business_day_iso(as_of)
    settlement_date = get_yieldbook_tba_settlement_date(as_of)
    last_done = get_latest_class_a_settlement_before(as_of)
    next_any = get_next_settlement_date(as_of)
    yb_ym = get_yieldbook_tba_contract_month(as_of)
    yb_mon = get_yieldbook_tba_prod_suffix(as_of)
    print(f"Pricing date (last business day): {pricing_date}")
    print(f"As-of (calendar):                 {as_of.isoformat()}")
    if last_done:
        print(f"Latest Class A settled (before as-of): {last_done}")
    print(f"Next Class A on or after as-of:   {next_any}")
    print(
        f"Yieldbook contract month:         {yb_ym} (PROD-{yb_mon}); "
        f"settlementDate for API: {settlement_date}"
    )
    print(f"TBAs: {len(TBA_CUSIPS)}")
    print(f"Shocks (bps): {SHOCKS_BPS}\n")

    print("Getting access token...")
    token = get_access_token()
    print("Token OK.\n")

    # Resolve TBA -> security name (e.g. FNMA3.0-PROD-MAY after roll) and fetch YBTBAPRICE PrevClose
    print("Fetching YBTBAPRICE PrevClose for each TBA (security name by contract month)...")
    cusip_to_level: Dict[str, float] = {}
    cusip_to_security_name: Dict[str, str] = {}
    for cusip in TBA_CUSIPS:
        sec_name = get_security_name_for_tba(cusip, as_of)
        cusip_to_security_name[cusip] = sec_name
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
            m["tba_security"] = cusip_to_security_name.get(cusip, "")
        metrics_list.append(m)
    print(f"PY done: {len(metrics_list)} results.\n")

    print(
        f"Running scenario-calc (YBSCEN /sync/bond/scenario-calc, OAS Change, pricing {pricing_date})..."
    )
    scenario_by_cusip = run_scenario_calc(
        token, pricing_date, settlement_date, metrics_list
    )
    print("Scenario-calc done.\n")

    # Merge PY metrics + shock columns
    rows: List[Dict[str, Any]] = []
    py_cols = [
        "tba_security",
        "price_last_close",
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
