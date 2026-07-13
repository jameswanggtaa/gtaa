"""
Run Yield Book CPR=0 cash flows for all CUSIPs in Holding_0626.csv
and write a Bloomberg BPIPE reply file.

Example:
  python yb_cashflow_bpipe_run.py
  python yb_cashflow_bpipe_run.py --limit 10
  python yb_cashflow_bpipe_run.py --batch-size 10 --holdings Holding_0626.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from yb_cashflow_bpipe_test import (
    API_BASE_URL,
    MAX_DATE_LOOKBACK,
    business_date,
    call_cash_flow,
    cashflow_ok,
    get_access_token,
    make_http_session,
    parse_holdings,
    prior_business_dates,
)
from yb_cashflow_to_bpipe import (
    build_bpipe_file_from_rows,
    build_bpipe_header,
    format_data_line,
    resolve_cusip,
)

Holding = Tuple[str, float]


def build_batch_payload(
    holdings: List[Holding],
    pricing_date: str,
    settlement_date: str,
) -> Dict[str, Any]:
    return {
        "input": [
            {
                "identifier": cusip,
                "parAmount": f"{par:.3f}",
                "prepay": {"rate": "0", "type": "CPR"},
                "floaterSettings": {},
                "settlementDate": settlement_date,
            }
            for cusip, par in holdings
        ],
        "globalSettings": {
            "pricingDate": pricing_date,
            "volatility": {"type": "Default"},
        },
    }


def cf_has_payments(cf: Any) -> bool:
    return (
        isinstance(cf, dict)
        and isinstance(cf.get("dataPaymentList"), list)
        and len(cf["dataPaymentList"]) > 0
    )


def resolve_asof_date(
    session,
    token: str,
    probe: Holding,
    preferred: date,
    lookback: int,
) -> date:
    """Find latest weekday with usable curves using one probe CUSIP."""
    for as_of in prior_business_dates(preferred, lookback):
        payload = build_batch_payload([probe], as_of.isoformat(), as_of.isoformat())
        print(f"[INFO] probing curves with {probe[0]} on {as_of.isoformat()}")
        data = call_cash_flow(session, token, payload)
        if cashflow_ok(data):
            if as_of != preferred:
                print(
                    f"[WARN] Curves not available for {preferred}; "
                    f"using pricing/settlement={as_of.isoformat()}"
                )
            return as_of
        errs = data.get("errors") or []
        msg = errs[0].get("description") if errs else "no payments"
        print(f"[WARN] probe failed for {as_of}: {msg}")
    raise RuntimeError(
        f"Could not resolve pricing date within {lookback} weekdays of {preferred}"
    )


def match_result_cf(
    results: List[Any],
    request_cusips: List[str],
    idx: int,
    cusip: str,
) -> Optional[Dict[str, Any]]:
    # Prefer positional match (batch order), then CUSIP / securityID match.
    if idx < len(results) and isinstance(results[idx], dict):
        cf = results[idx].get("cashFlow")
        if cf_has_payments(cf):
            return cf  # type: ignore[return-value]
    for item in results:
        if not isinstance(item, dict):
            continue
        cf = item.get("cashFlow")
        if not isinstance(cf, dict):
            continue
        rid = resolve_cusip(cf)
        if rid == cusip or cusip.startswith(rid) or rid.startswith(cusip[:8]):
            if cf_has_payments(cf):
                return cf
    return None


def result_error_for_cusip(data: Dict[str, Any], cusip: str, idx: int) -> str:
    errs = data.get("errors") or []
    if not errs:
        # Per-result diagnostic
        results = data.get("results") or []
        if idx < len(results) and isinstance(results[idx], dict):
            cf = results[idx].get("cashFlow") or {}
            if isinstance(cf, dict) and cf.get("diagnostic"):
                return str(cf.get("diagnostic"))
        return "no cashFlow payments returned"
    # Prefer error mentioning this CUSIP; else first error
    for e in errs:
        if not isinstance(e, dict):
            continue
        blob = json.dumps(e)
        if cusip in blob:
            return str(e.get("description") or blob)
    e0 = errs[0] if isinstance(errs[0], dict) else {"description": str(errs[0])}
    return str(e0.get("description") or e0)


def chunks(items: List[Holding], size: int) -> List[List[Holding]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def maybe_refresh_token(session, token: str, fetched_at: float, refresh_sec: float) -> Tuple[str, float]:
    if time.time() - fetched_at < refresh_sec:
        return token, fetched_at
    print("[INFO] refreshing Yield Book access token")
    token = get_access_token(session)
    return token, time.time()


def run(
    holdings_path: Path,
    out_path: Path,
    errors_path: Path,
    *,
    limit: Optional[int],
    batch_size: int,
    pricing_date: Optional[str],
    settlement_date: Optional[str],
    lookback: int,
    timeout_sec: int,
    token_refresh_sec: float,
    write_incremental: bool,
) -> None:
    holdings = parse_holdings(holdings_path, limit=limit)
    if not holdings:
        raise SystemExit(f"No holdings found in {holdings_path}")

    print(f"[INFO] holdings file: {holdings_path}")
    print(f"[INFO] CUSIPs to process: {len(holdings)}  batch_size={batch_size}")

    session = make_http_session()
    token = get_access_token(session)
    fetched_at = time.time()
    print("[INFO] access token acquired")

    preferred = business_date()
    if pricing_date and settlement_date:
        as_of_pricing = date.fromisoformat(pricing_date)
        as_of_settle = date.fromisoformat(settlement_date)
    elif pricing_date or settlement_date:
        # If only one provided, use it for both
        d = date.fromisoformat(pricing_date or settlement_date)  # type: ignore[arg-type]
        as_of_pricing = as_of_settle = d
    else:
        as_of = resolve_asof_date(session, token, holdings[0], preferred, lookback)
        as_of_pricing = as_of_settle = as_of

    pricing_s = as_of_pricing.isoformat()
    settle_s = as_of_settle.isoformat()
    print(f"[INFO] pricingDate={pricing_s}  settlementDate={settle_s}")

    success_rows: List[Tuple[str, Dict[str, Any]]] = []
    error_rows: List[Dict[str, str]] = []

    out_fh = None
    if write_incremental:
        out_fh = out_path.open("w", encoding="utf-8", newline="\n")
        out_fh.write(build_bpipe_header())
        out_fh.flush()

    batches = chunks(holdings, batch_size)
    t0 = time.time()
    done = 0
    for b_idx, batch in enumerate(batches, start=1):
        token, fetched_at = maybe_refresh_token(
            session, token, fetched_at, token_refresh_sec
        )
        payload = build_batch_payload(batch, pricing_s, settle_s)
        req_cusips = [c for c, _ in batch]
        print(
            f"[INFO] batch {b_idx}/{len(batches)} "
            f"({req_cusips[0]} .. {req_cusips[-1]}, n={len(batch)})"
        )

        # call_cash_flow uses fixed 120s; override via local post for long batches
        url = f"{API_BASE_URL.rstrip('/')}/sync/bond/cash-flow"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "*/*",
            "Content-Type": "application/json",
        }
        resp = session.post(url, headers=headers, json=payload, timeout=timeout_sec)
        print(f"[INFO] POST cash-flow -> HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            print(resp.text[:2000])
            for cusip, par in batch:
                error_rows.append(
                    {
                        "CUSIP": cusip,
                        "parAmount": f"{par:.3f}",
                        "error": f"non-JSON HTTP {resp.status_code}",
                    }
                )
            continue

        if resp.status_code == 401:
            print("[WARN] 401 — refreshing token and retrying batch once")
            token = get_access_token(session)
            fetched_at = time.time()
            headers["Authorization"] = f"Bearer {token}"
            resp = session.post(url, headers=headers, json=payload, timeout=timeout_sec)
            print(f"[INFO] retry POST cash-flow -> HTTP {resp.status_code}")
            try:
                data = resp.json()
            except Exception:
                print(resp.text[:2000])
                for cusip, par in batch:
                    error_rows.append(
                        {
                            "CUSIP": cusip,
                            "parAmount": f"{par:.3f}",
                            "error": f"non-JSON HTTP {resp.status_code} after retry",
                        }
                    )
                continue

        if resp.status_code >= 400:
            msg = json.dumps(data)[:500]
            print(f"[ERROR] batch HTTP {resp.status_code}: {msg}")
            for cusip, par in batch:
                error_rows.append(
                    {
                        "CUSIP": cusip,
                        "parAmount": f"{par:.3f}",
                        "error": f"HTTP {resp.status_code}: {msg}",
                    }
                )
            done += len(batch)
            continue

        results = data.get("results") or []
        for i, (cusip, par) in enumerate(batch):
            cf = match_result_cf(results, req_cusips, i, cusip)
            if cf is None:
                error_rows.append(
                    {
                        "CUSIP": cusip,
                        "parAmount": f"{par:.3f}",
                        "error": result_error_for_cusip(data, cusip, i),
                    }
                )
                continue
            # Keep request CUSIP (9-char) on the BPIPE line
            success_rows.append((cusip, cf))
            if out_fh is not None:
                out_fh.write(format_data_line(cusip, cf) + "\n")
                out_fh.flush()

        done += len(batch)
        ok_n = len(success_rows)
        err_n = len(error_rows)
        elapsed = time.time() - t0
        print(
            f"[INFO] progress {done}/{len(holdings)}  ok={ok_n} err={err_n}  "
            f"elapsed={elapsed:.1f}s  {elapsed / max(done, 1):.2f}s/CUSIP"
        )

    if out_fh is not None:
        out_fh.close()
    else:
        text = build_bpipe_file_from_rows(success_rows)
        out_path.write_text(text, encoding="utf-8")

    with errors_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["CUSIP", "parAmount", "error"])
        w.writeheader()
        w.writerows(error_rows)

    print(f"[INFO] BPIPE output -> {out_path}  ({len(success_rows)} securities)")
    print(f"[INFO] errors       -> {errors_path}  ({len(error_rows)} securities)")
    print(f"[INFO] elapsed {time.time() - t0:.1f}s")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="YB CPR=0 cash flows for Holding_0626.csv -> BPIPE file"
    )
    ap.add_argument(
        "--holdings",
        default="Holding_0626.csv",
        help="Holdings CSV (CUSIP, Total Issued, Par Amount)",
    )
    ap.add_argument(
        "--out",
        default="MQu_YBkCflowUpldr.out",
        help="BPIPE output filename",
    )
    ap.add_argument(
        "--errors",
        default="yb_cashflow_errors.csv",
        help="CSV of failed CUSIPs",
    )
    ap.add_argument("--limit", type=int, default=None, help="Process only first N CUSIPs")
    ap.add_argument("--batch-size", type=int, default=10, help="CUSIPs per API request")
    ap.add_argument("--pricing-date", default=None, help="YYYY-MM-DD (default: auto)")
    ap.add_argument("--settlement-date", default=None, help="YYYY-MM-DD (default: same as pricing)")
    ap.add_argument(
        "--lookback",
        type=int,
        default=MAX_DATE_LOOKBACK,
        help="Weekday lookback when auto-resolving pricing date",
    )
    ap.add_argument("--timeout", type=int, default=300, help="HTTP timeout seconds per batch")
    ap.add_argument(
        "--token-refresh-sec",
        type=float,
        default=5400,
        help="Refresh OAuth token after this many seconds",
    )
    ap.add_argument(
        "--no-incremental",
        action="store_true",
        help="Buffer all successes then write BPIPE once at end",
    )
    args = ap.parse_args()

    if args.batch_size < 1:
        raise SystemExit("--batch-size must be >= 1")

    run(
        Path(args.holdings),
        Path(args.out),
        Path(args.errors),
        limit=args.limit,
        batch_size=args.batch_size,
        pricing_date=args.pricing_date,
        settlement_date=args.settlement_date,
        lookback=args.lookback,
        timeout_sec=args.timeout,
        token_refresh_sec=args.token_refresh_sec,
        write_incremental=not args.no_incremental,
    )


if __name__ == "__main__":
    main()
