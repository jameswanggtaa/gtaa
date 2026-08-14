"""
Yieldbook REST API: TBA metrics and parallel yield curve shocks (M&T proxy-safe + parallel).

Why you saw "PY done" but your CSV had blanks
---------------------------------------------
Your run succeeded end-to-end (exit code 0), but the *parsing layer* did not find the
expected fields in the JSON payloads returned by Yield Book for:
  - POST /sync/bond/py
  - POST /sync/bond/scenario-calc and GET /results/{requestId}

So the script wrote rows with only:
  tba_security, price_last_close, cusip
and left everything else empty.

This v7 patch makes parsing *schema-tolerant* and adds optional debug dumps so you can
see the exact payload shape from your environment.

Key additions
-------------
1) Robust PY metric extraction:
   - Tries multiple known key paths and fallbacks (case variants, older/newer schemas).
2) Robust scenario-calc horizon price extraction:
   - Finds horizon arrays under several possible keys.
   - Maps results by scenarioRef.$ref (preferred) and falls back to scenarioID/order.
   - Extracts prices by scanning likely price fields (including nested 'py').
3) Debug dumps (optional):
   Set env var YB_DEBUG_DUMP=1 to dump:
     debug_py_<cusip>.json
     debug_scen_<cusip>_chunk<idx>_initial.json
     debug_scen_<cusip>_chunk<idx>_resolved.json

Proxy handling
--------------
Same as v6: PyPAC resolves proxies once, then worker threads use explicit proxies.

Output
------
- yieldbook_tba_metrics_results.csv

"""

import csv
import json
import os
import time
import faulthandler
import threading
import re
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

faulthandler.enable()

import requests
from requests.adapters import HTTPAdapter

# -----------------------------------------------------------------------------
# Local settlement helpers
# -----------------------------------------------------------------------------
from mbs_settlement import (
    get_last_business_day_iso,
    get_latest_class_a_settlement_before,
    get_next_settlement_date,
    get_yieldbook_tba_contract_month,
    get_yieldbook_tba_prod_suffix,
    get_yieldbook_tba_settlement_date,
)

from pathlib import Path

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
AUTH_URL = "https://www.yieldbook.com/x/oauth/api-token"
API_BASE_URL = "https://api.yieldbook.com/analytics/v2"

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

# Base map (APR) used to derive current PROD-{MON} via get_yieldbook_tba_prod_suffix(as_of)
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

SHOCKS_BPS = [-300, -200, -100, -50, -25, -10, -5, 5, 10, 25, 50, 100, 200, 300]
MAX_SCENARIOS_PER_REQUEST = 8
CURVE_TYPE = "SWAP_RFR"
PREPAY_RATE = 100
OUTPUT_CSV = "yieldbook_tba_metrics_results.csv"
PREPAY_MODEL = "Model"
VOLATILITY_TYPE = "LMMSOFRFlat"

DEBUG_DUMP = os.getenv("YB_DEBUG_DUMP", "1").strip() not in ("", "0", "false", "False")
DEBUG_DIR = os.getenv("YB_DEBUG_DIR", ".").strip() or "."

try:
    MAX_WORKERS = max(1, int(os.getenv("YB_MAX_WORKERS", "2")))
except ValueError:
    MAX_WORKERS = 2

try:
    _POOL = int(os.getenv("YB_POOL_MAXSIZE", "2"))
except ValueError:
    _POOL = 2

# -----------------------------------------------------------------------------
# Timing
# -----------------------------------------------------------------------------
_TIMING_SUMMARY: Dict[str, Dict[str, float]] = {}
_TIMING_LOCK = Lock()


def _timed_call_label(label: str, started_at: float) -> None:
    elapsed = time.perf_counter() - started_at
    print(f"[timing] {label}: {elapsed:.3f}s")
    _record_timing(label, elapsed)


def _timing_bucket(label: str) -> str:
    if label.startswith("POST /x/oauth/api-token"):
        return "POST /x/oauth/api-token"
    if label.startswith("GET /sync/tba-pricing"):
        return "GET /sync/tba-pricing"
    if label.startswith("POST /sync/bond/py retry"):
        return "POST /sync/bond/py retry"
    if label.startswith("POST /sync/bond/py"):
        return "POST /sync/bond/py"
    if label.startswith("POST /sync/bond/scenario-calc"):
        return "POST /sync/bond/scenario-calc"
    if label.startswith("GET /results/"):
        return "GET /results/{requestId}"
    return label


def _record_timing(label: str, elapsed: float) -> None:
    bucket = _timing_bucket(label)
    with _TIMING_LOCK:
        entry = _TIMING_SUMMARY.get(bucket)
        if entry is None:
            _TIMING_SUMMARY[bucket] = {"count": 1.0, "total": elapsed, "max": elapsed}
            return
        entry["count"] += 1.0
        entry["total"] += elapsed
        entry["max"] = max(entry["max"], elapsed)


def _print_timing_summary() -> None:
    if not _TIMING_SUMMARY:
        return
    print("\n[timing] Endpoint summary:")
    rows: List[Tuple[str, float, float, float]] = []
    for endpoint, data in _TIMING_SUMMARY.items():
        count = data.get("count", 0.0)
        total = data.get("total", 0.0)
        max_v = data.get("max", 0.0)
        avg = (total / count) if count else 0.0
        rows.append((endpoint, count, avg, max_v))
    rows.sort(key=lambda x: x[0])
    for endpoint, count, avg, max_v in rows:
        print(f"[timing] {endpoint}: count={int(count)}, avg={avg:.3f}s, max={max_v:.3f}s")


# -----------------------------------------------------------------------------
# Proxy resolution (single-thread) + per-thread sessions using explicit proxies
# -----------------------------------------------------------------------------

def _resolve_proxies_once(target_url: str) -> Dict[str, str]:
    """Resolve proxies for target_url using PyPAC once (single-thread)."""
    try:
        from pypac import pac_context_for_url  # type: ignore
    except Exception:
        return {}

    with pac_context_for_url(target_url):
        https_p = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
        http_p = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
        proxies: Dict[str, str] = {}
        if http_p:
            proxies["http"] = http_p
        if https_p:
            proxies["https"] = https_p
        return proxies


TOKEN_PROXIES = _resolve_proxies_once(AUTH_URL)
API_PROXIES = _resolve_proxies_once(API_BASE_URL)
if not TOKEN_PROXIES and API_PROXIES:
    TOKEN_PROXIES = dict(API_PROXIES)

_SESSION_KIND = "requests.Session(per-thread, explicit proxies)"
_SESSION_KIND += " [proxy enabled]" if (API_PROXIES or TOKEN_PROXIES) else " [no proxy detected]"

_THREAD_LOCAL = threading.local()


def _create_session(proxies: Dict[str, str]) -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=_POOL, pool_maxsize=_POOL)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.trust_env = False
    if proxies:
        s.proxies = dict(proxies)
    return s


def get_api_session() -> requests.Session:
    s = getattr(_THREAD_LOCAL, "api_session", None)
    if s is None:
        s = _create_session(API_PROXIES)
        _THREAD_LOCAL.api_session = s
    return s


def get_token_session() -> requests.Session:
    s = getattr(_THREAD_LOCAL, "token_session", None)
    if s is None:
        s = _create_session(TOKEN_PROXIES)
        _THREAD_LOCAL.token_session = s
    return s


# -----------------------------------------------------------------------------
# API helpers
# -----------------------------------------------------------------------------

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


def _load_api_credentials() -> Dict[str, str]:
    #api_id = os.getenv("YB_API_ID", "zwang@mtb.com-api")
    #api_key = os.getenv("YB_API_KEY", "")
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
    t0 = time.perf_counter()
    resp = get_token_session().post(AUTH_URL, data=auth_config, timeout=30)
    _timed_call_label("POST /x/oauth/api-token", t0)
    resp.raise_for_status()
    token = resp.json().get("accessToken")
    if not token:
        raise RuntimeError(f"No accessToken in response: {resp.text[:500]}")
    return token


# -----------------------------------------------------------------------------
# Utility: safe string + JSON dumps
# -----------------------------------------------------------------------------

def _safe(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float):
        return str(round(val, 6))
    return str(val)


def _dump_json(tag: str, payload: Any) -> None:
    if not DEBUG_DUMP:
        return
    try:
        Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)
        fn = os.path.join(DEBUG_DIR, tag)
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"[debug] dumped {fn}")
    except Exception as e:
        print(f"[debug] dump failed ({tag}): {e}")


# -----------------------------------------------------------------------------
# Helpers: tolerant JSON extraction
# -----------------------------------------------------------------------------

def _get_path(obj: Any, path: List[Any]) -> Any:
    """Safely walk a list of keys/indices. Returns None if missing."""
    cur = obj
    for p in path:
        if cur is None:
            return None
        if isinstance(p, int):
            if isinstance(cur, list) and 0 <= p < len(cur):
                cur = cur[p]
            else:
                return None
        else:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return None
    return cur


def _first_numeric(*vals: Any) -> Optional[float]:
    for v in vals:
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return float(v)
        # sometimes numbers come back as strings
        if isinstance(v, str):
            s = v.strip()
            if s == "":
                continue
            try:
                return float(s)
            except ValueError:
                continue
    return None


def _find_numeric_by_keys(obj: Any, keys: Tuple[str, ...], max_nodes: int = 2000) -> Optional[float]:
    """Depth-first search for first numeric value under any of the keys (dict keys only)."""
    seen = 0
    stack = [obj]
    while stack:
        cur = stack.pop()
        seen += 1
        if seen > max_nodes:
            break
        if isinstance(cur, dict):
            for k in keys:
                if k in cur:
                    n = _first_numeric(cur.get(k))
                    if n is not None:
                        return n
            # continue traversal
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, (dict, list)):
                    stack.append(v)
    return None


# -----------------------------------------------------------------------------
# YBTBAPRICE PrevClose
# -----------------------------------------------------------------------------

def get_security_name_for_tba(cusip: str, as_of: date) -> str:
    if cusip in TBA_CUSIP_TO_SECURITY_NAME_APR:
        suffix = get_yieldbook_tba_prod_suffix(as_of)
        prefix = TBA_CUSIP_TO_SECURITY_NAME_APR[cusip].rsplit("-", 1)[0]
        return f"{prefix}-{suffix}"
    return cusip


def get_prevclose_ybtbaprice(token: str, security_name: str) -> Optional[float]:
    base_url = api_url("tba-pricing", mode="sync")
    custom = os.getenv("YB_TBAPRICE_ENDPOINT", "").strip()
    if custom and custom.startswith("http"):
        base_url = custom

    params: Dict[str, Any] = {"name": security_name}
    pri = os.getenv("YB_TBA_PRICING_PRI", "").strip()
    if pri:
        try:
            params["pri"] = int(pri)
        except ValueError:
            pass

    try:
        t0 = time.perf_counter()
        resp = get_api_session().get(base_url, headers=api_headers(token), params=params, timeout=30)
        _timed_call_label(f"GET /sync/tba-pricing name={security_name}", t0)
        if resp.status_code == 200:
            data = resp.json()
            quotes = data.get("data", {}).get("quotes", [])
            for quote in quotes:
                ticker = quote.get("ticker", "")
                if ticker == security_name or security_name in ticker:
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
# PY calculation
# -----------------------------------------------------------------------------

def run_py_for_tbas(token: str, pricing_date: str, cusip_to_level: Dict[str, float]) -> List[Dict[str, Any]]:
    url = api_url("/bond/py", mode="sync")

    def _run_one(cusip: str) -> Dict[str, Any]:
        level = cusip_to_level.get(cusip) or 100.0
        body = {
            "globalSettings": {"pricingDate": pricing_date, "retrievePPMProjection": True},
            "input": [
                {
                    "identifier": cusip,
                    "idType": "securityIDEntry",
                    "userTag": cusip,
                    "level": level,
                    "curve": {
                            "curveType": "SWAP_RFR",
                            "currency": "USD",                          
                    },
                    "prepaySettings": {"type": "Model", "rate": PREPAY_RATE},
                    "volatility": {"type": "Default"},
                    "extraSettings": {"optionModel": "OASEDUR"},
                }
            ],
        }

        t0 = time.perf_counter()
        resp = get_api_session().post(url, headers=api_headers(token), json=body, timeout=90)
        _timed_call_label(f"POST /sync/bond/py cusip={cusip}", t0)
        if not resp.ok:
            print(f"PY failed for {cusip}: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()

        data = resp.json()
        res_list = data.get("results") or data.get("data") or []
        if isinstance(res_list, dict):
            res_list = [res_list]
        if not res_list:
            raise RuntimeError(f"No results in PY response for {cusip}: {list(data.keys())}")

        res = res_list[0]
        if DEBUG_DUMP:
            _dump_json(f"debug_py_{cusip.replace('/', '_')}.json", data)

        diag = _get_path(res, ["py", "diagnostic"]) or ""
        if isinstance(diag, str) and diag.startswith("Single volatility is not available"):
            body["input"][0]["volatility"] = {"type": "Default"}
            t0 = time.perf_counter()
            resp = get_api_session().post(url, headers=api_headers(token), json=body, timeout=90)
            _timed_call_label(f"POST /sync/bond/py retry cusip={cusip}", t0)
            resp.raise_for_status()
            data = resp.json()
            res_list = data.get("results") or data.get("data") or []
            if isinstance(res_list, dict):
                res_list = [res_list]
            res = res_list[0] if res_list else res

        return res

    by_cusip: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(TBA_CUSIPS))) as ex:
        fut_to_cusip = {ex.submit(_run_one, cusip): cusip for cusip in TBA_CUSIPS}
        for fut in as_completed(fut_to_cusip):
            cusip = fut_to_cusip[fut]
            by_cusip[cusip] = fut.result()

    return [by_cusip[c] for c in TBA_CUSIPS if c in by_cusip]


def extract_py_metrics(py_obj: Dict[str, Any]) -> Dict[str, Any]:
    """Schema-tolerant extraction of key metrics from a PY response item."""
    # Many responses have a top-level 'py' dict; some flatten fields.
    py = py_obj.get("py") if isinstance(py_obj.get("py"), dict) else {}

    # Candidate paths (ordered). If none found, fallback to deep key search.
    fwd_yield = _first_numeric(
        _get_path(py_obj, ["py", "forwardMeasures", "yield"]),
        _get_path(py_obj, ["py", "forwardMeasures", "forwardYield"]),
        _get_path(py_obj, ["py", "forwardYield"]),
        _get_path(py_obj, ["forwardYield"]),
    )
    if fwd_yield is None:
        fwd_yield = _find_numeric_by_keys(py_obj, ("forwardYield", "ForwardYield", "forward_yield"))

    ycm = _first_numeric(
        _get_path(py_obj, ["py", "yieldCurrentMargin"]),
        _get_path(py_obj, ["py", "yieldCurveMargin"]),
        _get_path(py_obj, ["py", "yieldCurrentmargin"]),
        _get_path(py_obj, ["yieldCurrentMargin"]),
    )
    if ycm is None:
        ycm = _find_numeric_by_keys(py_obj, ("yieldCurrentMargin", "yieldCurveMargin", "currentMargin"))

    oas = _first_numeric(
        _get_path(py_obj, ["py", "oas"]),
        _get_path(py_obj, ["py", "OAS"]),
        _get_path(py_obj, ["oas"]),
    )
    if oas is None:
        oas = _find_numeric_by_keys(py_obj, ("oas", "OAS", "optionAdjustedSpread"))

    fwd_wal = _first_numeric(
        _get_path(py_obj, ["py", "forwardWAL"]),
        _get_path(py_obj, ["py", "forwardWal"]),
        _get_path(py_obj, ["py", "wal"]),
        _get_path(py_obj, ["forwardWAL"]),
    )
    if fwd_wal is None:
        fwd_wal = _find_numeric_by_keys(py_obj, ("forwardWAL", "wal", "WAL"))

    duration = _first_numeric(_get_path(py_obj, ["py", "duration"]), _get_path(py_obj, ["duration"]))
    if duration is None:
        duration = _find_numeric_by_keys(py_obj, ("duration",))

    convexity = _first_numeric(_get_path(py_obj, ["py", "convexity"]), _get_path(py_obj, ["convexity"]))
    if convexity is None:
        convexity = _find_numeric_by_keys(py_obj, ("convexity",))

    eff_dur = _first_numeric(
        _get_path(py_obj, ["py", "effectiveDuration"]),
        _get_path(py_obj, ["py", "effDuration"]),
        _get_path(py_obj, ["effectiveDuration"]),
    )
    if eff_dur is None:
        eff_dur = _find_numeric_by_keys(py_obj, ("effectiveDuration", "effDuration"))

    eff_cvx = _first_numeric(
        _get_path(py_obj, ["py", "effectiveConvexity"]),
        _get_path(py_obj, ["py", "Effectiveconvexity"]),
        _get_path(py_obj, ["effectiveConvexity"]),
    )
    if eff_cvx is None:
        eff_cvx = _find_numeric_by_keys(py_obj, ("effectiveConvexity", "EffectiveConvexity"))

    # Longterm forward CPR: can be in PPM projection lists under various keys.
    lt_cpr = None
    ppm = (
        _get_path(py_obj, ["py", "dataPpmProjList"]) or
        _get_path(py_obj, ["py", "ppmProjection"]) or
        _get_path(py_obj, ["py", "ppmProjList"]) or
        []
    )
    if isinstance(ppm, list):
        for p in ppm:
            if not isinstance(p, dict):
                continue
            if p.get("prepayType") == "CPR" and "longTerm" in p:
                lt_cpr = p.get("longTerm")
                break
        if lt_cpr is None and ppm:
            p0 = ppm[0]
            if isinstance(p0, dict):
                lt_cpr = p0.get("longTerm")
    if lt_cpr is None:
        # fallback search (last resort)
        lt_cpr = _find_numeric_by_keys(py_obj, ("longTerm", "longTermCPR", "longTermCpr"))

    cusip = (
        py_obj.get("userTag") or
        _get_path(py_obj, ["py", "cusip"]) or
        py_obj.get("identifier")
    )

    return {
        "cusip": cusip,
        "Forwardyield": fwd_yield,
        "Yieldcurrentmargin": ycm,
        "OAS": oas,
        "ForwardWAL": fwd_wal,
        "LongtermfWDCPR": lt_cpr,
        "Duration": duration,
        "Convexity": convexity,
        "effectiveDuration": eff_dur,
        "Effectiveconvexity": eff_cvx,
        "price_last_close": _first_numeric(_get_path(py_obj, ["py", "pyLevel"]), _get_path(py_obj, ["py", "economicExposure"])),
    }


# -----------------------------------------------------------------------------
# Scenario-calc parsing helpers
# -----------------------------------------------------------------------------

def _ybscen_scenario_ref(bps: int) -> Dict[str, str]:
    q = os.getenv(
        "YB_YBSCEN_SCENARIO_QUERY",
        "timing=Gradual&reinvestmentRate=Default&swapSpreadConst=true",
    ).strip()
    path = f"/sys/scenario/Par/{bps}"
    ref = f"{path}?{q}" if q else path
    return {"$ref": ref}


def _build_ybscen_sync_body(
    cusip: str,
    settlement_date: str,
    level_prevclose: float,
    shocks_bps: Optional[List[int]] = None,
) -> Dict[str, Any]:
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
        "horizonPYMethod": "PRICE",
        "settlementInfo": {
            "settlementType": "CUSTOM",
            "settlementDate": settlement_date,
            "prepay": {"rate": str(PREPAY_RATE), "type": PREPAY_MODEL},
            "level": str(level_prevclose),
        },
        "volatility": {"type": VOLATILITY_TYPE},
        "assumeCall": False,
    }


def _bps_from_ref(ref: str) -> Optional[int]:
    if not ref:
        return None
    m = re.search(r"/Par/([+-]?\d+)", ref)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _extract_horizon_list(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Find horizon list in several plausible locations."""
    # canonical
    scenario = item.get("scenario") if isinstance(item.get("scenario"), dict) else {}
    for key in ("horizon", "horizonResults", "horizonInfo", "horizons"):
        h = scenario.get(key)
        if isinstance(h, list):
            return [x for x in h if isinstance(x, dict)]
        if isinstance(h, dict):
            # sometimes nested
            inner = h.get("horizon") or h.get("results")
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]

    # alternate at top-level
    for key in ("horizon", "horizonResults", "horizonInfo"):
        h = item.get(key)
        if isinstance(h, list):
            return [x for x in h if isinstance(x, dict)]

    return []


def _horizon_price(h: Dict[str, Any]) -> Optional[float]:
    # Prefer direct keys
    direct = _first_numeric(
        h.get("price"), h.get("actualPrice"), h.get("fullPrice"), h.get("actualFullPrice"),
        h.get("underlyingPrice"), h.get("marketValue"), h.get("economicExposure"),
        h.get("pyLevel"), h.get("value"),
    )
    if direct is not None:
        return direct

    # If horizon embeds a PY object
    py = h.get("py") if isinstance(h.get("py"), dict) else None
    if isinstance(py, dict):
        direct2 = _first_numeric(
            py.get("price"), py.get("actualPrice"), py.get("fullPrice"), py.get("actualFullPrice"),
            py.get("underlyingPrice"), py.get("marketValue"), py.get("economicExposure"),
            py.get("pyLevel"),
        )
        if direct2 is not None:
            return direct2

    # Last resort: deep scan for common price keys
    return _find_numeric_by_keys(h, ("price", "actualPrice", "fullPrice", "actualFullPrice", "pyLevel", "marketValue", "economicExposure", "value"))


def _horizon_price_map_from_payload(payload: Any, cusip: str, chunk: List[int]) -> Dict[int, float]:
    """Return {bps: price} from a scenario-calc result, keyed by scenarioRef.$ref if present."""
    out: Dict[int, float] = {}
    if not isinstance(payload, dict):
        return out

    results = payload.get("results") or payload.get("data")
    if results is None:
        return out
    if isinstance(results, dict):
        results = [results]
    if not isinstance(results, list) or not results:
        return out

    # find the right result item
    item = results[0]
    for r in results:
        if not isinstance(r, dict):
            continue
        tag = r.get("userTag") or r.get("identifier") or (r.get("py") or {}).get("cusip")
        if tag == cusip:
            item = r
            break

    horizons = _extract_horizon_list(item)
    if not horizons:
        return out

    # Preferred: map using scenarioRef.$ref -> bps
    for h in horizons:
        scen_ref = h.get("scenarioRef")
        ref = scen_ref.get("$ref", "") if isinstance(scen_ref, dict) else ""
        bps = _bps_from_ref(ref) if ref else None
        if bps is None:
            continue
        px = _horizon_price(h)
        if px is not None:
            out[bps] = px

    # Fallback: if scenarioRef not echoed, map by order for this chunk
    if not out:
        for i, h in enumerate(horizons[: len(chunk)]):
            bps = chunk[i]
            px = _horizon_price(h)
            if px is not None:
                out[bps] = px

    return out


def _resolve_scenario_payload(
    token: str,
    initial_payload: Dict[str, Any],
    max_wait_seconds: int = 180,
    initial_poll_seconds: float = 1.0,
    max_poll_seconds: float = 4.0,
) -> Dict[str, Any]:
    # sometimes sync endpoints return results inline
    if not isinstance(initial_payload, dict):
        return {}
    if initial_payload.get("results") is not None or initial_payload.get("data") is not None:
        return initial_payload

    request_id = initial_payload.get("requestId")
    if not request_id:
        return initial_payload

    results_url = api_url(f"/results/{request_id}")
    waited = 0.0
    poll_seconds = max(0.25, initial_poll_seconds)

    while waited <= max_wait_seconds:
        t0 = time.perf_counter()
        r = get_api_session().get(results_url, headers=api_headers(token), timeout=30)
        _timed_call_label(f"GET /results/{request_id}", t0)

        if r.status_code == 404:
            time.sleep(poll_seconds)
            waited += poll_seconds
            poll_seconds = min(max_poll_seconds, poll_seconds * 1.5)
            continue

        if not r.ok:
            return initial_payload

        j = r.json()
        status = (j.get("meta") or {}).get("status")
        if status == "DONE":
            return j
        if j.get("results") is not None or j.get("data") is not None:
            return j

        time.sleep(poll_seconds)
        waited += poll_seconds
        poll_seconds = min(max_poll_seconds, poll_seconds * 1.5)

    return initial_payload


def run_scenario_calc(
    token: str,
    pricing_date: str,
    settlement_date: str,
    py_metrics: List[Dict[str, Any]],
) -> Dict[str, Dict[str, str]]:
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

        row: Dict[str, str] = {f"price_bps_{bps:+d}": "" for bps in SHOCKS_BPS}

        for chunk_idx, chunk in enumerate(shock_chunks, start=1):
            body = {
                "input": [_build_ybscen_sync_body(cusip, settlement_date, base_price, chunk)],
                "globalSettings": global_settings,
            }

            t0 = time.perf_counter()
            resp = get_api_session().post(url, headers=api_headers(token), json=body, timeout=120)
            _timed_call_label(f"POST /sync/bond/scenario-calc cusip={cusip} chunk={chunk_idx}", t0)

            if not resp.ok:
                print(
                    f"scenario-calc sync failed for {cusip}, chunk {chunk_idx}: {resp.status_code} {resp.text[:300]}"
                )
                continue

            initial = resp.json()
            if DEBUG_DUMP and chunk_idx == 1:
                _dump_json(f"debug_scen_{cusip}_chunk{chunk_idx}_initial.json", initial)

            payload = _resolve_scenario_payload(token, initial)
            if DEBUG_DUMP and chunk_idx == 1:
                _dump_json(f"debug_scen_{cusip}_chunk{chunk_idx}_resolved.json", payload)

            price_map = _horizon_price_map_from_payload(payload, cusip, chunk)
            if not price_map:
                # lightweight on-screen hint
                keys = list(payload.keys()) if isinstance(payload, dict) else []
                print(f"[warn] empty price_map for {cusip} chunk {chunk_idx}. payload keys={keys}")

            for bps in chunk:
                col = f"price_bps_{bps:+d}"
                if bps in price_map:
                    row[col] = _safe(price_map[bps])

            if chunk_idx == 1:
                print(f" scenario-calc first chunk finished for {cusip} ({len(chunk)} shocks)")

        return str(cusip), row

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
    overall_started_at = time.perf_counter()
    started_wall_clock = datetime.now()

    print(f"[net] HTTP session: {_SESSION_KIND}")
    print(f"[net] MAX_WORKERS={MAX_WORKERS} (override with YB_MAX_WORKERS)")
    print(f"[net] API_PROXIES={'set' if bool(API_PROXIES) else 'none'}, TOKEN_PROXIES={'set' if bool(TOKEN_PROXIES) else 'none'}")
    if DEBUG_DUMP:
        print(f"[debug] YB_DEBUG_DUMP=1, directory={DEBUG_DIR}")
    print(f"[timing] Process started at: {started_wall_clock.isoformat(timespec='seconds')}")

    as_of = date.today()
    pricing_date = get_last_business_day_iso(as_of)
    settlement_date = get_yieldbook_tba_settlement_date(as_of)
    last_done = get_latest_class_a_settlement_before(as_of)
    next_any = get_next_settlement_date(as_of)
    yb_ym = get_yieldbook_tba_contract_month(as_of)
    yb_mon = get_yieldbook_tba_prod_suffix(as_of)

    print(f"Pricing date (last business day): {pricing_date}")
    print(f"As-of (calendar): {as_of.isoformat()}")
    if last_done:
        print(f"Latest Class A settled (before as-of): {last_done}")
    print(f"Next Class A on or after as-of: {next_any}")
    print(f"Yieldbook contract month: {yb_ym} (PROD-{yb_mon}); settlementDate for API: {settlement_date}")
    print(f"TBAs: {len(TBA_CUSIPS)}")
    print(f"Parallel workers: {MAX_WORKERS}")
    print(f"Shocks (bps): {SHOCKS_BPS}\n")

    print("Getting access token...")
    token = get_access_token()
    print("Token OK.\n")

    print("Fetching YBTBAPRICE PrevClose for each TBA (security name by contract month)...")
    cusip_to_level: Dict[str, float] = {}
    cusip_to_security_name: Dict[str, str] = {}

    def _fetch_prevclose(cusip: str) -> Tuple[str, str, Optional[float]]:
        sec_name = get_security_name_for_tba(cusip, as_of)
        price = get_prevclose_ybtbaprice(token, sec_name)
        return cusip, sec_name, price

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(TBA_CUSIPS))) as ex:
        futs = [ex.submit(_fetch_prevclose, cusip) for cusip in TBA_CUSIPS]
        for fut in as_completed(futs):
            cusip, sec_name, price = fut.result()
            cusip_to_security_name[cusip] = sec_name
            if price is not None:
                cusip_to_level[cusip] = price
                print(f" {cusip} -> {sec_name}: PrevClose = {price}")
            else:
                cusip_to_level[cusip] = 100.0
                print(f" {cusip} -> {sec_name}: PrevClose not found, using level=100")

    print("\nRunning PY for each TBA (at PrevClose level)...")
    py_results = run_py_for_tbas(token, pricing_date, cusip_to_level)

    metrics_list: List[Dict[str, Any]] = []
    for res in py_results:
        m = extract_py_metrics(res)
        cusip = m.get("cusip")
        if cusip and cusip in cusip_to_level:
            m["price_last_close"] = cusip_to_level[cusip]
            m["tba_security"] = cusip_to_security_name.get(cusip, "")
            metrics_list.append(m)

    print(f"PY done: {len(metrics_list)} results.\n")

    print(f"Running scenario-calc (YBSCEN /sync/bond/scenario-calc, OAS Change, pricing {pricing_date})...")
    scenario_by_cusip = run_scenario_calc(token, pricing_date, settlement_date, metrics_list)
    print("Scenario-calc done.\n")

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

    for r in rows:
        for h in all_headers:
            if h not in r:
                r[h] = ""

    if rows:
        print("Summary (first row):")
        print(" " + ", ".join(f"{k}={rows[0].get(k, '')}" for k in py_cols[:5]))
        print()
    else:
        print("Summary: no rows returned.\n")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Results saved to {OUTPUT_CSV}")

    ended_wall_clock = datetime.now()
    total_elapsed = time.perf_counter() - overall_started_at
    print(f"[timing] Process ended at: {ended_wall_clock.isoformat(timespec='seconds')}")
    print(f"[timing] Total runtime: {total_elapsed:.3f}s")
    _print_timing_summary()


if __name__ == "__main__":
    main()
