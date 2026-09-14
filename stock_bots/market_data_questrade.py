#!/usr/bin/env python3
"""
Trailing daily closes for the stock bots' adaptive/trend features, sourced
from Questrade's candles endpoint (same OAuth as venue_questrade.get_last_price).
Cached like the crypto market_data.py so the poll loop doesn't refetch every
iteration. Return shape matches market_data.load() exactly, so grid_bot.py's
trend filter / vol-spacing code (which only ever calls the asset-agnostic
market_data.sma()/daily_vol_pct() on the result) works completely unmodified.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import venue_questrade as vq

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "logs", "market_cache.json")
REFRESH_SECONDS = 3600


def _now() -> float:
    return time.time()


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


def _fetch_closes(symbol: str, timeout: int) -> list[float]:
    candles = vq.get_daily_candles(symbol, days=260, timeout=timeout)
    rows = [(c["start"], c["close"]) for c in candles if c.get("close") is not None]
    rows.sort(key=lambda r: r[0])  # oldest -> newest
    closes = [float(c) for _, c in rows]
    if len(closes) < 5:
        raise ValueError(f"only {len(closes)} candles returned for {symbol}")
    return closes


def load(cfg: dict) -> dict | None:
    """Return {"closes": [...oldest->newest...], "as_of": iso, "stale": bool}
    or None on total failure -- callers must degrade gracefully."""
    symbol = cfg.get("asset", "AAPL")
    timeout = int(cfg.get("price_feed", {}).get("timeout_sec", 10))

    cache = _read_cache()
    if cache is not None:
        age = _now() - float(cache.get("fetched_at_epoch", 0))
        if age < REFRESH_SECONDS:
            return {"closes": cache["closes"], "as_of": cache.get("as_of"), "stale": False}

    try:
        closes = _fetch_closes(symbol, timeout)
        _write_cache(closes)
        return {"closes": closes,
                "as_of": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "stale": False}
    except Exception:
        if cache is not None:
            return {"closes": cache["closes"], "as_of": cache.get("as_of"), "stale": True}
        return None
