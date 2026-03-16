"""
MBS TBA settlement dates — SIFMA Class A.

Settlement dates are from SIFMA MBS Notification and Settlement Dates:
https://www.sifma.org/resources/guides-playbooks/mbs-notification-and-settlement-dates

Class A settlement date is used as the "most close settle date" for TBA pricing
(e.g. if today is 3/15/2026, next settle is Apr-26 Class A = 2026-04-13).
"""

from datetime import date, timedelta
from typing import Optional

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


if __name__ == "__main__":
    # Quick sanity check
    t = date(2026, 3, 15)
    print("As of 2026-03-15:")
    print("  Last business day:", get_last_business_day_iso(t))
    print("  Next settle (Class A):", get_next_settlement_date(t))
    print("As of 2026-04-13:")
    print("  Next settle (Class A):", get_next_settlement_date(date(2026, 4, 13)))
