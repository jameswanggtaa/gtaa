"""Allow `python -m schwab_bot` to launch the poll loop."""

from schwab_bot.bot import main

raise SystemExit(main())
