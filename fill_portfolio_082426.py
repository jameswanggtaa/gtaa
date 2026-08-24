#!/usr/bin/env python3
"""
Fill Portfolio_082426_test_sub10.csv via Yield Book REST (YBPRICE + YBINDIC + YBSCEN).

Writes Portfolio_082426_test_sub10_output.csv. Depends only on New_Securities_Analysis.py.

Assumptions:
  Curve Date  = Report As Of Date
  Curve Type  = RFRSwap  (REST SWAP_RFR)
  Agency MBS* / Agency CMO: Prepay Model 2501 (REST OldModel / 2501), Prepay Rate 100,
                            Vol Model LMMSOFRFLAT
  Agency CMBS / Structured Agency CMBS: Prepay Model CPY, Prepay Rate 0, Vol Model LMMSOFRFLAT
  MUNI: Prepay Model Muni, Prepay Rate 100, Vol Model MatrixWSkew
  Treasuries: Prepay/Vol blank (REST Default vol)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import New_Securities_Analysis as nsa

DEFAULT_INPUT = "Portfolio_082426_test_sub10.csv"
DEFAULT_OUTPUT = "Portfolio_082426_test_sub10_output.csv"
LOG_FILE = "Portfolio_082426_test_sub10_fill.log"
FAILED_FILE = "Portfolio_082426_test_sub10_failed.csv"
SUMMARY_FILE = "Portfolio_082426_test_sub10_summary.json"

LARGE_BATCH_SIZE = int(os.environ.get("BULK_LARGE_BATCH_SIZE", "15"))
SMALL_BATCH_SIZE = int(os.environ.get("BULK_SMALL_BATCH_SIZE", "10"))
EST_MIN_LARGE_BATCH = float(os.environ.get("BULK_EST_MIN_LARGE_BATCH", "35"))
EST_MIN_SMALL_BATCH = float(os.environ.get("BULK_EST_MIN_SMALL_BATCH", "12"))
BATCH_RETRIES = int(os.environ.get("BULK_BATCH_RETRIES", "3"))
BATCH_RETRY_WAIT_S = float(os.environ.get("BULK_BATCH_RETRY_WAIT_S", "120"))
CHECKPOINT_EVERY = int(os.environ.get("BULK_CHECKPOINT_EVERY", "1"))
DEFAULT_WORKERS = int(os.environ.get("BULK_WORKERS", os.environ.get("YB_WORKERS", "4")))
FINAL_RETRY_ROUNDS = int(os.environ.get("BULK_FINAL_RETRY_ROUNDS", "3"))
FINAL_RETRY_WAIT_S = float(os.environ.get("BULK_FINAL_RETRY_WAIT_S", "60"))

SHOCKS = nsa.SHOCKS_BPS_0526
_log_lock = threading.Lock()

SKIP_SUB_TYPES = frozenset({
    "Misc_Securities",
    "Non-Agency MBS",
    "Regulatory_Stock",
    "Non-Agency CMBS",
    "Non-Agency CMO",
    "CRA_INV_FUND",
    "Foreign_Bonds",
    "CPC",
})

MBS_2501_SUB_TYPES = frozenset({
    "Agency MBS 10yr",
    "Agency MBS 15yr",
    "Agency MBS 20yr",
    "Agency MBS 30yr",
    "Agency MBS ARM",
    "Agency MBS Other",
})

LARGE_SAMPLE_SUB_TYPES = frozenset({"MUNI", "Treasuries"})

GROUP_ORDER = [
    "MUNI",
    "Treasuries",
    "Agency CMBS",
    "Structured Agency CMBS",
    "Agency CMO",
    "Agency MBS 30yr",
    "Agency MBS 15yr",
    "Agency MBS ARM",
    "Agency MBS Other",
    "Agency MBS 20yr",
    "Agency MBS 10yr",
]

ASSUMPTION_COLS = ("Curve Date", "Prepay Model", "Prepay Rate", "Vol Model", "Curve Type")

# GNMA A-3-Q passthrough CMBS reject CPY/0 on REST ("Invalid Prepay Type").
A3Q_AGENCY_CMBS_CUSIPS = frozenset({"36200C6W0", "36292H2W2"})


def build_field_map() -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = [
        ("Forward_Yield", "Forward Yield"),
        ("Prospective_Yield", "Prospective Yield"),
        ("Effective_Duration", "Effective Duration"),
        ("Effective_Convexity", "Effective Convexity"),
        ("Effective_DV01", "Effective DV01"),
        ("Dollar_Duration", "Dollar Duration"),
        ("PD_1Y", "1YR"),
        ("PD_2Y", "2YR"),
        ("PD_3Y", "3YR"),
        ("PD_5Y", "5YR"),
        ("PD_10Y", "10YR"),
        ("PD_20Y", "20YR"),
        ("PD_30Y", "30YR"),
        ("Average_Life", "Average Life"),
        ("LT_CPR", "LT CPR"),
        ("OAS", "OAS"),
        ("Z_Spread", "Z-Spread"),
        ("Factor", "Factor"),
        ("Life_CPR", "Life CPR"),
        ("GWAC", "GWAC"),
        ("WALA", "WALA"),
        ("WALS", "WALS"),
        ("MaxServicerName", "MaxServicerName"),
        ("MaxServicerPercent", "MaxServicerPercent"),
    ]
    excel_triplets = [
        ("Eff. Dur", "DV01", "Dollar Return"),
        ("Eff. Dur.1", "DV01.1", "Dollar Return.1"),
        ("Eff. Dur.2", "DV01.2", "Dollar Return.2"),
        ("Eff. Dur.3", "DV01.3", "Dollar Return.3"),
        ("Eff. Dur.4", "DV01.4", "Dollar Return.4"),
        ("Eff. Dur.5", "DV01.5", "Dollar Return.5"),
    ]
    for shock, (ed, dv, dr) in zip(nsa.SHOCKS_BPS_0526, excel_triplets):
        shock_label = f"{shock:+d}" if shock > 0 else str(shock)
        pairs.append((f"EffDur_{shock_label}", ed))
        pairs.append((f"DV01_{shock_label}", dv))
        pairs.append((f"DollarReturn_{shock_label}", dr))
    return pairs


FIELD_MAP = build_field_map()


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _cell_is_empty(val: Any) -> bool:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return True
    s = str(val).strip()
    return not s or s.lower() in {"nan", "none"}


def fmt_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return ""
    if hasattr(v, "item") and callable(getattr(v, "item", None)):
        try:
            v = v.item()
        except Exception:
            pass
    if isinstance(v, float):
        if pd.isna(v):
            return ""
        if abs(v - round(v)) < 1e-9 and abs(v) < 1e15:
            return str(int(round(v)))
        s = f"{v:.10g}"
        if "e" in s.lower():
            return s
        s = s.rstrip("0").rstrip(".")
        return s if s else "0"
    return str(v)


def map_row_to_csv_columns(row: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for api_key, csv_col in FIELD_MAP:
        val = row.get(api_key)
        if val is None or (isinstance(val, float) and pd.isna(val)):
            out[csv_col] = ""
        else:
            out[csv_col] = fmt_cell(val)
    return out


def row_is_filled(df: pd.DataFrame, idx: int) -> bool:
    if "Forward Yield" not in df.columns:
        return False
    fy = df.at[idx, "Forward Yield"]
    s = str(fy).strip() if fy is not None else ""
    return bool(s) and s.lower() not in {"nan", "none"}


def clear_rest_columns(df: pd.DataFrame, idx: int) -> None:
    for _, csv_col in FIELD_MAP:
        if csv_col in df.columns:
            df.at[idx, csv_col] = ""


def display_curve_date(raw: Any, iso: str) -> str:
    if raw is not None and not _cell_is_empty(raw):
        s = str(raw).strip()
        if s:
            return s
    try:
        dt = pd.to_datetime(iso)
        return f"{dt.month}/{dt.day}/{dt.year}"
    except Exception:
        return iso


def product_assumptions(sub_type: Optional[str]) -> Dict[str, Any]:
    """CSV + REST assumption fields for a Sub Type."""
    if nsa.sub_type_is_treasury(sub_type):
        return {
            "prepay_model": None,
            "prepay_rate": None,
            "vol_model": None,
            "curve_type": "RFRSwap",
            "csv_prepay_model": "",
            "csv_prepay_rate": "",
            "csv_vol_model": "",
            "force_prepay_type": None,
        }
    if nsa.sub_type_is_agency_cmbs_family(sub_type):
        return {
            "prepay_model": "CPY",
            "prepay_rate": 0.0,
            "vol_model": "LMMSOFRFLAT",
            "curve_type": "RFRSwap",
            "csv_prepay_model": "CPY",
            "csv_prepay_rate": "0",
            "csv_vol_model": "LMMSOFRFLAT",
            "force_prepay_type": None,
        }
    st = (sub_type or "").strip()
    if st in MBS_2501_SUB_TYPES:
        return {
            "prepay_model": "2501",
            "prepay_rate": 100.0,
            "vol_model": "LMMSOFRFLAT",
            "curve_type": "RFRSwap",
            "csv_prepay_model": "2501",
            "csv_prepay_rate": "100",
            "csv_vol_model": "LMMSOFRFLAT",
            "force_prepay_type": "OldModel",
        }
    if nsa.sub_type_is_agency_cmo(sub_type):
        return {
            "prepay_model": "2501",
            "prepay_rate": 100.0,
            "vol_model": "LMMSOFRFLAT",
            "curve_type": "RFRSwap",
            "csv_prepay_model": "2501",
            "csv_prepay_rate": "100",
            "csv_vol_model": "LMMSOFRFLAT",
            "force_prepay_type": 2501,
        }
    # MUNI and anything else priced with municipal convention
    return {
        "prepay_model": "Muni",
        "prepay_rate": 100.0,
        "vol_model": "MatrixWSkew",
        "curve_type": "RFRSwap",
        "csv_prepay_model": "Muni",
        "csv_prepay_rate": "100",
        "csv_vol_model": "MatrixWSkew",
        "force_prepay_type": None,
    }


def load_portfolio_securities(df: pd.DataFrame) -> List[Dict[str, Any]]:
    cols = nsa.col_lookup(df)
    cusip_col = nsa.find_col(cols, "CUSIP", "CUSIPs")
    sub_col = nsa.find_col(cols, "Sub Type", "Sub_Type", "Subtype")
    price_col = nsa.find_col(cols, "PRICE (Mkt)", "Market_price", "Market Price", "Market Price ")
    book_price_col = nsa.find_col(
        cols,
        "MTB_BOOK PRICE (Clean)",
        "MTB_BOOK_PRICE",
        "MTB Book Price (Clean)",
        "Book Price",
    )
    coupon_col = nsa.find_col(cols, "Coupon")
    maturity_col = nsa.find_col(cols, "Maturity Date", "Maturity_Date")
    report_col = nsa.find_col(
        cols, "Report As Of Date", "Report As Date", "Report As Of", "Report Date"
    )
    curve_date_col = nsa.find_col(cols, "Curve Date", "Curve_Date")
    nominal_col = nsa.find_col(cols, "Nominal", "CA_NOTIONAL", "Notional", "Current Balance")
    current_factor_col = nsa.find_col(
        cols, "Current Factor", "Current_Factor", "current_factor", "Factor_Input"
    )
    if not cusip_col or not price_col:
        raise ValueError(f"Need CUSIP and market price columns. Found: {list(df.columns)}")

    out: List[Dict[str, Any]] = []
    for idx, r in df.iterrows():
        cusip = str(r[cusip_col]).strip()
        if not cusip or cusip.lower() == "nan":
            continue
        sub_type = nsa.clean_text(r[sub_col]) if sub_col else None
        raw_report = r[report_col] if report_col else None
        if report_col and not _cell_is_empty(raw_report):
            curve_iso = nsa.normalize_date(raw_report)
        elif curve_date_col and not _cell_is_empty(r[curve_date_col]):
            curve_iso = nsa.normalize_date(r[curve_date_col])
            raw_report = r[curve_date_col]
        else:
            curve_iso = nsa.default_pricing_date()
        if not curve_iso:
            curve_iso = nsa.default_pricing_date()
        assum = product_assumptions(sub_type)
        cf_val = nsa.parse_number(r[current_factor_col]) if current_factor_col else None
        out.append({
            "_df_index": idx,
            "cusip": cusip,
            "sub_type": sub_type,
            "coupon": nsa.parse_number(r[coupon_col]) if coupon_col else None,
            "maturity": nsa.normalize_date(r[maturity_col]) if maturity_col else None,
            "market_price": nsa.parse_number(r[price_col]),
            "book_price": nsa.parse_number(r[book_price_col]) if book_price_col else None,
            "curve_date": curve_iso,
            "curve_date_display": display_curve_date(raw_report, curve_iso),
            "prepay_model": assum["prepay_model"],
            "prepay_rate": assum["prepay_rate"],
            "vol_model": assum["vol_model"],
            "curve_type": assum["curve_type"],
            "csv_prepay_model": assum["csv_prepay_model"],
            "csv_prepay_rate": assum["csv_prepay_rate"],
            "csv_vol_model": assum["csv_vol_model"],
            "force_prepay_type": assum["force_prepay_type"],
            "nominal": nsa.parse_number(r[nominal_col]) if nominal_col else None,
            "current_factor": float(cf_val) if cf_val is not None else 1.0,
        })
    return out


def apply_assumption_columns(df: pd.DataFrame, securities: List[Dict[str, Any]]) -> None:
    for col in ASSUMPTION_COLS:
        if col not in df.columns:
            df[col] = ""
    for sec in securities:
        idx = int(sec["_df_index"])
        df.at[idx, "Curve Date"] = sec.get("curve_date_display") or ""
        df.at[idx, "Prepay Model"] = sec.get("csv_prepay_model") or ""
        df.at[idx, "Prepay Rate"] = sec.get("csv_prepay_rate") or ""
        df.at[idx, "Vol Model"] = sec.get("csv_vol_model") or ""
        df.at[idx, "Curve Type"] = "RFRSwap"


def _has_market_price(sec: Dict[str, Any]) -> bool:
    mp = sec.get("market_price")
    if mp is None:
        return False
    try:
        return float(mp) > 0
    except (TypeError, ValueError):
        return False


def group_securities(securities: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for sec in securities:
        st = (sec.get("sub_type") or "").strip()
        if st in SKIP_SUB_TYPES:
            continue
        if not _has_market_price(sec):
            continue
        groups.setdefault(st, []).append(sec)
    for st in groups:
        groups[st].sort(key=lambda s: (str(s["cusip"]), int(s["_df_index"])))
    return groups


def dedupe_for_api(batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for sec in batch:
        key = str(sec["cusip"]).strip().upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(sec)
    return out


def agency_cmbs_prepay_for(sec: Dict[str, Any]) -> Dict[str, str]:
    if str(sec.get("cusip") or "").strip().upper() in A3Q_AGENCY_CMBS_CUSIPS:
        return {"type": "Model", "rate": "100"}
    return nsa.agency_cmbs_prepay_settings()


def build_py_input_row(sec: Dict[str, Any]) -> Dict[str, Any]:
    pricing_date = sec.get("curve_date") or nsa.default_pricing_date()
    curve_obj = nsa.curve_dict_for(sec)
    volatility = nsa.resolve_volatility(sec)
    row: Dict[str, Any] = {
        "identifier": nsa.yieldbook_security_identifier(sec),
        "userTag": sec["cusip"],
        "floaterSettings": {},
        "extraSettings": {"includePartials": True},
        "level": str(nsa.bond_py_level_for_request(sec)),
        "settlementDate": pricing_date,
    }

    if nsa.muni_sync_py_ybprice_rfrswap_shape(sec, curve_obj):
        row["curve"] = curve_obj
        row["volatility"] = {"type": "MatrixWSkew"}
        return row

    if nsa.agency_cmbs_ybprice_sync_shape(sec, curve_obj):
        row["curve"] = {"curveType": "SWAP_RFR"}
        row["prepaySettings"] = agency_cmbs_prepay_for(sec)
        row["volatility"] = volatility
        return row

    if nsa.treasury_ybprice_sync_shape(sec, curve_obj):
        row["curve"] = {"curveType": "SWAP_RFR"}
        row["prepaySettings"] = nsa.treasury_prepay_settings()
        row["volatility"] = {"type": "Default"}
        return row

    prepay_type = nsa.ybprice_prepay_type_for_agency_mortgage(sec)
    prepay_rate = nsa.ybprice_prepay_rate_str(sec)
    row["curve"] = {"curveType": curve_obj.get("curveType") or "SWAP_RFR"}
    row["prepaySettings"] = {"rate": prepay_rate, "type": prepay_type}
    row["volatility"] = volatility
    return row


def build_scenario_input_row(sec: Dict[str, Any]) -> Dict[str, Any]:
    pricing_date = sec.get("curve_date") or nsa.default_pricing_date()
    scen_curve = nsa.curve_dict_for(sec)
    timing = nsa.scenario_timing()

    if nsa.sub_type_is_treasury(sec.get("sub_type")):
        tsy_settle_prepay = nsa.treasury_scenario_settlement_prepay()
        return {
            "identifier": nsa.yieldbook_security_identifier(sec),
            "userTag": sec["cusip"],
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
                for shock in SHOCKS
            ],
            "curve": {"curveType": "SWAP_RFR"},
            "horizonPYMethod": nsa.scenario_horizon_py_method(),
            "settlementInfo": {
                "prepay": tsy_settle_prepay,
                "level": str(nsa.bond_py_level_value(sec)),
                "settlementDate": pricing_date,
            },
            "assumeCall": False,
        }

    if nsa.muni_sync_py_ybprice_rfrswap_shape(sec, scen_curve):
        prepay_type = nsa.clean_text(sec.get("prepay_model")) or "Muni"
        prepay_rate = nsa.ybprice_prepay_rate_str(sec)
        volatility = {"type": "MatrixWSkew"}
    elif nsa.sub_type_is_agency_cmbs_family(sec.get("sub_type")):
        cmbs_prepay = agency_cmbs_prepay_for(sec)
        prepay_type = cmbs_prepay["type"]
        prepay_rate = cmbs_prepay["rate"]
        volatility = nsa.resolve_volatility(sec)
    else:
        prepay_type = nsa.ybprice_prepay_type_for_agency_mortgage(sec)
        prepay_rate = nsa.ybprice_prepay_rate_str(sec)
        volatility = nsa.resolve_volatility(sec)

    return {
        "identifier": nsa.yieldbook_security_identifier(sec),
        "userTag": sec["cusip"],
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
            for shock in SHOCKS
        ],
        "curve": {"curveType": scen_curve.get("curveType") or "SWAP_RFR"},
        "horizonPYMethod": nsa.scenario_horizon_py_method(),
        "settlementInfo": {
            "prepay": {"rate": prepay_rate, "type": prepay_type},
            "level": str(nsa.bond_py_level_value(sec)),
            "settlementDate": pricing_date,
        },
        "volatility": volatility,
        "assumeCall": False,
    }


def build_bulk_py_payload(securities: List[Dict[str, Any]]) -> Dict[str, Any]:
    pricing_date = securities[0].get("curve_date") or nsa.default_pricing_date()
    return {
        "globalSettings": {"pricingDate": pricing_date},
        "input": [build_py_input_row(s) for s in securities],
    }


def build_book_py_payload(securities: List[Dict[str, Any]]) -> Dict[str, Any]:
    pricing_date = securities[0].get("curve_date") or nsa.default_pricing_date()
    rows = []
    for sec in securities:
        row = build_py_input_row(sec)
        book = nsa.parse_number(sec.get("book_price"))
        if book is not None and not pd.isna(book):
            row["level"] = str(book)
        rows.append(row)
    return {"globalSettings": {"pricingDate": pricing_date}, "input": rows}


def build_bulk_indic_payload(securities: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "input": [{"identifier": nsa.yieldbook_security_identifier(s)} for s in securities],
    }


def build_bulk_scenario_payload(securities: List[Dict[str, Any]]) -> Dict[str, Any]:
    pricing_date = securities[0].get("curve_date") or nsa.default_pricing_date()
    return {
        "globalSettings": {
            "pricingDate": pricing_date,
            "horizonDays": str(nsa.SCENARIO_HORIZON_DAYS),
            "horizonMonths": str(nsa.SCENARIO_HORIZON_MONTHS),
            "calcHorizonEffectiveMeasures": True,
        },
        "input": [build_scenario_input_row(s) for s in securities],
    }


def poll_results(session, token: str, request_id: str, timeout_s: float = 900.0) -> Dict[str, Any]:
    url = nsa.api_url(f"/results/{request_id}", mode=None)
    deadline = time.monotonic() + timeout_s
    last_bad_status: Optional[int] = None
    while time.monotonic() < deadline:
        resp = session.get(url, headers=nsa.api_headers(token), timeout=60)
        if resp.status_code == 404:
            time.sleep(2)
            continue
        if not resp.ok:
            if resp.status_code != last_bad_status:
                _log(f"  [WARN] results GET {request_id} HTTP {resp.status_code}: {(resp.text or '')[:200]}")
                last_bad_status = resp.status_code
            time.sleep(2)
            continue
        try:
            data = resp.json()
        except Exception:
            time.sleep(2)
            continue
        status = (data.get("meta") or {}).get("status")
        if status == "DONE":
            return data
        if status == "ERROR":
            raise RuntimeError(f"Job {request_id} ERROR: {data.get('errors') or data.get('meta')}")
        time.sleep(2)
    raise TimeoutError(f"Timed out polling {request_id}")


def finish_sync_bulk(
    session,
    token: str,
    initial: Dict[str, Any],
    *,
    timeout_s: float,
) -> List[Dict[str, Any]]:
    results = initial.get("results")
    if results:
        return list(results)
    request_id = initial.get("requestId")
    if not request_id:
        return []
    data = poll_results(session, token, request_id, timeout_s=timeout_s)
    return list(data.get("results") or [])


def _cusip_from_result(item: Dict[str, Any]) -> Optional[str]:
    tag = item.get("userTag")
    if tag:
        return str(tag).strip().upper()
    cusip = item.get("cusip")
    if cusip:
        raw = str(cusip).strip().upper()
        return raw[:-4] if raw.endswith(".CMO") else raw
    ident = item.get("identifier")
    if ident:
        raw = str(ident).strip().upper()
        return raw[:-4] if raw.endswith(".CMO") else raw
    return None


def effective_wal_from_py(py_obj: Dict[str, Any]) -> Any:
    return nsa.get_first_key(py_obj, "effectiveWAL", "EffectiveWAL")


def normalize_py_measures(
    session,
    token: str,
    sec: Dict[str, Any],
    py_obj: Dict[str, Any],
) -> Dict[str, Any]:
    partial_durations = nsa.extract_partial_durations(py_obj)
    if not partial_durations:
        partial_durations = nsa.fetch_partial_durations_by_keywords(session, token, sec)
    max_servicer = py_obj.get("maxServicer") or {}
    if isinstance(max_servicer, str):
        max_servicer = {"name": max_servicer, "percent": None}
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
        else nsa.get_first_key(py_obj.get("forwardMeasures") or {}, "yield", "Yield")
    )
    if fwd_yield is None and not nsa.sub_type_is_agency_mbs_family(sec.get("sub_type")):
        fwd_yield = flat_yield
    return {
        "forwardYield": fwd_yield,
        "bondYield": flat_yield or py_obj.get("effectiveYield") or py_obj.get("streetYield"),
        "effectiveDuration": eff_dur,
        "effectiveConvexity": eff_conv,
        "effectiveDV01": eff_dv01,
        "dollarDuration": py_obj.get("dollarDuration"),
        "partialDurations": partial_durations,
        "averageLife": effective_wal_from_py(py_obj),
        "LongTermCPR": nsa.extract_long_term_cpr(py_obj),
        "oas": py_obj.get("oas"),
        "zSpread": py_obj.get("zSpread"),
        "factor": nsa.get_first_key(py_obj, "factor", "Factor"),
        "GrossWAC": nsa.get_first_key(py_obj, "grossWAC", "GrossWAC", "gwac", "GWAC"),
        "LoanAge": nsa.get_first_key(py_obj, "loanAge", "LoanAge", "wala", "WALA"),
        "WeightedAvgLoanSize": nsa.get_first_key(
            py_obj, "weightedAvgLoanSize", "WeightedAvgLoanSize", "wals", "WALS"
        ),
        "maxServicer": max_servicer,
    }


def build_api_row_from_bulk(
    session,
    token: str,
    sec: Dict[str, Any],
    py_item: Dict[str, Any],
    indic_item: Optional[Dict[str, Any]],
    scen_item: Optional[Dict[str, Any]],
    *,
    py_book_item: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    py_obj = py_item.get("py") or {}
    py = normalize_py_measures(session, token, sec, py_obj)
    indic_raw = (indic_item or {}).get("indic") or {}
    indic_vals = nsa.normalize_indic_measures(indic_raw) if indic_raw else {}
    raw_h = ((scen_item or {}).get("scenario") or {}).get("horizon", [])
    scen = nsa.normalize_scenario_horizon(raw_h, shocks_bps=SHOCKS)
    partials = py.get("partialDurations") or {}
    row: Dict[str, Any] = {
        "CUSIP": sec["cusip"],
        "Sub Type": sec.get("sub_type"),
        "Forward_Yield": nsa.resolve_forward_yield_column(py, sec.get("sub_type")),
        "Effective_Duration": py.get("effectiveDuration"),
        "Effective_Convexity": py.get("effectiveConvexity"),
        "Effective_DV01": py.get("effectiveDV01"),
        "Dollar_Duration": nsa.computed_dollar_duration(sec, py),
        "PD_1Y": partials.get("1Y"),
        "PD_2Y": partials.get("2Y"),
        "PD_3Y": partials.get("3Y"),
        "PD_5Y": partials.get("5Y"),
        "PD_10Y": partials.get("10Y"),
        "PD_20Y": partials.get("20Y"),
        "PD_30Y": partials.get("30Y"),
        "Average_Life": py.get("averageLife"),
        "LT_CPR": py.get("LongTermCPR"),
        "OAS": py.get("oas"),
        "Z_Spread": py.get("zSpread"),
        "Factor": py.get("factor") if py.get("factor") is not None else indic_vals.get("factor"),
        "GWAC": py.get("GrossWAC") or indic_vals.get("GrossWAC"),
        "WALA": py.get("LoanAge") or indic_vals.get("LoanAge"),
        "WALS": py.get("WeightedAvgLoanSize") or indic_vals.get("WeightedAvgLoanSize"),
        "MaxServicerName": py.get("maxServicer", {}).get("name") or indic_vals.get("maxServicerName"),
        "MaxServicerPercent": py.get("maxServicer", {}).get("percent") or indic_vals.get("maxServicerPercent"),
        "Life_CPR": nsa.resolve_life_cpr_value(sec.get("sub_type"), py, indic_raw),
    }
    if py_book_item:
        py_book_obj = py_book_item.get("py") or {}
        py_book = normalize_py_measures(session, token, sec, py_book_obj)
        row["Prospective_Yield"] = nsa.resolve_prospective_yield_from_py(py_book, sec.get("sub_type"))
    if row.get("Life_CPR") is None:
        row["Life_CPR"] = nsa.resolve_life_cpr_value(sec.get("sub_type"), py, indic_raw)
    return nsa.merge_scenario_into_row(row, scen, shocks_bps=SHOCKS)


def fetch_indic_bulk_or_fallback(
    session,
    token: str,
    securities: List[Dict[str, Any]],
    *,
    sync_timeout_s: float,
) -> Dict[str, Dict[str, Any]]:
    payload = build_bulk_indic_payload(securities)
    indic_url = nsa.api_url("bond/indic", mode="sync")
    resp = session.post(indic_url, json=payload, headers=nsa.api_headers(token), timeout=120)
    out: Dict[str, Dict[str, Any]] = {}
    if resp.ok:
        items = finish_sync_bulk(session, token, resp.json(), timeout_s=sync_timeout_s)
        for item in items:
            cusip = _cusip_from_result(item)
            if cusip:
                out[cusip] = item
        if len(out) == len(securities):
            return out
        _log(f"  [WARN] bulk indic returned {len(out)}/{len(securities)}; filling per-CUSIP")
    else:
        _log(
            f"  [WARN] bulk indic HTTP {resp.status_code}: {(resp.text or '')[:300]}; "
            "falling back to per-CUSIP"
        )

    def _one(sec: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        cusip = str(sec["cusip"]).strip().upper()
        payload1 = {"input": [{"identifier": nsa.yieldbook_security_identifier(sec)}]}
        r = session.post(indic_url, json=payload1, headers=nsa.api_headers(token), timeout=60)
        if not r.ok:
            return cusip, {}
        items = finish_sync_bulk(session, token, r.json(), timeout_s=sync_timeout_s)
        return cusip, items[0] if items else {}

    with ThreadPoolExecutor(max_workers=nsa.parallel_worker_count()) as ex:
        futs = [ex.submit(_one, s) for s in securities]
        for fut in as_completed(futs):
            cusip, item = fut.result()
            if item:
                out[cusip] = item
    return out


def _index_bulk_results(
    items: List[Dict[str, Any]],
    securities: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    by_cusip: Dict[str, Dict[str, Any]] = {}
    for item in items:
        cusip = _cusip_from_result(item)
        if not cusip and item.get("identifier"):
            cusip = str(item["identifier"]).strip().upper()
        if cusip:
            by_cusip[cusip] = item
    if len(by_cusip) < len(securities):
        for i, sec in enumerate(securities):
            key = str(sec["cusip"]).strip().upper()
            if key not in by_cusip and i < len(items):
                by_cusip[key] = items[i]
    return by_cusip


def run_bulk_batch(
    session,
    token: str,
    batch: List[Dict[str, Any]],
    *,
    scenario_timeout_s: float,
) -> Tuple[Dict[str, Dict[str, str]], Optional[str]]:
    api_secs = dedupe_for_api(batch)
    if not api_secs:
        return {}, None

    py_url = nsa.api_url("bond/py", mode="sync")
    py_resp = session.post(
        py_url,
        json=build_bulk_py_payload(api_secs),
        headers=nsa.api_headers(token),
        timeout=300,
    )
    if not py_resp.ok:
        return {}, f"py HTTP {py_resp.status_code}: {(py_resp.text or '')[:300]}"
    py_items = finish_sync_bulk(session, token, py_resp.json(), timeout_s=scenario_timeout_s)
    py_by = _index_bulk_results(py_items, api_secs)

    book_secs = [
        s for s in api_secs
        if (bp := nsa.parse_number(s.get("book_price"))) is not None and float(bp) == float(bp)
    ]
    py_book_by: Dict[str, Dict[str, Any]] = {}
    if book_secs:
        book_resp = session.post(
            py_url,
            json=build_book_py_payload(book_secs),
            headers=nsa.api_headers(token),
            timeout=300,
        )
        if book_resp.ok:
            book_items = finish_sync_bulk(session, token, book_resp.json(), timeout_s=scenario_timeout_s)
            py_book_by = _index_bulk_results(book_items, book_secs)

    indic_by = fetch_indic_bulk_or_fallback(
        session, token, api_secs, sync_timeout_s=scenario_timeout_s,
    )

    scen_url = nsa.api_url("bond/scenario-calc", mode="req")
    scen_resp = session.post(
        scen_url,
        json=build_bulk_scenario_payload(api_secs),
        headers=nsa.api_headers(token),
        timeout=120,
    )
    if not scen_resp.ok:
        return {}, f"scenario HTTP {scen_resp.status_code}: {(scen_resp.text or '')[:300]}"
    request_id = scen_resp.json().get("requestId")
    if not request_id:
        return {}, "scenario: no requestId"
    scen_data = poll_results(session, token, request_id, timeout_s=scenario_timeout_s)
    scen_items = scen_data.get("results") or []
    scen_by = _index_bulk_results(scen_items, api_secs)

    out: Dict[str, Dict[str, str]] = {}
    for sec in api_secs:
        cusip = str(sec["cusip"]).strip().upper()
        py_item = py_by.get(cusip, {})
        indic_item = indic_by.get(cusip, {})
        scen_item = scen_by.get(cusip, {})
        if not py_item and not scen_item:
            continue
        book_item = py_book_by.get(cusip)
        api_row = build_api_row_from_bulk(
            session, token, sec, py_item, indic_item, scen_item, py_book_item=book_item,
        )
        out[cusip] = map_row_to_csv_columns(api_row)
    return out, None


def _is_transient_error(err: Optional[str]) -> bool:
    if not err:
        return False
    e = err.lower()
    return any(
        x in e
        for x in (
            "connectionerror",
            "connection aborted",
            "connectionreset",
            "nameresolution",
            "failed to resolve",
            "timeout",
            "timed out polling",
            "token expired",
            "http 401",
            "401",
        )
    )


def run_bulk_batch_with_retries(
    session,
    token: str,
    batch: List[Dict[str, Any]],
    *,
    scenario_timeout_s: float,
) -> Tuple[Dict[str, Dict[str, str]], Optional[str], Any, str]:
    last_err: Optional[str] = None
    for attempt in range(1, BATCH_RETRIES + 1):
        try:
            filled, err = run_bulk_batch(
                session, token, batch, scenario_timeout_s=scenario_timeout_s,
            )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            filled = {}
        if not err:
            return filled, None, session, token
        last_err = err
        if attempt < BATCH_RETRIES and _is_transient_error(err):
            _log(f"  retry {attempt}/{BATCH_RETRIES - 1} after transient error: {err[:120]}")
            time.sleep(BATCH_RETRY_WAIT_S)
            try:
                session = nsa.make_http_session()
                token = nsa.get_access_token(session)
            except Exception:
                pass
            continue
        break
    return {}, last_err, session, token


def apply_fill(df: pd.DataFrame, idx: int, cols: Dict[str, str]) -> None:
    for csv_col, val in cols.items():
        if csv_col in df.columns:
            df.at[idx, csv_col] = val


def batch_size_for(sub_type: str) -> int:
    return LARGE_BATCH_SIZE if sub_type in LARGE_SAMPLE_SUB_TYPES else SMALL_BATCH_SIZE


def est_min_per_batch(sub_type: str) -> float:
    return EST_MIN_LARGE_BATCH if sub_type in LARGE_SAMPLE_SUB_TYPES else EST_MIN_SMALL_BATCH


def bulk_worker_count(workers_arg: Optional[int]) -> int:
    if workers_arg is not None:
        return max(1, min(int(workers_arg), 16))
    return max(1, min(DEFAULT_WORKERS, 16))


def estimate_runtime(groups: Dict[str, List[Dict[str, Any]]]) -> Tuple[int, int, float]:
    batches = 0
    securities = 0
    est_min = 0.0
    for sub_type, secs in groups.items():
        unique = len({str(s["cusip"]).strip().upper() for s in secs})
        securities += unique
        bs = batch_size_for(sub_type)
        n_batches = max(1, math.ceil(unique / bs))
        batches += n_batches
        est_min += n_batches * est_min_per_batch(sub_type)
    return batches, securities, est_min


def ordered_sub_types(groups: Dict[str, List[Dict[str, Any]]]) -> List[str]:
    out = [st for st in GROUP_ORDER if st in groups]
    for st in sorted(groups.keys()):
        if st not in out:
            out.append(st)
    return out


@dataclass(frozen=True)
class BatchJob:
    sub_type: str
    batch_num: int
    total_batches_st: int
    batch: List[Dict[str, Any]]
    group_secs: List[Dict[str, Any]]


def build_batch_jobs(groups: Dict[str, List[Dict[str, Any]]]) -> List[BatchJob]:
    jobs: List[BatchJob] = []
    for sub_type in ordered_sub_types(groups):
        secs = groups[sub_type]
        unique_secs = dedupe_for_api(secs)
        bs = batch_size_for(sub_type)
        total_batches_st = math.ceil(len(unique_secs) / bs) if unique_secs else 0
        for bi in range(0, len(unique_secs), bs):
            batch = unique_secs[bi : bi + bs]
            batch_num = bi // bs + 1
            jobs.append(
                BatchJob(
                    sub_type=sub_type,
                    batch_num=batch_num,
                    total_batches_st=total_batches_st,
                    batch=batch,
                    group_secs=secs,
                )
            )
    return jobs


def _execute_batch_job(
    job: BatchJob,
    *,
    scenario_timeout_s: float,
) -> Tuple[BatchJob, Dict[str, Dict[str, str]], Optional[str]]:
    session = nsa.make_http_session()
    token = nsa.get_access_token(session)
    filled, err, _, _ = run_bulk_batch_with_retries(
        session, token, job.batch, scenario_timeout_s=scenario_timeout_s,
    )
    return job, filled, err


def _apply_batch_result(
    df: pd.DataFrame,
    job: BatchJob,
    filled: Dict[str, Dict[str, str]],
) -> int:
    n = 0
    for sec in job.group_secs:
        cusip = str(sec["cusip"]).strip().upper()
        cols = filled.get(cusip)
        if not cols:
            continue
        idx = int(sec["_df_index"])
        clear_rest_columns(df, idx)
        apply_fill(df, idx, cols)
        n += 1
    return n


def write_failed_log(failed: List[Dict[str, Any]], path: Path) -> None:
    if failed:
        pd.DataFrame(failed).to_csv(path, index=False, encoding="utf-8-sig")
    elif path.is_file():
        pd.DataFrame(columns=["sub_type", "cusip", "batch", "error", "phase"]).to_csv(
            path, index=False, encoding="utf-8-sig"
        )


def collect_unfilled_from_batch(
    job: BatchJob,
    filled: Dict[str, Dict[str, str]],
    *,
    phase: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for sec in job.batch:
        cusip = str(sec["cusip"]).strip().upper()
        if cusip not in filled:
            rows.append({
                "sub_type": job.sub_type,
                "cusip": cusip,
                "batch": job.batch_num,
                "error": "no py/scenario result in successful batch",
                "phase": phase,
            })
    return rows


def run_jobs(
    df: pd.DataFrame,
    jobs: List[BatchJob],
    *,
    workers: int,
    scenario_timeout_s: float,
    output_path: Path,
    n_batches: int,
    phase: str,
) -> Tuple[int, int, int, List[Dict[str, Any]]]:
    t0 = time.monotonic()
    ok_batches = 0
    fail_batches = 0
    filled_cusips = 0
    failed: List[Dict[str, Any]] = []
    completed = 0
    checkpoint_lock = threading.Lock()

    def _record_success(job: BatchJob, filled: Dict[str, Dict[str, str]], elapsed: float) -> None:
        nonlocal ok_batches, filled_cusips, completed
        with checkpoint_lock:
            ok_batches += 1
            filled_cusips += len(filled)
            completed += 1
            _apply_batch_result(df, job, filled)
            missing = collect_unfilled_from_batch(job, filled, phase=phase)
            failed.extend(missing)
            if completed % CHECKPOINT_EVERY == 0 or completed == n_batches:
                df.to_csv(output_path, index=False, encoding="utf-8-sig")
            elapsed_total = time.monotonic() - t0
            pct = 100.0 * completed / n_batches if n_batches else 100.0
            eta_min = (
                (elapsed_total / completed) * (n_batches - completed) / 60.0
                if completed else 0.0
            )
            miss_note = f", missing={len(missing)}" if missing else ""
            _log(
                f"  OK [{job.sub_type}] batch {job.batch_num}/{job.total_batches_st} "
                f"({len(filled)} CUSIPs{miss_note}, {elapsed:.0f}s) | "
                f"progress {completed}/{n_batches} ({pct:.1f}%) | ETA ~{eta_min:.0f} min"
            )

    def _record_failure(job: BatchJob, err: Optional[str], elapsed: float) -> None:
        nonlocal fail_batches, completed
        with checkpoint_lock:
            fail_batches += 1
            completed += 1
            _log(
                f"  FAIL [{job.sub_type}] batch {job.batch_num}/{job.total_batches_st} "
                f"({elapsed:.0f}s): {err}"
            )
            for sec in job.batch:
                failed.append({
                    "sub_type": job.sub_type,
                    "cusip": sec["cusip"],
                    "batch": job.batch_num,
                    "error": err,
                    "phase": phase,
                })
            elapsed_total = time.monotonic() - t0
            pct = 100.0 * completed / n_batches if n_batches else 100.0
            eta_min = (
                (elapsed_total / completed) * (n_batches - completed) / 60.0
                if completed else 0.0
            )
            _log(f"  progress {completed}/{n_batches} ({pct:.1f}%) | ETA ~{eta_min:.0f} min")

    if workers == 1:
        session = nsa.make_http_session()
        token = nsa.get_access_token(session)
        for job in jobs:
            _log(
                f"[{phase}] [{job.sub_type}] batch {job.batch_num}/{job.total_batches_st} "
                f"(global {completed + 1}/{n_batches}, {len(job.batch)} CUSIPs)..."
            )
            t_batch = time.monotonic()
            filled, err, session, token = run_bulk_batch_with_retries(
                session, token, job.batch, scenario_timeout_s=scenario_timeout_s,
            )
            elapsed = time.monotonic() - t_batch
            if err:
                _record_failure(job, err, elapsed)
            else:
                _record_success(job, filled, elapsed)
    else:
        _log(f"[{phase}] Submitting {len(jobs)} batches to ThreadPoolExecutor(max_workers={workers})")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_execute_batch_job, job, scenario_timeout_s=scenario_timeout_s): job
                for job in jobs
            }
            for fut in as_completed(futures):
                job = futures[fut]
                t_batch = time.monotonic()
                try:
                    job, filled, err = fut.result()
                    elapsed = time.monotonic() - t_batch
                except Exception as e:
                    elapsed = time.monotonic() - t_batch
                    _record_failure(job, f"{type(e).__name__}: {e}", elapsed)
                    continue
                if err:
                    _record_failure(job, err, elapsed)
                else:
                    _record_success(job, filled, elapsed)

    return ok_batches, fail_batches, filled_cusips, failed


def filter_groups_to_cusips(
    groups: Dict[str, List[Dict[str, Any]]],
    cusips: set[str],
) -> Dict[str, List[Dict[str, Any]]]:
    filtered: Dict[str, List[Dict[str, Any]]] = {}
    for st, secs in groups.items():
        hit = [s for s in secs if str(s["cusip"]).strip().upper() in cusips]
        if hit:
            filtered[st] = hit
    return filtered


def dump_sample_payloads(groups: Dict[str, List[Dict[str, Any]]]) -> None:
    for st in ordered_sub_types(groups):
        sec = groups[st][0]
        py_row = build_py_input_row(sec)
        prepay = py_row.get("prepaySettings")
        vol = py_row.get("volatility")
        _log(
            f"  sample [{st}] {sec['cusip']}: curve={py_row.get('curve')} "
            f"prepay={prepay} vol={vol} settle={py_row.get('settlementDate')} "
            f"csv=({sec.get('csv_prepay_model')!r}, {sec.get('csv_prepay_rate')!r}, "
            f"{sec.get('csv_vol_model')!r}, RFRSwap)"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk-fill Portfolio 082426_test_sub10 via REST")
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true", help="Print plan only; no API calls")
    parser.add_argument("--scenario-timeout", type=float, default=1200.0)
    parser.add_argument("--skip-filled", action="store_true")
    parser.add_argument("--failed-file", default=FAILED_FILE)
    parser.add_argument("--final-retries", type=int, default=FINAL_RETRY_ROUNDS)
    parser.add_argument("--workers", type=int, default=None)
    args = parser.parse_args()

    base = Path(__file__).resolve().parent
    input_path = Path(args.input)
    output_path = Path(args.output)
    fail_path = Path(args.failed_file)
    if not input_path.is_absolute():
        input_path = base / input_path
    if not output_path.is_absolute():
        output_path = base / output_path
    if not fail_path.is_absolute():
        fail_path = base / fail_path

    try:
        open(LOG_FILE, "w", encoding="utf-8").close()
    except OSError:
        pass

    if args.skip_filled and output_path.is_file() and output_path.stat().st_size > 0:
        source_path = output_path
    else:
        source_path = input_path
    df = pd.read_csv(source_path, dtype=str, encoding="utf-8-sig")
    for _, csv_col in FIELD_MAP:
        if csv_col not in df.columns:
            df[csv_col] = ""

    securities = load_portfolio_securities(df)
    runnable = [
        s for s in securities
        if (s.get("sub_type") or "").strip() not in SKIP_SUB_TYPES and _has_market_price(s)
    ]
    apply_assumption_columns(df, runnable)
    groups = group_securities(securities)

    if args.skip_filled:
        filtered_groups: Dict[str, List[Dict[str, Any]]] = {}
        skipped = 0
        for st, secs in groups.items():
            pending = [s for s in secs if not row_is_filled(df, int(s["_df_index"]))]
            skipped += len(secs) - len(pending)
            if pending:
                filtered_groups[st] = pending
        groups = filtered_groups
        _log(f"Skip-filled: {skipped} rows already done, {sum(len(v) for v in groups.values())} pending")

    n_batches, n_secs, est_min = estimate_runtime(groups)
    workers = bulk_worker_count(args.workers)
    curve_dates = sorted({s.get("curve_date") or "" for s in runnable})

    _log(f"Input: {input_path}")
    _log(f"Output: {output_path}")
    _log(f"Failed log: {fail_path}")
    _log(f"Progress log: {LOG_FILE}")
    _log(f"Curve date from Report As Of Date: {', '.join(curve_dates)}")
    _log("Prepay: Agency MBS*/CMO = 2501/OldModel @ 100; Agency CMBS* = CPY @ 0")
    _log("Vol: Agency MBS/CMO/CMBS = LMMSOFRFLAT; MUNI = MatrixWSkew; Treasuries = blank")
    _log("Curve Type: RFRSwap")
    _log(f"Workers: {workers}")
    _log(f"Final failed-CUSIP retry rounds: {args.final_retries}")
    _log(f"Batch sizes: large={LARGE_BATCH_SIZE} (MUNI/Treasuries), small={SMALL_BATCH_SIZE} (all others)")
    _log(f"Sub types: {len(groups)}, unique CUSIPs: {n_secs}, total batches: {n_batches}")
    if workers > 1:
        _log(f"Estimated runtime: {est_min:.0f} min sequential (~{est_min / workers:.0f} min at {workers} workers)")
    else:
        _log(f"Estimated runtime: {est_min:.0f} min")
    for st in ordered_sub_types(groups):
        u = len({str(s["cusip"]).strip().upper() for s in groups[st]})
        bs = batch_size_for(st)
        b = max(1, math.ceil(u / bs))
        _log(f"  {st}: {len(groups[st])} rows, {u} unique, {b} batches @ {bs}")
    dump_sample_payloads(groups)

    if args.dry_run:
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
        _log(f"Dry-run wrote assumption columns to {output_path}")
        return 0

    all_groups = group_securities(load_portfolio_securities(df))
    jobs = build_batch_jobs(groups)
    t0 = time.monotonic()
    ok_batches, fail_batches, filled_cusips, failed = run_jobs(
        df,
        jobs,
        workers=workers,
        scenario_timeout_s=args.scenario_timeout,
        output_path=output_path,
        n_batches=len(jobs),
        phase="main",
    )
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    write_failed_log(failed, fail_path)
    _log(
        f"MAIN DONE in {(time.monotonic() - t0) / 60:.1f} min: "
        f"batches ok={ok_batches} fail={fail_batches}, CUSIPs filled={filled_cusips}, "
        f"failed CUSIPs={len(failed)}"
    )

    remaining_failed = list(failed)
    recovered_total = 0
    for round_i in range(1, max(0, args.final_retries) + 1):
        if not remaining_failed:
            _log("No failed CUSIPs left; skipping further final retries.")
            break
        retry_cusips = {
            str(r["cusip"]).strip().upper()
            for r in remaining_failed
            if str(r.get("cusip", "")).strip()
        }
        still_needed = {
            c for c in retry_cusips
            if any(
                str(s["cusip"]).strip().upper() == c
                and not row_is_filled(df, int(s["_df_index"]))
                for secs in all_groups.values()
                for s in secs
            )
        }
        if not still_needed:
            _log(f"[final-retry {round_i}] All previously failed CUSIPs now filled.")
            remaining_failed = []
            break
        _log(f"===== FINAL RETRY ROUND {round_i}/{args.final_retries}: {len(still_needed)} CUSIPs =====")
        time.sleep(FINAL_RETRY_WAIT_S)
        retry_groups = filter_groups_to_cusips(all_groups, still_needed)
        retry_jobs = build_batch_jobs(retry_groups)
        r_ok, r_fail, r_filled, r_failed = run_jobs(
            df,
            retry_jobs,
            workers=min(workers, 2),
            scenario_timeout_s=args.scenario_timeout,
            output_path=output_path,
            n_batches=len(retry_jobs),
            phase=f"final-retry-{round_i}",
        )
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
        recovered = len(still_needed) - len({str(x["cusip"]).strip().upper() for x in r_failed})
        recovered_total += max(0, recovered)
        remaining_failed = r_failed
        write_failed_log(remaining_failed, fail_path)
        _log(
            f"[final-retry {round_i}] ok_batches={r_ok} fail_batches={r_fail} "
            f"filled={r_filled} still_failed={len(remaining_failed)}"
        )

    total_elapsed = time.monotonic() - t0
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    write_failed_log(remaining_failed, fail_path)
    filled_rows = sum(1 for i in range(len(df)) if row_is_filled(df, i))
    _log(
        f"ALL DONE in {total_elapsed / 60:.1f} min: "
        f"main batches ok={ok_batches} fail={fail_batches}, "
        f"CUSIPs filled this run={filled_cusips}, recovered in final retries~={recovered_total}, "
        f"rows with Forward Yield={filled_rows}/{len(df)}, "
        f"final failed CUSIPs={len(remaining_failed)}"
    )
    _log(f"Wrote {output_path}")
    if remaining_failed:
        _log(f"Final failed log: {fail_path} ({len(remaining_failed)} rows)")
    else:
        _log("No remaining failed CUSIPs.")

    summary = {
        "input": str(input_path),
        "output": str(output_path),
        "curve_dates": curve_dates,
        "batches_ok": ok_batches,
        "batches_fail": fail_batches,
        "cusips_filled": filled_cusips,
        "final_retry_rounds": args.final_retries,
        "final_failed_cusips": len(remaining_failed),
        "rows_with_forward_yield": filled_rows,
        "elapsed_min": round(total_elapsed / 60, 1),
        "estimated_min": round(est_min, 1),
        "workers": workers,
    }
    (base / SUMMARY_FILE).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0 if not remaining_failed else 1


if __name__ == "__main__":
    sys.exit(main())
