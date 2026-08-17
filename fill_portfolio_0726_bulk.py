#!/usr/bin/env python3
"""
Bulk-fill Portfolio 0726 via REST: YBPRICE + YBINDIC + YBSCEN per Sub Type batch.

Reads Portfolio_0726_input.csv, writes Portfolio_0726_output.csv (checkpointed each batch).

- Progress logged to Portfolio_0726_output_bulk_fill.log
- Failed CUSIPs logged to Portfolio_0726_output_bulk_failed.csv
- After main run, failed CUSIPs are retried FINAL_RETRY_ROUNDS times

Agency MBS* use OldModel (2501); Agency CMO uses type 2501.
Curve date: 2026-07-31.
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
import bulk_portfolio_0526_test as bt
from bulk_portfolio_0626_test import (
    LARGE_SAMPLE_SUB_TYPES,
    MBS_2501_SUB_TYPES,
    patch_bulk_builders,
)
from portfolio_0526_fill_common import (
    FIELD_MAP,
    SKIP_SUB_TYPES,
    clear_rest_columns,
    map_row_to_csv_columns,
    row_is_filled,
)
from portfolio_benchmark import load_portfolio_securities

patch_bulk_builders()

DEFAULT_INPUT = "Portfolio_0726_input.csv"
DEFAULT_OUTPUT = "Portfolio_0726_output.csv"
DEFAULT_CURVE_DATE = "2026-07-31"
LOG_FILE = "Portfolio_0726_output_bulk_fill.log"
FAILED_FILE = "Portfolio_0726_output_bulk_failed.csv"
SUMMARY_FILE = "Portfolio_0726_output_bulk_summary.json"
LARGE_BATCH_SIZE = int(os.environ.get("BULK_LARGE_BATCH_SIZE", "50"))
SMALL_BATCH_SIZE = int(os.environ.get("BULK_SMALL_BATCH_SIZE", "4"))
EST_MIN_LARGE_BATCH = float(os.environ.get("BULK_EST_MIN_LARGE_BATCH", "35"))
EST_MIN_SMALL_BATCH = float(os.environ.get("BULK_EST_MIN_SMALL_BATCH", "6"))
BATCH_RETRIES = int(os.environ.get("BULK_BATCH_RETRIES", "3"))
BATCH_RETRY_WAIT_S = float(os.environ.get("BULK_BATCH_RETRY_WAIT_S", "120"))
CHECKPOINT_EVERY = int(os.environ.get("BULK_CHECKPOINT_EVERY", "1"))
DEFAULT_WORKERS = int(os.environ.get("BULK_WORKERS", os.environ.get("YB_WORKERS", "4")))
FINAL_RETRY_ROUNDS = int(os.environ.get("BULK_FINAL_RETRY_ROUNDS", "3"))
FINAL_RETRY_WAIT_S = float(os.environ.get("BULK_FINAL_RETRY_WAIT_S", "60"))

_log_lock = threading.Lock()

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


def batch_size_for(sub_type: str) -> int:
    return LARGE_BATCH_SIZE if sub_type in LARGE_SAMPLE_SUB_TYPES else SMALL_BATCH_SIZE


def est_min_per_batch(sub_type: str) -> float:
    return EST_MIN_LARGE_BATCH if sub_type in LARGE_SAMPLE_SUB_TYPES else EST_MIN_SMALL_BATCH


def bulk_worker_count(workers_arg: Optional[int]) -> int:
    if workers_arg is not None:
        return max(1, min(int(workers_arg), 16))
    return max(1, min(DEFAULT_WORKERS, 16))


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


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
        sec = dict(sec)
        if st in MBS_2501_SUB_TYPES:
            sec["force_prepay_type"] = "OldModel"
        elif st == "Agency CMO":
            sec["force_prepay_type"] = 2501
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


def build_book_py_payload(securities: List[Dict[str, Any]]) -> Dict[str, Any]:
    pricing_date = securities[0].get("curve_date") or nsa.default_pricing_date()
    rows = []
    for sec in securities:
        row = bt.build_py_input_row(sec)
        book = nsa.parse_number(sec.get("book_price"))
        if book is not None and not pd.isna(book):
            row["level"] = str(book)
        rows.append(row)
    return {"globalSettings": {"pricingDate": pricing_date}, "input": rows}


def _index_bulk_results(
    items: List[Dict[str, Any]],
    securities: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    by_cusip: Dict[str, Dict[str, Any]] = {}
    for item in items:
        cusip = bt._cusip_from_result(item)
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
    sub_type: str,
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
        json=bt.build_bulk_py_payload(api_secs),
        headers=nsa.api_headers(token),
        timeout=300,
    )
    if not py_resp.ok:
        return {}, f"py HTTP {py_resp.status_code}: {(py_resp.text or '')[:300]}"
    py_items = bt.finish_sync_bulk(session, token, py_resp.json(), timeout_s=scenario_timeout_s)
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
            book_items = bt.finish_sync_bulk(session, token, book_resp.json(), timeout_s=scenario_timeout_s)
            py_book_by = _index_bulk_results(book_items, book_secs)

    indic_by = bt.fetch_indic_bulk_or_fallback(
        session, token, api_secs, sync_timeout_s=scenario_timeout_s,
    )

    scen_url = nsa.api_url("bond/scenario-calc", mode="req")
    scen_resp = session.post(
        scen_url,
        json=bt.build_bulk_scenario_payload(api_secs),
        headers=nsa.api_headers(token),
        timeout=120,
    )
    if not scen_resp.ok:
        return {}, f"scenario HTTP {scen_resp.status_code}: {(scen_resp.text or '')[:300]}"
    request_id = scen_resp.json().get("requestId")
    if not request_id:
        return {}, "scenario: no requestId"
    scen_data = bt.poll_results(session, token, request_id, timeout_s=scenario_timeout_s)
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
        api_row = bt.build_api_row_from_bulk(
            session,
            token,
            sec,
            py_item,
            indic_item,
            scen_item,
            py_book_item=book_item,
            keyword_fallback=False,
        )
        out[cusip] = map_row_to_csv_columns(api_row)
    return out, None


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
    sub_type: str,
    batch: List[Dict[str, Any]],
    *,
    scenario_timeout_s: float,
) -> Tuple[Dict[str, Dict[str, str]], Optional[str], Any, str]:
    last_err: Optional[str] = None
    for attempt in range(1, BATCH_RETRIES + 1):
        try:
            filled, err = run_bulk_batch(
                session, token, sub_type, batch,
                scenario_timeout_s=scenario_timeout_s,
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
        session,
        token,
        job.sub_type,
        job.batch,
        scenario_timeout_s=scenario_timeout_s,
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
        # keep an empty header so resume tools still work
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
            _log(
                f"  progress {completed}/{n_batches} ({pct:.1f}%) | ETA ~{eta_min:.0f} min"
            )

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
                session, token, job.sub_type, job.batch,
                scenario_timeout_s=scenario_timeout_s,
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
                ex.submit(
                    _execute_batch_job,
                    job,
                    scenario_timeout_s=scenario_timeout_s,
                ): job
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk-fill Portfolio 0726 via REST")
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true", help="Print plan and time estimate only")
    parser.add_argument("--scenario-timeout", type=float, default=1200.0)
    parser.add_argument(
        "--skip-filled",
        action="store_true",
        help="Load --output if present and skip rows that already have Forward Yield",
    )
    parser.add_argument(
        "--failed-file",
        default=FAILED_FILE,
        help="Failed CUSIP log path",
    )
    parser.add_argument(
        "--final-retries",
        type=int,
        default=FINAL_RETRY_ROUNDS,
        help="Retry rounds for failed CUSIPs after main run (default 3)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel bulk batch workers (default: BULK_WORKERS or YB_WORKERS or 4)",
    )
    args = parser.parse_args()

    os.environ.setdefault("PORTFOLIO_DEFAULT_CURVE_DATE", DEFAULT_CURVE_DATE)
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

    # Fresh log for this run
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

    _log(f"Input: {input_path}")
    _log(f"Output: {output_path}")
    _log(f"Failed log: {fail_path}")
    _log(f"Progress log: {LOG_FILE}")
    _log(f"Curve date: {DEFAULT_CURVE_DATE}")
    _log(f"Workers: {workers}")
    _log(f"Final failed-CUSIP retry rounds: {args.final_retries}")
    _log(f"Batch sizes: large={LARGE_BATCH_SIZE} (MUNI/Treasuries), small={SMALL_BATCH_SIZE} (all others)")
    _log(f"Sub types: {len(groups)}, unique CUSIPs: {n_secs}, total batches: {n_batches}")
    if workers > 1:
        _log(f"Estimated runtime: {est_min:.0f} min sequential (~{est_min / workers:.0f} min at {workers} workers)")
    else:
        _log(f"Estimated runtime: {est_min:.0f} min ({est_min / 60:.1f} hours)")
    for st in ordered_sub_types(groups):
        u = len({str(s["cusip"]).strip().upper() for s in groups[st]})
        bs = batch_size_for(st)
        b = max(1, math.ceil(u / bs))
        _log(f"  {st}: {len(groups[st])} rows, {u} unique, {b} batches @ {bs}")

    if args.dry_run:
        return 0

    # Full securities map for final retries (all runnable, not just pending)
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
    if failed:
        _log(f"Failed rows log: {fail_path}")

    # Final retry rounds for failed CUSIPs
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
        # Only retry those still unfilled in output
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
        # Prefer smaller batches / fewer workers for retries (more stable)
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
        "curve_date": DEFAULT_CURVE_DATE,
        "batches_ok": ok_batches,
        "batches_fail": fail_batches,
        "cusips_filled": filled_cusips,
        "final_retry_rounds": args.final_retries,
        "final_failed_cusips": len(remaining_failed),
        "rows_with_forward_yield": filled_rows,
        "elapsed_min": round(total_elapsed / 60, 1),
        "estimated_min": round(est_min, 1),
        "large_batch_size": LARGE_BATCH_SIZE,
        "small_batch_size": SMALL_BATCH_SIZE,
        "workers": workers,
    }
    (base / SUMMARY_FILE).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0 if not remaining_failed else 1


if __name__ == "__main__":
    sys.exit(main())
