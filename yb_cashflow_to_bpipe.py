"""
Convert Yield Book /sync/bond/cash-flow JSON into Bloomberg BPIPE
reply format matching BPIPE_Cashflow_output_sample.txt.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def fmt_coupon(v: float) -> str:
    return f"{v:.3f}"


def fmt_money(v: float) -> str:
    return f"{v:.2f}"


def yyyy_mm_dd_to_mmddyyyy(d: str) -> str:
    return datetime.strptime(d[:10], "%Y-%m-%d").strftime("%m/%d/%Y")


def build_mtg_cash_flow_blob(cf: Dict[str, Any]) -> str:
    """
    Bloomberg bulk MTG_CASH_FLOW:
      ;2;<nPeriods>;6;
      then for each period i=1..n:
        2;<i>;5;<mm/dd/yyyy>;2;<coupon>;2;<interest>;2;<principal>;2;<balance>
    """
    payments = cf.get("dataPaymentList") or []
    if not isinstance(payments, list) or not payments:
        raise ValueError("cashFlow missing dataPaymentList")

    coupon = float(cf.get("currentCoupon") or 0.0)
    n = len(payments)
    parts: List[str] = ["", "2", str(n), "6"]  # leading '' -> leading ';'

    for i, p in enumerate(payments, start=1):
        if not isinstance(p, dict):
            continue
        pay_date = yyyy_mm_dd_to_mmddyyyy(str(p.get("date")))
        interest = float(p.get("interestPayment") or 0.0)
        principal = float(p.get("principalPayment") or 0.0)
        balance = float(
            p.get("endPrincipalBalance")
            if p.get("endPrincipalBalance") is not None
            else p.get("principalBalance")
            or 0.0
        )
        # Period coupon: use security currentCoupon; last period may differ slightly in BPIPE.
        parts.extend(
            [
                "2",
                str(i),
                "5",
                pay_date,
                "2",
                fmt_coupon(coupon),
                "2",
                fmt_money(interest),
                "2",
                fmt_money(principal),
                "2",
                fmt_money(balance),
            ]
        )
    return ";".join(parts) + ";"


def resolve_cusip(cf: Dict[str, Any], fallback: Optional[str] = None) -> str:
    cusip = str(cf.get("cusip") or "").strip()
    if len(cusip) >= 8:
        return cusip
    sec = str(cf.get("securityID") or "").strip()
    if fallback:
        return fallback
    return sec or cusip


def format_data_line(cusip: str, cf: Dict[str, Any]) -> str:
    blob = build_mtg_cash_flow_blob(cf)
    # |0|4| = status/num fields; MARKET_SECTOR_DES=Mtge; last field FLD UNKNOWN
    return f"{cusip}|0|4|{cusip}|Mtge|{blob}|FLD UNKNOWN|"


def extract_cashflows(
    data: Dict[str, Any],
    request_cusips: Optional[List[str]] = None,
) -> List[tuple[str, Dict[str, Any]]]:
    rows: List[tuple[str, Dict[str, Any]]] = []
    results = data.get("results") or []
    for idx, item in enumerate(results):
        if not isinstance(item, dict):
            continue
        cf = item.get("cashFlow")
        if not isinstance(cf, dict):
            continue
        if not isinstance(cf.get("dataPaymentList"), list) or not cf["dataPaymentList"]:
            continue
        fallback = request_cusips[idx] if request_cusips and idx < len(request_cusips) else None
        cusip = resolve_cusip(cf, fallback=fallback)
        # Prefer 9-char request CUSIP when JSON returns 8-char securityID only
        if fallback and len(cusip) == 8 and fallback.startswith(cusip):
            cusip = fallback
        rows.append((cusip, cf))
    return rows


def build_bpipe_header(
    *,
    rundate: Optional[str] = None,
    generated_at: Optional[datetime] = None,
    reply_filename: Optional[str] = None,
) -> str:
    now = generated_at or datetime.now().astimezone()
    rd = rundate or now.strftime("%Y%m%d")
    reply = reply_filename or f"outputData{rd}.txt"
    utc = now.astimezone(timezone.utc)
    hour12 = utc.hour % 12 or 12
    ampm = "AM" if utc.hour < 12 else "PM"
    gen_gmt = (
        f"{utc.month:02d}/{utc.day:02d}/{utc.strftime('%y')} "
        f"{hour12}:{utc.strftime('%M:%S')}.000 {ampm} GMT"
    )
    tz_name = now.tzname() or "EDT"
    if "Daylight" in tz_name or tz_name.upper() == "EDT":
        tz_abbr = "EDT"
    elif "Standard" in tz_name or tz_name.upper() == "EST":
        tz_abbr = "EST"
    else:
        tz_abbr = tz_name
    timestarted = (
        f"{now.strftime('%a %b')} {now.day} {now.strftime('%H:%M:%S')} {tz_abbr} {now.year}"
    )
    return f"""START-OF-FILE
RUNDATE={rd}


#  Calypso Request File (Generated: {gen_gmt})

#  Request Header
VOL_SURFACE=yes
CLOSINGVALUES=yes
REPORT=yes
PROGRAMFLAG=oneshot
FIRMNAME=MTB
REPLYFILENAME={reply}
HISTORICAL=yes
DERIVED=yes
SECMASTER=yes
PROGRAMNAME=getdata
DATEFORMAT=mmddyyyy

#  Request Fields
START-OF-FIELDS
ID_CUSIP
MARKET_SECTOR_DES
MTG_CASH_FLOW
CALYPSO-UPDNAME_YBkCflowUpldr
END-OF-FIELDS

#  Request Data
TIMESTARTED={timestarted}
START-OF-DATA
"""


def _tz_abbr(now: datetime) -> str:
    tz_name = now.tzname() or "EDT"
    if "Daylight" in tz_name or tz_name.upper() == "EDT":
        return "EDT"
    if "Standard" in tz_name or tz_name.upper() == "EST":
        return "EST"
    return tz_name


def build_bpipe_footer(*, finished_at: Optional[datetime] = None) -> str:
    """
    Footer sample:
      END-OF-DATA
      TIMEFINISHED=Thu Jun 23 16:37:12 EDT 2026
      END-OF-FILE
    """
    now = finished_at or datetime.now().astimezone()
    tz_abbr = _tz_abbr(now)
    timefinished = (
        f"{now.strftime('%a %b')} {now.day} {now.strftime('%H:%M:%S')} {tz_abbr} {now.year}"
    )
    return f"END-OF-DATA\nTIMEFINISHED={timefinished}\nEND-OF-FILE\n"


def build_bpipe_file_from_rows(
    rows: List[tuple[str, Dict[str, Any]]],
    *,
    rundate: Optional[str] = None,
    generated_at: Optional[datetime] = None,
    reply_filename: Optional[str] = None,
) -> str:
    if not rows:
        raise ValueError("No cashFlow rows to format")
    header = build_bpipe_header(
        rundate=rundate,
        generated_at=generated_at,
        reply_filename=reply_filename,
    )
    body = "\n".join(format_data_line(cusip, cf) for cusip, cf in rows)
    footer = build_bpipe_footer(finished_at=datetime.now().astimezone())
    return header + body + "\n" + footer


def build_bpipe_file(
    data: Dict[str, Any],
    *,
    request_cusips: Optional[List[str]] = None,
    rundate: Optional[str] = None,
    generated_at: Optional[datetime] = None,
    reply_filename: Optional[str] = None,
) -> str:
    rows = extract_cashflows(data, request_cusips=request_cusips)
    return build_bpipe_file_from_rows(
        rows,
        rundate=rundate,
        generated_at=generated_at,
        reply_filename=reply_filename,
    )


def request_cusips_from_payload(data: Dict[str, Any]) -> Optional[List[str]]:
    # Not present in response; caller may pass separately.
    return None


def main() -> None:
    today = datetime.now().strftime("%Y%m%d")
    default_out = f"outputData{today}.txt"
    ap = argparse.ArgumentParser(description="Format YB cash-flow JSON as BPIPE reply file")
    ap.add_argument(
        "--json",
        default="yb_cashflow_first3_raw.json",
        help="Input Yield Book cash-flow JSON",
    )
    ap.add_argument(
        "--out",
        default=default_out,
        help=f"Output BPIPE-format file (default: {default_out})",
    )
    ap.add_argument(
        "--cusips",
        default="",
        help="Optional comma-separated request CUSIPs (order match results)",
    )
    args = ap.parse_args()

    path = Path(args.json)
    data = json.loads(path.read_text(encoding="utf-8"))
    cusips = [c.strip() for c in args.cusips.split(",") if c.strip()] or None

    text = build_bpipe_file(
        data,
        request_cusips=cusips,
        rundate=today,
        reply_filename=Path(args.out).name,
    )
    out = Path(args.out)
    out.write_text(text, encoding="utf-8")
    n = text.count("|FLD UNKNOWN|")
    print(f"[INFO] wrote {out}  ({n} securities)")


if __name__ == "__main__":
    main()
