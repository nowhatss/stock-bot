#!/usr/bin/env python3
"""
Trailing daily closes for the grid bot's adaptive features (trend filter,
volatility-scaled spacing).

Source: Coinbase Exchange PUBLIC candles endpoint -- no auth, no API key.
    https://api.exchange.coinbase.com/products/<PID>/candles?granularity=86400

Cached to logs/market_cache.json and refreshed at most once per hour, so the
main 60s poll loop does not hammer the endpoint. On a fetch failure it falls
back to the (stale) cache; if there is no cache either it returns None and the
caller degrades gracefully (adaptive features simply do nothing).

Stdlib only.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "logs", "market_cache.json")
REFRESH_SECONDS = 3600
_CANDLE_URL = "https://api.exchange.coinbase.com/products/{pid}/candles?granularity=86400"


def _now() -> float:
    return time.time()


def _fetch_closes(product_id: str, timeout: int) -> list[float]:
    url = _CANDLE_URL.format(pid=product_id)
    req = Request(url, headers={"User-Agent": "eth-grid-bot/market-data"})
    with urlopen(req, timeout=timeout) as resp:
        rows = json.loads(resp.read().decode("utf-8"))
    # rows: [[time, low, high, open, close, volume], ...] newest-first
    rows = [r for r in rows if isinstance(r, list) and len(r) >= 5]
    rows.sort(key=lambda r: r[0])                # oldest -> newest
    closes = [float(r[4]) for r in rows]
    if len(closes) < 5:
        raise ValueError(f"only {len(closes)} candles returned for {product_id}")
    return closes


def _read_cache() -> dict | None:
    if not os.path.exists(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH, encoding="utf-8-sig") as fh:
            c = json.load(fh)
        if isinstance(c.get("closes"), list) and c["closes"]:
            return c
    except Exception:
        return None
    return None


def _write_cache(closes: list[float]) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    payload = {
        "as_of": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "fetched_at_epoch": _now(),
        "closes": closes,
    }
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, CACHE_PATH)


def load(cfg: dict) -> dict | None:
    """Return {"closes": [...oldest->newest...], "as_of": iso, "stale": bool} or None."""
    product_id = cfg.get("asset", "ETH-USD")
    timeout = int(cfg.get("price_feed", {}).get("timeout_sec", 10))

    cache = _read_cache()
    if cache is not None:
        age = _now() - float(cache.get("fetched_at_epoch", 0))
        if age < REFRESH_SECONDS:
            return {"closes": cache["closes"], "as_of": cache.get("as_of"), "stale": False}

    try:
        closes = _fetch_closes(product_id, timeout)
        _write_cache(closes)
        return {"closes": closes,
                "as_of": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "stale": False}
    except Exception:
        if cache is not None:               # fall back to stale data
            return {"closes": cache["closes"], "as_of": cache.get("as_of"), "stale": True}
        return None


# --------------------------------------------------------------------------- #
# indicators -- operate on an oldest->newest list of closes
# --------------------------------------------------------------------------- #
def sma(closes: list[float], n: int) -> float | None:
    if not closes or n <= 0 or len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def daily_vol_pct(closes: list[float], n: int) -> float | None:
    """Standard deviation of the last n daily % returns, in percent."""
    if not closes or n <= 1 or len(closes) < n + 1:
        return None
    window = closes[-(n + 1):]
    rets = [(window[i] / window[i - 1] - 1.0) for i in range(1, len(window))]
    if len(rets) < 2:
        return None
    return statistics.pstdev(rets) * 100.0


if __name__ == "__main__":
    import sys
    cfg = {"asset": sys.argv[1] if len(sys.argv) > 1 else "ETH-USD",
           "price_feed": {"timeout_sec": 10}}
    md = load(cfg)
    if not md:
        print("market data unavailable")
        raise SystemExit(1)
    c = md["closes"]
    print(f"closes: {len(c)}  as_of {md['as_of']}  stale={md['stale']}")
    print(f"latest close   : {c[-1]:.2f}")
    print(f"SMA(20)        : {sma(c, 20):.2f}")
    print(f"SMA(50)        : {sma(c, 50):.2f}")
    print(f"daily vol(14)  : {daily_vol_pct(c, 14):.2f}%")
    print(f"daily vol(30)  : {daily_vol_pct(c, 30):.2f}%")
