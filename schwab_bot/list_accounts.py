"""List linked Schwab accounts and hashed account numbers.

Usage:
  python -m schwab_bot.list_accounts
"""

from __future__ import annotations

import json
import logging
import os
import sys

from schwab_bot.auth import auth_from_env
from schwab_bot.client import SchwabClient

LOG = logging.getLogger("schwab_bot.list_accounts")


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if not load_dotenv("schwab_bot/.env"):
        load_dotenv(".env")


def main() -> int:
    _load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")

    # account hash unused for /accounts list; placeholder is fine
    client = SchwabClient(auth_from_env(), account_hash="unused")
    accounts = client.accounts()
    print(json.dumps(accounts, indent=2))

    print("\n# Copy one accountNumber hash into SCHWAB_ACCOUNT_HASH:", file=sys.stderr)
    for item in accounts:
        sec = item.get("securitiesAccount") or {}
        print(
            f"# type={sec.get('type')} accountNumber={sec.get('accountNumber')}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
