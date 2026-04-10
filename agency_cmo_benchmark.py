"""
Benchmark Yield Book REST output against Agency_CMO.csv Excel add-in columns (W:BE).

Input: Agency_CMO.csv (row 1 = junk, row 2 = header).
Uses C=CUSIP, D=Sub Type, P=market price (PRICE (Mkt)), plus Curve Date, Prepay Model,
Prepay Rate, Vol Model, Curve Type, Coupon, Maturity Date — same as New_Securities_Analysis.

Outputs:
  - Agency_CMO_rest_output.csv   — API results (same shape as tba_analysis_results columns)
  - Agency_CMO_benchmark_compare.csv — per-field excel vs api vs delta

Optional: set AGENCY_CMO_LIMIT=N to process only the first N securities (smoke test).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import New_Securities_Analysis as nsa

INPUT_CSV = "Agency_CMO.csv"
OUT_REST = "Agency_CMO_rest_output.csv"
OUT_COMPARE = "Agency_CMO_benchmark_compare.csv"

# Excel columns W:BE (from header row); scenario shocks use duplicated names → .1 .2 .3
EXCEL_BENCHMARK_COLS: List[str] = [
    "Forward Yield",
    "Effective Duration",
    "Effective Convexity",
    "Effective DV01",
    "Dollar Duration",
    "1YR",
    "2YR",
    "3YR",
    "5YR",
    "10YR",
    "20YR",
    "30YR",
    "Average Life",
    "LT CPR",
    "OAS",
    "Z-Spread",
    "Factor",
    "Life CPR",
    "GWAC",
    "WALA",
    "WALS",
    "MaxServicerName",
    "MaxServicerPercent",
    "Eff. Dur",
    "DV01",
    "Dollar Return",
    "Eff. Dur.1",
    "DV01.1",
    "Dollar Return.1",
    "Eff. Dur.2",
    "DV01.2",
    "Dollar Return.2",
    "Eff. Dur.3",
    "DV01.3",
    "Dollar Return.3",
]

# API row keys (OUTPUT_COLUMNS order) aligned to EXCEL_BENCHMARK_COLS
API_BENCHMARK_KEYS: List[str] = [
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
    "OAS",
    "Z_Spread",
    "Factor",
    "Life_CPR",
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


def _parse_excel_cell(val: Any) -> Optional[float]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s.upper() in {"#SPILL!", "#N/A", "N/A", "NA", "-"}:
        return None
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_excel_text(val: Any) -> Optional[str]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s.upper() in {"#SPILL!", "#N/A"}:
        return None
    return s


def load_agency_cmo_securities(df: pd.DataFrame) -> List[Dict[str, Any]]:
    cols = nsa.col_lookup(df)
    cusip_col = nsa.find_col(cols, "CUSIP", "CUSIPs")
    sub_col = nsa.find_col(cols, "Sub Type", "Sub_Type", "Subtype")
    price_col = nsa.find_col(cols, "PRICE (Mkt)", "Market_price", "Market Price", "Market Price ")
    coupon_col = nsa.find_col(cols, "Coupon")
    maturity_col = nsa.find_col(cols, "Maturity Date", "Maturity_Date")
    curve_date_col = nsa.find_col(cols, "Curve Date", "Curve_Date")
    prepay_model_col = nsa.find_col(cols, "Prepay Model", "Prepay_Model")
    prepay_rate_col = nsa.find_col(cols, "Prepay Rate", "Prepay_Rate")
    vol_col = nsa.find_col(cols, "Vol Model", "Vol_Model")
    curve_type_col = nsa.find_col(cols, "Curve Type", "Curve_Type", "curve_trpe")
    nominal_col = nsa.find_col(cols, "Nominal", "CA_NOTIONAL", "Notional", "Current Balance")
    current_factor_col = nsa.find_col(cols, "Current Factor", "Current_Factor", "current_factor", "Factor_Input")

    if not cusip_col or not price_col:
        raise ValueError(f"Need CUSIP and market price columns. Found: {list(df.columns)}")

    out: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        cusip = str(r[cusip_col]).strip()
        if not cusip or cusip.lower() == "nan":
            continue
        sub_type = nsa.clean_text(r[sub_col]) if sub_col else None
        prepay_model = r[prepay_model_col] if prepay_model_col else None
        cf_val = nsa.parse_number(r[current_factor_col]) if current_factor_col else None
        if nsa.sub_type_is_treasury(sub_type):
            prepay_model = None
            prepay_rate_val = None
            vol_model_val = None
        else:
            prepay_rate_val = nsa.parse_number(r[prepay_rate_col]) if prepay_rate_col else 100.0
            vol_model_val = nsa.clean_text(r[vol_col]) if vol_col else None
        out.append({
            "cusip": cusip,
            "sub_type": sub_type,
            "coupon": nsa.parse_number(r[coupon_col]) if coupon_col else None,
            "maturity": nsa.normalize_date(r[maturity_col]) if maturity_col else None,
            "market_price": nsa.parse_number(r[price_col]) or r[price_col],
            "curve_date": nsa.normalize_date(r[curve_date_col]) if curve_date_col else nsa.default_pricing_date(),
            "prepay_model": prepay_model,
            "prepay_rate": prepay_rate_val,
            "vol_model": vol_model_val,
            "curve_type": nsa.clean_text(r[curve_type_col]) if curve_type_col else "SWAP_RFR",
            "nominal": nsa.parse_number(r[nominal_col]) if nominal_col else None,
            "current_factor": float(cf_val) if cf_val is not None else 1.0,
            "muni_curve_points": [],
        })
    return out


def fetch_benchmark_api_row(token: str, sec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fresh HTTP session per call (thread-safe for parallel workers)."""
    sess = nsa.make_http_session()
    try:
        return build_api_row(sess, token, sec)
    except Exception as e:
        cusip = sec.get("cusip", "?")
        print(f"[ERROR] {cusip}: {type(e).__name__}: {e}")
        return None


def build_api_row(session, token: str, sec: Dict[str, Any]) -> Dict[str, Any]:
    py = nsa.run_py(session, token, sec)
    py_kw = nsa.run_py_keyword_measures(session, token, sec)
    scen = nsa.run_scenarios(session, token, sec)
    indic = nsa.run_indic(session, token, sec)

    row: Dict[str, Any] = {
        "CUSIP": sec["cusip"],
        "Sub Type": sec.get("sub_type"),
        "Forward_Yield": nsa.resolve_forward_yield_column(py, sec.get("sub_type")),
        "Effective_Duration": py.get("effectiveDuration"),
        "Effective_Convexity": py.get("effectiveConvexity"),
        "Effective_DV01": py.get("effectiveDV01"),
        "Dollar_Duration": nsa.computed_dollar_duration(sec, py),
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
    row = nsa.merge_scenario_into_row(row, scen)
    return row


def extract_excel_benchmark_row(r: pd.Series) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    for col in EXCEL_BENCHMARK_COLS:
        if col not in r.index:
            d[col] = None
            continue
        v = r[col]
        # Text columns
        if col in {"MaxServicerName", "MaxServicerPercent"}:
            d[col] = _parse_excel_text(v)
        else:
            d[col] = _parse_excel_cell(v)
    return d


def compare_numeric(
    excel_val: Optional[float], api_val: Any, rtol: float = 1e-3, atol: float = 1e-2
) -> Tuple[Optional[float], Optional[str]]:
    if excel_val is None and (api_val is None or (isinstance(api_val, float) and pd.isna(api_val))):
        return None, "both_missing"
    if excel_val is None:
        return None, "excel_missing"
    try:
        av = float(api_val)
    except (TypeError, ValueError):
        return None, "api_missing"
    if pd.isna(av):
        return None, "api_missing"
    diff = av - excel_val
    if abs(excel_val) > 1e-9:
        rel = abs(diff / excel_val)
        if rel <= rtol or abs(diff) <= atol:
            return diff, "ok"
    else:
        if abs(diff) <= atol:
            return diff, "ok"
    return diff, "mismatch"


def main() -> None:
    path = os.path.join(os.path.dirname(__file__) or ".", INPUT_CSV)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    df = pd.read_csv(path, header=1)
    df = df.dropna(how="all")
    securities = load_agency_cmo_securities(df)
    limit_raw = (os.environ.get("AGENCY_CMO_LIMIT") or "").strip()
    if limit_raw:
        try:
            lim = max(1, int(limit_raw))
            securities = securities[:lim]
            print(f"[INFO] AGENCY_CMO_LIMIT={lim}: processing {len(securities)} security(ies).")
        except ValueError:
            print(f"[WARN] Invalid AGENCY_CMO_LIMIT={limit_raw!r}; processing all.")

    auth_session = nsa.make_http_session()
    token = nsa.get_access_token(auth_session)

    workers = nsa.parallel_worker_count()
    if workers > 1:
        print(f"[INFO] YB_WORKERS={workers} (parallel securities).", flush=True)

    rest_rows: List[Dict[str, Any]] = []
    compare_rows: List[Dict[str, Any]] = []

    # Index by CUSIP for Excel row lookup
    cusip_idx = {}
    for i, row in df.iterrows():
        c = str(row.get("CUSIP", "")).strip()
        if c:
            cusip_idx[c] = i

    nsec = len(securities)
    api_rows: List[Optional[Dict[str, Any]]] = []
    if workers > 1:

        def _work(isec: tuple[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            i, sec = isec
            cusip = sec["cusip"]
            print(f"[{i + 1}/{nsec}] {cusip} ...", flush=True)
            return fetch_benchmark_api_row(token, sec)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            api_rows = list(ex.map(_work, enumerate(securities)))
    else:
        for i, sec in enumerate(securities):
            cusip = sec["cusip"]
            print(f"[{i + 1}/{nsec}] {cusip} ...", flush=True)
            try:
                api_rows.append(build_api_row(auth_session, token, sec))
            except Exception as e:
                print(f"[ERROR] {cusip}: {type(e).__name__}: {e}")
                api_rows.append(None)

    for sec, api_row in zip(securities, api_rows):
        if api_row is None:
            continue
        cusip = sec["cusip"]
        rest_rows.append(api_row)

        idx = cusip_idx.get(cusip)
        if idx is None:
            continue
        excel_b = extract_excel_benchmark_row(df.loc[idx])

        for ek, ak in zip(EXCEL_BENCHMARK_COLS, API_BENCHMARK_KEYS):
            ev = excel_b.get(ek)
            av = api_row.get(ak)
            if ek in {"MaxServicerName", "MaxServicerPercent"}:
                es = excel_b.get(ek)
                as_ = api_row.get(ak)
                match = (es or "") == (as_ or "") if es is not None or as_ is not None else True
                compare_rows.append({
                    "CUSIP": cusip,
                    "field": ek,
                    "excel": es,
                    "api": as_,
                    "delta": None,
                    "status": "ok" if match else "mismatch",
                })
            else:
                diff, status = compare_numeric(ev, av)
                compare_rows.append({
                    "CUSIP": cusip,
                    "field": ek,
                    "excel": ev,
                    "api": av,
                    "delta": diff,
                    "status": status,
                })

    out_df = pd.DataFrame(rest_rows)
    out_df = out_df.reindex(columns=nsa.OUTPUT_COLUMNS)
    out_path = os.path.join(os.path.dirname(__file__) or ".", OUT_REST)
    out_df.to_csv(out_path, index=False, float_format="%.6f")
    print(f"Wrote {len(out_df)} rows to {out_path}")

    cmp_df = pd.DataFrame(compare_rows)
    cmp_path = os.path.join(os.path.dirname(__file__) or ".", OUT_COMPARE)
    cmp_df.to_csv(cmp_path, index=False, float_format="%.6f")
    print(f"Wrote {len(cmp_df)} comparisons to {cmp_path}")

    if len(cmp_df):
        mism = cmp_df[cmp_df["status"] == "mismatch"]
        print(f"Benchmark rows: {len(cmp_df)}; mismatches: {len(mism)}")


if __name__ == "__main__":
    main()
