#!/usr/bin/env python3
"""
Naive regular-session market-hours check: Mon-Fri 9:30-16:00 America/New_York.
Covers both NYSE/NASDAQ and TSX (same regular-session hours).

KNOWN LIMITATION: this does NOT know about market holidays (Christmas, Good
Friday, Thanksgiving, etc.) or early-close days. On those days it will
incorrectly report the market as open. Good enough to stop the stock bots
from "trading" every night and every weekend; if you're dry-running around a
known holiday, expect a stray iteration that finds no real price movement
(harmless -- it just won't fill anything, since nothing is actually moving).
"""
from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_OPEN = dtime(9, 30)
_CLOSE = dtime(16, 0)


def market_open_now(now: datetime | None = None) -> bool:
    now = (now or datetime.now(_ET)).astimezone(_ET)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return _OPEN <= now.time() <= _CLOSE


if __name__ == "__main__":
    print("market open now:", market_open_now())
