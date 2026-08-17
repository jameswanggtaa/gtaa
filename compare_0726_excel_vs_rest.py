#!/usr/bin/env python3
"""
Compare Excel add-in output (securities_0726_1.txt) vs REST (Portfolio_0726_output.csv).

Per Sub Type, classifies each security and each risk field as:
  - exact: within tight absolute/relative tolerance (rounding only)
  - close: within looser tolerance (timing / minor model drift OK)
  - mismatch: outside close tolerance
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from portfolio_0526_fill_common import FIELD_MAP, SKIP_SUB_TYPES

BASE = Path(__file__).resolve().parent
DEFAULT_EXCEL = BASE / "securities_0726_1.txt"
DEFAULT_REST = BASE / "Portfolio_0726_output.csv"
REPORT_JSON = BASE / "portfolio_0726_excel_vs_rest_report.json"
REPORT_CSV = BASE / "portfolio_0726_excel_vs_rest_by_subtype.csv"

MISSING = {"", "nan", "none", "na", "#spill!", "#spill", "n/a", "-", "—"}

# Risk columns to compare (from FIELD_MAP CSV side)
COMPARE_COLS = [csv_col for _, csv_col in FIELD_MAP]


def parse_val(v: Any) -> Optional[Any]:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    s = str(v).strip()
    if not s or s.lower() in MISSING:
        return None
    # Excel sometimes uses " - " or accounting blanks
    if s.replace(".", "").replace(",", "").replace("-", "").strip() == "":
        return None
    s = s.replace(",", "").replace(" ", "").replace("$", "")
    try:
        return float(s)
    except ValueError:
        return s


def is_zero_placeholder(v: Any) -> bool:
    n = parse_val(v)
    return n == 0.0


def tolerances(col: str) -> Tuple[float, float, float, float]:
    """
    Return (exact_abs, exact_rel, close_abs, close_rel).
    Match if abs(a-b) <= max(abs_tol, rel_tol * max(|a|,|b|,1)).
    """
    if col in {"Forward Yield", "Prospective Yield"}:
        return 0.005, 0.001, 0.05, 0.01
    if col in {"OAS", "Z-Spread"}:
        return 0.5, 0.005, 5.0, 0.05
    if col == "Average Life":
        return 0.02, 0.005, 0.25, 0.08
    if col in {"LT CPR", "Life CPR"}:
        return 0.1, 0.005, 1.0, 0.05
    if col in {"Factor"}:
        return 1e-5, 0.001, 1e-3, 0.02
    if col in {"GWAC", "WALA", "WALS", "MaxServicerPercent"}:
        return 0.01, 0.001, 0.5, 0.05
    if "Dollar Return" in col or col == "Dollar Duration":
        return 0.1, 0.005, 2.0, 0.05
    if col in {"DV01", "Effective DV01"} or col.startswith("DV01"):
        return 0.0005, 0.005, 0.01, 0.05
    if col in {"Effective Duration", "Effective Convexity"} or col.startswith("Eff. Dur"):
        return 0.01, 0.005, 0.15, 0.05
    if col.endswith("YR"):  # partial durations
        return 0.01, 0.005, 0.15, 0.05
    return 0.01, 0.005, 0.05, 0.02


def classify_values(a: Any, b: Any, col: str) -> str:
    """Return 'exact' | 'close' | 'mismatch' | 'both_blank'."""
    pa, pb = parse_val(a), parse_val(b)

    # Indic placeholders: Excel 0.00 vs REST blank often means N/A
    indic_cols = {
        "Factor", "Life CPR", "GWAC", "WALA", "WALS",
        "MaxServicerName", "MaxServicerPercent", "LT CPR",
    }
    if col in indic_cols:
        if is_zero_placeholder(a) and pb is None:
            return "exact"
        if is_zero_placeholder(b) and pa is None:
            return "exact"

    if pa is None and pb is None:
        return "both_blank"
    if pa is None or pb is None:
        return "mismatch"

    if isinstance(pa, str) or isinstance(pb, str):
        if str(pa).strip().upper() == str(pb).strip().upper():
            return "exact"
        return "mismatch"

    scale = max(abs(pa), abs(pb), 1.0)
    diff = abs(pa - pb)
    ea, er, ca, cr = tolerances(col)
    if diff <= max(ea, er * scale):
        return "exact"
    if diff <= max(ca, cr * scale):
        return "close"
    return "mismatch"


def normalize_shock_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Map duplicate Excel shock headers to Eff. Dur / Eff. Dur.1 ... style."""
    cols = list(df.columns)
    # If already pandas-style (.1, .2), nothing to do
    if "Eff. Dur.1" in cols:
        return df

    # Tab file may have duplicate names; pandas suffixes as .1, .2 automatically
    # when reading with header=2. Handle Unnamed / exact duplicates.
    rename: Dict[str, str] = {}
    shock_names = ["Eff. Dur", "DV01", "Dollar Return"]
    counts = {n: 0 for n in shock_names}
    new_cols: List[str] = []
    for c in cols:
        base = str(c).strip()
        if base in shock_names:
            idx = counts[base]
            counts[base] += 1
            new_cols.append(base if idx == 0 else f"{base}.{idx}")
        else:
            new_cols.append(base)
    df = df.copy()
    df.columns = new_cols
    return df


def load_excel(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, sep="\t", header=2, dtype=str, encoding="utf-8-sig")
    df = df.dropna(how="all")
    df = normalize_shock_columns(df)
    df["CUSIP"] = df["CUSIP"].astype(str).str.strip().str.upper()
    df["Sub Type"] = df["Sub Type"].astype(str).str.strip()
    if "Position ID" in df.columns:
        df["Position ID"] = df["Position ID"].astype(str).str.strip()
    return df


def load_rest(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    df = df.dropna(how="all")
    df["CUSIP"] = df["CUSIP"].astype(str).str.strip().str.upper()
    df["Sub Type"] = df["Sub Type"].astype(str).str.strip()
    if "Position ID" in df.columns:
        df["Position ID"] = df["Position ID"].astype(str).str.strip()
    return df


def rest_row_for_key(rest_df: pd.DataFrame, cusip: str, pos_id: Optional[str]) -> Optional[pd.Series]:
    hits = rest_df[rest_df["CUSIP"] == cusip]
    if hits.empty:
        return None
    if pos_id and "Position ID" in hits.columns:
        by_pos = hits[hits["Position ID"] == pos_id]
        if not by_pos.empty:
            hits = by_pos
    filled = hits[
        hits["Forward Yield"].astype(str).str.strip().replace({"nan": ""}) != ""
    ]
    if not filled.empty:
        return filled.iloc[0]
    return hits.iloc[0]


def mismatch_reason(field: str, excel_v: Any, rest_v: Any) -> str:
    pe, pr = parse_val(excel_v), parse_val(rest_v)
    if pe is None and pr is not None:
        return "Excel blank / REST filled"
    if pr is None and pe is not None:
        return "REST blank / Excel filled"
    if field in {"MaxServicerName", "MaxServicerPercent"}:
        return "Servicer / indic mapping (#SPILL or blank vs REST)"
    if field in {"LT CPR", "Life CPR", "Factor", "GWAC", "WALA", "WALS"}:
        return "Indic/prepay field (Excel placeholder 0 vs REST, or model CPR)"
    if field in {"Forward Yield", "Prospective Yield", "OAS", "Z-Spread", "Average Life"}:
        return "Core risk metric delta (curve timing / model / settlement)"
    if field.startswith("Eff. Dur") or field.startswith("DV01") or "Dollar Return" in field:
        return "Scenario shock delta"
    if field.endswith("YR") or field in {"Effective Duration", "Effective Convexity", "Effective DV01", "Dollar Duration"}:
        return "Duration / partials delta (model or curve timing)"
    return "Other field delta"


def sec_tier(field_tiers: List[str]) -> str:
    """Aggregate field classifications to security tier (ignore both_blank)."""
    active = [t for t in field_tiers if t != "both_blank"]
    if not active:
        return "exact"
    if any(t == "mismatch" for t in active):
        return "mismatch"
    if any(t == "close" for t in active):
        return "close"
    return "exact"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Excel vs REST Portfolio 0726")
    parser.add_argument("--excel", default=str(DEFAULT_EXCEL))
    parser.add_argument("--rest", default=str(DEFAULT_REST))
    parser.add_argument("--report", default=str(REPORT_JSON))
    args = parser.parse_args()

    excel_path = Path(args.excel)
    rest_path = Path(args.rest)
    report_path = Path(args.report)

    excel_df = load_excel(excel_path)
    rest_df = load_rest(rest_path)

    # Only compare overlapping risk columns present in both
    compare_cols = [c for c in COMPARE_COLS if c in excel_df.columns and c in rest_df.columns]

    by_subtype: Dict[str, Dict[str, Any]] = {}
    mismatches: List[Dict[str, Any]] = []
    close_examples: List[Dict[str, Any]] = []
    reason_counts: Counter = Counter()
    field_mismatch_counts: Counter = Counter()

    # Dedupe excel by Position ID + CUSIP when possible
    excel_df = excel_df.drop_duplicates(subset=["CUSIP", "Position ID"] if "Position ID" in excel_df.columns else ["CUSIP"])

    for st in sorted(excel_df["Sub Type"].dropna().unique(), key=lambda x: str(x).upper()):
        if st in SKIP_SUB_TYPES or st in {"nan", "None"}:
            continue
        rows = excel_df[excel_df["Sub Type"] == st]
        sec_exact = sec_close = sec_mis = 0
        fld_exact = fld_close = fld_mis = fld_blank = 0
        compared = 0
        missing_rest = 0

        for _, s in rows.iterrows():
            cusip = str(s["CUSIP"]).strip().upper()
            pos = str(s.get("Position ID", "")).strip() if "Position ID" in s.index else ""
            r = rest_row_for_key(rest_df, cusip, pos or None)
            if r is None:
                missing_rest += 1
                continue
            # Skip if REST not filled
            if not str(r.get("Forward Yield", "")).strip() or str(r.get("Forward Yield", "")).lower() == "nan":
                missing_rest += 1
                continue

            compared += 1
            tiers: List[str] = []
            for col in compare_cols:
                sv, rv = s.get(col, ""), r.get(col, "")
                tier = classify_values(sv, rv, col)
                tiers.append(tier)
                if tier == "exact":
                    fld_exact += 1
                elif tier == "close":
                    fld_close += 1
                    if len(close_examples) < 40:
                        close_examples.append({
                            "sub_type": st, "cusip": cusip, "field": col,
                            "excel": str(sv).strip(), "rest": str(rv).strip(),
                        })
                elif tier == "mismatch":
                    fld_mis += 1
                    field_mismatch_counts[col] += 1
                    reason = mismatch_reason(col, sv, rv)
                    reason_counts[reason] += 1
                    mismatches.append({
                        "sub_type": st, "cusip": cusip, "field": col,
                        "excel": str(sv).strip(), "rest": str(rv).strip(),
                        "reason": reason,
                    })
                else:
                    fld_blank += 1

            tier = sec_tier(tiers)
            if tier == "exact":
                sec_exact += 1
            elif tier == "close":
                sec_close += 1
            else:
                sec_mis += 1

        by_subtype[st] = {
            "sub_type": st,
            "excel_rows": int(len(rows)),
            "compared": compared,
            "missing_in_rest": missing_rest,
            "sec_exact": sec_exact,
            "sec_close": sec_close,
            "sec_mismatch": sec_mis,
            "sec_exact_pct": round(100 * sec_exact / compared, 1) if compared else 0.0,
            "sec_close_pct": round(100 * sec_close / compared, 1) if compared else 0.0,
            "sec_mismatch_pct": round(100 * sec_mis / compared, 1) if compared else 0.0,
            "fld_exact": fld_exact,
            "fld_close": fld_close,
            "fld_mismatch": fld_mis,
            "fld_both_blank": fld_blank,
            "fld_checks_active": fld_exact + fld_close + fld_mis,
        }

    summary = list(by_subtype.values())
    totals = {
        "compared": sum(r["compared"] for r in summary),
        "sec_exact": sum(r["sec_exact"] for r in summary),
        "sec_close": sum(r["sec_close"] for r in summary),
        "sec_mismatch": sum(r["sec_mismatch"] for r in summary),
        "fld_exact": sum(r["fld_exact"] for r in summary),
        "fld_close": sum(r["fld_close"] for r in summary),
        "fld_mismatch": sum(r["fld_mismatch"] for r in summary),
        "missing_in_rest": sum(r["missing_in_rest"] for r in summary),
    }
    n = totals["compared"] or 1
    totals["sec_exact_pct"] = round(100 * totals["sec_exact"] / n, 1)
    totals["sec_close_pct"] = round(100 * totals["sec_close"] / n, 1)
    totals["sec_mismatch_pct"] = round(100 * totals["sec_mismatch"] / n, 1)
    fa = totals["fld_exact"] + totals["fld_close"] + totals["fld_mismatch"] or 1
    totals["fld_exact_pct"] = round(100 * totals["fld_exact"] / fa, 1)
    totals["fld_close_pct"] = round(100 * totals["fld_close"] / fa, 1)
    totals["fld_mismatch_pct"] = round(100 * totals["fld_mismatch"] / fa, 1)

    # Top mismatch fields and sample mismatches per subtype
    top_fields = field_mismatch_counts.most_common(15)
    reasons_by_st: Dict[str, Dict[str, int]] = defaultdict(Counter)
    for m in mismatches:
        reasons_by_st[m["sub_type"]][m["reason"]] += 1

    # Curve date / prepay model peek
    excel_curve = Counter(excel_df["Curve Date"].dropna().astype(str).str.strip()) if "Curve Date" in excel_df.columns else {}
    rest_curve = Counter(rest_df["Curve Date"].dropna().astype(str).str.strip()) if "Curve Date" in rest_df.columns else {}
    excel_prepay = Counter(excel_df["Prepay Model"].dropna().astype(str).str.strip()) if "Prepay Model" in excel_df.columns else {}

    report = {
        "excel_file": str(excel_path),
        "rest_file": str(rest_path),
        "compare_fields": compare_cols,
        "definition": {
            "exact": "within tight tol (rounding)",
            "close": "within looser tol (timing/minor model drift)",
            "mismatch": "outside close tol",
            "security_tier": "exact if all fields exact/blank; close if any close and no mismatch; else mismatch",
        },
        "excel_curve_dates": dict(excel_curve),
        "rest_curve_dates": dict(rest_curve),
        "excel_prepay_models": dict(excel_prepay.most_common(10)),
        "summary_by_sub_type": summary,
        "totals": totals,
        "mismatch_reasons": dict(reason_counts.most_common()),
        "mismatch_reasons_by_sub_type": {k: dict(v.most_common()) for k, v in reasons_by_st.items()},
        "top_mismatch_fields": top_fields,
        "close_examples": close_examples[:25],
        "mismatch_examples": mismatches[:80],
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    pd.DataFrame(summary).to_csv(REPORT_CSV, index=False, encoding="utf-8-sig")

    print(f"Excel: {excel_path.name}")
    print(f"REST:  {rest_path.name}")
    print(f"Fields compared: {len(compare_cols)}")
    print(
        f"Securities: exact={totals['sec_exact']} ({totals['sec_exact_pct']}%)  "
        f"close={totals['sec_close']} ({totals['sec_close_pct']}%)  "
        f"mismatch={totals['sec_mismatch']} ({totals['sec_mismatch_pct']}%)  "
        f"compared={totals['compared']}  missing_rest={totals['missing_in_rest']}"
    )
    print(
        f"Fields:     exact={totals['fld_exact']} ({totals['fld_exact_pct']}%)  "
        f"close={totals['fld_close']} ({totals['fld_close_pct']}%)  "
        f"mismatch={totals['fld_mismatch']} ({totals['fld_mismatch_pct']}%)"
    )
    print()
    print(
        f"{'Sub Type':<26} {'N':>5} {'Exact':>6} {'Close':>6} {'Mis':>5} "
        f"{'Ex%':>6} {'Cl%':>6} {'Mis%':>6}"
    )
    print("-" * 80)
    for r in summary:
        print(
            f"{r['sub_type']:<26} {r['compared']:>5} "
            f"{r['sec_exact']:>6} {r['sec_close']:>6} {r['sec_mismatch']:>5} "
            f"{r['sec_exact_pct']:>5.1f}% {r['sec_close_pct']:>5.1f}% {r['sec_mismatch_pct']:>5.1f}%"
        )
    print("-" * 80)
    print(
        f"{'TOTAL':<26} {totals['compared']:>5} "
        f"{totals['sec_exact']:>6} {totals['sec_close']:>6} {totals['sec_mismatch']:>5} "
        f"{totals['sec_exact_pct']:>5.1f}% {totals['sec_close_pct']:>5.1f}% {totals['sec_mismatch_pct']:>5.1f}%"
    )
    print("\nMismatch reason buckets (field-level):")
    for reason, cnt in reason_counts.most_common():
        print(f"  {cnt:6d}  {reason}")
    print("\nTop mismatch fields:")
    for fld, cnt in top_fields:
        print(f"  {cnt:6d}  {fld}")
    print(f"\nWrote {report_path.name} and {REPORT_CSV.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
