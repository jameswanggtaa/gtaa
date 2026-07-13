"""
Single-CUSIP Yield Book cash-flow smoke test (CPR=0).

Reads the first row from Holding_0626.csv, divides Par Amount by 1000,
calls POST /sync/bond/cash-flow, and writes the raw JSON response.

Pricing/settlement default to today's weekday business date. If YB returns the
On-The-Run curve error (common before curves are published for the day), the
script walks back prior weekdays until a successful cash-flow is returned.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests as rq

try:
    from pypac import PACSession
except ImportError:
    PACSession = None

AUTH_URL = "https://www.yieldbook.com/x/oauth/api-token"
API_BASE_URL = "https://api.yieldbook.com/analytics/v2"
HOLDINGS_FILE = Path(__file__).with_name("Holding_0626.csv")
RAW_OUT = Path(__file__).with_name("yb_cashflow_test_raw.json")
MAX_DATE_LOOKBACK = 10  # weekdays


def _parse_par_amount(raw: str) -> float:
    return float(str(raw).strip().replace(",", "").replace(" ", "").replace('"', "")) / 1000.0


def parse_holdings(path: Path, limit: Optional[int] = None) -> List[Tuple[str, float]]:
    """Read CUSIP + Par Amount/1000 from holdings CSV (or legacy TSV .txt)."""
    rows: List[Tuple[str, float]] = []
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise ValueError(f"No header in {path}")
            # tolerate slight header spacing differences
            field_map = {name.strip().lower(): name for name in reader.fieldnames}
            cusip_key = field_map.get("cusip")
            par_key = field_map.get("par amount") or field_map.get("paramount")
            if not cusip_key or not par_key:
                raise ValueError(
                    f"{path} must have CUSIP and Par Amount columns; got {reader.fieldnames}"
                )
            for row in reader:
                cusip = str(row.get(cusip_key) or "").strip()
                if not cusip:
                    continue
                try:
                    par = _parse_par_amount(str(row.get(par_key) or ""))
                except ValueError:
                    print(f"[WARN] skip bad par amount for {cusip!r}: {row.get(par_key)!r}")
                    continue
                rows.append((cusip, par))
                if limit is not None and len(rows) >= limit:
                    break
        return rows

    # Legacy tab/space-delimited .txt
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines[1:]:
        parts = ln.split("\t")
        if len(parts) < 3:
            parts = [p for p in ln.replace(",", "").split() if p]
            if len(parts) < 2:
                continue
            cusip, par_raw = parts[0], parts[-1]
        else:
            cusip = parts[0].strip()
            par_raw = parts[2]
        if not cusip:
            continue
        try:
            par = _parse_par_amount(par_raw)
        except ValueError:
            print(f"[WARN] skip bad par amount for {cusip!r}: {par_raw!r}")
            continue
        rows.append((cusip, par))
        if limit is not None and len(rows) >= limit:
            break
    return rows


def make_http_session():
    if PACSession is not None:
        return PACSession()
    return rq.Session()


def load_api_credentials() -> Dict[str, str]:
    client_id = os.environ.get("YB_CLIENT_ID", "zwang@mtb.com-api")
    client_secret = os.environ.get(
        "YB_CLIENT_SECRET", "557ee405-5bc7-f273-5ec4-d9ff91697656"
    )
    return {"client_id": client_id, "client_secret": client_secret}


def get_access_token(session) -> str:
    creds = load_api_credentials()
    resp = session.post(
        AUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "audience": "API2-PROD",
            "ttl": "7200",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["accessToken"]


def business_date(d: Optional[date] = None) -> date:
    """Weekday business date (skip Sat/Sun). No holiday calendar."""
    d = d or date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def prior_business_dates(start: date, n: int) -> List[date]:
    out: List[date] = []
    d = start
    for _ in range(n):
        out.append(d)
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return out


def parse_first_holding(path: Path) -> Tuple[str, float]:
    rows = parse_holdings(path, limit=1)
    if not rows:
        raise ValueError(f"No data rows in {path}")
    return rows[0]


def build_cashflow_payload(cusip: str, par_amount: float, as_of: date) -> Dict[str, Any]:
    """Payload shape matching Excel YBCF / REST sample (prepay, not prepaySettings)."""
    as_of_s = as_of.isoformat()
    return {
        "input": [
            {
                "identifier": cusip,
                "parAmount": f"{par_amount:.3f}",
                "prepay": {"rate": "0", "type": "CPR"},
                "floaterSettings": {},
                "settlementDate": as_of_s,
            }
        ],
        "globalSettings": {
            "pricingDate": as_of_s,
            "volatility": {"type": "Default"},
        },
    }


def cashflow_ok(data: Dict[str, Any]) -> bool:
    results = data.get("results") or []
    if not results or not isinstance(results[0], dict):
        return False
    cf = results[0].get("cashFlow")
    if not isinstance(cf, dict):
        return False
    payments = cf.get("dataPaymentList")
    return isinstance(payments, list) and len(payments) > 0


def call_cash_flow(session, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{API_BASE_URL.rstrip('/')}/sync/bond/cash-flow"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "Content-Type": "application/json",
    }
    resp = session.post(url, headers=headers, json=payload, timeout=120)
    print(f"[INFO] POST {url} -> HTTP {resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        print(resp.text[:2000])
        resp.raise_for_status()
        raise
    if resp.status_code >= 400:
        print(json.dumps(data, indent=2)[:4000])
        resp.raise_for_status()
    return data


def summarize_cashflow(data: Dict[str, Any]) -> None:
    cf = data["results"][0]["cashFlow"]
    payments = cf["dataPaymentList"]
    print(f"[INFO] securityID={cf.get('securityID')} ticker={cf.get('ticker')}")
    print(f"[INFO] pricingDate={cf.get('pricingDate')} settlementDate={cf.get('settlementDate')}")
    print(f"[INFO] coupon={cf.get('currentCoupon')} periods={len(payments)}")
    print("[INFO] first period:")
    print(json.dumps(payments[0], indent=2))
    print("[INFO] last period:")
    print(json.dumps(payments[-1], indent=2))


def main() -> None:
    today_bd = business_date()
    cusip, par = parse_first_holding(HOLDINGS_FILE)

    print(f"[INFO] holdings file: {HOLDINGS_FILE}")
    print(f"[INFO] CUSIP={cusip}  parAmount={par:.3f}  (Par Amount / 1000)")
    print(f"[INFO] preferred business date={today_bd.isoformat()}")

    session = make_http_session()
    token = get_access_token(session)
    print("[INFO] access token acquired")

    last_data: Optional[Dict[str, Any]] = None
    used_date: Optional[date] = None
    for as_of in prior_business_dates(today_bd, MAX_DATE_LOOKBACK):
        payload = build_cashflow_payload(cusip, par, as_of)
        print(f"[INFO] trying pricing/settlement={as_of.isoformat()}")
        data = call_cash_flow(session, token, payload)
        last_data = data
        if cashflow_ok(data):
            used_date = as_of
            break
        errs = data.get("errors") or []
        msg = errs[0].get("description") if errs else "no payments"
        print(f"[WARN] no cash flow for {as_of}: {msg}")

    if last_data is None:
        raise RuntimeError("No cash-flow response received")

    RAW_OUT.write_text(json.dumps(last_data, indent=2), encoding="utf-8")
    print(f"[INFO] wrote raw response -> {RAW_OUT}")

    if used_date is None or not cashflow_ok(last_data):
        raise SystemExit(
            f"[ERROR] Could not get cash flows within {MAX_DATE_LOOKBACK} weekdays "
            f"back from {today_bd}. See {RAW_OUT}."
        )

    if used_date != today_bd:
        print(
            f"[WARN] Curves not available for {today_bd}; used prior business date {used_date}."
        )
    summarize_cashflow(last_data)


if __name__ == "__main__":
    main()
