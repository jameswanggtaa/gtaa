#!/usr/bin/env python3
"""
Bulk-fill Agency MBS* + Agency CMO for Portfolio 0726 with prepay Model (2601).

Starts from Portfolio_0726_output.csv (2501 run), clears REST columns only for
Agency MBS* and Agency CMO, writes Portfolio_0726_model2601.csv.
All other sub types keep existing values from the 2501 run.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

import New_Securities_Analysis as nsa
import fill_portfolio_0726_bulk as fp
from bulk_portfolio_0626_test import MBS_2501_SUB_TYPES, patch_bulk_builders
from fill_portfolio_0726_bulk import (
    BATCH_RETRIES,
    BATCH_RETRY_WAIT_S,
    CHECKPOINT_EVERY,
    DEFAULT_CURVE_DATE,
    EST_MIN_SMALL_BATCH,
    FIELD_MAP,
    FINAL_RETRY_ROUNDS,
    FINAL_RETRY_WAIT_S,
    SKIP_SUB_TYPES,
    BatchJob,
    _apply_batch_result,
    _execute_batch_job,
    _has_market_price,
    _log_lock,
    build_batch_jobs,
    bulk_worker_count,
    clear_rest_columns,
    estimate_runtime,
    filter_groups_to_cusips,
    ordered_sub_types,
    row_is_filled,
    run_bulk_batch_with_retries,
    run_jobs,
    write_failed_log,
)
from portfolio_benchmark import load_portfolio_securities

patch_bulk_builders()

DEFAULT_BASE = "Portfolio_0726_output.csv"
DEFAULT_OUTPUT = "Portfolio_0726_model2601.csv"
LOG_FILE = "Portfolio_0726_model2601_bulk_fill.log"
FAILED_FILE = "Portfolio_0726_model2601_bulk_failed.csv"
SUMMARY_FILE = "Portfolio_0726_model2601_bulk_summary.json"

MODEL_2601_SUB_TYPES = frozenset(MBS_2501_SUB_TYPES) | frozenset({"Agency CMO"})
PREPAY_TYPE_2601 = "Model"  # current model (~2601)

DEFAULT_SMALL_BATCH_SIZE = int(os.environ.get("BULK_SMALL_BATCH_SIZE", "4"))
DEFAULT_WORKERS = int(os.environ.get("BULK_WORKERS", os.environ.get("YB_WORKERS", "4")))

GROUP_ORDER = [
    "Agency CMO",
    "Agency MBS 30yr",
    "Agency MBS 15yr",
    "Agency MBS ARM",
    "Agency MBS Other",
    "Agency MBS 20yr",
    "Agency MBS 10yr",
]


def batch_size_for(_sub_type: str) -> int:
    return DEFAULT_SMALL_BATCH_SIZE


def est_min_per_batch(_sub_type: str) -> float:
    return EST_MIN_SMALL_BATCH


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def group_securities(securities: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for sec in securities:
        st = (sec.get("sub_type") or "").strip()
        if st in SKIP_SUB_TYPES or st not in MODEL_2601_SUB_TYPES:
            continue
        if not _has_market_price(sec):
            continue
        sec = dict(sec)
        sec["force_prepay_type"] = PREPAY_TYPE_2601
        groups.setdefault(st, []).append(sec)
    for st in groups:
        groups[st].sort(key=lambda s: (str(s["cusip"]), int(s["_df_index"])))
    return groups


def init_output_from_base(
    df: pd.DataFrame,
    securities: List[Dict[str, Any]],
    *,
    clear_targets: bool,
) -> int:
    """Copy base portfolio; optionally clear REST columns for MBS+CMO rows."""
    cleared = 0
    if not clear_targets:
        return cleared
    for sec in securities:
        st = (sec.get("sub_type") or "").strip()
        if st not in MODEL_2601_SUB_TYPES or not _has_market_price(sec):
            continue
        idx = int(sec["_df_index"])
        clear_rest_columns(df, idx)
        cleared += 1
    return cleared


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bulk-fill Portfolio 0726 Agency MBS+CMO with prepay Model (2601)",
    )
    parser.add_argument("--base", default=DEFAULT_BASE, help="Full portfolio CSV to start from (2501 output)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true", help="Print plan and time estimate only")
    parser.add_argument("--scenario-timeout", type=float, default=1200.0)
    parser.add_argument(
        "--skip-filled",
        action="store_true",
        help="Load --output if present and skip rows that already have Forward Yield",
    )
    parser.add_argument("--failed-file", default=FAILED_FILE)
    parser.add_argument("--batch-size", type=int, default=None, help="CUSIPs per batch (default 4)")
    parser.add_argument("--workers", type=int, default=None, help="Parallel workers (default 4)")
    parser.add_argument(
        "--final-retries",
        type=int,
        default=FINAL_RETRY_ROUNDS,
        help="Retry rounds for failed CUSIPs after main run (default 3)",
    )
    args = parser.parse_args()

    global DEFAULT_SMALL_BATCH_SIZE
    if args.batch_size is not None:
        DEFAULT_SMALL_BATCH_SIZE = max(1, int(args.batch_size))
    workers = bulk_worker_count(args.workers if args.workers is not None else DEFAULT_WORKERS)

    fp.SMALL_BATCH_SIZE = DEFAULT_SMALL_BATCH_SIZE
    fp.batch_size_for = batch_size_for
    fp.est_min_per_batch = est_min_per_batch
    fp.LOG_FILE = LOG_FILE
    fp._log = _log

    os.environ.setdefault("PORTFOLIO_DEFAULT_CURVE_DATE", DEFAULT_CURVE_DATE)
    base_dir = Path(__file__).resolve().parent
    base_path = Path(args.base)
    output_path = Path(args.output)
    fail_path = Path(args.failed_file)
    if not base_path.is_absolute():
        base_path = base_dir / base_path
    if not output_path.is_absolute():
        output_path = base_dir / output_path
    if not fail_path.is_absolute():
        fail_path = base_dir / fail_path

    try:
        open(LOG_FILE, "w", encoding="utf-8").close()
    except OSError:
        pass

    if not base_path.is_file():
        _log(f"[ERROR] Base file not found: {base_path}")
        return 1

    if args.dry_run:
        df = pd.read_csv(base_path, dtype=str, encoding="utf-8-sig")
        for _, csv_col in FIELD_MAP:
            if csv_col not in df.columns:
                df[csv_col] = ""
        securities = load_portfolio_securities(df)
        groups = group_securities(securities)
        n_batches, n_secs, est_min = estimate_runtime(groups)
        _log(f"Base: {base_path}")
        _log(f"Output: {output_path}")
        _log(f"Curve date: {DEFAULT_CURVE_DATE}")
        _log(f"Prepay: {PREPAY_TYPE_2601} (model 2601) for Agency MBS* + Agency CMO")
        _log(f"Other Sub Types: copied from base, untouched")
        _log(f"Workers: {workers}")
        _log(f"Batch size: {DEFAULT_SMALL_BATCH_SIZE}")
        _log(f"Sub types: {len(groups)}, unique CUSIPs: {n_secs}, total batches: {n_batches}")
        if workers > 1:
            _log(f"Estimated runtime: {est_min:.0f} min sequential (~{est_min / workers:.0f} min at {workers} workers)")
        else:
            _log(f"Estimated runtime: {est_min:.0f} min ({est_min / 60:.1f} hours)")
        for st in GROUP_ORDER:
            if st not in groups:
                continue
            u = len({str(s["cusip"]).strip().upper() for s in groups[st]})
            bs = batch_size_for(st)
            b = max(1, math.ceil(u / bs))
            _log(f"  {st}: {len(groups[st])} rows, {u} unique, {b} batches @ {bs}")
        _log("Dry-run only: no files written.")
        return 0

    fresh_start = not (
        args.skip_filled and output_path.is_file() and output_path.stat().st_size > 0
    )

    if fresh_start:
        df = pd.read_csv(base_path, dtype=str, encoding="utf-8-sig")
        for _, csv_col in FIELD_MAP:
            if csv_col not in df.columns:
                df[csv_col] = ""
        securities = load_portfolio_securities(df)
        cleared = init_output_from_base(df, securities, clear_targets=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
        _log(f"Initialized {output_path.name} from {base_path.name}")
        _log(f"Cleared REST columns on {cleared} Agency MBS/CMO rows (others unchanged)")
    else:
        df = pd.read_csv(output_path, dtype=str, encoding="utf-8-sig")
        for _, csv_col in FIELD_MAP:
            if csv_col not in df.columns:
                df[csv_col] = ""
        _log(f"Resuming from existing {output_path.name}")

    securities = load_portfolio_securities(df)
    groups = group_securities(securities)
    all_groups = dict(groups)

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

    _log(f"Base: {base_path}")
    _log(f"Output: {output_path}")
    _log(f"Failed log: {fail_path}")
    _log(f"Progress log: {LOG_FILE}")
    _log(f"Curve date: {DEFAULT_CURVE_DATE}")
    _log(f"Prepay: {PREPAY_TYPE_2601} (model 2601) for Agency MBS* + Agency CMO")
    _log(f"Other Sub Types: copied from base, untouched")
    _log(f"Workers: {workers}")
    _log(f"Final failed-CUSIP retry rounds: {args.final_retries}")
    _log(f"Batch size: {DEFAULT_SMALL_BATCH_SIZE}")
    _log(f"Sub types: {len(groups)}, unique CUSIPs: {n_secs}, total batches: {n_batches}")
    if workers > 1:
        _log(f"Estimated runtime: {est_min:.0f} min sequential (~{est_min / workers:.0f} min at {workers} workers)")
    else:
        _log(f"Estimated runtime: {est_min:.0f} min ({est_min / 60:.1f} hours)")
    for st in GROUP_ORDER:
        if st not in groups:
            continue
        u = len({str(s["cusip"]).strip().upper() for s in groups[st]})
        bs = batch_size_for(st)
        b = max(1, math.ceil(u / bs))
        _log(f"  {st}: {len(groups[st])} rows, {u} unique, {b} batches @ {bs}")

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

        _log(
            f"===== FINAL RETRY ROUND {round_i}/{args.final_retries}: "
            f"{len(still_needed)} CUSIPs ====="
        )
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

    filled_mbs_cmo = sum(
        1
        for secs in all_groups.values()
        for s in secs
        if row_is_filled(df, int(s["_df_index"]))
    )
    total_mbs_cmo = sum(len(v) for v in all_groups.values())
    _log(
        f"ALL DONE in {total_elapsed / 60:.1f} min: "
        f"main batches ok={ok_batches} fail={fail_batches}, "
        f"CUSIPs filled={filled_cusips}, recovered~={recovered_total}, "
        f"MBS/CMO rows filled={filled_mbs_cmo}/{total_mbs_cmo}, "
        f"final failed={len(remaining_failed)}"
    )
    _log(f"Wrote {output_path}")

    summary = {
        "base": str(base_path),
        "output": str(output_path),
        "curve_date": DEFAULT_CURVE_DATE,
        "prepay_type": PREPAY_TYPE_2601,
        "model_code": 2601,
        "sub_types": sorted(MODEL_2601_SUB_TYPES),
        "batches_ok": ok_batches,
        "batches_fail": fail_batches,
        "cusips_filled": filled_cusips,
        "final_retry_rounds": args.final_retries,
        "final_failed_cusips": len(remaining_failed),
        "mbs_cmo_rows_filled": filled_mbs_cmo,
        "mbs_cmo_rows_total": total_mbs_cmo,
        "elapsed_min": round(total_elapsed / 60, 1),
        "estimated_min": round(est_min, 1),
        "batch_size": DEFAULT_SMALL_BATCH_SIZE,
        "workers": workers,
        "batch_retries": BATCH_RETRIES,
        "batch_retry_wait_s": BATCH_RETRY_WAIT_S,
    }
    (base_dir / SUMMARY_FILE).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0 if not remaining_failed else 1


if __name__ == "__main__":
    sys.exit(main())
