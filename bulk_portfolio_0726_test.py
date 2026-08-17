#!/usr/bin/env python3
"""
Bulk REST smoke test for Portfolio 0726: 1 CUSIP per Sub Type.

Uses same builders as 0626 (Model 2501 / OldModel for Agency MBS & CMO).
Curve date default: 2026-07-31.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

import New_Securities_Analysis as nsa
from bulk_portfolio_0526_test import (
    build_api_row_from_bulk,
    load_group_securities,
    run_bulk_group,
)
from bulk_portfolio_0626_test import (
    GROUP_ORDER,
    LARGE_SAMPLE_SUB_TYPES,
    MBS_2501_SUB_TYPES,
    build_bulk_groups,
    patch_bulk_builders,
)
from portfolio_0526_fill_common import SKIP_SUB_TYPES

OUT_DIR = Path(__file__).resolve().parent / "bulk_test_results_0726"
DEFAULT_CSV = "Portfolio_0726_input.csv"
DEFAULT_CURVE_DATE = "2026-07-31"


def prepare_input_from_fi(fi_path: Path, out_path: Path) -> None:
    df = pd.read_csv(fi_path, dtype=str, encoding="utf-8-sig", header=2)
    df = df.dropna(how="all")
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"[INFO] Wrote {out_path.name}: {len(df)} rows", flush=True)


def print_picks(csv_path: Path, bulk_groups: Dict[str, Dict[str, Any]]) -> None:
    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig")
    print("\n=== Sample picks (1 per Sub Type) ===", flush=True)
    for st, cfg in bulk_groups.items():
        cusip = cfg["cusips"][0]
        row = df[df["CUSIP"].astype(str).str.strip().str.upper() == cusip].iloc[0]
        print(
            f"  {st}: {cusip}  px={row.get('PRICE (Mkt)')}  "
            f"issuer={str(row.get('Issuer', ''))[:40]}  "
            f"prepay={cfg.get('force_prepay_type')}",
            flush=True,
        )


def summarize_api_rows(
    session,
    token: str,
    group_name: str,
    securities: List[Dict[str, Any]],
) -> None:
    slug = group_name.replace(" ", "_").lower()
    py_path = OUT_DIR / f"{slug}_py.json"
    indic_path = OUT_DIR / f"{slug}_indic.json"
    scen_path = OUT_DIR / f"{slug}_scenario.json"
    if not py_path.exists() or not scen_path.exists():
        print(f"[WARN] {group_name}: missing result files", flush=True)
        return

    py_items = json.loads(py_path.read_text(encoding="utf-8")).get("results") or []
    indic_items = json.loads(indic_path.read_text(encoding="utf-8")).get("results") or []
    scen_items = json.loads(scen_path.read_text(encoding="utf-8")).get("results") or []

    from bulk_portfolio_0526_test import _cusip_from_result

    py_by = {_cusip_from_result(x): x for x in py_items if _cusip_from_result(x)}
    indic_by = {_cusip_from_result(x): x for x in indic_items if _cusip_from_result(x)}
    scen_by = {_cusip_from_result(x): x for x in scen_items if _cusip_from_result(x)}

    print(f"\n=== {group_name} ===", flush=True)
    for sec in securities:
        cusip = str(sec["cusip"]).strip().upper()
        row = build_api_row_from_bulk(
            session,
            token,
            sec,
            py_by.get(cusip, {}),
            indic_by.get(cusip),
            scen_by.get(cusip),
        )
        print(
            f"  {cusip}: FwdYld={row.get('Forward_Yield')} EffDur={row.get('Effective_Duration')} "
            f"OAS={row.get('OAS')} AvgLife={row.get('Average_Life')} "
            f"Prospective={row.get('Prospective_Yield', 'n/a')}",
            flush=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk YB smoke test for Portfolio 0726")
    parser.add_argument("--csv", default=DEFAULT_CSV)
    parser.add_argument("--fi", default="FI_position_0726.csv", help="Source FI file if input missing")
    parser.add_argument("--run", action="store_true", help="Call REST API (default: dry-run)")
    parser.add_argument("--scenario-timeout", type=float, default=900.0)
    parser.add_argument("--sample", type=int, default=1, help="CUSIPs per Sub Type")
    parser.add_argument("--group", action="append", help="Run only these Sub Type(s)")
    args = parser.parse_args()

    os.environ.setdefault("PORTFOLIO_DEFAULT_CURVE_DATE", DEFAULT_CURVE_DATE)
    base = Path(__file__).resolve().parent
    csv_path = Path(args.csv)
    if not csv_path.is_absolute():
        csv_path = base / csv_path

    if not csv_path.is_file():
        fi = Path(args.fi)
        if not fi.is_absolute():
            fi = base / fi
        prepare_input_from_fi(fi, csv_path)

    # Force sample=1 for both large and small groups
    bulk_groups = build_bulk_groups(csv_path, args.sample, args.sample)
    if args.group:
        want = set(args.group)
        bulk_groups = {k: v for k, v in bulk_groups.items() if k in want}

    print_picks(csv_path, bulk_groups)
    # silence unused import warning for SKIP/GROUP if linted
    _ = (SKIP_SUB_TYPES, LARGE_SAMPLE_SUB_TYPES, GROUP_ORDER)

    patch_bulk_builders()
    import bulk_portfolio_0526_test as bt

    old_out = bt.OUT_DIR
    bt.OUT_DIR = OUT_DIR
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    session = None
    token = ""
    if args.run:
        session = nsa.make_http_session()
        token = nsa.get_access_token(session)

    all_summaries: List[Dict[str, Any]] = []
    try:
        for group_name, cfg in bulk_groups.items():
            securities = load_group_securities(csv_path, group_name, cfg)
            print(
                f"[INFO] {group_name}: {len(securities)} CUSIP(s) "
                f"(prepay={cfg.get('force_prepay_type')})",
                flush=True,
            )
            if args.run:
                summary = run_bulk_group(
                    session,
                    token,
                    group_name,
                    securities,
                    dry_run=False,
                    scenario_timeout_s=args.scenario_timeout,
                )
                summarize_api_rows(session, token, group_name, securities)
            else:
                summary = run_bulk_group(
                    None, "", group_name, securities, dry_run=True, scenario_timeout_s=0,
                )
                print(
                    f"  dry-run: py={summary['py_input_rows']} indic={summary['indic_input_rows']} "
                    f"scenario={summary['scenario_input_rows']} prepay={summary.get('sample_prepay')}",
                    flush=True,
                )
            all_summaries.append(summary)
    finally:
        bt.OUT_DIR = old_out

    (OUT_DIR / "bulk_run_summary.json").write_text(
        json.dumps(all_summaries, indent=2), encoding="utf-8",
    )

    if args.run:
        print(f"\n[DONE] Wrote results to {OUT_DIR}", flush=True)
    else:
        print("\n[DRY-RUN] No API calls made. Re-run with --run to execute.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
