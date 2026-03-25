"""
MBS TBA settlement dates — SIFMA Class A.

Settlement dates are from SIFMA MBS Notification and Settlement Dates:
https://www.sifma.org/resources/guides-playbooks/mbs-notification-and-settlement-dates

Class A settlement date is used as the "most close settle date" for TBA pricing
(e.g. if today is 3/15/2026, next settle is Apr-26 Class A = 2026-04-13).

Yieldbook TBA names (FNMAx.x-PROD-{MON}) roll to the next month when as_of is
within TBA_YB_ROLL_DAYS_BEFORE_CLASS_A calendar days before that next settle
(e.g. with 2 days: use PROD-MAY from 2026-04-11 when next settle is 2026-04-13).
"""

from datetime import date, timedelta
from typing import Optional

# Calendar days before next Class A settle to roll PROD-{MON} for Yieldbook (TBA roll).
TBA_YB_ROLL_DAYS_BEFORE_CLASS_A = 2

# YYYY-MM month -> three-letter suffix for FNMA-PROD-{MON}
_MONTH_SUFFIX = {
    "01": "JAN",
    "02": "FEB",
    "03": "MAR",
    "04": "APR",
    "05": "MAY",
    "06": "JUN",
    "07": "JUL",
    "08": "AUG",
    "09": "SEP",
    "10": "OCT",
    "11": "NOV",
    "12": "DEC",
}

# SIFMA Class A settlement dates by settlement month (YYYY-MM -> YYYY-MM-DD).
# Source: SIFMA MBS Notification and Settlement Dates (updated for 2027).
# Extend this table when new years are published; fallback for missing months below.
SIFMA_CLASS_A_SETTLEMENT: dict[str, str] = {
    # 2026
    "2026-01": "2026-01-14",
    "2026-02": "2026-02-12",
    "2026-03": "2026-03-12",
    "2026-04": "2026-04-13",
    "2026-05": "2026-05-13",
    "2026-06": "2026-06-11",
    "2026-07": "2026-07-13",
    "2026-08": "2026-08-13",
    "2026-09": "2026-09-14",
    "2026-10": "2026-10-13",
    "2026-11": "2026-11-12",
    "2026-12": "2026-12-10",
    # 2027
    "2027-01": "2027-01-14",
    "2027-02": "2027-02-11",
    "2027-03": "2027-03-11",
    "2027-04": "2027-04-13",
    "2027-05": "2027-05-13",
    "2027-06": "2027-06-14",
    "2027-07": "2027-07-14",
    "2027-08": "2027-08-12",
    "2027-09": "2027-09-14",
    "2027-10": "2027-10-14",
    "2027-11": "2027-11-15",
    "2027-12": "2027-12-13",
}


def get_settlement_date(settlement_month: str) -> Optional[str]:
    """
    Return SIFMA Class A settlement date (YYYY-MM-DD) for a given month (YYYY-MM).

    Returns None if the month is not in the table.
    """
    return SIFMA_CLASS_A_SETTLEMENT.get(settlement_month.strip())


def get_next_settlement_date(as_of: Optional[date] = None) -> str:
    """
    Return the next SIFMA Class A settlement date on or after as_of.

    If as_of is before this month's Class A date, returns this month's date;
    otherwise returns next month's Class A date.
    Used for "most close settle date" for TBA (e.g. as_of=2026-03-15 -> 2026-04-13).
    """
    if as_of is None:
        as_of = date.today()
    # Try current month first
    month_key = as_of.strftime("%Y-%m")
    settle_str = get_settlement_date(month_key)
    if settle_str:
        settle_d = date.fromisoformat(settle_str)
        if as_of <= settle_d:
            return settle_str
    # Next month
    if as_of.month == 12:
        next_month_key = f"{as_of.year + 1}-01"
    else:
        next_month_key = f"{as_of.year}-{as_of.month + 1:02d}"
    next_settle = get_settlement_date(next_month_key)
    if next_settle:
        return next_settle
    raise ValueError(
        f"No SIFMA Class A settlement date for month {next_month_key}. "
        "Extend SIFMA_CLASS_A_SETTLEMENT in mbs_settlement.py."
    )


def _add_one_calendar_month(ym: str) -> str:
    """Advance YYYY-MM by one calendar month."""
    y = int(ym[:4])
    m = int(ym[5:7])
    if m == 12:
        return f"{y + 1}-01"
    return f"{y}-{m + 1:02d}"


def get_yieldbook_tba_contract_month(as_of: Optional[date] = None) -> str:
    """
    Return YYYY-MM for Yieldbook FNMA coupon PROD-{MON} (e.g. 2026-04 -> APR).

    Starts from the next Class A settlement on or after ``as_of`` (see
    get_next_settlement_date). If ``as_of`` is on or after
    (that settle date - TBA_YB_ROLL_DAYS_BEFORE_CLASS_A) calendar days,
    returns the *following* month — matching the usual TBA roll into the next
    settlement month before the current one settles.
    """
    if as_of is None:
        as_of = date.today()
    next_settle_str = get_next_settlement_date(as_of)
    settle_d = date.fromisoformat(next_settle_str)
    month_key = next_settle_str[:7]
    roll_start = settle_d - timedelta(days=TBA_YB_ROLL_DAYS_BEFORE_CLASS_A)
    if as_of >= roll_start:
        return _add_one_calendar_month(month_key)
    return month_key


def get_yieldbook_tba_prod_suffix(as_of: Optional[date] = None) -> str:
    """Three-letter month for PROD-{MON}, e.g. MAY (uses get_yieldbook_tba_contract_month)."""
    ym = get_yieldbook_tba_contract_month(as_of)
    return _MONTH_SUFFIX.get(ym[5:7], "APR")


def get_yieldbook_tba_settlement_date(as_of: Optional[date] = None) -> str:
    """
    SIFMA Class A YYYY-MM-DD for the same contract month as PROD-{MON}
    (get_yieldbook_tba_contract_month). Use this with Yieldbook when settlement
    must match the rolled ticker month.
    """
    if as_of is None:
        as_of = date.today()
    ym = get_yieldbook_tba_contract_month(as_of)
    s = get_settlement_date(ym)
    if s:
        return s
    raise ValueError(
        f"No SIFMA Class A date for contract month {ym}. "
        "Extend SIFMA_CLASS_A_SETTLEMENT in mbs_settlement.py."
    )


def get_latest_class_a_settlement_before(as_of: Optional[date] = None) -> Optional[str]:
    """
    Most recent SIFMA Class A settlement date strictly before ``as_of`` (YYYY-MM-DD),
    or None if no date in the table qualifies.
    """
    if as_of is None:
        as_of = date.today()
    best: Optional[date] = None
    for settle_str in SIFMA_CLASS_A_SETTLEMENT.values():
        d = date.fromisoformat(settle_str)
        if d < as_of and (best is None or d > best):
            best = d
    return best.isoformat() if best else None


def get_last_business_day(as_of: Optional[date] = None) -> date:
    """
    Return the last US business day on or before as_of (weekends excluded; no holiday calendar).
    """
    if as_of is None:
        as_of = date.today()
    d = as_of
    while d.weekday() >= 5:  # 5=Saturday, 6=Sunday
        d -= timedelta(days=1)
    return d


def get_last_business_day_iso(as_of: Optional[date] = None) -> str:
    """Return last business day as YYYY-MM-DD for API pricing date (e.g. last close)."""
    return get_last_business_day(as_of).strftime("%Y-%m-%d")


# Use in Yieldbook API:
# - pricing_date = get_last_business_day_iso()  -> last business day close for TBA
# - settlement_date = get_next_settlement_date() -> SIFMA Class A settle for the trade
# - PROD-{MON} = get_yieldbook_tba_prod_suffix(as_of)  -> ticker month with roll rule


if __name__ == "__main__":
    # Quick sanity check
    t = date(2026, 3, 15)
    print("As of 2026-03-15:")
    print("  Last business day:", get_last_business_day_iso(t))
    print("  Next settle (Class A):", get_next_settlement_date(t))
    print("  Latest settle before as_of:", get_latest_class_a_settlement_before(t))
    print("  Yieldbook PROD month:", get_yieldbook_tba_contract_month(t), get_yieldbook_tba_prod_suffix(t))
    print("As of 2026-04-11 (2 days before Apr 2026 settle):")
    t2 = date(2026, 4, 11)
    print("  Next settle (Class A):", get_next_settlement_date(t2))
    print("  Yieldbook PROD month:", get_yieldbook_tba_contract_month(t2), get_yieldbook_tba_prod_suffix(t2))
    print("As of 2026-04-13:")
    print("  Next settle (Class A):", get_next_settlement_date(date(2026, 4, 13)))
