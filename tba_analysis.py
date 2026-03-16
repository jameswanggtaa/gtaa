import os
import time
import json
import csv
import requests as rq
from typing import Dict, Any, List, Optional

try:
    import pandas as pd
except ImportError:
    pd = None

OUTPUT_CSV = "tba_analysis_results.csv"
INPUT_EXCEL = "tba_analysis_input.xlsx"

AUTH_URL = "https://www.yieldbook.com/x/oauth/api-token"
API_BASE_URL = "https://api.yieldbook.com/analytics/v2"

# ----------------- Auth helpers ----------------- #

def _load_api_credentials() -> Dict[str, str]:
    """
    Load API client_id and client_secret.

    For now we hard‑code your values here so you don't
    need to rely on the file format.
    """
    api_id = "zwang@mtb.com-api"
    api_key = "557ee405-5bc7-f273-5ec4-d9ff91697656"
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
        raise RuntimeError(f"No accessToken in response: {resp.text}")
    return token


def api_url(endpoint: str, mode: str | None = None) -> str:
    if not mode:
        return "/".join([API_BASE_URL.strip("/"), endpoint.strip("/")])
    return "/".join([API_BASE_URL.strip("/"), mode.strip("/"), endpoint.strip("/")])


def api_headers(token: str) -> Dict[str, str]:
    return {
        "Accept": "*/*",
        "Authorization": f"Bearer {token}",
        "content-type": "application/json",
    }

# ----------------- Input data ----------------- #

# Defaults when not in Excel or for scenario-calc single-date job
PRICING_DATE = "2025-12-31"
PREPAY_RATE = 100
CURVE_TYPE = "SWAP_RFR"   # API value for RFRSwap
MODEL_CODE = 2501

# API-allowed prepay.type values; API also allows a numeric model code
_CANONICAL_PREPAY: Dict[str, str] = {
    "model": "Model", "currentmodel": "CurrentModel", "cpr": "CPR", "mhp": "MHP",
    "hep": "HEP", "abs": "ABS", "cpb": "CPB", "hpc": "HPC", "cpj": "CPJ", "cpy": "CPY",
    "vpr": "VPR", "ppv": "PPV", "psj": "PSJ", "psa": "PSA", "cpp": "CPP",
    "newmodel": "NewModel", "oldmodel": "OldModel", "preexpmodel": "PreExpModel",
    "oldexpmodel": "OldExpModel", "expmodel": "ExpModel",
}


def _normalize_prepay_type(val: Any) -> Any:
    """Return API-allowed prepay type; if unknown (e.g. Muni), return 'Model'."""
    if val is None:
        return "Model"
    if isinstance(val, (int, float)) and not (isinstance(val, bool)):
        return val
    s = str(val).strip()
    if not s:
        return "Model"
    if s.lower() == "muni":
        return "Model"
    return _CANONICAL_PREPAY.get(s.lower(), "Model")

# Loaded from tba_analysis_input.xlsx in main(); used by PY and scenario
SECURITIES: List[Dict[str, Any]] = []


def _normalize_excel_date(val: Any) -> Optional[str]:
    """Convert Excel date or string to YYYY-MM-DD."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    if not s:
        return None
    try:
        if pd is not None:
            parsed = pd.to_datetime(s)
            return parsed.strftime("%Y-%m-%d")
    except Exception:
        pass
    return s


def _curve_type_for_api(val: Any) -> str:
    """Map curve type from file (e.g. RFRSwap) to API (e.g. SWAP_RFR)."""
    if val is None or (isinstance(val, float) and pd is not None and pd.isna(val)):
        return CURVE_TYPE
    s = str(val).strip().upper()
    if not s:
        return CURVE_TYPE
    if "RFR" in s or "RFRSWAP" in s or s == "SWAP_RFR":
        return "SWAP_RFR"
    if s == "GVT":
        return "GVT"
    return s if len(s) > 0 else CURVE_TYPE


def load_securities_from_excel(path: str = INPUT_EXCEL) -> List[Dict[str, Any]]:
    """
    Read CUSIPs and model assumptions from tba_analysis_input.xlsx.
    Expected columns (case-insensitive): CUSIP/CUSIPs, coupon, maturity, market_price,
    book_price, curve_date, prepay_model, Prepay_rate, Vol_model, Curve_type/Curve_trpe.
    """
    if pd is None:
        raise RuntimeError("pandas is required to read Excel. Install: pip install pandas openpyxl")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Input file not found: {path}\n"
            "Create an Excel file with columns: CUSIP (or CUSIPs), coupon, maturity, market_price, "
            "book_price, curve_date, prepay_model, Prepay_rate, Vol_model, Curve_type (or Curve_trpe)."
        )

    # Read CUSIP as string to preserve values like 912828Z78 (Excel may otherwise convert to number)
    df = pd.read_excel(path, engine="openpyxl", dtype=str)
    df = df.fillna("")
    cols = {str(c).strip().lower(): c for c in df.columns}

    def col(*names: str) -> Optional[str]:
        for n in names:
            k = n.lower().strip()
            if k in cols:
                return cols[k]
            if k.replace("_", "") in cols:
                return cols[k.replace("_", "")]
            if k == "curve_trpe" or k == "curvetrpe":
                for k2 in cols:
                    if "curve" in k2 and "trpe" in k2:
                        return cols[k2]
            if "curve" in k and "type" in k:
                for k2 in cols:
                    if "curve" in k2 and "type" in k2:
                        return cols[k2]
        return None

    cusip_col = col("cusip", "cusips")
    if cusip_col is None:
        raise ValueError(f"Excel must have a CUSIP or CUSIPs column. Found: {list(df.columns)}")

    market_col = col("market_price", "marketprice")
    book_col = col("book_price", "bookprice")
    if market_col is None or book_col is None:
        raise ValueError("Excel must have market_price and book_price columns.")

    securities: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for _, row in df.iterrows():
        cusip_raw = row.get(cusip_col, "")
        cusip = str(cusip_raw).strip()
        # Restore CUSIPs that Excel read as number (e.g. 9.12828e+10 -> we can't recover Z; avoid float lookalike)
        if cusip and ("e+" in cusip.lower() or ("." in cusip and cusip.replace(".", "").replace("-", "").isdigit())):
            try:
                cusip = str(int(float(cusip)))
            except (TypeError, ValueError):
                pass
        if not cusip or cusip.lower() == "nan":
            if str(cusip_raw).strip():
                skipped.append(f"CUSIP '{cusip_raw}' (empty or invalid)")
            continue
        try:
            mp = float(row[market_col])
        except (TypeError, ValueError):
            mp = 0.0
        try:
            bp = float(row[book_col])
        except (TypeError, ValueError):
            bp = 0.0
        if mp <= 0:
            skipped.append(f"{cusip} (market_price missing or <= 0)")
            continue
        sec: Dict[str, Any] = {
            "cusip": cusip,
            "market_price": mp,
            "book_price": bp,
        }

        def _val(c: Optional[str]) -> Any:
            if not c or c not in row:
                return None
            v = row[c]
            if pd is not None and pd.isna(v):
                return None
            return v

        coupon_col = col("coupon")
        if coupon_col and _val(coupon_col) is not None:
            try:
                sec["coupon"] = float(_val(coupon_col))
            except (ValueError, TypeError):
                pass
        mat_col = col("maturity", "maturity date")
        if mat_col and _val(mat_col) is not None:
            sec["maturity"] = _normalize_excel_date(_val(mat_col))
        curve_date_col = col("curve_date", "curvedate")
        if curve_date_col and _val(curve_date_col) is not None:
            sec["curve_date"] = _normalize_excel_date(_val(curve_date_col))
        prepay_col = col("prepay_model", "prepaymodel")
        if prepay_col and _val(prepay_col) is not None:
            sec["prepay_type"] = _normalize_prepay_type(_val(prepay_col))
        else:
            sec["prepay_type"] = "Model"
        rate_col = col("prepay_rate", "prepayrate")
        if rate_col and _val(rate_col) is not None:
            try:
                sec["prepay_rate"] = int(float(_val(rate_col)))
            except (ValueError, TypeError):
                sec["prepay_rate"] = PREPAY_RATE
        else:
            sec["prepay_rate"] = PREPAY_RATE
        vol_col = col("vol_model", "volmodel")
        if vol_col and _val(vol_col) is not None:
            v = str(_val(vol_col)).strip()
            sec["vol_type"] = v if v else "Default"
        else:
            sec["vol_type"] = "LMMSOFRFLAT"
        curve_col = col("curve_type", "curve_trpe", "curvetype")
        if curve_col and _val(curve_col) is not None:
            sec["curve_type"] = _curve_type_for_api(_val(curve_col))
        else:
            sec["curve_type"] = CURVE_TYPE
        # Optional: current_balance / notional, book_value, market_value (for output table)
        bal_col = col("current_balance", "currentbalance", "notional")
        if bal_col and _val(bal_col) is not None and str(_val(bal_col)).strip():
            try:
                sec["current_balance"] = float(str(_val(bal_col)).replace(",", ""))
            except (ValueError, TypeError):
                pass
        bv_col = col("book_value", "bookvalue")
        if bv_col and _val(bv_col) is not None and str(_val(bv_col)).strip():
            try:
                sec["book_value"] = float(str(_val(bv_col)).replace(",", ""))
            except (ValueError, TypeError):
                pass
        mv_col = col("market_value", "marketvalue")
        if mv_col and _val(mv_col) is not None and str(_val(mv_col)).strip():
            try:
                sec["market_value"] = float(str(_val(mv_col)).replace(",", ""))
            except (ValueError, TypeError):
                pass
        securities.append(sec)
    if skipped:
        print("Skipped rows:", ", ".join(skipped[:10]), "..." if len(skipped) > 10 else "")
    return securities

SHOCKS_BPS = [-200, -100, 100, 200]

# ----------------- PY (price/yield) ----------------- #

def run_py_for_securities(token: str) -> List[Dict[str, Any]]:
    """Call /bond/py in sync mode for each security.

    Some securities do not support the "Single" volatility model, so if the
    first request indicates that, we retry with the default volatility setting.
    """

    def _build_input(sec: Dict[str, Any], vol_model: str) -> Dict[str, Any]:
        volatility = {"type": vol_model}
        if (vol_model or "").upper() == "SINGLE":
            volatility["rate"] = 0

        prepay_type = _normalize_prepay_type(sec.get("prepay_type", "Model"))
        prepay_rate = sec.get("prepay_rate", PREPAY_RATE)

        curve_type = sec.get("curve_type", CURVE_TYPE)
        return {
            "identifier": sec["cusip"],
            "idType": "securityIDEntry",
            "level": f"{sec['market_price']}",
            "userTag": sec["cusip"],
            "curve": {
                "curveType": curve_type,
            },
            "prepaySettings": {
                "type": prepay_type,
                "rate": prepay_rate,
            },
            "volatility": volatility,
            "extraSettings": {
                "optionModel": "OASEDUR",
            },
        }

    endpoint = "/bond/py"
    url = api_url(endpoint, mode="sync")

    results: List[Dict[str, Any]] = []
    for sec in SECURITIES:
        vol_model = (sec.get("vol_type") or "").strip() or "Default"
        pricing_date = sec.get("curve_date", PRICING_DATE) or PRICING_DATE
        body = {
            "globalSettings": {
                "pricingDate": pricing_date,
                "retrievePPMProjection": True,
            },
            "input": [_build_input(sec, vol_model)],
        }
        resp = rq.post(url, headers=api_headers(token), json=body)
        if not resp.ok:
            print(f"PY request failed (vol={vol_model}) for {sec['cusip']}. Response: {resp.text[:2000]}")
            resp.raise_for_status()
        data = resp.json()
        res = data.get("results", [])[0]

        # If Single vol is not supported for this security, retry with Default
        if (res.get("py") or {}).get("diagnostic", "").startswith("Single volatility is not available"):
            body["input"] = [_build_input(sec, "Default")]
            resp = rq.post(url, headers=api_headers(token), json=body)
            if not resp.ok:
                print(f"PY request failed (vol=Default) for {sec['cusip']}. Response: {resp.text[:2000]}")
                resp.raise_for_status()
            data = resp.json()
            res = data.get("results", [])[0]

        results.append(res)

    return results


def summarize_py_result(py_obj: Dict[str, Any], sec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract key measures from a single PY result.

    Adds the input security metadata (coupon/maturity/current balance) when available.
    """
    py = py_obj["py"]

    # approximate dollar duration ≈ effectiveDuration * price/100
    dollar_duration = py.get("effectiveDuration", 0.0) * py.get("economicExposure", 100.0) / 100.0

    return {
        "cusip": py.get("cusip"),
        "coupon": sec.get("coupon"),
        "maturity": sec.get("maturity"),
        "current_balance": sec.get("current_balance"),
        "book_value": sec.get("book_value"),
        "market_value": sec.get("market_value"),
        "book_price": sec.get("book_price"),
        "market_price": float(py.get("pyLevel", 0.0)),
        "forward_yield": py.get("forwardMeasures", {}).get("yield"),
        "effective_duration": py.get("effectiveDuration"),
        "effective_convexity": py.get("effectiveConvexity"),
        "effective_dv01": py.get("effectiveDV01"),
        "dollar_duration": dollar_duration,
        "average_life": py.get("wal"),
        "lt_cpr": py.get("dataPpmProjList", [{}])[1].get("longTerm") if len(py.get("dataPpmProjList", [])) > 1 else None,
        "oas": py.get("oas"),
        "z_spread": py.get("zSpread"),
    }


def _safe(val: Any) -> str:
    """Format value for CSV (handle None, float)."""
    if val is None:
        return ""
    if isinstance(val, float):
        return str(round(val, 6))
    return str(val)


def build_summary_table(summaries: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Flatten summaries for table/CSV: one row per CUSIP, string values."""
    rows = []
    for s in summaries:
        row = {k: _safe(v) for k, v in s.items()}
        rows.append(row)
    return rows


def print_summary_table(rows: List[Dict[str, Any]], headers: List[str]) -> None:
    """Print a formatted text table."""
    widths = [max(len(str(h)), 4) for h in headers]
    for row in rows:
        for i, h in enumerate(headers):
            v = row.get(h, "")
            widths[i] = max(widths[i], min(len(str(v)), 30))
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    head_line = "|" + "|".join(f" {str(h):<{widths[i]}} " for i, h in enumerate(headers)) + "|"
    print(sep)
    print(head_line)
    print(sep)
    for row in rows:
        print("|" + "|".join(f" {str(row.get(h, ''))[:widths[i]]:<{widths[i]}} " for i, h in enumerate(headers)) + "|")
    print(sep)


def save_summary_csv(rows: List[Dict[str, Any]], path: str) -> None:
    """Write summary rows to CSV. First column is cusip."""
    if not rows:
        return
    headers = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)
    print(f"Results saved to {path}")


# ----------------- Scenario calc ----------------- #

# Map scenarioID (scen1..scen4) to CSV column name for SHOCKS_BPS = [-200, -100, 100, 200]
SHOCK_COLUMN_NAMES = [
    "shock_neg200_bps",
    "shock_neg100_bps",
    "shock_pos100_bps",
    "shock_pos200_bps",
]
# Per-shock columns: price, effective duration, DV01, dollar return (same order as SHOCKS_BPS)
SHOCK_PRICE_COLS = ["price_neg200_bps", "price_neg100_bps", "price_pos100_bps", "price_pos200_bps"]
SHOCK_EFFECTIVE_DURATION_COLS = ["effective_duration_neg200_bps", "effective_duration_neg100_bps", "effective_duration_pos100_bps", "effective_duration_pos200_bps"]
SHOCK_DV01_COLS = ["dv01_neg200_bps", "dv01_neg100_bps", "dv01_pos100_bps", "dv01_pos200_bps"]
SHOCK_DOLLAR_RETURN_COLS = ["dollar_return_neg200_bps", "dollar_return_neg100_bps", "dollar_return_pos100_bps", "dollar_return_pos200_bps"]
ALL_SCENARIO_COLUMNS = (
    SHOCK_COLUMN_NAMES
    + SHOCK_PRICE_COLS
    + SHOCK_EFFECTIVE_DURATION_COLS
    + SHOCK_DV01_COLS
    + SHOCK_DOLLAR_RETURN_COLS
)


def _normalize_cusip(c: str) -> str:
    """Normalize for matching (API may return 8-char securityID vs 9-char CUSIP)."""
    return (c or "").strip()[:9]


def _extract_scenario_row_from_item(item: Dict[str, Any]) -> Dict[str, str]:
    """
    Parse Yield Book scenario-calc response: results[].scenario.horizon[] (4 items).
    Each horizon has: price, duration, dollarReturn, totalReturn, effectiveDV01 (if present).
    Returns a flat dict with all scenario columns (total return, price, effective duration, dv01, dollar return per shock).
    """
    row: Dict[str, str] = {col: "" for col in ALL_SCENARIO_COLUMNS}
    scenario = item.get("scenario") or {}
    horizon = scenario.get("horizon")
    if not isinstance(horizon, list) or len(horizon) < 4:
        return row
    for i, h in enumerate(horizon[:4]):
        if not isinstance(h, dict):
            continue
        # Total return (existing shock_* columns)
        if i < len(SHOCK_COLUMN_NAMES):
            tr = h.get("totalReturn")
            if tr is not None:
                row[SHOCK_COLUMN_NAMES[i]] = _safe(tr)
        # Price
        if i < len(SHOCK_PRICE_COLS):
            p = h.get("price") or h.get("actualPrice")
            if p is not None:
                row[SHOCK_PRICE_COLS[i]] = _safe(p)
        # Effective duration (duration at horizon)
        if i < len(SHOCK_EFFECTIVE_DURATION_COLS):
            ed = h.get("effectiveDuration") or h.get("duration")
            if ed is not None:
                row[SHOCK_EFFECTIVE_DURATION_COLS[i]] = _safe(ed)
        # DV01 (API may provide effectiveDV01 in horizon; often only at settlement)
        if i < len(SHOCK_DV01_COLS):
            dv = h.get("effectiveDV01") or h.get("dv01")
            if dv is not None:
                row[SHOCK_DV01_COLS[i]] = _safe(dv)
        # Dollar return
        if i < len(SHOCK_DOLLAR_RETURN_COLS):
            dr = h.get("dollarReturn")
            if dr is not None:
                row[SHOCK_DOLLAR_RETURN_COLS[i]] = _safe(dr)
    return row


def extract_scenario_columns(scen_results: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """
    Parse scenario-calc response into a dict: cusip -> { shock_*: val, price_*: val, ... }.
    Uses results[].scenario.horizon[] (Yield Book API structure).
    """
    out: Dict[str, Dict[str, str]] = {}
    results = scen_results.get("results") or scen_results.get("data") or []
    for idx, item in enumerate(results):
        row = _extract_scenario_row_from_item(item)
        cusip = SECURITIES[idx]["cusip"] if idx < len(SECURITIES) else (item.get("userTag") or item.get("cusip") or item.get("securityID") or str(idx))
        out[cusip] = row
    for sec in SECURITIES:
        c = sec["cusip"]
        if c not in out:
            out[c] = {col: "" for col in ALL_SCENARIO_COLUMNS}
    return out


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
    return scenarios


def build_horizon_info(curve_shifts_bps: List[int], prepay_rate: Optional[int] = None) -> List[Dict[str, Any]]:
    rate = prepay_rate if prepay_rate is not None else PREPAY_RATE
    infos = []
    for i, _ in enumerate(curve_shifts_bps, start=1):
        infos.append(
            {
                "scenarioID": f"scen{i}",
                "level": 0,
                "prepay": {
                    "rate": rate,
                },
            }
        )
    return infos


def run_scenario_calc(token: str) -> Dict[str, Any]:
    """
    Run scenario-calc for all three CUSIPs with shocks -200, -100, +100, +200 bps.
    """
    endpoint = "/bond/scenario-calc"
    url = api_url(endpoint, mode="req")

    scenarios = build_scenarios(SHOCKS_BPS)
    inputs = []

    # Use first security's curve_date for scenario job (API has one global pricingDate per job)
    scenario_pricing_date = SECURITIES[0].get("curve_date", PRICING_DATE) if SECURITIES else PRICING_DATE
    scenario_pricing_date = scenario_pricing_date or PRICING_DATE

    for sec in SECURITIES:
        prepay_type = _normalize_prepay_type(sec.get("prepay_type", "Model"))
        prepay_rate = sec.get("prepay_rate", PREPAY_RATE)
        curve_type = sec.get("curve_type", CURVE_TYPE)
        inputs.append(
            {
                "userTag": sec["cusip"],
                "identifier": sec["cusip"],
                "idType": "securityIDEntry",
                "curve": {
                    "curveType": curve_type,
                    "currency": "USD",
                },
                "settlementInfo": {
                    "level": sec["market_price"],
                    "settlementType": "CUSTOM",
                    "settlementDate": scenario_pricing_date,
                    "prepay": {
                        "type": prepay_type,
                        "rate": prepay_rate,
                    },
                },
                "horizonInfo": build_horizon_info(SHOCKS_BPS, prepay_rate=prepay_rate),
                "assumeCall": False,
                "horizonPYMethod": "OAS Change",
            }
        )

    body = {
        "globalSettings": {
            "pricingDate": scenario_pricing_date,
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

    # Poll results
    results_url = api_url(f"/results/{request_id}", mode=None)
    max_wait_seconds = 120
    waited = 0
    interval = 5
    while True:
        r = rq.get(results_url, headers=api_headers(token))
        if r.status_code == 404:
            time.sleep(interval)
            waited += interval
            if waited >= max_wait_seconds:
                print("Timeout waiting for scenario results (still 404). Returning latest response.")
                return {"meta": {"status": "UNKNOWN"}, "results": []}
            continue
        r.raise_for_status()
        j = r.json()
        if j.get("meta", {}).get("status") == "DONE":
            return j
        if waited >= max_wait_seconds:
            print("Timeout waiting for scenario results. Returning latest response.")
            return j
        time.sleep(interval)
        waited += interval

# ----------------- Main ----------------- #

def main() -> None:
    global SECURITIES
    print(f"Loading input from {INPUT_EXCEL}...")
    SECURITIES = load_securities_from_excel(INPUT_EXCEL)
    if not SECURITIES:
        raise SystemExit(f"No rows loaded from {INPUT_EXCEL}. Check CUSIP column and that rows are non-empty.")
    print(f"Loaded {len(SECURITIES)} security(ies): {', '.join(s['cusip'] for s in SECURITIES)}.\n")

    print("Getting access token...")
    token = get_access_token()
    print("Access token OK.\n")

    print("Running PY for CUSIPs...")
    py_results = run_py_for_securities(token)
    summaries = []
    for res, sec in zip(py_results, SECURITIES):
        summaries.append(summarize_py_result(res, sec))

    print("Running shock scenarios...")
    scen_results = run_scenario_calc(token)

    # Merge scenario columns (shock_neg200_bps, shock_neg100_bps, shock_pos100_bps, shock_pos200_bps) into summaries
    scenario_by_cusip = extract_scenario_columns(scen_results)
    for s in summaries:
        cusip = s.get("cusip")
        if cusip and cusip in scenario_by_cusip:
            s.update(scenario_by_cusip[cusip])

    # Build table: one row per CUSIP = cusip + PY outputs + shock scenario columns
    table_rows = build_summary_table(summaries)
    headers = list(table_rows[0].keys()) if table_rows else []

    print("\nSummary table (PY + shock scenarios):")
    print_summary_table(table_rows, headers)

    save_summary_csv(table_rows, OUTPUT_CSV)

    print("\nScenario result (raw JSON):")
    print(json.dumps(scen_results, indent=2))


if __name__ == "__main__":
    main()