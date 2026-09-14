#!/usr/bin/env python3
"""
Questrade API adapter -- READ ONLY. Quotes, symbol lookup, daily candles.
No order placement of any kind is implemented here; this module exists purely
to feed the stock bots' dry-run price/market-data needs from a real venue.

Auth: Questrade's OAuth2 refresh-token flow (no client secret / app review
needed for personal use):
  1. Log into Questrade -> My Accounts -> App Hub -> "Personal apps" ->
     generate a new refresh token.
  2. Save JUST the token string (nothing else) into questrade_refresh_token.txt
     in this folder. That file is gitignored.
  3. First run exchanges it for an access token + account-specific api_server,
     and caches both (logs/questrade_token_cache.json, also gitignored).

Questrade ROTATES the refresh token every time it's used -- this file is
overwritten with the newest token automatically after every refresh. Don't
hand-edit questrade_refresh_token.txt while a bot is running, and don't reuse
the same token file from two bots refreshing at the same instant (fine here,
since the grid and trend stock bots each poll independently but infrequently
enough that a collision is very unlikely; if you ever see repeated auth
failures from both at once, stagger their poll intervals).

Quotes are real-time only if the Questrade account has a real-time data
package; otherwise expect ~15-minute-delayed data. Fine for a dry run -- just
don't mistake it for a live price feed.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "questrade_refresh_token.txt")
TOKEN_CACHE_FILE = os.path.join(HERE, "logs", "questrade_token_cache.json")


class QuestradeAuthError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #
def _read_refresh_token() -> str:
    if not os.path.exists(TOKEN_FILE):
        raise QuestradeAuthError(
            f"No Questrade refresh token found at {TOKEN_FILE}. Get one from "
            f"Questrade -> My Accounts -> App Hub -> \"Personal apps\" -> "
            f"generate a new token, and save just the token string into that "
            f"file (see stock_bots/README.md)."
        )
    with open(TOKEN_FILE, encoding="utf-8-sig") as fh:
        token = fh.read().strip()
    if not token:
        raise QuestradeAuthError(f"{TOKEN_FILE} is empty.")
    return token


def _save_refresh_token(token: str) -> None:
    with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
        fh.write(token.strip() + "\n")


def _load_token_cache() -> dict | None:
    if not os.path.exists(TOKEN_CACHE_FILE):
        return None
    try:
        with open(TOKEN_CACHE_FILE, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return None


def _save_token_cache(access_token: str, api_server: str, expires_at: float) -> None:
    os.makedirs(os.path.dirname(TOKEN_CACHE_FILE), exist_ok=True)
    tmp = TOKEN_CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"access_token": access_token, "api_server": api_server,
                   "expires_at": expires_at}, fh)
    os.replace(tmp, TOKEN_CACHE_FILE)


def _refresh_access_token(timeout: int = 10) -> tuple[str, str]:
    """Exchange the refresh token for a fresh access token + api_server, and
    immediately persist the ROTATED refresh token Questrade hands back --
    the old one is invalid as soon as this call succeeds."""
    token = _read_refresh_token()
    url = "https://login.questrade.com/oauth2/token?" + urlencode(
        {"grant_type": "refresh_token", "refresh_token": token})
    req = Request(url, headers={"User-Agent": "stock-bots/questrade"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise QuestradeAuthError(
            f"Questrade token refresh failed: {exc!r}. The refresh token may "
            f"be expired or already used up -- generate a new one from "
            f"Questrade's App Hub and replace questrade_refresh_token.txt."
        ) from exc

    new_refresh = data.get("refresh_token")
    if new_refresh:
        _save_refresh_token(new_refresh)
    access_token = data["access_token"]
    api_server = data["api_server"]
    expires_at = time.time() + int(data.get("expires_in", 1800)) - 60  # refresh a minute early
    _save_token_cache(access_token, api_server, expires_at)
    return access_token, api_server


def _get_access_token(timeout: int = 10) -> tuple[str, str]:
    cache = _load_token_cache()
    if cache and cache.get("access_token") and time.time() < float(cache.get("expires_at", 0)):
        return cache["access_token"], cache["api_server"]
    return _refresh_access_token(timeout=timeout)


def _api_get(path: str, timeout: int = 10) -> dict:
    access_token, api_server = _get_access_token(timeout=timeout)
    url = api_server.rstrip("/") + "/" + path.lstrip("/")
    req = Request(url, headers={"Authorization": f"Bearer {access_token}",
                                 "User-Agent": "stock-bots/questrade"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        # one retry after a forced refresh, in case the cached token expired early
        access_token, api_server = _refresh_access_token(timeout=timeout)
        url = api_server.rstrip("/") + "/" + path.lstrip("/")
        req = Request(url, headers={"Authorization": f"Bearer {access_token}",
                                     "User-Agent": "stock-bots/questrade"})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# market data
# --------------------------------------------------------------------------- #
_SYMBOL_ID_CACHE: dict[str, int] = {}


def get_symbol_id(symbol: str, timeout: int = 10) -> int:
    if symbol in _SYMBOL_ID_CACHE:
        return _SYMBOL_ID_CACHE[symbol]
    data = _api_get(f"v1/symbols?names={symbol}", timeout=timeout)
    matches = data.get("symbols", [])
    if not matches:
        raise ValueError(f"Questrade returned no symbol match for {symbol!r}")
    sid = int(matches[0]["symbolId"])
    _SYMBOL_ID_CACHE[symbol] = sid
    return sid


def get_last_price(symbol: str, timeout: int = 10) -> float:
    sid = get_symbol_id(symbol, timeout=timeout)
    data = _api_get(f"v1/markets/quotes/{sid}", timeout=timeout)
    quotes = data.get("quotes", [])
    if not quotes:
        raise ValueError(f"Questrade returned no quote for {symbol!r}")
    q = quotes[0]
    price = q.get("lastTradePrice")
    if price is None:            # e.g. before the first trade of the session
        price = q.get("bidPrice") or q.get("askPrice")
    if price is None:
        raise ValueError(f"Questrade quote for {symbol!r} has no usable price: {q}")
    return float(price)


def get_quotes_batch(symbols: list[str], timeout: int = 10) -> dict[str, float]:
    """One Questrade API call for many symbols at once (batched by symbolId),
    instead of one call per symbol -- used by the watchlist bot, which needs a
    fresh price for its whole watchlist every poll. Returns {symbol: price};
    a symbol is silently omitted if Questrade has no usable price for it this
    call (caller should treat a missing symbol as 'skip this poll for it').

    Uses the `?ids=id1,id2,...` query-parameter form -- confirmed directly
    against a live account. The comma-separated-ids *path* form
    (v1/markets/quotes/id1,id2,...) looks superficially similar but returns a
    400 from Questrade (misleadingly reported as an Accept-header/content-type
    error), so don't switch back to it.
    """
    ids: list[int] = []
    id_to_symbol: dict[int, str] = {}
    for sym in symbols:
        try:
            sid = get_symbol_id(sym, timeout=timeout)
        except Exception:
            continue
        ids.append(sid)
        id_to_symbol[sid] = sym
    if not ids:
        return {}
    id_str = ",".join(str(i) for i in ids)
    data = _api_get(f"v1/markets/quotes?ids={id_str}", timeout=timeout)
    out: dict[str, float] = {}
    for q in data.get("quotes", []):
        sid = q.get("symbolId")
        sym = id_to_symbol.get(sid)
        if not sym:
            continue
        price = q.get("lastTradePrice")
        if price is None:
            price = q.get("bidPrice") or q.get("askPrice")
        if price is not None:
            out[sym] = float(price)
    return out


def get_daily_candles(symbol: str, days: int = 260, timeout: int = 10) -> list[dict]:
    """Daily OHLC candles, oldest -> newest. `days` is calendar days requested;
    padded to account for weekends/holidays where nothing trades."""
    sid = get_symbol_id(symbol, timeout=timeout)
    end = _dt.datetime.now(_dt.timezone.utc)
    start = end - _dt.timedelta(days=int(days * 1.6 + 10))
    params = urlencode({
        "startTime": start.strftime("%Y-%m-%dT%H:%M:%S-00:00"),
        "endTime": end.strftime("%Y-%m-%dT%H:%M:%S-00:00"),
        "interval": "OneDay",
    })
    data = _api_get(f"v1/markets/candles/{sid}?{params}", timeout=timeout)
    return data.get("candles", [])


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(f"symbolId({sym}) = {get_symbol_id(sym)}")
    print(f"last price      = {get_last_price(sym)}")
    candles = get_daily_candles(sym, days=30)
    print(f"daily candles   = {len(candles)} (last: {candles[-1] if candles else None})")
