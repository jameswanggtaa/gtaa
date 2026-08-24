import os
import math
import csv
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from datetime import date

import requests as rq
import pandas as pd

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
    raw = (os.environ.get("YB_WORKERS") or os.environ.get("YB_PARALLEL_WORKERS") or "10").strip()
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
# Portfolio 0526 Excel layout: -300 … +300 (six YBSCEN Par shocks).
SHOCKS_BPS_0526 = [-300, -200, -100, 100, 200, 300]
SCENARIO_TIMING = "Immediate"
SCENARIO_SHIFT_TYPE = "Par"
SCENARIO_INTERPOLATION_TYPE = "Years"
SCENARIO_HORIZON_PY_METHOD = "OAS Change"
SCENARIO_HORIZON_DAYS = 0
SCENARIO_HORIZON_MONTHS = 0


def scenario_horizon_py_method() -> str:
    """
    Horizon repricing method for /bond/scenario-calc (e.g. OAS Change, Spread Change).
    Override with env YB_SCENARIO_HORIZON_PY_METHOD to match Excel/YBSCEN workbook defaults.
    """
    v = (os.environ.get("YB_SCENARIO_HORIZON_PY_METHOD") or "").strip()
    return v if v else SCENARIO_HORIZON_PY_METHOD


def scenario_timing() -> str:
    """Scenario curve shift timing (e.g. Immediate, Gradual). Override: YB_SCENARIO_TIMING."""
    v = (os.environ.get("YB_SCENARIO_TIMING") or "").strip()
    return v if v else SCENARIO_TIMING


def normalize_scenario_horizon(
    horizon: Any,
    shocks_bps: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """
    Reorder scenario horizon entries to scen1..scenN matching shocks_bps when scenarioIDs exist.
    Avoids mis-mapping EffDur columns if the API returns horizons out of order.
    """
    shocks = list(shocks_bps if shocks_bps is not None else SHOCKS_BPS)
    if not isinstance(horizon, list):
        return []
    rows = [h for h in horizon if isinstance(h, dict)]
    if not rows:
        return []
    by_id = {
        str(h["scenarioID"]): h
        for h in rows
        if h.get("scenarioID") is not None and str(h.get("scenarioID")).strip() != ""
    }
    expected = [f"scen{i}" for i in range(1, len(shocks) + 1)]
    if all(sid in by_id for sid in expected):
        return [by_id[sid] for sid in expected]
    return rows


# Keys for scenario horizon effective duration (Excel YBSCEN EffectiveDurationAtHorizon).
# With calcHorizonEffectiveMeasures, API populates effectiveDuration / effectiveDV01.
SCENARIO_EFFDUR_KEYS = (
    "effectiveDurationAtHorizon",
    "fundedEffectiveDurationAtHorizon",
    "fundedEffectiveDuration",
    "effectiveDuration",
    "durationAtHorizon",
    "duration",
)
SCENARIO_DV01_KEYS = (
    "effectiveDV01AtHorizon",
    "dv01AtHorizon",
    "effectiveDV01",
    "dv01",
    "spreadDV01",
)
OUTPUT_COLUMNS = [
    "CUSIP",
    "Sub Type",

    "Forward_Yield",
    "Prospective_Yield",
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



# Token ttl is 7200s; refresh well before expiry so multi-hour benchmark runs don't hit 401s.
_TOKEN_STATE: Dict[str, Any] = {"token": None, "fetched_at": 0.0}
_TOKEN_LOCK = threading.Lock()
TOKEN_REFRESH_SEC = 5400


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
    token = resp.json()["accessToken"]
    _TOKEN_STATE["token"] = token
    _TOKEN_STATE["fetched_at"] = time.time()
    return token


def api_headers(token: str) -> Dict[str, str]:
    cached = _TOKEN_STATE.get("token")
    if cached:
        if time.time() - _TOKEN_STATE["fetched_at"] > TOKEN_REFRESH_SEC:
            with _TOKEN_LOCK:
                if time.time() - _TOKEN_STATE["fetched_at"] > TOKEN_REFRESH_SEC:
                    try:
                        get_access_token(make_http_session())
                        print("[INFO] Yield Book access token refreshed (ttl keep-alive).", flush=True)
                    except Exception as e:
                        print(f"[WARN] Yield Book token refresh failed ({e}); using current token.", flush=True)
        token = _TOKEN_STATE["token"]
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
    Map Excel prepay model labels to REST ``prepaySettings.type``.

    Excel's **Prepay Model = Muni** names the municipal *pricing curve* recipe (YBSW),
    not a mortgage PSA/Model stream. For actual REST payloads we normally send
    ``CPY`` / ``0`` instead (see ``prepay_type_and_rate_for_api``); set
    ``YB_MUNI_PREPAY_LEGACY=1`` to restore the older ``Model`` / file-rate mapping.
    """
    if val is None:
        return "Model"
    s = str(val).strip().lower()
    if s in {"", "nan", "none"}:
        return "Model"
    if s == "muni":
        return "Model"
    if s.startswith("model"):
        digits = "".join(filter(str.isdigit, s))
        if digits:
            return int(digits)
        # Excel often shows bare "Model" (versioned id omitted); portfolio default is Model2501.
        return 2501
    return s.upper()


def security_uses_municipal_curve(sec: Dict[str, Any]) -> bool:
    """True when Excel prepay model is Muni."""
    return str(sec.get("prepay_model", "")).strip().lower() == "muni"


def security_is_municipal_bond(sec: Dict[str, Any]) -> bool:
    """
    Municipal product for REST prepay/vol heuristics: prepay model Muni and/or Sub Type MUNI.
    """
    if security_uses_municipal_curve(sec):
        return True
    st = (sec.get("sub_type") or "").strip().upper()
    return "MUNI" in st if st else False


def prepay_type_and_rate_for_api(sec: Dict[str, Any]) -> Tuple[Any, float]:
    """
    ``prepaySettings`` type and rate for /bond/py, scenario-calc, and keyword py.

    Municipal bonds: default **CPY / 0** (same convention as Agency passthrough rows in
    many portfolio exports). Excel often shows Prepay Model Muni with Prepay Rate 100;
    sending **Model / 100** to REST can look like a fully ramped mortgage model and
    leave option-sensitive fields (OAS, WAL, CPR paths) empty.

    Override defaults:
    - ``YB_MUNI_PREPAY_LEGACY=1`` — use ``normalize_prepay_type`` + file prepay rate.
    - ``YB_MUNI_CPY_RATE`` — if set (numeric), use **CPY** with that rate instead of 0.
    """
    if sub_type_is_treasury(sec.get("sub_type")):
        pr = sec.get("prepay_rate")
        if pr is None:
            pr = 0.0
        try:
            prf = float(pr)
        except (TypeError, ValueError):
            pn = parse_number(pr)
            prf = float(pn) if pn is not None else 0.0
        return normalize_prepay_type(sec.get("prepay_model")), prf

    legacy = (os.environ.get("YB_MUNI_PREPAY_LEGACY") or "").strip().lower() in {"1", "true", "yes", "y"}
    if not legacy and security_is_municipal_bond(sec):
        cpy_custom = parse_number(os.environ.get("YB_MUNI_CPY_RATE"))
        rate = float(cpy_custom) if cpy_custom is not None else 0.0
        return "CPY", rate

    if sub_type_is_agency_cmbs_family(sec.get("sub_type")):
        return "CPY", 0.0

    pr = sec.get("prepay_rate")
    if pr is None:
        pr = 100.0
    try:
        prf = float(pr)
    except (TypeError, ValueError):
        pn = parse_number(pr)
        prf = float(pn) if pn is not None else 100.0
    return normalize_prepay_type(sec.get("prepay_model")), prf


def py_calc_props(sec: Dict[str, Any]) -> Dict[str, str]:
    """REST ``props`` for pyCalcInputs / sync input (subtype is not a top-level field)."""
    st = (sec.get("sub_type") or "").strip()
    return {"subType": st} if st else {}


def yieldbook_security_identifier(sec: Dict[str, Any]) -> str:
    """
    REST ``identifier`` for ``securityIDEntry`` (/bond/py, scenario-calc, indic, async py, …).

    Many **Structured Agency CMBS** tranches are stored in Yield Book under ``<CUSIP>.cmo``.
    Workbooks often list the nine-character CUSIP only; with that form, sync PY can return
    empty yields and durations while still returning a few collateral fields. Appending
    ``.cmo`` (when not already present) restores full pricing for those names. Other sub types
    are returned unchanged (``Agency CMBS`` / ``Agency CMO`` rows behave with or without the suffix
    in typical tenants).
    """
    c = str(sec.get("cusip") or "").strip()
    if not c or c.upper().endswith(".CMO"):
        return c
    st = (sec.get("sub_type") or "").strip().lower()
    if st == "structured agency cmbs":
        return f"{c}.cmo"
    return c


def curve_type(val: Any, prepay_model: Any) -> str:
    """
    REST curveType string for Yield Book (maps file labels like RFRSwap to API enums).

    For prepay model Muni, default to SWAP_RFR because many tenants reject
    municipal enum labels in REST curveType validation.
    Override with YB_MUNI_CURVE_TYPE if your tenant supports a municipal enum.
    """
    if str(prepay_model).strip().lower() == "muni":
        ct = (os.environ.get("YB_MUNI_CURVE_TYPE") or "SWAP_RFR").strip()
        return ct if ct else "SWAP_RFR"
    s = str(val).strip().upper()
    if s in {"", "NAN", "NONE"}:
        return "SWAP_RFR"
    if "RFR" in s:
        return "SWAP_RFR"
    return s or "SWAP_RFR"


def curve_dict_for(sec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Full curve object for /bond/py and scenario-calc.

    Prepay Model Muni rows use ``{curveType}`` only (e.g. ``SWAP_RFR``), matching the
    confirmed Excel ``YBPRICE(..., \"RFRSwap\", ...)`` REST body. Other products add currency.
    """
    ct = curve_type(sec.get("curve_type"), sec.get("prepay_model"))
    if security_uses_municipal_curve(sec):
        return {"curveType": ct}
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


def treasury_prepay_settings() -> Dict[str, str]:
    """Confirmed Excel YBPRICE for Treasuries: CPR with empty rate string."""
    return {"type": "CPR", "rate": ""}


def treasury_scenario_settlement_prepay() -> Dict[str, str]:
    """Confirmed Excel YBSCEN settlement prepay for Treasuries: CPY with empty rate."""
    return {"type": "CPY", "rate": ""}


def sub_type_is_agency_mbs_family(sub_type: Optional[str]) -> bool:
    """
    Agency mortgage products that use the confirmed Excel YBSCEN-style scenario body:
    Agency CMO, Agency MBS 10/15/20/30yr, Agency MBS ARM, Agency MBS Other.

    Excludes Agency CMBS / Structured Agency CMBS and Non-Agency rows.
    """
    st = (sub_type or "").strip().upper()
    if not st:
        return False
    return st == "AGENCY CMO" or st.startswith("AGENCY MBS")


def sub_type_is_agency_cmo(sub_type: Optional[str]) -> bool:
    return (sub_type or "").strip().upper() == "AGENCY CMO"


def sub_type_is_agency_cmbs_family(sub_type: Optional[str]) -> bool:
    """Agency CMBS and Structured Agency CMBS share confirmed YBPRICE/YBSCEN bodies (CPY/0)."""
    st = (sub_type or "").strip().lower()
    return st in {"agency cmbs", "structured agency cmbs"}


def agency_cmbs_prepay_settings() -> Dict[str, str]:
    """Confirmed Excel YBPRICE/YBSCEN: CPY with rate 0 (string in REST body)."""
    return {"type": "CPY", "rate": "0"}


def default_prepay_vol_curve_assumptions(
    sub_type: Optional[str],
) -> tuple[Any, Optional[float], Optional[str], str]:
    """
    Portfolio/Excel defaults when input Prepay Model, Prepay Rate, Vol Model, Curve Type are blank.

    Agency CMBS and Structured Agency CMBS use **CPY / 0** (not Model2501).
    Agency CMO and Agency MBS* use Model2501 / 100.
    """
    if sub_type_is_treasury(sub_type):
        return None, None, None, "SWAP_RFR"
    st = (sub_type or "").strip().upper()
    if "MUNI" in st:
        return "Muni", 100.0, "Single", "RFRSwap"
    if sub_type_is_agency_cmbs_family(sub_type):
        return "CPY", 0.0, "LMMSOFRFLAT", "RFRSwap"
    if sub_type_is_agency_cmo(sub_type) or sub_type_is_agency_mbs_family(sub_type):
        return "Model2501", 100.0, "LMMSOFRFLAT", "RFRSwap"
    return "Model2501", 100.0, "LMMSOFRFLAT", "RFRSwap"


def ybprice_prepay_rate_str(sec: Dict[str, Any]) -> str:
    rate_n = parse_number(sec.get("prepay_rate"))
    return f"{rate_n:g}" if rate_n is not None else "100"


def agency_mbs_prepay_type_override() -> Any:
    """Optional REST prepay type for Agency MBS* (not CMO). Env: YB_AGENCY_MBS_PREPAY_TYPE."""
    raw = os.environ.get("YB_AGENCY_MBS_PREPAY_TYPE", "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    return raw


def agency_cmo_prepay_type_override() -> Any:
    """Optional REST prepay type for Agency CMO. Env: YB_AGENCY_CMO_PREPAY_TYPE."""
    raw = os.environ.get("YB_AGENCY_CMO_PREPAY_TYPE", "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    return raw


def ybprice_prepay_type_for_agency_mortgage(sec: Dict[str, Any]) -> Any:
    """
    REST ``prepaySettings.type`` for confirmed YBPRICE/YBSCEN bodies.

    Agency CMO: versioned model id (e.g. **2501** from Model2501) — Excel shows \"Model\"
    but REST requires the numeric model id.

    Agency MBS*: generic **\"Model\"** (current model, ~2601) even when the file shows Model2501.
    Use **OldModel** or **2501** via YB_AGENCY_MBS_PREPAY_TYPE to pin the legacy model.

    Overrides: sec[\"force_prepay_type\"], or YB_AGENCY_MBS_PREPAY_TYPE / YB_AGENCY_CMO_PREPAY_TYPE.
    """
    force = sec.get("force_prepay_type")
    if force is not None and str(force).strip() not in {"", "nan"}:
        if isinstance(force, str) and force.strip().isdigit():
            return int(force.strip())
        return force

    pm = clean_text(sec.get("prepay_model")) or "Model"
    if sub_type_is_agency_cmo(sec.get("sub_type")):
        ovr = agency_cmo_prepay_type_override()
        if ovr is not None:
            return ovr
        return normalize_prepay_type(pm)
    st = (sec.get("sub_type") or "").strip().upper()
    if st.startswith("AGENCY MBS"):
        ovr = agency_mbs_prepay_type_override()
        if ovr is not None:
            return ovr
        return "Model" if pm.lower().startswith("model") else pm
    if sub_type_is_agency_mbs_family(sec.get("sub_type")):
        return "Model" if pm.lower().startswith("model") else pm
    return normalize_prepay_type(pm)


def resolve_forward_yield_column(py: Dict[str, Any], sub_type: Optional[str]) -> Any:
    """
    OUTPUT column Forward_Yield mapping rule:
    - Muni/Treasury: use bond yield (py.yield / effectiveYield)
    - All others (Agency MBS/CMO/etc.): use forward yield (forwardMeasures.yield)
    """
    fy = py.get("forwardYield")
    if fy is None:
        fwd_measures = py.get("forwardMeasures") or {}
        if isinstance(fwd_measures, dict):
            fy = get_first_key(fwd_measures, "yield", "Yield")
    y = get_first_key(py, "bondYield", "yield", "effectiveYield", "streetYield", "Yield")
    if sub_type_uses_bond_yield(sub_type):
        return y
    if sub_type_is_agency_mbs_family(sub_type):
        # Agency CMO / MBS: forward yield only, never substitute bond yield.
        return fy
    # Other non-Muni/Treasury: prefer forward yield; fallback to other yield fields if missing.
    return fy if fy is not None else y


def resolve_prospective_yield_from_py(py_at_book: Optional[Dict[str, Any]], sub_type: Optional[str]) -> Any:
    """
    OUTPUT column Prospective_Yield: same measure as ``Forward_Yield`` (Excel YBPRICE ``forwardYield`` slot),
    but from a **book-price** /bond/py pass (``MTB_BOOK PRICE (Clean)``), matching workbook Prospective Yield.
    """
    if not py_at_book:
        return pd.NA
    return resolve_forward_yield_column(py_at_book, sub_type)


def resolve_volatility(sec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build volatility payload aligned to product conventions.
    For Muni, Excel-style runs commonly use MatrixWSkew when input shows Single.
    """
    if sub_type_is_treasury(sec.get("sub_type")):
        return {"type": "Default"}

    vol_raw = sec.get("vol_model")
    vol_model = (str(vol_raw).strip() if vol_raw is not None else "") or "Default"
    is_muni = security_is_municipal_bond(sec)
    vol_upper = vol_model.upper()

    if is_muni and vol_upper == "SINGLE":
        return {"type": "MatrixWSkew"}
    if vol_upper == "SINGLE":
        return {"type": "Single", "rate": 0}
    return {"type": vol_model}


def computed_dollar_duration(sec: Dict[str, Any], py: Dict[str, Any]) -> Any:
    """
    Dollar_Duration = current_factor * Nominal * effectiveDV01 / 100
    current_factor defaults to 1 when not in input; Nominal from input column.
    Uses formula-only output (no API dollarDuration fallback).
    """
    dv01 = py.get("effectiveDV01")
    if dv01 is None:
        return None
    try:
        dv01_f = float(dv01)
    except (TypeError, ValueError):
        return None

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
        return None
    try:
        nom_f = float(nom)
    except (TypeError, ValueError):
        return None

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


def bond_py_level_value(sec: Dict[str, Any]) -> float:
    """
    Numeric market level for Yield Book bond/py (field ``level``).
    Raises ValueError if missing or non-finite; the API rejects e.g. ``Invalid level: nan``.
    """
    mp = sec.get("market_price")
    if mp is None or pd.isna(mp):
        raise ValueError(
            "Missing or invalid market price (Yield Book requires a numeric level / price)."
        )
    try:
        v = float(mp)
    except (TypeError, ValueError):
        pn = parse_number(mp)
        if pn is None or pd.isna(pn):
            raise ValueError(
                "Missing or invalid market price (Yield Book requires a numeric level / price)."
            )
        v = float(pn)
    if math.isnan(v) or math.isinf(v):
        raise ValueError(
            "Missing or invalid market price (Yield Book requires a numeric level / price)."
        )
    return v


def bond_py_level_for_request(sec: Dict[str, Any], level_override: Optional[float] = None) -> float:
    """
    Numeric ``level`` for /bond/py. Defaults to ``market_price`` (``bond_py_level_value``).
    ``level_override`` is used for a second pass at **book price** (Excel Prospective Yield / YBPRICE book).
    """
    if level_override is not None:
        if isinstance(level_override, float) and pd.isna(level_override):
            raise ValueError("Invalid level_override (nan) for Yield Book /bond/py level.")
        try:
            v = float(level_override)
        except (TypeError, ValueError):
            pn = parse_number(level_override)
            if pn is None or (isinstance(pn, float) and pd.isna(pn)):
                raise ValueError("Invalid level_override for Yield Book /bond/py level.")
            v = float(pn)
        if math.isnan(v) or math.isinf(v):
            raise ValueError("Invalid level_override for Yield Book /bond/py level.")
        return v
    return bond_py_level_value(sec)


def clean_text(val: Any) -> Optional[str]:
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    return s or None


def get_first_key(d: Dict[str, Any], *names: str) -> Any:
    """
    Return the first non-None value from candidate keys, case-insensitive.
    """
    if not isinstance(d, dict) or not d:
        return None
    lower_map = {str(k).lower(): v for k, v in d.items()}
    for name in names:
        if name in d and d.get(name) is not None:
            return d.get(name)
        v = lower_map.get(str(name).lower())
        if v is not None:
            return v
    return None


def extract_pphist_cpr_life(payload: Dict[str, Any]) -> Any:
    """
    Extract Life CPR from either flat keys or nested PPM history list shape:
    dataPPMHistoryList -> prepayType=CPR -> dataPPMHistoryDetailList -> month=Life.
    """
    direct = get_first_key(payload, "ppHistCPRLife", "PPHistCPRLife", "lifeCPR", "LifeCPR")
    if direct is not None:
        return direct

    history_list = get_first_key(payload, "dataPPMHistoryList", "datappmhistorylist")
    if not isinstance(history_list, list):
        return None

    for block in history_list:
        if not isinstance(block, dict):
            continue
        prepay_type = str(get_first_key(block, "prepayType", "prepaytype") or "").strip().upper()
        if prepay_type != "CPR":
            continue
        details = get_first_key(block, "dataPPMHistoryDetailList", "datappmhistorydetaillist")
        if not isinstance(details, list):
            continue
        for item in details:
            if not isinstance(item, dict):
                continue
            month = str(get_first_key(item, "month", "Month") or "").strip().upper()
            if month == "LIFE":
                return get_first_key(item, "prepayRate", "prepayrate", "rate", "value")
    return None


def extract_long_term_cpr(payload: Dict[str, Any]) -> Any:
    """
    Extract LongTerm CPR from either flat keys or nested projection list shape:
    dataPpmProjList -> prepayType=CPR -> longTerm.
    """
    direct = get_first_key(payload, "longTermCPR", "LongTermCPR", "ltCPR", "LTCPR")
    if direct is not None:
        return direct

    proj_list = get_first_key(payload, "dataPpmProjList", "dataPPMProjList", "datappmprojlist")
    if not isinstance(proj_list, list):
        return None

    for block in proj_list:
        if not isinstance(block, dict):
            continue
        prepay_type = str(get_first_key(block, "prepayType", "prepaytype") or "").strip().upper()
        if prepay_type != "CPR":
            continue
        return get_first_key(block, "longTerm", "longterm", "lt", "value")
    return None


def resolve_life_cpr_value(sub_type: Optional[str], py: Dict[str, Any], indic: Dict[str, Any]) -> Any:
    """
    Life CPR selection:
    1) PY Life CPR (dataPPMHistoryList CPR / month Life)
    2) INDIC Life CPR (same nested shape)
    3) For CMO-style sectors, fallback to CPR long-term projection as proxy
    """
    py_life = extract_pphist_cpr_life(py) if isinstance(py, dict) else None
    if py_life is None and isinstance(py, dict):
        py_life = py.get("PPHistCPRLife")
    if py_life is not None:
        return py_life

    indic_life = extract_pphist_cpr_life(indic) if isinstance(indic, dict) else None
    if indic_life is None and isinstance(indic, dict):
        indic_life = indic.get("PPHistCPRLife")
    if indic_life is not None:
        return indic_life

    st = (sub_type or "").strip().upper()
    if "CMO" in st:
        py_lt = extract_long_term_cpr(py) if isinstance(py, dict) else None
        if py_lt is None and isinstance(py, dict):
            py_lt = py.get("LongTermCPR")
        if py_lt is not None:
            return py_lt
        indic_lt = extract_long_term_cpr(indic) if isinstance(indic, dict) else None
        if indic_lt is None and isinstance(indic, dict):
            indic_lt = indic.get("LongTermCPR")
        if indic_lt is not None:
            return indic_lt
    return None


def default_pricing_date() -> str:
    return date.today().strftime("%Y-%m-%d")


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
    book_price_col = find_col(
        cols,
        "MTB_BOOK PRICE (Clean)",
        "MTB_BOOK_PRICE",
        "MTB Book Price (Clean)",
        "Book Price",
    )
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
            "market_price": parse_number(r[market_price_col]),
            "book_price": parse_number(r[book_price_col]) if book_price_col else None,
            "curve_date": normalize_date(r[curve_date_col]) if curve_date_col else default_pricing_date(),
            "prepay_model": prepay_model,
            "prepay_rate": prepay_rate_val,
            "vol_model": vol_model_val,
            "curve_type": clean_text(r[curve_type_col]) if curve_type_col else "SWAP_RFR",
            "nominal": parse_number(r[nominal_col]) if nominal_col else None,
            "current_factor": float(cf_val) if cf_val is not None else 1.0,
        })
    return out

# ---------------------------------------------------------
# PY Analytics
# ---------------------------------------------------------
def muni_sync_py_ybprice_rfrswap_shape(sec: Dict[str, Any], curve_obj: Dict[str, Any]) -> bool:
    """
    True when sync ``/bond/py`` should match Excel ``YBPRICE(..., \"RFRSwap\", ...)``:
    only ``curveType`` on the curve (no ``currency``).
    """
    if not security_uses_municipal_curve(sec):
        return False
    return set(curve_obj.keys()) == {"curveType"}


def agency_cmbs_ybprice_sync_shape(sec: Dict[str, Any], curve_obj: Dict[str, Any]) -> bool:
    """
    True when sync ``/bond/py`` should match confirmed Excel for **Agency CMBS** and
    **Structured Agency CMBS**: ``CUSIP`` or ``CUSIP.cmo`` id, ``curveType`` = SWAP_RFR only,
    ``prepaySettings`` CPY / 0, ``volatility`` = LMMSOFRFLAT, ``includePartials`` only,
    ``floaterSettings`` = ``{}``, ``globalSettings.pricingDate`` only.

    Matches ``YBPRICE(..., \"RFRSwap\", ..., \"LMMSOFRFLAT\", \"\", \"CPY\", 0, \"PDUR7\")``.
    """
    if not sub_type_is_agency_cmbs_family(sec.get("sub_type")):
        return False
    return bool(str(curve_obj.get("curveType") or "").strip())


def treasury_ybprice_sync_shape(sec: Dict[str, Any], curve_obj: Dict[str, Any]) -> bool:
    """
    True when sync ``/bond/py`` should match confirmed Excel for **Treasuries**:
    ``CUSIP`` id, ``curveType`` = SWAP_RFR only, ``prepaySettings`` CPR / empty rate,
    ``volatility`` = Default, ``includePartials`` only, ``floaterSettings`` = ``{}``,
    ``globalSettings.pricingDate`` only.

    Matches ``YBPRICE(..., \"RFRSwap\", ..., \"Default\", \"\", \"CPR\", \"\", \"PDUR7\")``.
    """
    if not sub_type_is_treasury(sec.get("sub_type")):
        return False
    return bool(str(curve_obj.get("curveType") or "").strip())


def extract_sync_py_obj(
    session,
    token: str,
    response_json: Dict[str, Any],
    *,
    cusip: str,
) -> Dict[str, Any]:
    """
    Parse sync ``/bond/py`` JSON. Some heavy Agency CMO/MBS jobs return only
    ``requestId`` (HTTP 200); poll ``/results/{requestId}`` until DONE.
    """
    results = response_json.get("results")
    if results:
        return (results[0] or {}).get("py", {}) or {}

    request_id = response_json.get("requestId")
    if not request_id:
        raise KeyError("results")

    results_url = api_url(f"/results/{request_id}", mode=None)
    last_bad_status: Optional[int] = None
    for _ in range(120):
        rr = session.get(results_url, headers=api_headers(token), timeout=30)
        if rr.status_code == 404:
            time.sleep(2)
            continue
        if not rr.ok:
            if rr.status_code != last_bad_status:
                print(
                    f"[WARN] sync /bond/py results GET {request_id} HTTP {rr.status_code} "
                    f"for {cusip} (will retry): {(rr.text or '')[:300]}"
                )
                last_bad_status = rr.status_code
            time.sleep(2)
            continue
        try:
            jr = rr.json()
        except Exception:
            time.sleep(2)
            continue
        meta = jr.get("meta") or {}
        if meta.get("status") == "DONE":
            polled = jr.get("results") or []
            if not polled:
                return {}
            return (polled[0] or {}).get("py", {}) or {}
        if meta.get("status") == "ERROR":
            print(
                f"[WARN] sync /bond/py results ERROR for {cusip} request {request_id}: "
                f"{jr.get('errors') or meta}"
            )
            return {}
        time.sleep(2)
    print(f"[WARN] Timed out waiting for sync /bond/py results for {cusip} ({request_id})")
    return {}


def run_py(session, token: str, sec: Dict[str, Any], *, level_override: Optional[float] = None) -> Dict[str, Any]:
    pricing_date = sec.get("curve_date") or default_pricing_date()
    curve_obj = curve_dict_for(sec)

    prepay_type, prepay_rate = prepay_type_and_rate_for_api(sec)
    volatility = resolve_volatility(sec)
    yb_muni = muni_sync_py_ybprice_rfrswap_shape(sec, curve_obj)
    yb_mbs = sub_type_is_agency_mbs_family(sec.get("sub_type"))
    yb_cmbs = agency_cmbs_ybprice_sync_shape(sec, curve_obj)
    yb_treasury = treasury_ybprice_sync_shape(sec, curve_obj)

    if yb_mbs:
        # Confirmed Excel YBPRICE-style body for Agency CMO / Agency MBS family.
        prepay_type = ybprice_prepay_type_for_agency_mortgage(sec)
        prepay_rate = ybprice_prepay_rate_str(sec)
        input_row = {
            "identifier": yieldbook_security_identifier(sec),
            "floaterSettings": {},
            "extraSettings": {
                "includePartials": True,
            },
            "level": str(bond_py_level_for_request(sec, level_override)),
            "curve": {"curveType": curve_obj.get("curveType") or "SWAP_RFR"},
            "prepaySettings": {"rate": prepay_rate, "type": prepay_type},
            "settlementDate": pricing_date,
            "volatility": volatility,
        }
        payload = {
            "globalSettings": {"pricingDate": pricing_date},
            "input": [input_row],
        }
    elif yb_muni:
        # Match Excel YBPRICE(..., "RFRSwap", ..., "MatrixWSkew", ...) — minimal sync /bond/py body.
        # Sync /bond/py does not accept prepay.type "Muni" (bulk API does); omit prepaySettings.
        volatility = {"type": "MatrixWSkew"}
        prepay_type, prepay_rate = None, None
        input_row = {
            "identifier": yieldbook_security_identifier(sec),
            "floaterSettings": {},
            "extraSettings": {
                "includePartials": True,
            },
            "level": str(bond_py_level_for_request(sec, level_override)),
            "curve": curve_obj,
            "settlementDate": pricing_date,
            "volatility": volatility,
        }
        payload = {
            "globalSettings": {"pricingDate": pricing_date},
            "input": [input_row],
        }
    elif yb_cmbs:
        cmbs_prepay = agency_cmbs_prepay_settings()
        input_row = {
            "identifier": yieldbook_security_identifier(sec),
            "floaterSettings": {},
            "extraSettings": {"includePartials": True},
            "level": str(bond_py_level_for_request(sec, level_override)),
            "curve": {"curveType": "SWAP_RFR"},
            "prepaySettings": cmbs_prepay,
            "settlementDate": pricing_date,
            "volatility": volatility,
        }
        payload = {
            "globalSettings": {"pricingDate": pricing_date},
            "input": [input_row],
        }
        prepay_type, prepay_rate = cmbs_prepay["type"], cmbs_prepay["rate"]
    elif yb_treasury:
        tsy_prepay = treasury_prepay_settings()
        input_row = {
            "identifier": yieldbook_security_identifier(sec),
            "floaterSettings": {},
            "extraSettings": {"includePartials": True},
            "level": str(bond_py_level_for_request(sec, level_override)),
            "curve": {"curveType": "SWAP_RFR"},
            "prepaySettings": tsy_prepay,
            "settlementDate": pricing_date,
            "volatility": {"type": "Default"},
        }
        payload = {
            "globalSettings": {"pricingDate": pricing_date},
            "input": [input_row],
        }
        prepay_type, prepay_rate = tsy_prepay["type"], tsy_prepay["rate"]
    else:
        input_row = {
            "identifier": yieldbook_security_identifier(sec),
            "idType": "securityIDEntry",
            "level": str(bond_py_level_for_request(sec, level_override)),
            "settlementDate": pricing_date,
            "userTag": sec["cusip"],
            "curve": curve_obj,
            "prepaySettings": {"type": prepay_type, "rate": prepay_rate},
            "volatility": volatility,
            "extraSettings": {
                "optionModel": "OASEDUR",
                # Reverse-engineered from YBPRICE partial duration behavior.
                "includePartials": True,
            },
        }
        props = py_calc_props(sec)
        if props:
            input_row["props"] = props

        payload = {
            "globalSettings": {
                "pricingDate": pricing_date,
                "retrievePPMProjection": True,
            },
            "input": [input_row],
        }

    if yb_mbs:
        print(
            f"[INFO] {sec['cusip']}: {sec.get('sub_type')} sync /bond/py = YBPRICE-style "
            f"(SWAP_RFR-only curve, prepay={prepay_type}/{prepay_rate}, vol={volatility!r}, "
            "includePartials only)."
        )
    elif yb_muni:
        print(
            f"[INFO] {sec['cusip']}: Muni sync /bond/py = YBPRICE RFRSwap-style "
            f"(curve={curve_obj!r}, vol={volatility!r}; no prepaySettings in body)."
        )
    elif yb_cmbs:
        print(
            f"[INFO] {sec['cusip']}: {sec.get('sub_type')} sync /bond/py = YBPRICE-style "
            f"(SWAP_RFR-only curve, prepay=CPY/0, vol={volatility!r}, includePartials only; "
            f"id={yieldbook_security_identifier(sec)!r}).",
            flush=True,
        )
    elif yb_treasury:
        print(
            f"[INFO] {sec['cusip']}: {sec.get('sub_type')} sync /bond/py = YBPRICE-style "
            f"(SWAP_RFR-only curve, prepay=CPR/'', vol=Default, includePartials only).",
            flush=True,
        )

    url = api_url("bond/py", mode="sync")
    py_timeout = 120 if yb_mbs else 60
    r = session.post(url, json=payload, headers=api_headers(token), timeout=py_timeout)
    if not r.ok:
        print(f"[ERROR] /bond/py failed for {sec['cusip']} HTTP {r.status_code}")
        print(f"[ERROR] pricingDate={pricing_date}, curve={curve_obj}, prepayType={prepay_type}, prepayRate={prepay_rate}, volatility={volatility}")
        print(f"[ERROR] response: {r.text[:2000]}")

        # Retry once with safest generic settings.
        retry_payload = payload.copy()
        retry_input = dict(payload["input"][0])
        retry_input["curve"] = {"curveType": "SWAP_RFR"}
        retry_input["volatility"] = {"type": "Default"}
        for k in ("prepaySettings", "idType", "userTag", "props"):
            retry_input.pop(k, None)
        if yb_muni:
            retry_input.setdefault("floaterSettings", {})
            retry_input["extraSettings"] = {"includePartials": True}
        elif yb_treasury:
            retry_input.setdefault("floaterSettings", {})
            retry_input["extraSettings"] = {"includePartials": True}
            retry_input["prepaySettings"] = treasury_prepay_settings()
        else:
            retry_input["prepaySettings"] = {"type": "Model", "rate": 100}
            retry_input["curve"] = {"curveType": "SWAP_RFR", "currency": "USD"}
        retry_payload["input"] = [retry_input]
        if yb_muni or yb_treasury:
            retry_payload["globalSettings"] = {"pricingDate": default_pricing_date()}
        else:
            retry_payload["globalSettings"] = {"pricingDate": default_pricing_date(), "retrievePPMProjection": True}

        rr = session.post(url, json=retry_payload, headers=api_headers(token), timeout=py_timeout)
        if not rr.ok:
            print(f"[ERROR] Retry /bond/py failed for {sec['cusip']} HTTP {rr.status_code}")
            print(f"[ERROR] retry response: {rr.text[:2000]}")
            rr.raise_for_status()
        py_obj = extract_sync_py_obj(session, token, rr.json(), cusip=sec["cusip"])
    else:
        py_obj = extract_sync_py_obj(session, token, r.json(), cusip=sec["cusip"])

    partial_durations = extract_partial_durations(py_obj)
    if not partial_durations:
        partial_durations = fetch_partial_durations_by_keywords(session, token, sec)
    if not partial_durations:
        print(f"[WARN] {sec['cusip']}: API did not return partial durations (including YBPRICE PDUR keywords).")

    # Normalize sync /bond/py response to expected downstream shape.
    max_servicer = (py_obj.get("maxServicer") or {})
    if isinstance(max_servicer, str):
        max_servicer = {"name": max_servicer, "percent": None}
    # YBPRICE-style flat ``py`` uses duration / dv01 / yield / convexity (not effective* names).
    eff_dur = py_obj.get("effectiveDuration")
    if eff_dur is None:
        eff_dur = py_obj.get("duration") or py_obj.get("durationToWorstCase")
    eff_conv = py_obj.get("effectiveConvexity")
    if eff_conv is None:
        eff_conv = py_obj.get("convexity")
    eff_dv01 = py_obj.get("effectiveDV01")
    if eff_dv01 is None:
        eff_dv01 = py_obj.get("dv01") or py_obj.get("dv01ToNextCall")
    flat_yield = py_obj.get("yield") or py_obj.get("yieldToWorst") or py_obj.get("semiAnnualizedYield")
    fwd_yield = (
        (py_obj.get("ForwardYield") or {}).get("Yield")
        if isinstance(py_obj.get("ForwardYield"), dict)
        else get_first_key(py_obj.get("forwardMeasures") or {}, "yield", "Yield")
    )
    if fwd_yield is None and not sub_type_is_agency_mbs_family(sec.get("sub_type")):
        fwd_yield = flat_yield
    return {
        "forwardYield": fwd_yield,
        "bondYield": flat_yield or py_obj.get("effectiveYield") or py_obj.get("streetYield"),
        "effectiveDuration": eff_dur,
        "effectiveConvexity": eff_conv,
        "effectiveDV01": eff_dv01,
        "dollarDuration": py_obj.get("dollarDuration"),
        "partialDurations": partial_durations,
        "averageLife": get_first_key(py_obj, "effectiveWAL", "EffectiveWAL"),
        "LongTermCPR": extract_long_term_cpr(py_obj),
        "PPHistCPRLife": extract_pphist_cpr_life(py_obj),
        "oas": py_obj.get("oas"),
        "zSpread": py_obj.get("zSpread"),
        "factor": get_first_key(py_obj, "factor", "Factor"),
        "GrossWAC": get_first_key(py_obj, "grossWAC", "GrossWAC", "gwac", "GWAC"),
        "LoanAge": get_first_key(py_obj, "loanAge", "LoanAge", "wala", "WALA"),
        "WeightedAvgLoanSize": get_first_key(py_obj, "weightedAvgLoanSize", "WeightedAvgLoanSize", "wals", "WALS"),
        "maxServicer": max_servicer,
    }

# ---------------------------------------------------------
# Scenario Calc
# ---------------------------------------------------------
def run_scenarios(
    session,
    token: str,
    sec: Dict[str, Any],
    *,
    shocks_bps: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    shocks = list(shocks_bps if shocks_bps is not None else SHOCKS_BPS)
    pricing_date = sec.get("curve_date") or default_pricing_date()
    scen_curve = curve_dict_for(sec)
    scen_volatility = resolve_volatility(sec)
    yb_muni_scen = muni_sync_py_ybprice_rfrswap_shape(sec, scen_curve)
    yb_mbs_scen = sub_type_is_agency_mbs_family(sec.get("sub_type"))
    yb_cmbs_scen = sub_type_is_agency_cmbs_family(sec.get("sub_type"))
    yb_treasury_scen = sub_type_is_treasury(sec.get("sub_type"))
    prepay_type: Any = None
    prepay_rate: Any = None

    if yb_treasury_scen:
        timing = scenario_timing()
        tsy_settle_prepay = treasury_scenario_settlement_prepay()
        prepay_type, prepay_rate = tsy_settle_prepay["type"], tsy_settle_prepay["rate"]
        payload = {
            "globalSettings": {
                "pricingDate": pricing_date,
                "horizonDays": str(SCENARIO_HORIZON_DAYS),
                "horizonMonths": str(SCENARIO_HORIZON_MONTHS),
                "calcHorizonEffectiveMeasures": True,
            },
            "input": [{
                "identifier": yieldbook_security_identifier(sec),
                "horizonInfo": [
                    {
                        "level": "0",
                        "scenarioRef": {
                            "$ref": (
                                f"/sys/scenario/Par/{shock}?timing={timing}"
                                "&reinvestmentRate=Default&swapSpreadConst=false"
                            )
                        },
                    }
                    for shock in shocks
                ],
                "curve": {"curveType": "SWAP_RFR"},
                "horizonPYMethod": scenario_horizon_py_method(),
                "settlementInfo": {
                    "prepay": tsy_settle_prepay,
                    "level": str(bond_py_level_value(sec)),
                    "settlementDate": pricing_date,
                },
                "assumeCall": False,
            }],
        }
        print(
            f"[INFO] {sec['cusip']}: {sec.get('sub_type')} scenario-calc = YBSCEN-style "
            f"(sys Par scenarioRef {shocks}, settlement prepay=CPY/'', no horizon prepay/vol, "
            "calcHorizonEffectiveMeasures)."
        )
    elif yb_muni_scen or yb_mbs_scen or yb_cmbs_scen:
        # Confirmed Excel YBSCEN-style body: system Par scenarios via scenarioRef,
        # calcHorizonEffectiveMeasures for EffectiveDurationAtHorizon.
        if yb_muni_scen:
            prepay_type = clean_text(sec.get("prepay_model")) or "Muni"
            volatility = {"type": "MatrixWSkew"}
            prepay_rate = ybprice_prepay_rate_str(sec)
        elif yb_cmbs_scen:
            cmbs_prepay = agency_cmbs_prepay_settings()
            prepay_type = cmbs_prepay["type"]
            prepay_rate = cmbs_prepay["rate"]
            volatility = resolve_volatility(sec)
        else:
            prepay_type = ybprice_prepay_type_for_agency_mortgage(sec)
            volatility = resolve_volatility(sec)
            prepay_rate = ybprice_prepay_rate_str(sec)
        timing = scenario_timing()
        payload = {
            "globalSettings": {
                "pricingDate": pricing_date,
                "horizonDays": str(SCENARIO_HORIZON_DAYS),
                "horizonMonths": str(SCENARIO_HORIZON_MONTHS),
                "calcHorizonEffectiveMeasures": True,
            },
            "input": [{
                "identifier": yieldbook_security_identifier(sec),
                "horizonInfo": [
                    {
                        "prepay": {"rate": prepay_rate},
                        "level": "0",
                        "scenarioRef": {
                            "$ref": (
                                f"/sys/scenario/Par/{shock}?timing={timing}"
                                "&reinvestmentRate=Default&swapSpreadConst=false"
                            )
                        },
                    }
                    for shock in shocks
                ],
                # Confirmed body uses curveType only (no currency).
                "curve": {"curveType": scen_curve.get("curveType") or "SWAP_RFR"},
                "horizonPYMethod": scenario_horizon_py_method(),
                "settlementInfo": {
                    "prepay": {"rate": prepay_rate, "type": prepay_type},
                    "level": str(bond_py_level_value(sec)),
                    "settlementDate": pricing_date,
                },
                "volatility": volatility,
                "assumeCall": False,
            }],
        }
        label = (
            "Muni" if yb_muni_scen
            else (sec.get("sub_type") or "Agency MBS")
        )
        print(
            f"[INFO] {sec['cusip']}: {label} scenario-calc = YBSCEN-style "
            f"(sys Par scenarioRef {shocks}, prepay={prepay_type}/{prepay_rate}, "
            f"vol={volatility!r}, calcHorizonEffectiveMeasures)."
        )
    else:
        scenarios = []

        prepay_type, prepay_rate = prepay_type_and_rate_for_api(sec)
        for i, s in enumerate(shocks, start=1):
            scenarios.append({
                "scenarioID": f"scen{i}",
                "timing": scenario_timing(),
                "definition": {
                    "userScenario": {
                        "shiftType": SCENARIO_SHIFT_TYPE,
                        "interpolationType": SCENARIO_INTERPOLATION_TYPE,
                        "curveShifts": [{"year": 0.25, "value": s}],
                    }
                }
            })
        payload = {
            "globalSettings": {
                "pricingDate": pricing_date,
                "horizonDays": SCENARIO_HORIZON_DAYS,
                "horizonMonths": SCENARIO_HORIZON_MONTHS,
            },
            "scenarios": scenarios,
            "calcInputs": [{
                "userTag": sec["cusip"],
                "identifier": yieldbook_security_identifier(sec),
                "idType": "securityIDEntry",
                **({"props": py_calc_props(sec)} if py_calc_props(sec) else {}),
                "curve": scen_curve,
                "volatility": scen_volatility,
                "settlementInfo": {
                    "level": bond_py_level_value(sec),
                    "settlementType": "CUSTOM",
                    "settlementDate": pricing_date,
                    "prepay": {
                        "type": prepay_type,
                        "rate": prepay_rate,
                    },
                },
                # Match settlement prepay: horizon legs previously sent rate-only and could diverge from Excel.
                "horizonInfo": [
                    {
                        "scenarioID": f"scen{i}",
                        "level": 0,
                        "prepay": {"type": prepay_type, "rate": prepay_rate},
                    }
                    for i, _ in enumerate(shocks, start=1)
                ],
                "assumeCall": False,
                "horizonPYMethod": scenario_horizon_py_method(),
            }],
        }

    #url = f"{API_BASE}/req/bond/scenario-calc"
    url = api_url("bond/scenario-calc", mode="req")
    r = session.post(url, json=payload, headers=api_headers(token), timeout=120)
    if not r.ok:
        print(f"[ERROR] /bond/scenario-calc failed for {sec['cusip']} HTTP {r.status_code}")
        print(f"[ERROR] pricingDate={pricing_date}, curve={scen_curve}, prepayType={prepay_type}, prepayRate={prepay_rate}")
        print(f"[ERROR] response: {r.text[:2000]}")
        return []
    data = r.json()
    request_id = data.get("requestId")
    if not request_id:
        print(f"[WARN] scenario-calc: no requestId for {sec['cusip']}: {data}")
        return []

    results_url = api_url(f"/results/{request_id}", mode=None)
    last_bad_status: Optional[int] = None
    # MBS path-dependent scenario calcs can take >1 min; allow up to ~4 min of polling.
    for attempt in range(120):
        rr = session.get(results_url, headers=api_headers(token), timeout=30)
        if rr.status_code == 404:
            time.sleep(2)
            continue
        if not rr.ok:
            if rr.status_code != last_bad_status:
                print(
                    f"[WARN] scenario results GET {request_id} HTTP {rr.status_code} "
                    f"for {sec['cusip']} (will retry): {(rr.text or '')[:300]}"
                )
                last_bad_status = rr.status_code
            time.sleep(2)
            continue
        try:
            jr = rr.json()
        except Exception:
            time.sleep(2)
            continue
        meta = jr.get("meta") or {}
        if meta.get("status") == "DONE":
            results = jr.get("results", [])
            if not results:
                return []
            raw_h = (results[0].get("scenario") or {}).get("horizon", [])
            return normalize_scenario_horizon(raw_h, shocks_bps=shocks)
        if meta.get("status") == "ERROR":
            print(
                f"[WARN] scenario results ERROR for {sec['cusip']} request {request_id}: "
                f"{jr.get('errors') or meta}"
            )
            return []
        time.sleep(2)
    print(f"[WARN] Timed out waiting for scenario results for {sec['cusip']} ({request_id})")
    return []


def pick_metric(h: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in h and h.get(k) is not None:
            return h.get(k)
    return None


def derive_effective_dv01_at_horizon(h: Dict[str, Any]) -> Optional[float]:
    """
    Fallback when API does not return effectiveDV01: effectiveDuration * Price / 10000.
    """
    dur = pick_metric(h, *SCENARIO_EFFDUR_KEYS)
    px = pick_metric(h, "fullPrice", "price", "actualFullPrice", "actualPrice")
    if dur is None or px is None:
        return None
    try:
        return float(dur) * float(px) / 10000.0
    except (TypeError, ValueError):
        return None


def merge_scenario_into_row(
    row: Dict[str, Any],
    scen: List[Dict[str, Any]],
    *,
    shocks_bps: Optional[List[int]] = None,
) -> Dict[str, Any]:
    shocks = list(shocks_bps if shocks_bps is not None else SHOCKS_BPS)
    for i, shock in enumerate(shocks):
        h = scen[i] if i < len(scen) else {}
        shock_label = f"{shock:+d}" if shock > 0 else str(shock)
        row[f"EffDur_{shock_label}"] = pick_metric(h, *SCENARIO_EFFDUR_KEYS)
        dv01_val = pick_metric(h, *SCENARIO_DV01_KEYS)
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
        row[f"DollarReturn_{shock_label}"] = h.get("dollarReturn")
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
    curve_obj = curve_dict_for(sec)
    prepay_type, prepay_rate = prepay_type_and_rate_for_api(sec)
    scen_vol = resolve_volatility(sec)

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

    if sub_type_is_treasury(sec.get("sub_type")):
        py_in: Dict[str, Any] = {
            "identifier": yieldbook_security_identifier(sec),
            "level": str(bond_py_level_value(sec)),
            "settlementDate": pricing_date,
            "curve": {"curveType": curve_obj.get("curveType") or "SWAP_RFR"},
            "prepaySettings": treasury_prepay_settings(),
            "volatility": {"type": "Default"},
            "extraSettings": {"includePartials": True},
            "floaterSettings": {},
        }
    else:
        py_in = {
            "identifier": yieldbook_security_identifier(sec),
            "idType": "securityIDEntry",
            "level": str(bond_py_level_value(sec)),
            "userTag": sec["cusip"],
            "curve": curve_obj,
            "prepaySettings": {"type": prepay_type, "rate": prepay_rate},
            "volatility": scen_vol,
            "extraSettings": {"optionModel": "OASEDUR"},
        }
        props = py_calc_props(sec)
        if props:
            py_in["props"] = props

    body = {
        "keywords": keywords,
        "globalSettings": {"pricingDate": pricing_date},
        "pyCalcInputs": [py_in],
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


def normalize_indic_measures(indic_obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize sync/bulk ``/bond/indic`` payload to flat output keys.
    Life CPR: dataPPMHistoryList prepayType CPR, month Life, prepayRate.
    GWAC/WALA/WALS: top-level or first dataCollateralList row.
    """
    if not indic_obj:
        return {}

    collateral_list = indic_obj.get("dataCollateralList")
    collateral = collateral_list[0] if isinstance(collateral_list, list) and collateral_list else {}
    if not isinstance(collateral, dict):
        collateral = {}

    gross_wac = get_first_key(indic_obj, "grossWAC", "GrossWAC", "gwac", "GWAC")
    loan_age = get_first_key(indic_obj, "loanAge", "LoanAge", "wala", "WALA")
    wals = get_first_key(indic_obj, "weightedAvgLoanSize", "WeightedAvgLoanSize", "wals", "WALS")
    if gross_wac is None:
        gross_wac = get_first_key(collateral, "grossWAC", "GrossWAC", "gwac", "GWAC")
    if loan_age is None:
        loan_age = get_first_key(collateral, "loanAge", "LoanAge", "wala", "WALA")
    if wals is None:
        wals = get_first_key(
            collateral,
            "weightedAvgLoanSize",
            "WeightedAvgLoanSize",
            "wals",
            "WALS",
            "loanSize",
            "LoanSize",
            "geographicAverageLoanSize",
        )

    max_name = get_first_key(indic_obj, "maxServicerName", "MaxServicerName", "maxServiceName", "MaxServiceName")
    max_pct = get_first_key(indic_obj, "maxServicerPercent", "MaxServicerPercent")
    if max_name is None or max_pct is None:
        serv_list = collateral.get("maxServicerList")
        if isinstance(serv_list, list) and serv_list and isinstance(serv_list[0], dict):
            top_serv = serv_list[0]
            if max_name is None:
                max_name = top_serv.get("name")
            if max_pct is None:
                max_pct = top_serv.get("percent")

    return {
        "factor": get_first_key(indic_obj, "factor", "Factor"),
        "PPHistCPRLife": extract_pphist_cpr_life(indic_obj),
        "GrossWAC": gross_wac,
        "LoanAge": loan_age,
        "WeightedAvgLoanSize": wals,
        "maxServicerName": max_name,
        "maxServicerPercent": max_pct,
    }


def run_indic(session, token: str, sec: Dict[str, Any]) -> Dict[str, Any]:
    """
    YBINDIC-equivalent pull via REST /sync/bond/indic.
    Requested fields:
    Factor, PPHistCPRLife, GrossWAC, LoanAge, WeightedAvgLoanSize,
    MaxServiceName/MaxServicerName, MaxServicerPercent.
    """
    def _request_indic() -> Dict[str, Any]:
        # Confirmed Excel YBINDIC REST body: identifier only.
        payload = {
            "input": [{
                "identifier": yieldbook_security_identifier(sec),
            }],
        }
        resp = session.post(
            api_url("bond/indic", mode="sync"),
            json=payload,
            headers=api_headers(token),
            timeout=60,
        )
        if not resp.ok:
            print(f"[WARN] /bond/indic failed for {sec['cusip']} HTTP {resp.status_code}: {resp.text[:500]}")
            return {}
        results = resp.json().get("results") or []
        return results[0].get("indic") or {} if results else {}

    indic = _request_indic()
    if not indic:
        return {}
    return normalize_indic_measures(indic)


def run_py_keyword_measures(session, token: str, sec: Dict[str, Any]) -> Dict[str, Any]:
    """
    YBPRICE keyword-style pull for:
    EffectiveWAL, LongTermCPR, OAS, ZSpread.
    """
    pricing_date = sec.get("curve_date") or default_pricing_date()
    sub_type = (sec.get("sub_type") or "").strip()
    prepay_type, prepay_rate = prepay_type_and_rate_for_api(sec)
    # Optional override for Agency MBS ARM benchmarking; default is to respect input prepay rate.
    arm_force_raw = (os.environ.get("YB_ARM_FORCE_PREPAY_RATE") or "").strip()
    if sub_type.lower() == "agency mbs arm" and arm_force_raw:
        arm_force = parse_number(arm_force_raw)
        if arm_force is not None:
            prepay_rate = float(arm_force)
    curve_obj = curve_dict_for(sec)

    def _submit(volatility: Dict[str, Any]) -> Dict[str, Any]:
        if sub_type_is_treasury(sub_type):
            body = {
                "keywords": ["EffectiveWAL", "LongTermCPR", "OAS", "ZSpread"],
                "globalSettings": {"pricingDate": pricing_date},
                "pyCalcInputs": [{
                    "identifier": yieldbook_security_identifier(sec),
                    "level": str(bond_py_level_value(sec)),
                    "settlementDate": pricing_date,
                    "curve": {"curveType": curve_obj.get("curveType") or "SWAP_RFR"},
                    "prepaySettings": treasury_prepay_settings(),
                    "volatility": {"type": "Default"},
                    "extraSettings": {"includePartials": True},
                    "floaterSettings": {},
                }],
            }
        elif sub_type_is_agency_cmbs_family(sub_type):
            body = {
                "keywords": ["EffectiveWAL", "LongTermCPR", "OAS", "ZSpread"],
                "globalSettings": {"pricingDate": pricing_date},
                "pyCalcInputs": [{
                    "identifier": yieldbook_security_identifier(sec),
                    "level": str(bond_py_level_value(sec)),
                    "settlementDate": pricing_date,
                    "curve": {"curveType": curve_obj.get("curveType") or "SWAP_RFR"},
                    "prepaySettings": agency_cmbs_prepay_settings(),
                    "volatility": volatility,
                    "extraSettings": {"includePartials": True},
                    "floaterSettings": {},
                }],
            }
        elif sub_type_is_agency_mbs_family(sub_type):
            ptype = ybprice_prepay_type_for_agency_mortgage(sec)
            prate = ybprice_prepay_rate_str(sec)
            body = {
                "keywords": ["EffectiveWAL", "LongTermCPR", "OAS", "ZSpread"],
                "globalSettings": {"pricingDate": pricing_date},
                "pyCalcInputs": [{
                    "identifier": yieldbook_security_identifier(sec),
                    "level": str(bond_py_level_value(sec)),
                    "settlementDate": pricing_date,
                    "curve": {"curveType": curve_obj.get("curveType") or "SWAP_RFR"},
                    "prepaySettings": {"type": ptype, "rate": prate},
                    "volatility": volatility,
                    "extraSettings": {"includePartials": True},
                    "floaterSettings": {},
                }],
            }
        else:
            body = {
                "keywords": ["EffectiveWAL", "LongTermCPR", "OAS", "ZSpread"],
                "globalSettings": {"pricingDate": pricing_date},
                "pyCalcInputs": [{
                    "identifier": yieldbook_security_identifier(sec),
                    "idType": "securityIDEntry",
                    "level": str(bond_py_level_value(sec)),
                    "settlementDate": pricing_date,
                    # REST does not expose a direct securitySubType field, use props to pass subtype context.
                    "props": py_calc_props(sec) or {"subType": sub_type} if sub_type else {},
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

    # Try resolved volatility first, then fallback to Default.
    primary_vol = resolve_volatility(sec)
    py_kw = _submit(primary_vol)
    if py_kw.get("returnCode") == 1 or not py_kw:
        if str(primary_vol.get("type", "")).strip().upper() != "DEFAULT":
            py_kw = _submit({"type": "Default"})

    return {
        "averageLife": py_kw.get("effectiveWAL"),
        "LongTermCPR": extract_long_term_cpr(py_kw),
        "oas": py_kw.get("oas"),
        "zSpread": py_kw.get("zSpread"),
    }


def run_full_analysis_for_security(session, token: str, sec: Dict[str, Any]) -> Dict[str, Any]:
    """One security: PY + keyword + scenario + indic; merged output row for OUTPUT_COLUMNS."""
    py = run_py(session, token, sec)
    book_lvl = parse_number(sec.get("book_price"))
    py_book: Optional[Dict[str, Any]] = None
    if book_lvl is not None and not pd.isna(book_lvl):
        try:
            py_book = run_py(session, token, sec, level_override=float(book_lvl))
        except Exception as e:
            print(f"[WARN] {sec['cusip']}: Prospective_Yield book-price /bond/py skipped: {type(e).__name__}: {e}")
    py_kw = run_py_keyword_measures(session, token, sec)
    scen = run_scenarios(session, token, sec)
    indic = run_indic(session, token, sec)

    row = {
        "CUSIP": sec["cusip"],
        "Sub Type": sec.get("sub_type"),

        "Forward_Yield": resolve_forward_yield_column(py, sec.get("sub_type")),
        "Prospective_Yield": resolve_prospective_yield_from_py(py_book, sec.get("sub_type")),
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
        "LT_CPR": py_kw.get("LongTermCPR") if py_kw.get("LongTermCPR") is not None else py.get("LongTermCPR"),
        "Life_CPR": resolve_life_cpr_value(sec.get("sub_type"), py, indic),
        "OAS": py_kw.get("oas") if py_kw.get("oas") is not None else py.get("oas"),
        "Z_Spread": py_kw.get("zSpread") if py_kw.get("zSpread") is not None else py.get("zSpread"),
        "Factor": py.get("factor") if py.get("factor") is not None else indic.get("factor"),
        "GWAC": py.get("GrossWAC") if py.get("GrossWAC") is not None else indic.get("GrossWAC"),
        "WALA": py.get("LoanAge") if py.get("LoanAge") is not None else indic.get("LoanAge"),
        "WALS": py.get("WeightedAvgLoanSize") if py.get("WeightedAvgLoanSize") is not None else indic.get("WeightedAvgLoanSize"),
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
