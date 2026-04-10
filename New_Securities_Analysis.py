import os
import csv
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from datetime import date

import requests as rq
import pandas as pd
from openpyxl import load_workbook

# ---------------------------------------------------------
# PAC / Proxy‑aware HTTP session (M&T compatible)
# ---------------------------------------------------------
try:
    from pypac import PACSession
except ImportError:
    PACSession = None


def make_http_session():
    """
    Create a requests session that honors corporate proxy/PAC settings.
    If pypac is installed, PACSession will use the system PAC/proxy config.
    Otherwise we fall back to requests.Session (may fail behind proxy).
    """
    if PACSession is not None:
        return PACSession()
    return rq.Session()


def parallel_worker_count() -> int:
    """
    How many securities to process concurrently (thread pool).
    Set YB_WORKERS or YB_PARALLEL_WORKERS (default 1 = sequential).
    Use a modest value (e.g. 4–8); the API may rate-limit heavy parallelism.
    """
    raw = (os.environ.get("YB_WORKERS") or os.environ.get("YB_PARALLEL_WORKERS") or "1").strip()
    try:
        n = int(raw)
        return max(1, min(n, 32))
    except ValueError:
        return 1

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------
INPUT_EXCEL = "tba_analysis_input.csv"
INPUT_EXCEL_FALLBACKS = [
    "tba_analysis_input.csv",
    "tba_analysis_input.xlsx",
    "tba_analysis_input_with_cpr.xlsx",
    "tba_analysis_input_old.xlsx",
]
OUTPUT_CSV = "tba_analysis_results.csv"

AUTH_URL = "https://www.yieldbook.com/x/oauth/api-token"
#API_BASE = "https://api.yieldbook.com/analytics/v2"

API_BASE_URL = "https://api.yieldbook.com/analytics/v2"

def api_url(endpoint: str, mode: str | None = None) -> str:
    if not mode:
        return "/".join([API_BASE_URL.strip("/"), endpoint.strip("/")])
    return "/".join([API_BASE_URL.strip("/"), mode.strip("/"), endpoint.strip("/")])


SHOCKS_BPS = [-200, -100, 100, 200]

OUTPUT_COLUMNS = [
    "CUSIP",
    "Sub Type",

    "Forward_Yield",
    "Effective_Duration",
    "Effective_Convexity",
    "Effective_DV01",
    "Dollar_Duration",

    "PD_1Y",
    "PD_2Y",
    "PD_3Y",
    "PD_5Y",
    "PD_10Y",
    "PD_20Y",
    "PD_30Y",

    "Average_Life",
    "LT_CPR",
    "Life_CPR",
    "OAS",
    "Z_Spread",
    "Factor",
    "GWAC",
    "WALA",
    "WALS",
    "MaxServicerName",
    "MaxServicerPercent",

    "EffDur_-200",
    "DV01_-200",
    "DollarReturn_-200",

    "EffDur_-100",
    "DV01_-100",
    "DollarReturn_-100",

    "EffDur_+100",
    "DV01_+100",
    "DollarReturn_+100",

    "EffDur_+200",
    "DV01_+200",
    "DollarReturn_+200",
]


def scenario_columns() -> List[str]:
    cols: List[str] = []
    for shock in SHOCKS_BPS:
        shock_label = f"{shock:+d}" if shock > 0 else str(shock)
        cols.extend(
            [
                f"EffDur_{shock_label}",
                f"DV01_{shock_label}",
                f"DollarReturn_{shock_label}",
            ]
        )
    return cols
# ---------------------------------------------------------
# Auth
# ---------------------------------------------------------
def load_api_credentials() -> Dict[str, str]:
    """
    Expect credentials from environment variables:
    YB_CLIENT_ID, YB_CLIENT_SECRET
    """
    #return {
    #    "client_id": os.environ["YB_CLIENT_ID"],
    #    "client_secret": os.environ["YB_CLIENT_SECRET"],
    #}
    api_id = "zwang@mtb.com-api"
    api_key = "557ee405-5bc7-f273-5ec4-d9ff91697656"
    return {"client_id": api_id, "client_secret": api_key}



def get_access_token(session) -> str:
    creds = load_api_credentials()
    resp = session.post(
        AUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "audience": "API2-PROD",
            "ttl": "7200",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["accessToken"]


def api_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "Content-Type": "application/json",
    }

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
def normalize_date(val: Any) -> Optional[str]:
    if val is None or pd.isna(val):
        return None
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    return pd.to_datetime(val).strftime("%Y-%m-%d")


def normalize_prepay_type(val: Any) -> Any:
    """
    REST does not support Excel YBSW / muni curve construction.
    Muni is treated as Model for analytics.
    """
    if val is None:
        return "Model"
    s = str(val).strip().lower()
    if s in {"", "nan", "none"}:
        return "Model"
    if s == "muni":
        return "Model"
    if s.startswith("model"):
        return int("".join(filter(str.isdigit, s)))
    return s.upper()


def curve_type(val: Any, prepay_model: Any) -> str:
    """
    REST curveType string for Yield Book (maps file labels like RFRSwap to API enums).

    For prepay model Muni, Excel uses YBSW(\"MUNI\",\"USD\",...); the REST schema only
    accepts certain enums (e.g. SWAP_RFR). A trial of curveType MUNI returned HTTP 400
    \"Invalid value for field 'curveType'\". Default Munis to SWAP_RFR; set env
    YB_MUNI_CURVE_TYPE if LSEG documents a valid muni-specific value for your tenant.
    """
    if str(prepay_model).strip().lower() == "muni":
        ct = (os.environ.get("YB_MUNI_CURVE_TYPE") or "SWAP_RFR").strip()
        return ct if ct else "SWAP_RFR"
    s = str(val).strip().upper()
    if "RFR" in s:
        return "SWAP_RFR"
    return s or "SWAP_RFR"


def curve_dict_for(sec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Full curve object for /bond/py and scenario-calc.

    Excel YBSW(\"MUNI\",\"USD\", tenors, rates, ...) is evaluated in the add-in only; REST
    does not expose a YBSW() RPC. To align with that curve you must either use a
    documented curveType from LSEG or pass user-defined spots if/when your contract's
    OpenAPI lists the exact field names (trial \"MUNI\" as curveType returned HTTP 400).

    Optional tenor/rate pairs are stored on the security as muni_curve_points (from
    Excel B2:B14/E2:E14 or muni_ybsw_curve.csv) for logging and for future custom-curve
    wiring once the schema is known.
    """
    ct = curve_type(sec.get("curve_type"), sec.get("prepay_model"))
    return {"curveType": ct, "currency": "USD"}


def sub_type_uses_bond_yield(sub_type: Optional[str]) -> bool:
    """Muni and Treasury rows use street/yield; others use forward yield from the API."""
    st = (sub_type or "").strip().upper()
    if not st:
        return False
    if "MUNI" in st:
        return True
    if "TREASUR" in st:
        return True
    return False


def sub_type_is_treasury(sub_type: Optional[str]) -> bool:
    """Treasuries have no prepayment / vol model in the add-in; input fields stay blank."""
    st = (sub_type or "").strip().upper()
    return bool(st and "TREASUR" in st)


def resolve_forward_yield_column(py: Dict[str, Any], sub_type: Optional[str]) -> Any:
    """
    OUTPUT column Forward_Yield: for Muni/Treasury use bond yield (py.yield / effectiveYield);
    otherwise use forward yield (forwardMeasures.yield).
    """
    fy = py.get("forwardYield")
    y = py.get("bondYield")
    if sub_type_uses_bond_yield(sub_type):
        if y is not None:
            return y
        return fy
    if fy is not None:
        return fy
    return y


def computed_dollar_duration(sec: Dict[str, Any], py: Dict[str, Any]) -> Any:
    """
    Dollar_Duration = current_factor * Nominal * effectiveDV01 / 100
    current_factor defaults to 1 when not in input; Nominal from input column.
    Falls back to API dollarDuration when Nominal is missing.
    """
    api_dd = py.get("dollarDuration")
    dv01 = py.get("effectiveDV01")
    if dv01 is None:
        return api_dd
    try:
        dv01_f = float(dv01)
    except (TypeError, ValueError):
        return api_dd

    cf_raw = sec.get("current_factor")
    if cf_raw is None:
        cf = 1.0
    else:
        try:
            cf = float(cf_raw)
        except (TypeError, ValueError):
            cf = 1.0

    nom = sec.get("nominal")
    if nom is None:
        return api_dd
    try:
        nom_f = float(nom)
    except (TypeError, ValueError):
        return api_dd

    return cf * nom_f * dv01_f / 100.0


def pick_input_file() -> str:
    if os.path.isfile(INPUT_EXCEL):
        return INPUT_EXCEL
    for path in INPUT_EXCEL_FALLBACKS:
        if os.path.isfile(path):
            return path
    raise FileNotFoundError(
        "No input file found. Expected one of: "
        + ", ".join(INPUT_EXCEL_FALLBACKS)
    )


def normalize_colname(s: Any) -> str:
    return str(s).strip().lower().replace(" ", "").replace("_", "")


def col_lookup(df: pd.DataFrame) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for c in df.columns:
        out[normalize_colname(c)] = c
    return out


def find_col(cols: Dict[str, str], *names: str) -> Optional[str]:
    for n in names:
        key = normalize_colname(n)
        if key in cols:
            return cols[key]
    # Handle frequent typo: curve_trpe
    if any(normalize_colname(n) in {"curvetrpe", "curvetype"} for n in names):
        for key, actual in cols.items():
            if "curve" in key and ("type" in key or "trpe" in key):
                return actual
    return None


def parse_number(val: Any) -> Optional[float]:
    if val is None or pd.isna(val):
        return None
    s = str(val).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean_text(val: Any) -> Optional[str]:
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    return s or None


def default_pricing_date() -> str:
    return date.today().strftime("%Y-%m-%d")


def read_muni_curve_points(input_file: str) -> List[Dict[str, float]]:
    """
    Read tenor/rate columns that feed Excel YBSW(\"MUNI\",\"USD\", $B$2:$B$14, $E$2:$E$14, ...).

    From .xlsx: active sheet B2:B14 (years) and E2:E14 (rates, as in the add-in).
    If the input is not xlsx or ranges are empty, tries read_muni_spot_curve_sidecar().
    """
    if str(input_file).lower().endswith(".xlsx"):
        try:
            wb = load_workbook(input_file, data_only=True, read_only=True)
            ws = wb.active
            points: List[Dict[str, float]] = []
            for r in range(2, 15):
                tenor = parse_number(ws[f"B{r}"].value)
                rate = parse_number(ws[f"E{r}"].value)
                if tenor is None or rate is None:
                    continue
                points.append({"year": float(tenor), "rate": float(rate)})
            wb.close()
            if points:
                return points
        except Exception:
            pass
    return read_muni_spot_curve_sidecar(input_file)


def read_muni_spot_curve_sidecar(input_file: str) -> List[Dict[str, float]]:
    """
    Optional CSV alongside the portfolio file (same directory as input_file):

      muni_ybsw_curve.csv  (or Muni_ybsw_curve.csv)

    Columns (case-insensitive): year or tenor | rate or spot or yield
    Export the evaluated YBSW(MUNI,USD,...) curve from Excel into this file so CSV-only
    pipelines carry the same discount inputs as the add-in (for logging / future REST).
    """
    d = os.path.dirname(os.path.abspath(input_file))
    for name in ("muni_ybsw_curve.csv", "Muni_ybsw_curve.csv"):
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        try:
            df = pd.read_csv(p)
            cols = col_lookup(df)
            ycol = find_col(cols, "year", "tenor", "maturity", "maturityyears", "term")
            rcol = find_col(cols, "rate", "spot", "yield", "par", "zero")
            if not ycol or not rcol:
                continue
            out: List[Dict[str, float]] = []
            for _, row in df.iterrows():
                yv = parse_number(row[ycol])
                rv = parse_number(row[rcol])
                if yv is None or rv is None:
                    continue
                out.append({"year": float(yv), "rate": float(rv)})
            return out
        except Exception:
            continue
    return []


def muni_curve_points_debug_json(sec: Dict[str, Any]) -> str:
    """Compact JSON of muni_curve_points for support / OpenAPI questions."""
    import json

    pts = sec.get("muni_curve_points") or []
    return json.dumps(pts, indent=2)

# ---------------------------------------------------------
# Load Excel
# ---------------------------------------------------------
def load_securities() -> List[Dict[str, Any]]:
    input_file = pick_input_file()
    if input_file.lower().endswith(".csv"):
        df = pd.read_csv(input_file)
    else:
        df = pd.read_excel(input_file)

    cols = col_lookup(df)
    cusip_col = find_col(cols, "CUSIP", "CUSIPs")
    coupon_col = find_col(cols, "Coupon")
    maturity_col = find_col(cols, "Maturity_Date", "Maturity Date", "Maturity")
    sub_type_col = find_col(cols, "Sub Type", "Sub_Type", "Subtype")
    market_price_col = find_col(cols, "Market_price", "Market Price")
    curve_date_col = find_col(cols, "Curve_Date", "Curve date")
    prepay_model_col = find_col(cols, "Prepay_Model", "Prepayment model", "Prepayment_Model")
    prepay_rate_col = find_col(cols, "Prepay_Rate", "prepay_rate")
    vol_model_col = find_col(cols, "Vol_Model", "vol_model")
    curve_type_col = find_col(cols, "Curve_Type", "curve_type", "curve_trpe")
    nominal_col = find_col(cols, "Nominal", "CA_NOTIONAL", "Notional", "Current Balance")
    current_factor_col = find_col(cols, "Current Factor", "Current_Factor", "current_factor", "Factor_Input")

    required = {
        "CUSIP": cusip_col,
        "Coupon": coupon_col,
        "Maturity_Date": maturity_col,
        "Market_price": market_price_col,
    }
    missing = [k for k, v in required.items() if v is None]
    if missing:
        raise ValueError(f"Missing required column(s): {missing}. Found columns: {list(df.columns)}")

    muni_curve_points = read_muni_curve_points(input_file)
    out = []

    for _, r in df.iterrows():
        sub_type = clean_text(r[sub_type_col]) if sub_type_col else None
        prepay_model = r[prepay_model_col] if prepay_model_col else None
        cf_val = parse_number(r[current_factor_col]) if current_factor_col else None
        if sub_type_is_treasury(sub_type):
            prepay_model = None
            prepay_rate_val = None
            vol_model_val = None
        else:
            prepay_rate_val = parse_number(r[prepay_rate_col]) if prepay_rate_col else 100.0
            vol_model_val = clean_text(r[vol_model_col]) if vol_model_col else None
        out.append({
            "cusip": str(r[cusip_col]).strip(),
            "sub_type": sub_type,
            "coupon": parse_number(r[coupon_col]) if coupon_col else None,
            "maturity": normalize_date(r[maturity_col]),
            "market_price": parse_number(r[market_price_col]) or r[market_price_col],
            "curve_date": normalize_date(r[curve_date_col]) if curve_date_col else default_pricing_date(),
            "prepay_model": prepay_model,
            "prepay_rate": prepay_rate_val,
            "vol_model": vol_model_val,
            "curve_type": clean_text(r[curve_type_col]) if curve_type_col else "SWAP_RFR",
            "nominal": parse_number(r[nominal_col]) if nominal_col else None,
            "current_factor": float(cf_val) if cf_val is not None else 1.0,
            "muni_curve_points": muni_curve_points if str(prepay_model).strip().lower() == "muni" else [],
        })
    return out

# ---------------------------------------------------------
# PY Analytics
# ---------------------------------------------------------
def run_py(session, token: str, sec: Dict[str, Any]) -> Dict[str, Any]:
    prepay_rate = sec.get("prepay_rate")
    if sub_type_is_treasury(sec.get("sub_type")):
        if prepay_rate is None:
            prepay_rate = 0.0
    elif prepay_rate is None:
        prepay_rate = 100.0

    pricing_date = sec.get("curve_date") or default_pricing_date()
    curve_obj = curve_dict_for(sec)

    prepay_type = normalize_prepay_type(sec.get("prepay_model"))
    vol_raw = sec.get("vol_model")
    vol_model = (str(vol_raw).strip() or "Default") if vol_raw is not None else "Default"
    if sub_type_is_treasury(sec.get("sub_type")):
        vol_model = "Default"

    payload = {
        "globalSettings": {
            "pricingDate": pricing_date,
            "retrievePPMProjection": True,
        },
        "input": [{
            "identifier": sec["cusip"],
            "idType": "securityIDEntry",
            "level": str(sec["market_price"]),
            "userTag": sec["cusip"],
            "curve": curve_obj,
            "prepaySettings": {"type": prepay_type, "rate": prepay_rate},
            "volatility": {"type": vol_model},
            "extraSettings": {
                "optionModel": "OASEDUR",
                # Reverse-engineered from YBPRICE partial duration behavior.
                "includePartials": True,
            },
        }],
    }
    if vol_model.strip().upper() == "SINGLE":
        payload["input"][0]["volatility"]["rate"] = 0

    if str(sec.get("prepay_model", "")).strip().lower() == "muni":
        points = sec.get("muni_curve_points", [])
        if points:
            print(
                f"[INFO] {sec['cusip']}: found {len(points)} workbook tenor/rate points (B2:B14/E2:E14). "
                f"Pricing with {curve_obj} (YBSW MUNI/USD equivalent; override YB_MUNI_CURVE_TYPE)."
            )
        else:
            print(
                f"[INFO] {sec['cusip']}: Muni - pricing with {curve_obj} "
                f"(YB_MUNI_CURVE_TYPE overrides; API may reject 'MUNI' - use LSEG enum names)."
            )
        if (os.environ.get("YB_PRINT_MUNI_CURVE_POINTS") or "").strip().lower() in {"1", "true", "yes", "y"}:
            print(f"[INFO] {sec['cusip']} muni_curve_points JSON:\n{muni_curve_points_debug_json(sec)}")

    url = api_url("bond/py", mode="sync")
    r = session.post(url, json=payload, headers=api_headers(token), timeout=60)
    if not r.ok:
        print(f"[ERROR] /bond/py failed for {sec['cusip']} HTTP {r.status_code}")
        print(f"[ERROR] pricingDate={pricing_date}, curve={curve_obj}, prepayType={prepay_type}, prepayRate={prepay_rate}, volModel={vol_model}")
        print(f"[ERROR] response: {r.text[:2000]}")

        # Retry once with safest generic settings.
        retry_payload = payload.copy()
        retry_input = dict(payload["input"][0])
        retry_input["curve"] = {"curveType": "SWAP_RFR", "currency": "USD"}
        retry_input["prepaySettings"] = {"type": "Model", "rate": 100}
        retry_input["volatility"] = {"type": "Default"}
        retry_payload["input"] = [retry_input]
        retry_payload["globalSettings"] = {"pricingDate": default_pricing_date(), "retrievePPMProjection": True}

        rr = session.post(url, json=retry_payload, headers=api_headers(token), timeout=60)
        if not rr.ok:
            print(f"[ERROR] Retry /bond/py failed for {sec['cusip']} HTTP {rr.status_code}")
            print(f"[ERROR] retry response: {rr.text[:2000]}")
            rr.raise_for_status()
        py_obj = rr.json()["results"][0].get("py", {})
    else:
        py_obj = r.json()["results"][0].get("py", {})

    partial_durations = extract_partial_durations(py_obj)
    if not partial_durations:
        partial_durations = fetch_partial_durations_by_keywords(session, token, sec)
    if not partial_durations:
        print(f"[WARN] {sec['cusip']}: API did not return partial durations (including YBPRICE PDUR keywords).")

    # Normalize sync /bond/py response to expected downstream shape.
    max_servicer = (py_obj.get("maxServicer") or {})
    if isinstance(max_servicer, str):
        max_servicer = {"name": max_servicer, "percent": None}
    return {
        "forwardYield": (py_obj.get("ForwardYield") or {}).get("Yield"),
        "bondYield": py_obj.get("yield") or py_obj.get("effectiveYield") or py_obj.get("streetYield"),
        "effectiveDuration": py_obj.get("effectiveDuration"),
        "effectiveConvexity": py_obj.get("effectiveConvexity"),
        "effectiveDV01": py_obj.get("effectiveDV01"),
        "dollarDuration": py_obj.get("dollarDuration"),
        "partialDurations": partial_durations,
        "averageLife": py_obj.get("wal") or py_obj.get("averageLife"),
        "ltCPR": py_obj.get("ltCPR"),
        "lifeCPR": py_obj.get("lifeCPR"),
        "oas": py_obj.get("oas"),
        "zSpread": py_obj.get("zSpread"),
        "factor": py_obj.get("factor"),
        "gwac": py_obj.get("gwac"),
        "wala": py_obj.get("wala"),
        "wals": py_obj.get("wals"),
        "maxServicer": max_servicer,
    }

# ---------------------------------------------------------
# Scenario Calc
# ---------------------------------------------------------
def run_scenarios(session, token: str, sec: Dict[str, Any]) -> List[Dict[str, Any]]:
    pricing_date = sec.get("curve_date") or default_pricing_date()
    prepay_rate = sec.get("prepay_rate")
    if sub_type_is_treasury(sec.get("sub_type")):
        if prepay_rate is None:
            prepay_rate = 0.0
    elif prepay_rate is None:
        prepay_rate = 100.0
    scen_curve = curve_dict_for(sec)

    scenarios = []

    for i, s in enumerate(SHOCKS_BPS, start=1):
        scenarios.append({
            "scenarioID": f"scen{i}",
            "timing": "Gradual",
            "definition": {
                "userScenario": {
                    "shiftType": "Par",
                    "interpolationType": "Years",
                    "curveShifts": [{"year": 0.25, "value": s}],
                }
            }
        })
    payload = {
        "globalSettings": {
            "pricingDate": pricing_date,
            "horizonDays": 30,
        },
        "scenarios": scenarios,
        "calcInputs": [{
            "userTag": sec["cusip"],
            "identifier": sec["cusip"],
            "idType": "securityIDEntry",
            "curve": scen_curve,
            "settlementInfo": {
                "level": sec["market_price"],
                "settlementType": "CUSTOM",
                "settlementDate": pricing_date,
                "prepay": {
                    "type": normalize_prepay_type(sec.get("prepay_model")),
                    "rate": prepay_rate,
                },
            },
            "horizonInfo": [
                {"scenarioID": f"scen{i}", "level": 0, "prepay": {"rate": prepay_rate}}
                for i, _ in enumerate(SHOCKS_BPS, start=1)
            ],
            "assumeCall": False,
            "horizonPYMethod": "OAS Change",
        }],
    }

    #url = f"{API_BASE}/req/bond/scenario-calc"
    url = api_url("bond/scenario-calc", mode="req")
    r = session.post(url, json=payload, headers=api_headers(token), timeout=120)
    if not r.ok:
        print(f"[ERROR] /bond/scenario-calc failed for {sec['cusip']} HTTP {r.status_code}")
        print(f"[ERROR] pricingDate={pricing_date}, curve={scen_curve}, prepayRate={prepay_rate}")
        print(f"[ERROR] response: {r.text[:2000]}")
    r.raise_for_status()
    data = r.json()
    request_id = data.get("requestId")
    if not request_id:
        raise RuntimeError(f"No requestId returned from scenario-calc: {data}")

    results_url = api_url(f"/results/{request_id}", mode=None)
    for _ in range(24):
        rr = session.get(results_url, headers=api_headers(token), timeout=30)
        if rr.status_code == 404:
            time.sleep(2)
            continue
        rr.raise_for_status()
        jr = rr.json()
        if jr.get("meta", {}).get("status") == "DONE":
            results = jr.get("results", [])
            if not results:
                return []
            return (results[0].get("scenario") or {}).get("horizon", [])
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for scenario results for {sec['cusip']}")


def pick_metric(h: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in h and h.get(k) is not None:
            return h.get(k)
    return None


def derive_effective_dv01_at_horizon(h: Dict[str, Any]) -> Optional[float]:
    """
    Fallback approximation when API does not return EffectiveDV01AtHorizon.
    DV01 ~= Duration * Price / 10000.
    """
    dur = pick_metric(h, "effectiveDurationAtHorizon", "effectiveDuration", "duration")
    px = pick_metric(h, "fullPrice", "price", "actualFullPrice", "actualPrice")
    if dur is None or px is None:
        return None
    try:
        return float(dur) * float(px) / 10000.0
    except (TypeError, ValueError):
        return None


def merge_scenario_into_row(row: Dict[str, Any], scen: List[Dict[str, Any]]) -> Dict[str, Any]:
    for i, shock in enumerate(SHOCKS_BPS):
        h = scen[i] if i < len(scen) else {}
        shock_label = f"{shock:+d}" if shock > 0 else str(shock)
        row[f"EffDur_{shock_label}"] = pick_metric(
            h,
            "effectiveDurationAtHorizon",
            "effectiveDuration",
            "durationAtHorizon",
            "duration",
        )
        dv01_val = pick_metric(
            h,
            "effectiveDV01AtHorizon",
            "dv01AtHorizon",
            "effectiveDV01",
            "dv01",
            "spreadDV01",
        )
        dv01_num: Optional[float] = None
        try:
            if dv01_val is not None:
                dv01_num = float(dv01_val)
        except (TypeError, ValueError):
            dv01_num = None
        if dv01_num is None or abs(dv01_num) < 1e-12:
            derived_dv01 = derive_effective_dv01_at_horizon(h)
            if derived_dv01 is not None:
                dv01_val = derived_dv01
        row[f"DV01_{shock_label}"] = dv01_val
        row[f"DollarReturn_{shock_label}"] = pick_metric(
            h,
            "dollarReturnAtHorizon",
            "dollarReturn",
            "totalReturn",
        )
    return row


def extract_partial_durations(py_obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract partial/key-rate durations from common response shapes.
    Returns keys normalized to: 1Y, 2Y, 3Y, 5Y, 10Y, 20Y, 30Y.
    """
    candidates = [
        py_obj.get("partialDurations"),
        py_obj.get("partialDuration"),
        py_obj.get("keyRateDurations"),
        py_obj.get("keyrateDurations"),
        py_obj.get("keyRateDuration"),
    ]

    valid = {"1Y", "2Y", "3Y", "5Y", "10Y", "20Y", "30Y"}

    for c in candidates:
        if not c:
            continue
        if isinstance(c, dict):
            out: Dict[str, Any] = {}
            for k, v in c.items():
                k2 = str(k).strip().upper().replace("YR", "Y")
                if k2 in {"1", "2", "3", "5", "10", "20", "30"}:
                    k2 = f"{k2}Y"
                if k2 in valid:
                    out[k2] = v
            if out:
                return out
        if isinstance(c, list):
            out = {}
            for item in c:
                if not isinstance(item, dict):
                    continue
                node = pick_metric(item, "node", "year", "tenor", "key")
                val = pick_metric(item, "value", "duration", "partialDuration")
                if node is None or val is None:
                    continue
                try:
                    key = f"{int(float(node))}Y"
                except Exception:
                    continue
                if key in valid:
                    out[key] = val
            if out:
                return out
    # Common sync/bond/py shape when extraSettings.includePartials = true
    c = py_obj.get("dataPartialDurationList")
    if isinstance(c, list):
        out = {}
        for item in c:
            if not isinstance(item, dict):
                continue
            node = pick_metric(item, "partialDurationYear", "year", "node")
            val = pick_metric(item, "partialDuration", "duration", "value")
            if node is None or val is None:
                continue
            try:
                key = f"{int(float(node))}Y"
            except Exception:
                continue
            if key in {"1Y", "2Y", "3Y", "5Y", "10Y", "20Y", "30Y"}:
                out[key] = val
        if out:
            return out
    return {}


def extract_partial_durations_from_keyword_py(py_obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract partial durations from keyword-based YBPRICE style field names.
    Examples: PartialDuration1year, partialDuration10Year, PDUR1..PDUR7.
    """
    out: Dict[str, Any] = {}
    if not isinstance(py_obj, dict):
        return out

    year_map = {
        "1": "1Y",
        "2": "2Y",
        "3": "3Y",
        "5": "5Y",
        "10": "10Y",
        "20": "20Y",
        "30": "30Y",
    }
    pdur_map = {
        "PDUR1": "1Y",
        "PDUR2": "2Y",
        "PDUR3": "3Y",
        "PDUR4": "5Y",
        "PDUR5": "10Y",
        "PDUR6": "20Y",
        "PDUR7": "30Y",
    }

    for k, v in py_obj.items():
        ku = str(k).strip().upper()
        if ku in pdur_map and v is not None:
            out[pdur_map[ku]] = v
            continue
        if "PARTIALDURATION" in ku:
            # Handle forms like PartialDuration1year / PartialDuration10Year
            digits = "".join(ch for ch in ku if ch.isdigit())
            if digits in year_map and v is not None:
                out[year_map[digits]] = v
    return out


def fetch_partial_durations_by_keywords(session, token: str, sec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Try YBPRICE-style keyword request for partial durations via req/bond/py.
    """
    pricing_date = sec.get("curve_date") or default_pricing_date()
    if sub_type_is_treasury(sec.get("sub_type")):
        prepay_rate = sec.get("prepay_rate") if sec.get("prepay_rate") is not None else 0.0
    else:
        prepay_rate = sec.get("prepay_rate") or 100.0
    curve_obj = curve_dict_for(sec)
    prepay_type = normalize_prepay_type(sec.get("prepay_model"))

    keywords = [
        "PartialDuration1year",
        "PartialDuration2year",
        "PartialDuration3year",
        "PartialDuration5year",
        "PartialDuration10year",
        "PartialDuration20year",
        "PartialDuration30year",
        "PDUR1",
        "PDUR2",
        "PDUR3",
        "PDUR4",
        "PDUR5",
        "PDUR6",
        "PDUR7",
    ]

    body = {
        "keywords": keywords,
        "globalSettings": {"pricingDate": pricing_date},
        "pyCalcInputs": [{
            "identifier": sec["cusip"],
            "idType": "securityIDEntry",
            "level": str(sec["market_price"]),
            "userTag": sec["cusip"],
            "curve": curve_obj,
            "prepaySettings": {"type": prepay_type, "rate": prepay_rate},
            "volatility": {"type": "Single", "rate": 0},
            "extraSettings": {"optionModel": "OASEDUR"},
        }],
    }

    r = session.post(api_url("bond/py", mode="req"), json=body, headers=api_headers(token), timeout=60)
    if not r.ok:
        return {}
    request_id = r.json().get("requestId")
    if not request_id:
        return {}

    results_url = api_url(f"/results/{request_id}", mode=None)
    for _ in range(20):
        rr = session.get(results_url, headers=api_headers(token), timeout=30)
        if rr.status_code == 404:
            time.sleep(1)
            continue
        if not rr.ok:
            return {}
        jr = rr.json()
        if jr.get("meta", {}).get("status") == "DONE":
            py_kw = (jr.get("results") or [{}])[0].get("py", {})
            return extract_partial_durations_from_keyword_py(py_kw)
        time.sleep(1)
    return {}


def run_indic(session, token: str, sec: Dict[str, Any]) -> Dict[str, Any]:
    """
    YBINDIC-equivalent pull via REST /sync/bond/indic.
    Requested fields:
    Factor, PPHistCPRLife, GrossWAC, LoanAge, WeightedAvgLoanSize,
    MaxServiceName/MaxServicerName, MaxServicerPercent.
    """
    indic_date = sec.get("curve_date") or default_pricing_date()
    keywords = [
        "Factor",
        "PPHistCPRLife",
        "GrossWAC",
        "LoanAge",
        "WeightedAvgLoanSize",
        "MaxServiceName",      # keep user-requested spelling
        "MaxServicerName",     # also try API-servicer spelling
        "MaxServicerPercent",
    ]
    payload = {
        "keywords": keywords,
        "identifierInfos": [{
            "identifier": sec["cusip"],
            "idType": "securityIDEntry",
        }],
        "globalSettings": {
            "indicDate": indic_date,
        },
    }
    r = session.post(api_url("bond/indic", mode="sync"), json=payload, headers=api_headers(token), timeout=60)
    if not r.ok:
        print(f"[WARN] /bond/indic failed for {sec['cusip']} HTTP {r.status_code}: {r.text[:500]}")
        return {}

    results = r.json().get("results") or []
    if not results:
        return {}
    indic = results[0].get("indic") or {}
    if not indic:
        return {}

    max_name = indic.get("maxServicerName")
    if max_name is None:
        max_name = indic.get("maxServiceName")

    return {
        "factor": indic.get("factor"),
        "lifeCPR": indic.get("ppHistCPRLife"),
        "gwac": indic.get("grossWAC"),
        "wala": indic.get("loanAge"),
        "wals": indic.get("weightedAvgLoanSize"),
        "maxServicerName": max_name,
        "maxServicerPercent": indic.get("maxServicerPercent"),
    }


def run_py_keyword_measures(session, token: str, sec: Dict[str, Any]) -> Dict[str, Any]:
    """
    YBPRICE keyword-style pull for:
    EffectiveWAL, LongTermCPR, OAS, ZSpread.
    """
    pricing_date = sec.get("curve_date") or default_pricing_date()
    sub_type = (sec.get("sub_type") or "").strip()
    prepay_rate = sec.get("prepay_rate")
    if sub_type_is_treasury(sec.get("sub_type")):
        if prepay_rate is None:
            prepay_rate = 0.0
    elif prepay_rate is None:
        prepay_rate = 100.0
    # User rule: for Agency MBS ARM, force prepay rate to 15 in YBPRICE avg-life call.
    if sub_type.lower() == "agency mbs arm":
        prepay_rate = 15.0
    curve_obj = curve_dict_for(sec)
    prepay_type = normalize_prepay_type(sec.get("prepay_model"))

    def _submit(volatility: Dict[str, Any]) -> Dict[str, Any]:
        body = {
            "keywords": ["EffectiveWAL", "LongTermCPR", "OAS", "ZSpread"],
            "globalSettings": {"pricingDate": pricing_date},
            "pyCalcInputs": [{
                "identifier": sec["cusip"],
                "idType": "securityIDEntry",
                "level": str(sec["market_price"]),
                # REST does not expose a direct securitySubType field, use props to pass subtype context.
                "props": {"subType": sub_type} if sub_type else {},
                "curve": curve_obj,
                "prepaySettings": {"type": prepay_type, "rate": prepay_rate},
                "volatility": volatility,
                "extraSettings": {"optionModel": "OASEDUR"},
            }],
        }
        r = session.post(api_url("bond/py", mode="req"), json=body, headers=api_headers(token), timeout=60)
        if not r.ok:
            return {}
        request_id = r.json().get("requestId")
        if not request_id:
            return {}
        results_url = api_url(f"/results/{request_id}", mode=None)
        for _ in range(20):
            rr = session.get(results_url, headers=api_headers(token), timeout=30)
            if rr.status_code == 404:
                time.sleep(1)
                continue
            if not rr.ok:
                return {}
            jr = rr.json()
            if jr.get("meta", {}).get("status") == "DONE":
                return (jr.get("results") or [{}])[0].get("py", {}) or {}
            time.sleep(1)
        return {}

    # Try Single first for consistency, then fallback to Default.
    py_kw = _submit({"type": "Single", "rate": 0})
    if py_kw.get("returnCode") == 1 or not py_kw:
        py_kw = _submit({"type": "Default"})

    return {
        "averageLife": py_kw.get("effectiveWAL"),
        "ltCPR": py_kw.get("longTermCPR") if py_kw.get("longTermCPR") is not None else py_kw.get("ltCPR"),
        "oas": py_kw.get("oas"),
        "zSpread": py_kw.get("zSpread"),
    }


def run_full_analysis_for_security(session, token: str, sec: Dict[str, Any]) -> Dict[str, Any]:
    """One security: PY + keyword + scenario + indic; merged output row for OUTPUT_COLUMNS."""
    py = run_py(session, token, sec)
    py_kw = run_py_keyword_measures(session, token, sec)
    scen = run_scenarios(session, token, sec)
    indic = run_indic(session, token, sec)

    row = {
        "CUSIP": sec["cusip"],
        "Sub Type": sec.get("sub_type"),

        "Forward_Yield": resolve_forward_yield_column(py, sec.get("sub_type")),
        "Effective_Duration": py.get("effectiveDuration"),
        "Effective_Convexity": py.get("effectiveConvexity"),
        "Effective_DV01": py.get("effectiveDV01"),
        "Dollar_Duration": computed_dollar_duration(sec, py),

        "PD_1Y": py.get("partialDurations", {}).get("1Y"),
        "PD_2Y": py.get("partialDurations", {}).get("2Y"),
        "PD_3Y": py.get("partialDurations", {}).get("3Y"),
        "PD_5Y": py.get("partialDurations", {}).get("5Y"),
        "PD_10Y": py.get("partialDurations", {}).get("10Y"),
        "PD_20Y": py.get("partialDurations", {}).get("20Y"),
        "PD_30Y": py.get("partialDurations", {}).get("30Y"),

        "Average_Life": py_kw.get("averageLife") if py_kw.get("averageLife") is not None else py.get("averageLife"),
        "LT_CPR": py_kw.get("ltCPR") if py_kw.get("ltCPR") is not None else py.get("ltCPR"),
        "Life_CPR": py.get("lifeCPR") if py.get("lifeCPR") is not None else indic.get("lifeCPR"),
        "OAS": py_kw.get("oas") if py_kw.get("oas") is not None else py.get("oas"),
        "Z_Spread": py_kw.get("zSpread") if py_kw.get("zSpread") is not None else py.get("zSpread"),
        "Factor": py.get("factor") if py.get("factor") is not None else indic.get("factor"),
        "GWAC": py.get("gwac") if py.get("gwac") is not None else indic.get("gwac"),
        "WALA": py.get("wala") if py.get("wala") is not None else indic.get("wala"),
        "WALS": py.get("wals") if py.get("wals") is not None else indic.get("wals"),
        "MaxServicerName": py.get("maxServicer", {}).get("name")
        if py.get("maxServicer", {}).get("name") is not None
        else indic.get("maxServicerName"),
        "MaxServicerPercent": py.get("maxServicer", {}).get("percent")
        if py.get("maxServicer", {}).get("percent") is not None
        else indic.get("maxServicerPercent"),
    }

    row = merge_scenario_into_row(row, scen)
    return row


def run_fast_scenario_refresh(session, token: str, securities: List[Dict[str, Any]]) -> None:
    """
    Refresh only scenario shock columns in existing OUTPUT_CSV.
    """
    scen_cols = scenario_columns()
    if os.path.isfile(OUTPUT_CSV):
        df = pd.read_csv(OUTPUT_CSV)
    else:
        df = pd.DataFrame(columns=["CUSIP"] + scen_cols)

    if "CUSIP" not in df.columns:
        raise ValueError(f"{OUTPUT_CSV} must contain a CUSIP column for fast refresh mode.")
    if "Sub Type" not in df.columns:
        df["Sub Type"] = pd.NA

    for c in scen_cols:
        if c not in df.columns:
            df[c] = pd.NA

    # Normalize join key
    df["CUSIP"] = df["CUSIP"].astype(str).str.strip()
    idx_by_cusip = {c: i for i, c in enumerate(df["CUSIP"].tolist())}

    for sec in securities:
        cusip = str(sec.get("cusip", "")).strip()
        sub_type = sec.get("sub_type")
        if not cusip:
            continue
        try:
            scen = run_scenarios(session, token, sec)
        except Exception as e:
            print(f"[ERROR] Scenario refresh failed for {cusip}: {type(e).__name__}: {e}")
            continue

        upd = merge_scenario_into_row({"CUSIP": cusip}, scen)
        if cusip in idx_by_cusip:
            row_idx = idx_by_cusip[cusip]
            df.at[row_idx, "Sub Type"] = sub_type
            for c in scen_cols:
                df.at[row_idx, c] = upd.get(c)
        else:
            new_row = {col: pd.NA for col in df.columns}
            new_row["CUSIP"] = cusip
            new_row["Sub Type"] = sub_type
            for c in scen_cols:
                new_row[c] = upd.get(c)
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            idx_by_cusip[cusip] = len(df) - 1

    df.to_csv(OUTPUT_CSV, index=False, float_format="%.6f")
    print(f"Fast scenario refresh complete: {OUTPUT_CSV}")

# ---------------------------------------------------------
# Main
# ---------------------------------------------------------
def main():
    session = make_http_session()
    token = get_access_token(session)

    securities = load_securities()
    fast_mode = str(os.getenv("FAST_SCENARIO_ONLY", "")).strip().lower() in {"1", "true", "yes", "y"}
    if fast_mode:
        run_fast_scenario_refresh(session, token, securities)
        return

    workers = parallel_worker_count()
    rows: List[Dict[str, Any]] = []

    if workers > 1:
        print(f"[INFO] YB_WORKERS={workers} (parallel securities).")

        def _work(sec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            s = make_http_session()
            try:
                return run_full_analysis_for_security(s, token, sec)
            except Exception as e:
                print(f"[ERROR] Skipping {sec.get('cusip')} due to API error: {type(e).__name__}: {e}")
                return None

        with ThreadPoolExecutor(max_workers=workers) as ex:
            out = list(ex.map(_work, securities))
        rows = [r for r in out if r is not None]
    else:
        for sec in securities:
            try:
                rows.append(run_full_analysis_for_security(session, token, sec))
            except Exception as e:
                print(f"[ERROR] Skipping {sec.get('cusip')} due to API error: {type(e).__name__}: {e}")
                continue

    df = pd.DataFrame(rows)

    # Enforce exact Excel template layout
    df = df.reindex(columns=OUTPUT_COLUMNS)

    df.to_csv(OUTPUT_CSV, index=False, float_format="%.6f")
    print(f"Saved results to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
