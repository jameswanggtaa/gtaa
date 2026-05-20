"""
Shared Yield Book REST benchmark against Excel add-in columns (W:BE).

Used by us_treasury_benchmark, muni_benchmark, agency_cmbs_benchmark, agency_mbs30yr_benchmark.
See agency_cmo_benchmark.EXCEL_BENCHMARK_COLS / API_BENCHMARK_KEYS for field mapping.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

import New_Securities_Analysis as nsa

from agency_cmo_benchmark import (
    API_BENCHMARK_KEYS,
    EXCEL_BENCHMARK_COLS,
    build_api_row,
    compare_numeric,
    extract_excel_benchmark_row,
    servicer_benchmark_fields_match,
)


@dataclass(frozen=True)
class BenchmarkRunConfig:
    """input_file is relative to this package directory unless absolute."""

    input_file: str
    out_rest: str
    out_compare: str
    sep: str = "\t"
    pandas_header: int = 0
    limit_env: str = "PORTFOLIO_BENCHMARK_LIMIT"
    export_csv: Optional[str] = None
    # Optional: align API prepay with another book (e.g. Agency MBS 30yr uses Model2501 + 100).
    force_prepay_model: Optional[Any] = None
    force_prepay_rate: Optional[float] = None
    # Optional: process only the first N securities after load (before limit_env).
    securities_head: Optional[int] = None
    # Muni benchmarks: SWAP_RFR curve object only — no muni_ybsw pillars / curveSpots.
    # Sets YB_MUNI_INCLUDE_CURVE_POINTS=0; sync /bond/py uses YBPRICE RFRSwap-style body; Excel YBCURVE diverges.
    muni_rfr_swap_curve_only: bool = False


def load_portfolio_securities(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """CUSIP, Sub Type, market price, curve/prepay fields; _df_index for W:BE row alignment."""
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
    for idx, r in df.iterrows():
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
            "_df_index": idx,
            "cusip": cusip,
            "sub_type": sub_type,
            "coupon": nsa.parse_number(r[coupon_col]) if coupon_col else None,
            "maturity": nsa.normalize_date(r[maturity_col]) if maturity_col else None,
            # Do not fall back to raw cell: pandas NaN becomes str "nan" and breaks /bond/py level.
            "market_price": nsa.parse_number(r[price_col]),
            "book_price": nsa.parse_number(r[book_price_col]) if book_price_col else None,
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


def _sec_for_api(sec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in sec.items() if k != "_df_index"}


def fetch_benchmark_api_row(token: str, sec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Run build_api_row with a fresh HTTP session (thread-safe for parallel workers).
    """
    sess = nsa.make_http_session()
    try:
        return build_api_row(sess, token, _sec_for_api(sec))
    except Exception as e:
        cusip = sec.get("cusip", "?")
        print(f"[ERROR] {cusip}: {type(e).__name__}: {e}")
        return None


def run_benchmark(cfg: BenchmarkRunConfig) -> None:
    base = os.path.dirname(__file__) or "."
    path = cfg.input_file if os.path.isabs(cfg.input_file) else os.path.join(base, cfg.input_file)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    df = pd.read_csv(path, sep=cfg.sep, header=cfg.pandas_header, encoding="utf-8")
    df = df.dropna(how="all")

    if cfg.export_csv:
        csv_path = cfg.export_csv if os.path.isabs(cfg.export_csv) else os.path.join(base, cfg.export_csv)
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"[INFO] Wrote {csv_path} ({len(df)} rows).")

    securities = load_portfolio_securities(df)

    if cfg.muni_rfr_swap_curve_only:
        os.environ["YB_MUNI_INCLUDE_CURVE_POINTS"] = "0"
        print(
            "[INFO] muni_rfr_swap_curve_only: REST muni curve = curveType only (e.g. SWAP_RFR; "
            "no embedded pillars; sync /bond/py matches YBPRICE RFRSwap-style body; Excel YBCURVE diverges).",
            flush=True,
        )
        if (os.environ.get("YB_MUNI_JOB_STORE") or "").strip().lower() in {"1", "true", "yes", "y"}:
            print(
                "[WARN] YB_MUNI_JOB_STORE is set: pricing will use uploaded userDefined $ref "
                "if upload succeeds, not plain SWAP_RFR. Unset YB_MUNI_JOB_STORE for RFR-disc-only.",
                flush=True,
            )

    vol_single_from_curve = (os.environ.get("YB_MUNI_VOL_SINGLE_FROM_CURVE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    if cfg.muni_rfr_swap_curve_only and vol_single_from_curve:
        muni_for_vol = nsa.read_muni_curve_points(path)
        if muni_for_vol:
            for s in securities:
                if str(s.get("prepay_model", "")).strip().lower() == "muni":
                    s["muni_curve_points"] = list(muni_for_vol)
            print(
                f"[INFO] YB_MUNI_VOL_SINGLE_FROM_CURVE: loaded {len(muni_for_vol)} muni pillar(s) "
                "for volatility Single.rate only (REST curve stays SWAP_RFR-only).",
                flush=True,
            )
        else:
            print(
                "[WARN] YB_MUNI_VOL_SINGLE_FROM_CURVE set but no muni sidecar curve "
                "(muni_ybsw_curve.csv / muni_ybcurve.csv beside input); vol falls back to MatrixWSkew rules.",
                flush=True,
            )

    # YBSW(MUNI) tenor/rate columns: Excel B/E or optional muni_ybsw_curve.csv beside input file
    muni_pts: List[Dict[str, Any]] = []
    if not cfg.muni_rfr_swap_curve_only:
        muni_pts = nsa.read_muni_curve_points(path)
    if muni_pts:
        for s in securities:
            if str(s.get("prepay_model", "")).strip().lower() == "muni":
                s["muni_curve_points"] = muni_pts

    if cfg.force_prepay_model is not None:
        for s in securities:
            if not nsa.sub_type_is_treasury(s.get("sub_type")):
                s["prepay_model"] = cfg.force_prepay_model
    if cfg.force_prepay_rate is not None:
        for s in securities:
            if not nsa.sub_type_is_treasury(s.get("sub_type")):
                s["prepay_rate"] = float(cfg.force_prepay_rate)

    if cfg.securities_head is not None and cfg.securities_head > 0:
        securities = securities[: cfg.securities_head]
        print(f"[INFO] securities_head={cfg.securities_head}: using first {len(securities)} security row(s).")

    limit_raw = (os.environ.get(cfg.limit_env) or "").strip()
    if limit_raw:
        try:
            lim = max(1, int(limit_raw))
            securities = securities[:lim]
            print(f"[INFO] {cfg.limit_env}={lim}: processing {len(securities)} row(s).")
        except ValueError:
            print(f"[WARN] Invalid {cfg.limit_env}={limit_raw!r}; processing all.")

    auth_session = nsa.make_http_session()
    token = nsa.get_access_token(auth_session)
    nsa.prepare_muni_job_store_curve_ref(auth_session, token, securities)

    workers = nsa.parallel_worker_count()
    if workers > 1:
        print(f"[INFO] YB_WORKERS={workers} (parallel securities).", flush=True)

    rest_rows: List[Dict[str, Any]] = []
    compare_rows: List[Dict[str, Any]] = []

    nsec = len(securities)
    api_rows: List[Optional[Dict[str, Any]]] = []
    if workers > 1:

        def _work(isec: tuple[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            i, sec = isec
            cusip = sec["cusip"]
            df_idx = sec["_df_index"]
            print(f"[{i + 1}/{nsec}] {cusip} (row {df_idx}) ...", flush=True)
            return fetch_benchmark_api_row(token, sec)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            api_rows = list(ex.map(_work, enumerate(securities)))
    else:
        for i, sec in enumerate(securities):
            cusip = sec["cusip"]
            df_idx = sec["_df_index"]
            print(f"[{i + 1}/{nsec}] {cusip} (row {df_idx}) ...", flush=True)
            try:
                api_rows.append(build_api_row(auth_session, token, _sec_for_api(sec)))
            except Exception as e:
                print(f"[ERROR] {cusip}: {type(e).__name__}: {e}")
                api_rows.append(None)

    for sec, api_row in zip(securities, api_rows):
        if api_row is None:
            continue
        cusip = sec["cusip"]
        df_idx = sec["_df_index"]
        rest_rows.append(api_row)

        excel_b = extract_excel_benchmark_row(df.loc[df_idx])

        for ek, ak in zip(EXCEL_BENCHMARK_COLS, API_BENCHMARK_KEYS):
            ev = excel_b.get(ek)
            av = api_row.get(ak)
            if ek in {"MaxServicerName", "MaxServicerPercent"}:
                es = excel_b.get(ek)
                as_ = api_row.get(ak)
                match = servicer_benchmark_fields_match(es, as_)
                compare_rows.append({
                    "CUSIP": cusip,
                    "field": ek,
                    "excel": es,
                    "api": as_,
                    "delta": None,
                    "status": "ok" if match else "mismatch",
                })
            else:
                diff, status = compare_numeric(ev, av, field=ek)
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
    out_path = os.path.join(base, cfg.out_rest)
    out_df.to_csv(out_path, index=False, float_format="%.6f")
    print(f"Wrote {len(out_df)} rows to {out_path}")

    cmp_df = pd.DataFrame(compare_rows)
    cmp_alt = (os.environ.get("YB_BENCHMARK_COMPARE_OUT") or "").strip()
    if cmp_alt:
        cmp_path = cmp_alt if os.path.isabs(cmp_alt) else os.path.join(base, cmp_alt)
    else:
        cmp_path = os.path.join(base, cfg.out_compare)
    cmp_df.to_csv(cmp_path, index=False, float_format="%.6f")
    print(f"Wrote {len(cmp_df)} comparisons to {cmp_path}")

    if len(cmp_df):
        mism = cmp_df[cmp_df["status"] == "mismatch"]
        print(f"Benchmark rows: {len(cmp_df)}; mismatches: {len(mism)}")
