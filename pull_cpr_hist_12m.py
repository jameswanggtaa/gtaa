import os
import csv
from typing import Any, Dict, List, Optional

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

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------
INPUT_EXCEL = "tba_analysis_input.xlsx"
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
    if s == "muni":
        return "Model"
    if s.startswith("model"):
        return int("".join(filter(str.isdigit, s)))
    return s.upper()


def curve_type(val: Any, prepay_model: Any) -> str:
    """
    REST-safe curve handling.
    """
    if str(prepay_model).strip().lower() == "muni":
        return "SWAP_RFR"
    s = str(val).strip().upper()
    if "RFR" in s:
        return "SWAP_RFR"
    return s or "SWAP_RFR"

# ---------------------------------------------------------
# Load Excel
# ---------------------------------------------------------
def load_securities() -> List[Dict[str, Any]]:
    df = pd.read_excel(INPUT_EXCEL)
    out = []

    for _, r in df.iterrows():
        out.append({
            "cusip": str(r["CUSIP"]).strip(),
            "coupon": r["Coupon"],
            "maturity": normalize_date(r["Maturity_Date"]),
            "market_price": r["Market_price"],
            "curve_date": normalize_date(r["Curve_Date"]),
            "prepay_model": r["Prepay_Model"],
            "prepay_rate": r["Prepay_Rate"],
            "vol_model": r["Vol_Model"],
            "curve_type": r["Curve_Type"],
        })
    return out

# ---------------------------------------------------------
# PY Analytics
# ---------------------------------------------------------
def run_py(session, token: str, sec: Dict[str, Any]) -> Dict[str, Any]:

    security = {
        "cusip": sec["cusip"],
        #"price": sec["market_price"],
        "curveType": curve_type(sec["curve_type"], sec["prepay_model"]),
        "prepay": {
            "type": normalize_prepay_type(sec["prepay_model"]),
            "rate": sec["prepay_rate"],
        },
    }

    # ✅ ONLY add volModel if REST‑valid
    vol_model = sec.get("vol_model")
    if vol_model:
        vol_model = str(vol_model).strip().upper()
        if vol_model != "SINGLE":
            security["volModel"] = vol_model

    payload = {
        "pricingDate": sec["curve_date"],
        "securities": [security],
        "pyOptions": {
            "analytics": {
                "forwardYield": True,
                "effectiveDuration": True,
                "effectiveConvexity": True,
                "effectiveDV01": True,
                "dollarDuration": True,
                "averageLife": True,
                "oas": True,
                "zSpread": True,
                "factor": True,
                "lifeCPR": True,
                "ltCPR": True,
                "gwac": True,
                "wala": True,
                "wals": True,
                "maxServicer": True,
                "partialDurations": {
                    "type": "Effective",
                    "nodes": [1, 2, 3, 5, 10, 20, 30],
                },
            }
        },
    }

    url = api_url("bond/py", mode="req")
    r = session.post(url, json=payload, headers=api_headers(token), timeout=60)
    r.raise_for_status()
    return r.json()["results"][0]["analytics"]

# ---------------------------------------------------------
# Scenario Calc
# ---------------------------------------------------------
def run_scenarios(session, token: str, sec: Dict[str, Any]) -> List[Dict[str, Any]]:
    scenarios = []
    horizon_info = []

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
        horizon_info.append({
            "scenarioID": f"scen{i}",
            "level": 0,
            "prepay": {"rate": sec["prepay_rate"]}
        })

    payload = {
        "pricingDate": sec["curve_date"],
        "securities": [{
            "cusip": sec["cusip"],
            "coupon": sec["coupon"],
            "maturity": sec["maturity"],
            "price": sec["market_price"],
            "curveDate": sec["curve_date"],
            "curveType": curve_type(sec["curve_type"], sec["prepay_model"]),
        }],
        "scenarios": scenarios,
        "horizonInfo": horizon_info,
    }

    #url = f"{API_BASE}/req/bond/scenario-calc"
    url = api_url("bond/scenario-calc", mode="req")
    r = session.post(url, json=payload, headers=api_headers(token), timeout=120)
    r.raise_for_status()
    return r.json()["results"][0]["scenario"]["horizon"]

# ---------------------------------------------------------
# Main
# ---------------------------------------------------------
def main():
    session = make_http_session()
    token = get_access_token(session)

    securities = load_securities()
    rows = []

    for sec in securities:
        py = run_py(session, token, sec)
        scen = run_scenarios(session, token, sec)

        row = {
            "CUSIP": sec["cusip"],

            "Forward_Yield": py.get("forwardYield"),
            "Effective_Duration": py.get("effectiveDuration"),
            "Effective_Convexity": py.get("effectiveConvexity"),
            "Effective_DV01": py.get("effectiveDV01"),
            "Dollar_Duration": py.get("dollarDuration"),

            "PD_1Y": py.get("partialDurations", {}).get("1Y"),
            "PD_2Y": py.get("partialDurations", {}).get("2Y"),
            "PD_3Y": py.get("partialDurations", {}).get("3Y"),
            "PD_5Y": py.get("partialDurations", {}).get("5Y"),
            "PD_10Y": py.get("partialDurations", {}).get("10Y"),
            "PD_20Y": py.get("partialDurations", {}).get("20Y"),
            "PD_30Y": py.get("partialDurations", {}).get("30Y"),

            "Average_Life": py.get("averageLife"),
            "LT_CPR": py.get("ltCPR"),
            "Life_CPR": py.get("lifeCPR"),
            "OAS": py.get("oas"),
            "Z_Spread": py.get("zSpread"),
            "Factor": py.get("factor"),
            "GWAC": py.get("gwac"),
            "WALA": py.get("wala"),
            "WALS": py.get("wals"),
            "MaxServicerName": py.get("maxServicer", {}).get("name"),
            "MaxServicerPercent": py.get("maxServicer", {}).get("percent"),
        }

        # --- Scenario block (order‑stable) ---
        for i, shock in enumerate(SHOCKS_BPS):
            h = scen[i]
            row[f"EffDur_{shock}"] = h.get("effectiveDuration")
            row[f"DV01_{shock}"] = h.get("effectiveDV01")
            row[f"DollarReturn_{shock}"] = h.get("dollarReturn")

        rows.append(row)

    df = pd.DataFrame(rows)

    # Enforce exact Excel template layout
    df = df.reindex(columns=OUTPUT_COLUMNS)

    df.to_csv(OUTPUT_CSV, index=False, float_format="%.6f")
    print(f"Saved results to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
