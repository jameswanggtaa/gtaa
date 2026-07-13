"""
Pull YB CPR=0 cash flows for the first N holdings and compare to
BPIPE_Cashflow_output_sample.txt (MTG_CASH_FLOW blobs).

Uses the sample's pricingDate / settlementDate so schedules align.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from yb_cashflow_bpipe_test import (
    HOLDINGS_FILE,
    call_cash_flow,
    cashflow_ok,
    get_access_token,
    make_http_session,
    parse_holdings,
)

SAMPLE_FILE = Path(__file__).with_name("BPIPE_Cashflow_output_sample.txt")
RAW_OUT = Path(__file__).with_name("yb_cashflow_first3_raw.json")
COMPARE_OUT = Path(__file__).with_name("yb_vs_bpipe_first3_compare.txt")

# Dates from the user's YBCF / BPIPE sample
SAMPLE_PRICING_DATE = "2026-07-07"
SAMPLE_SETTLEMENT_DATE = "2026-07-20"
N_CUSIPS = 3
ABS_TOL = 1.0  # dollar tolerance for interest/principal/balance
ABS_TOL_COUPON = 0.01


def build_batch_payload(
    holdings: List[Tuple[str, float]],
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


def parse_bpipe_mtg_cash_flow(blob: str) -> List[Dict[str, Any]]:
    """
    Parse Bloomberg bulk MTG_CASH_FLOW:
      ;2;<nPeriods>;6;  then nPeriods of:
      2;<i>;5;<mm/dd/yyyy>;2;<coupon>;2;<interest>;2;<principal>;2;<balance>
    """
    blob = blob.strip().rstrip("|").strip()
    if blob.startswith(";"):
        blob = blob[1:]
    toks = [t for t in blob.split(";") if t != ""]
    # Expect leading: 2, nPeriods, 6
    if len(toks) < 3:
        return []
    # Skip type=2, nPeriods, nFields=6
    try:
        n_periods = int(float(toks[1]))
        n_fields = int(float(toks[2]))
    except ValueError:
        return []
    i = 3
    periods: List[Dict[str, Any]] = []
    for _ in range(n_periods):
        vals: List[str] = []
        for _f in range(n_fields):
            if i + 1 >= len(toks):
                break
            # type code then value
            i += 1  # skip type
            vals.append(toks[i])
            i += 1
        if len(vals) < 6:
            break
        periods.append(
            {
                "period": int(float(vals[0])),
                "date": vals[1],  # mm/dd/yyyy
                "coupon": float(vals[2]),
                "interest": float(vals[3]),
                "principal": float(vals[4]),
                "balance": float(vals[5]),
            }
        )
    return periods


def parse_bpipe_sample(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    out: Dict[str, List[Dict[str, Any]]] = {}
    for ln in text.splitlines():
        if "|" not in ln or ln.startswith("#") or ln.startswith("START"):
            continue
        parts = ln.split("|")
        if len(parts) < 6:
            continue
        cusip = parts[0].strip()
        # fields: CUSIP|0|4|CUSIP|Mtge|blob|...
        blob = parts[5] if len(parts) > 5 else ""
        if ";2;" not in blob and not blob.startswith(";2"):
            # sometimes blob is in parts[5] starting with ;
            pass
        # Find the cashflow blob (starts with ;2;)
        blob_match = re.search(r";2;\d+;\d+;.*", ln)
        if not blob_match:
            continue
        blob = blob_match.group(0).split("|")[0]
        out[cusip] = parse_bpipe_mtg_cash_flow(blob)
    return out


def yb_date_to_bpipe(d: str) -> str:
    """YYYY-MM-DD -> mm/dd/yyyy"""
    dt = datetime.strptime(d[:10], "%Y-%m-%d")
    return dt.strftime("%m/%d/%Y")


def yb_to_periods(cf: Dict[str, Any]) -> List[Dict[str, Any]]:
    coupon = float(cf.get("currentCoupon") or 0.0)
    periods: List[Dict[str, Any]] = []
    for idx, p in enumerate(cf.get("dataPaymentList") or [], start=1):
        periods.append(
            {
                "period": idx,
                "date": yb_date_to_bpipe(str(p.get("date"))),
                "coupon": coupon,
                # last period in sample sometimes shows 5.001 — keep raw coupon;
                # compare will flag if needed
                "interest": float(p.get("interestPayment") or 0.0),
                "principal": float(p.get("principalPayment") or 0.0),
                "balance": float(
                    p.get("endPrincipalBalance")
                    if p.get("endPrincipalBalance") is not None
                    else p.get("principalBalance")
                    or 0.0
                ),
            }
        )
    # Match sample quirk: final coupon sometimes rounded oddly — leave as API coupon
    return periods


def index_yb_results(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        cf = item.get("cashFlow")
        if not isinstance(cf, dict):
            continue
        cusip = str(cf.get("cusip") or cf.get("securityID") or "").strip()
        # securityID may be 8-char; prefer full CUSIP from request if present
        if len(cusip) == 8:
            # try to find matching 9-char later
            out[cusip] = cf
        else:
            out[cusip] = cf
    return out


def match_cf(yb_by_id: Dict[str, Dict[str, Any]], cusip: str) -> Optional[Dict[str, Any]]:
    if cusip in yb_by_id:
        return yb_by_id[cusip]
    # 8-char securityID
    for k, v in yb_by_id.items():
        if cusip.startswith(k) or k.startswith(cusip[:8]):
            return v
    return None


def compare_periods(
    cusip: str,
    yb: List[Dict[str, Any]],
    bp: List[Dict[str, Any]],
) -> List[str]:
    lines: List[str] = []
    lines.append(f"=== {cusip} ===")
    lines.append(f"YB periods={len(yb)}  BPIPE periods={len(bp)}")
    if not yb:
        lines.append("ERROR: no YB periods")
        return lines
    if not bp:
        lines.append("ERROR: no BPIPE periods")
        return lines

    n = min(len(yb), len(bp))
    date_mismatch = 0
    field_mismatches = {"coupon": 0, "interest": 0, "principal": 0, "balance": 0}
    max_abs = {"coupon": 0.0, "interest": 0.0, "principal": 0.0, "balance": 0.0}
    sample_diffs: List[str] = []

    for i in range(n):
        a, b = yb[i], bp[i]
        if a["date"] != b["date"]:
            date_mismatch += 1
            if len(sample_diffs) < 5:
                sample_diffs.append(
                    f"  period {i+1}: DATE YB={a['date']} BPIPE={b['date']}"
                )
        for fld, tol in (
            ("coupon", ABS_TOL_COUPON),
            ("interest", ABS_TOL),
            ("principal", ABS_TOL),
            ("balance", ABS_TOL),
        ):
            diff = abs(float(a[fld]) - float(b[fld]))
            max_abs[fld] = max(max_abs[fld], diff)
            if diff > tol:
                field_mismatches[fld] += 1
                if len(sample_diffs) < 12:
                    sample_diffs.append(
                        f"  period {i+1} {a['date']}: {fld} "
                        f"YB={a[fld]:.4f} BPIPE={b[fld]:.4f} diff={diff:.4f}"
                    )

    # First / last side-by-side
    lines.append(
        f"First YB:    {yb[0]['date']} cpn={yb[0]['coupon']:.3f} "
        f"int={yb[0]['interest']:.2f} prin={yb[0]['principal']:.2f} bal={yb[0]['balance']:.2f}"
    )
    lines.append(
        f"First BPIPE: {bp[0]['date']} cpn={bp[0]['coupon']:.3f} "
        f"int={bp[0]['interest']:.2f} prin={bp[0]['principal']:.2f} bal={bp[0]['balance']:.2f}"
    )
    lines.append(
        f"Last  YB:    {yb[-1]['date']} cpn={yb[-1]['coupon']:.3f} "
        f"int={yb[-1]['interest']:.2f} prin={yb[-1]['principal']:.2f} bal={yb[-1]['balance']:.2f}"
    )
    lines.append(
        f"Last  BPIPE: {bp[-1]['date']} cpn={bp[-1]['coupon']:.3f} "
        f"int={bp[-1]['interest']:.2f} prin={bp[-1]['principal']:.2f} bal={bp[-1]['balance']:.2f}"
    )
    lines.append(
        f"Count delta={len(yb)-len(bp)}  date mismatches in overlap={date_mismatch}/{n}"
    )
    lines.append(
        "Max |diff|: "
        + ", ".join(f"{k}={v:.4f}" for k, v in max_abs.items())
    )
    lines.append(
        "Periods over tol: "
        + ", ".join(f"{k}={v}" for k, v in field_mismatches.items())
    )
    if sample_diffs:
        lines.append("Sample diffs:")
        lines.extend(sample_diffs)
    else:
        lines.append("All overlapping periods within tolerance.")
    return lines


def main() -> None:
    holdings = parse_holdings(HOLDINGS_FILE, limit=N_CUSIPS)
    print("[INFO] holdings:", holdings)
    print(
        f"[INFO] using sample dates pricing={SAMPLE_PRICING_DATE} "
        f"settlement={SAMPLE_SETTLEMENT_DATE}"
    )

    bpipe = parse_bpipe_sample(SAMPLE_FILE)
    print(f"[INFO] BPIPE sample CUSIPs parsed: {list(bpipe.keys())}")

    session = make_http_session()
    token = get_access_token(session)
    payload = build_batch_payload(
        holdings, SAMPLE_PRICING_DATE, SAMPLE_SETTLEMENT_DATE
    )
    print("[INFO] request payload:")
    print(json.dumps(payload, indent=2))

    data = call_cash_flow(session, token, payload)
    RAW_OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[INFO] wrote {RAW_OUT}")

    if not cashflow_ok(data) and not any(
        isinstance((r or {}).get("cashFlow"), dict)
        and isinstance((r.get("cashFlow") or {}).get("dataPaymentList"), list)
        for r in (data.get("results") or [])
        if isinstance(r, dict)
    ):
        # batch may put multiple results; check any success
        print("[ERROR] cash-flow call failed:")
        print(json.dumps(data.get("errors"), indent=2)[:2000])
        raise SystemExit(1)

    yb_by_id = index_yb_results(data)
    # Map results by order if needed
    results = data.get("results") or []

    report: List[str] = [
        f"YB vs BPIPE compare  pricing={SAMPLE_PRICING_DATE} "
        f"settlement={SAMPLE_SETTLEMENT_DATE}  CPR=0",
        f"Holdings: {holdings}",
        "",
    ]

    for idx, (cusip, par) in enumerate(holdings):
        cf = match_cf(yb_by_id, cusip)
        if cf is None and idx < len(results):
            item = results[idx]
            if isinstance(item, dict):
                cf = item.get("cashFlow")
        if not isinstance(cf, dict) or not cf.get("dataPaymentList"):
            report.append(f"=== {cusip} ===")
            report.append("ERROR: no YB cashFlow result")
            # show error if aligned
            errs = data.get("errors") or []
            if errs:
                report.append(str(errs[:2]))
            report.append("")
            continue
        yb_periods = yb_to_periods(cf)
        bp_periods = bpipe.get(cusip, [])
        report.extend(compare_periods(cusip, yb_periods, bp_periods))
        report.append("")

    text = "\n".join(report)
    COMPARE_OUT.write_text(text, encoding="utf-8")
    print(text)
    print(f"[INFO] wrote {COMPARE_OUT}")


if __name__ == "__main__":
    main()
