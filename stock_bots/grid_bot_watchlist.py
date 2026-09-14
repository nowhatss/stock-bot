#!/usr/bin/env python3
"""
Watchlist grid bot -- DRY RUN ONLY.

Scans a list of stocks each poll (via Questrade) instead of trading one fixed
symbol, and buys the dip in whichever symbol currently looks best by the
active ranking rule. Every symbol in the watchlist gets its own grid (anchor
+ rungs, same mechanics/fill modelling as grid_bot.py/grid_bot_stock.py,
reused here rather than reimplemented); a rung still has to be armed, primed,
and actually reached for a symbol to be "eligible" at all -- the ranking rule
only decides which symbol wins the next tranche when more than one qualifies
on the same poll, and a shared capital pool + risk rails apply across the
whole watchlist rather than to one asset.

Two ranking rules are implemented; only one is active at a time
(config ranking.mode):
  - "vol_normalized_dip" (ACTIVE by default) -- ranks by how far each symbol
    has fallen from its own recent high, scaled by its own volatility, so a
    5% drop in a calm stock counts for more than a 5% drop in a choppy one.
  - "grid_depth" (built, INACTIVE) -- ranks by how many grid rungs deep each
    symbol's price has fallen. Pure grid mechanics, no volatility involved.
    Switch ranking.mode to "grid_depth" in watchlist_config.json to try it.

If a stop-loss fires, that symbol also gets a non-pinging price check-in
posted to Discord every execution.price_notify_interval_sec (default 300s /
5 min) until market close that day -- lets you watch how it's behaving right
after getting stopped out, without a steady drip of updates on quiet days.

Needs a Questrade refresh token -- see stock_bots/README.md.

Run:  python grid_bot_watchlist.py             (continuous loop)
      python grid_bot_watchlist.py --once      (single iteration; no-op if market closed)
      python grid_bot_watchlist.py --status
      python grid_bot_watchlist.py --summary
      python grid_bot_watchlist.py --reset
      python grid_bot_watchlist.py --test-notify

THIS BUILD PLACES NO REAL ORDERS.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT)

import grid_bot as g  # noqa: E402  -- reused: logging, Discord, fill modelling, fee math
import venue_questrade as vq  # noqa: E402
from market_hours import market_open_now  # noqa: E402

CONFIG_PATH = os.path.join(HERE, "watchlist_config.json")
STATE_PATH = os.path.join(HERE, "watchlist_state.json")
TRADES_CSV = os.path.join(HERE, "logs", "watchlist_trades.csv")
DAILY_CSV = os.path.join(HERE, "logs", "watchlist_daily_summary.csv")
CANDLE_CACHE = os.path.join(HERE, "logs", "watchlist_candle_cache.json")

g.EVENTS_LOG = os.path.join(HERE, "logs", "watchlist_events.log")
g.HEARTBEAT_LOG = os.path.join(HERE, "logs", "watchlist_heartbeat.log")


def now_utc():
    return g.now_utc()


def iso(dt):
    return g.iso(dt)


# --------------------------------------------------------------------------- #
# config / state
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
        return json.load(fh)


def default_symbol_state() -> dict:
    return {
        "anchor_price": None,
        "grid_levels": [],
        "recent_high": None,
        "vol_pct": None,
        "last_price": None,
        "prev_price": None,
        "candles_as_of": None,
        "stop_loss_watch_date": None,
    }


def default_state(cfg: dict) -> dict:
    return {
        "created_at": iso(now_utc()),
        "symbols": {sym: default_symbol_state() for sym in cfg["watchlist_symbols"]},
        "open_tranches": [],
        "closed_tranches": [],
        "realized_pnl_usd": 0.0,
        "fees_paid_usd": 0.0,
        "day": {"date": g.today_str(), "buy_count": 0, "sell_count": 0, "realized_pnl_usd": 0.0},
        "halted": False,
        "paused": False,
        "halt_reason": None,
        "last_iteration_at": None,
        "_fill_calls": 0,
        "_last_skips": {},
    }


def load_state(cfg: dict) -> dict:
    if not os.path.exists(STATE_PATH):
        return default_state(cfg)
    with open(STATE_PATH, encoding="utf-8-sig") as fh:
        state = json.load(fh)
    # symbols added to the watchlist since the last run get initialised on the fly
    for sym in cfg["watchlist_symbols"]:
        state["symbols"].setdefault(sym, default_symbol_state())
    return state


def save_state(state: dict) -> None:
    state["last_iteration_at"] = iso(now_utc())
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    # os.replace can transiently fail on Windows if something else (OneDrive
    # sync, antivirus) briefly holds a handle on the destination -- a backtest
    # hits this file thousands of times in quick succession, so retry a few
    # times with a short backoff instead of crashing the whole run over it.
    for attempt in range(5):
        try:
            os.replace(tmp, STATE_PATH)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


# --------------------------------------------------------------------------- #
# per-symbol market data: recent high + volatility, refreshed at most hourly
# --------------------------------------------------------------------------- #
def _read_candle_cache() -> dict:
    if not os.path.exists(CANDLE_CACHE):
        return {}
    try:
        with open(CANDLE_CACHE, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_candle_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CANDLE_CACHE), exist_ok=True)
    tmp = CANDLE_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    os.replace(tmp, CANDLE_CACHE)


def refresh_symbol_market_data(state: dict, cfg: dict, symbol: str) -> None:
    """Updates state.symbols[symbol].recent_high / vol_pct from Questrade daily
    candles, at most once an hour per symbol (cached to disk)."""
    r = cfg["ranking"]
    lookback = max(int(r.get("high_lookback_days", 20)), int(r.get("vol_lookback_days", 20)))
    cache = _read_candle_cache()
    entry = cache.get(symbol)
    fresh = entry and (time.time() - float(entry.get("fetched_at_epoch", 0)) < 3600)

    if not fresh:
        try:
            candles = vq.get_daily_candles(symbol, days=lookback + 10,
                                           timeout=cfg["price_feed"]["timeout_sec"])
            rows = [(c["start"], float(c["high"]), float(c["close"]))
                    for c in candles if c.get("close") is not None and c.get("high") is not None]
            rows.sort(key=lambda row: row[0])
            if len(rows) >= 5:
                entry = {
                    "fetched_at_epoch": time.time(),
                    "highs": [h for _, h, _ in rows],
                    "closes": [c for _, _, c in rows],
                }
                cache[symbol] = entry
                _write_candle_cache(cache)
        except Exception as exc:  # noqa: BLE001
            g.log_event(f"[{symbol}] candle refresh failed ({exc!r}); "
                        f"using stale/no data for ranking", level="WARN")
            if entry is None:
                return

    if not entry:
        return
    highs = entry["highs"][-int(r.get("high_lookback_days", 20)):]
    closes = entry["closes"]
    import market_data  # parent module -- pure sma/daily_vol_pct math only, asset-agnostic
    vol = market_data.daily_vol_pct(closes, int(r.get("vol_lookback_days", 20)))

    sdata = state["symbols"][symbol]
    sdata["recent_high"] = max(highs) if highs else sdata.get("recent_high")
    sdata["vol_pct"] = vol if vol is not None else sdata.get("vol_pct")
    sdata["candles_as_of"] = entry.get("fetched_at_epoch")


def ensure_grid_for_symbol(state: dict, cfg: dict, symbol: str, price: float) -> None:
    """First init: build the symbol's grid. After that: re-anchor it on a
    breakout, but ONLY while flat in that symbol (reshaping while holding
    would orphan the open tranche's rung tracking) -- same rule the crypto
    bot uses. Without this, a symbol that runs away from its original anchor
    (e.g. a stock on a long uptrend) would simply stop generating any new
    entries for the rest of the bot's life, since its rungs -- all below a
    now-stale anchor -- would never be reached again. Off unless
    reanchor.enabled is true."""
    sdata = state["symbols"][symbol]
    n = cfg["grid"]["num_levels"]
    spacing = cfg["grid"]["grid_spacing_pct"]

    if sdata["anchor_price"] is None:
        sdata["anchor_price"] = round(price, 4)
        sdata["grid_levels"] = g.build_levels(price, spacing, n)
        g.log_event(f"[{symbol}] grid initialised. anchor={sdata['anchor_price']} "
                   f"levels={[lvl['price'] for lvl in sdata['grid_levels']]}")
        return

    reanchor = cfg.get("reanchor", {}) or {}
    if not reanchor.get("enabled"):
        return
    if any(t["symbol"] == symbol for t in state["open_tranches"]):
        return  # never reshape while holding a position in this symbol

    breakout_pct = float(reanchor.get("breakout_pct", 8.0)) / 100.0
    if breakout_pct <= 0 or price < sdata["anchor_price"] * (1 + breakout_pct):
        return

    old_anchor = sdata["anchor_price"]
    sdata["anchor_price"] = round(price, 4)
    sdata["grid_levels"] = g.build_levels(price, spacing, n)
    msg = (f"[{symbol}] grid re-anchored on breakout: {old_anchor} -> {sdata['anchor_price']} "
          f"levels={[lvl['price'] for lvl in sdata['grid_levels']]}")
    g.log_event(msg)
    g.discord_send(f"♻️ {symbol} grid re-anchored  (watchlist)", msg,
                   color=g.COLOR_INFO, event="reshape")


# --------------------------------------------------------------------------- #
# ranking rules -- pure functions, unit-tested independently in test_watchlist.py
# --------------------------------------------------------------------------- #
def dip_pct(price: float | None, high: float | None) -> float:
    """% below a reference high; 0 if at/above it or no reference yet."""
    if not high or high <= 0 or price is None:
        return 0.0
    return max(0.0, (high - price) / high * 100.0)


def score_vol_normalized_dip(price: float, high: float | None, vol_pct: float | None,
                             min_vol_pct_floor: float, min_dip_pct: float) -> float | None:
    """Option 3 (ACTIVE by default). None if the dip doesn't clear min_dip_pct."""
    d = dip_pct(price, high)
    if d < min_dip_pct:
        return None
    v = max(float(vol_pct) if vol_pct else 0.0, min_vol_pct_floor)
    return d / v


def score_grid_depth(levels: list[dict], price: float) -> float | None:
    """Option 2 (built, inactive). None if no armed+primed rung has been reached."""
    reached = _reached_levels(levels, price)
    if not reached:
        return None
    deepest = max(reached, key=lambda lvl: lvl["index"])
    return float(deepest["index"] + 1)


def _reached_levels(levels: list[dict], price: float | None) -> list[dict]:
    if price is None:
        return []
    return [lvl for lvl in levels
            if not lvl["held"] and lvl.get("primed") and price <= lvl["price"]
            and not g.level_in_cooldown(lvl)]


def eligible_candidates(state: dict, cfg: dict) -> list[tuple[str, float, dict]]:
    """Returns [(symbol, score, level)] for every symbol currently eligible for
    a new tranche (a real armed+primed+reached rung), best-ranked first. The
    ranking rule only decides ORDER among symbols that already qualify -- it
    never makes an otherwise-untouched symbol eligible."""
    mode = cfg["ranking"]["mode"]
    max_per_symbol = int(cfg["risk"].get("max_tranches_per_symbol", 1))
    r = cfg["ranking"]
    out: list[tuple[str, float, dict]] = []
    for sym, sdata in state["symbols"].items():
        price = sdata.get("last_price")
        if price is None:
            continue
        open_count = sum(1 for t in state["open_tranches"] if t["symbol"] == sym)
        if open_count >= max_per_symbol:
            continue
        reached = _reached_levels(sdata.get("grid_levels", []), price)
        if not reached:
            continue
        level = max(reached, key=lambda lvl: lvl["index"])

        if mode == "grid_depth":
            score = float(level["index"] + 1)
        else:  # vol_normalized_dip (default/active)
            score = score_vol_normalized_dip(
                price, sdata.get("recent_high"), sdata.get("vol_pct"),
                float(r.get("min_vol_pct_floor", 0.5)), float(r.get("min_dip_pct", 1.0)),
            )
            if score is None:
                continue
        out.append((sym, score, level))
    out.sort(key=lambda c: c[1], reverse=True)
    return out


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
TRADE_FIELDS = ["timestamp", "mode", "action", "symbol", "level_index", "client_order_id",
                "price", "size_usd", "qty", "fee_usd", "realized_pnl_delta_usd",
                "realized_pnl_total_usd", "note"]


def log_trade(row: dict) -> None:
    os.makedirs(os.path.dirname(TRADES_CSV), exist_ok=True)
    new = g._csv_needs_header(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in TRADE_FIELDS})


# --------------------------------------------------------------------------- #
# core actions
# --------------------------------------------------------------------------- #
def open_tranche(state: dict, cfg: dict, symbol: str, level: dict, price: float) -> None:
    size_usd = cfg["grid"]["tranche_size_usd"]
    ok, reason = g._budget_ok(state, cfg, size_usd)
    if not ok:
        key = f"{symbol}:{level['index']}"
        skips = state.setdefault("_last_skips", {})
        if skips.get(key) != reason:
            skips[key] = reason
            g.log_event(f"BUY skipped {symbol} L{level['index']} ({level['price']}): {reason}")
        return
    state.setdefault("_last_skips", {}).pop(f"{symbol}:{level['index']}", None)

    tp_pct = cfg["grid"]["take_profit_pct"]
    coid = f"wl-buy-{symbol}-L{level['index']}-{uuid.uuid4().hex[:12]}"

    preview = g.preview_order(cfg, "buy", price, size_usd, coid,
                              fee_rate_override=g.fee_rate(cfg, "maker"))
    fill = g.place_order(cfg, preview)

    tranche = {
        "id": coid,
        "symbol": symbol,
        "level_index": level["index"],
        "fill_price": fill["est_fill_price"],
        "fill_kind": "maker",
        "qty": fill["est_qty_eth"],
        "cost_usd": round(size_usd, 6),
        "buy_fee_usd": fill["est_fee_usd"],
        "take_profit_pct": round(tp_pct, 4),
        "take_profit_price": round(fill["est_fill_price"] * (1 + tp_pct / 100.0), 4),
        "opened_at": fill["filled_at"],
    }
    state["open_tranches"].append(tranche)
    level["held"] = True
    state["fees_paid_usd"] += fill["est_fee_usd"]
    state["day"]["buy_count"] += 1

    log_trade({
        "timestamp": fill["filled_at"], "mode": cfg["execution"]["mode"], "action": "BUY",
        "symbol": symbol, "level_index": level["index"], "client_order_id": coid,
        "price": fill["est_fill_price"], "size_usd": size_usd,
        "qty": fill["est_qty_eth"], "fee_usd": fill["est_fee_usd"],
        "realized_pnl_delta_usd": 0.0,
        "realized_pnl_total_usd": round(state["realized_pnl_usd"], 4),
        "note": f"rank_mode={cfg['ranking']['mode']} take_profit_at={tranche['take_profit_price']}",
    })
    g.log_event(
        f"BUY  {symbol} L{level['index']} @ {fill['est_fill_price']}  "
        f"qty={fill['est_qty_eth']}  fee={fill['est_fee_usd']}  "
        f"TP={tranche['take_profit_price']}  deployed=${deployed_usd(state)}"
    )
    g.discord_send(
        f"🟦 BUY  {symbol}  (watchlist, {cfg['ranking']['mode']})",
        fields=[
            ("Symbol", symbol),
            ("Fill price", f"${fill['est_fill_price']:,}"),
            ("Size", f"{fill['est_qty_eth']} shares  (${size_usd})"),
            ("Fee", f"${fill['est_fee_usd']}"),
            ("Take-profit at", f"${tranche['take_profit_price']:,}"),
            ("Deployed", f"${deployed_usd(state)} / ${cfg['risk']['max_capital_deployed_usd']}"),
        ],
        color=g.COLOR_BUY, event="buy",
    )


def close_tranche(state: dict, cfg: dict, tranche: dict, exit_price: float,
                  reason: str, fill_kind: str = "maker") -> None:
    if state["paused"]:
        return
    symbol = tranche["symbol"]
    gross = tranche["qty"] * exit_price
    coid = f"wl-sell-{symbol}-L{tranche['level_index']}-{uuid.uuid4().hex[:12]}"

    preview = g.preview_order(cfg, "sell", gross, gross, coid,
                              fee_rate_override=g.fee_rate(cfg, fill_kind))
    fill = g.place_order(cfg, preview)

    net_proceeds = gross - fill["est_fee_usd"]
    pnl_delta = round(net_proceeds - tranche["cost_usd"], 6)

    state["realized_pnl_usd"] = round(state["realized_pnl_usd"] + pnl_delta, 6)
    state["day"]["realized_pnl_usd"] = round(state["day"]["realized_pnl_usd"] + pnl_delta, 6)
    state["fees_paid_usd"] += fill["est_fee_usd"]
    state["day"]["sell_count"] += 1

    sdata = state["symbols"][symbol]
    for lvl in sdata["grid_levels"]:
        if lvl["index"] == tranche["level_index"]:
            lvl["held"] = False
            lvl.pop("cooldown_until", None)

    is_stop_loss = reason == "stop_loss"
    if is_stop_loss:
        # start a price-watch for this symbol: every price_notify_interval_sec
        # until market close today (see notify_watched_prices) -- cleared
        # automatically once the calendar date rolls over.
        sdata["stop_loss_watch_date"] = g.today_str()

    state["open_tranches"] = [t for t in state["open_tranches"] if t["id"] != tranche["id"]]
    state["closed_tranches"].append({
        **tranche, "sell_price": round(exit_price, 4), "sell_fill_kind": fill_kind,
        "sell_fee_usd": fill["est_fee_usd"], "pnl_usd": pnl_delta,
        "closed_at": fill["filled_at"], "close_reason": reason,
    })
    state["closed_tranches"] = state["closed_tranches"][-250:]

    log_trade({
        "timestamp": fill["filled_at"], "mode": cfg["execution"]["mode"], "action": "SELL",
        "symbol": symbol, "level_index": tranche["level_index"], "client_order_id": coid,
        "price": round(exit_price, 4), "size_usd": round(gross, 6),
        "qty": tranche["qty"], "fee_usd": fill["est_fee_usd"],
        "realized_pnl_delta_usd": pnl_delta,
        "realized_pnl_total_usd": round(state["realized_pnl_usd"], 4),
        "note": f"bought_at={tranche['fill_price']} reason={reason} {fill_kind}",
    })
    g.log_event(
        f"SELL {symbol} L{tranche['level_index']} @ {round(exit_price, 4)}  "
        f"pnl={pnl_delta:+.4f}  realized_total={round(state['realized_pnl_usd'], 4)}"
    )
    if is_stop_loss:
        # Always an alert (pings regardless of whatever notifications.mention_events
        # says about plain "sell" events) -- a stop-loss is a risk event, not routine
        # profit-taking, and shouldn't depend on that config staying a certain way.
        g.discord_send(
            f"🛑 STOP-LOSS HIT  {symbol}  (watchlist)",
            fields=[
                ("Symbol", symbol),
                ("Sell price", f"${round(exit_price, 2):,}"),
                ("Bought at", f"${tranche['fill_price']:,}"),
                ("P&L this tranche", f"{pnl_delta:+.4f} USD"),
                ("Fee", f"${fill['est_fee_usd']}"),
                ("Realized total", f"${round(state['realized_pnl_usd'], 4)}"),
            ],
            color=g.COLOR_ALERT, event="alert",
        )
    else:
        g.discord_send(
            f"{'🟩' if pnl_delta >= 0 else '🟧'} SELL  {symbol}  (watchlist)",
            fields=[
                ("Symbol", symbol),
                ("Sell price", f"${round(exit_price, 2):,}"),
                ("Bought at", f"${tranche['fill_price']:,}"),
                ("P&L this tranche", f"{pnl_delta:+.4f} USD"),
                ("Fee", f"${fill['est_fee_usd']}"),
                ("Realized total", f"${round(state['realized_pnl_usd'], 4)}"),
            ],
            color=g.COLOR_SELL_WIN if pnl_delta >= 0 else g.COLOR_SELL_LOSS, event="sell",
        )


def deployed_usd(state: dict) -> float:
    return round(sum(t["cost_usd"] for t in state["open_tranches"]), 4)


def compute_unrealized(state: dict, cfg: dict) -> float:
    exit_rate = g.fee_rate(cfg, "maker")
    total = 0.0
    for t in state["open_tranches"]:
        price = state["symbols"].get(t["symbol"], {}).get("last_price")
        if price is None:
            continue
        gross = t["qty"] * price
        total += (gross - gross * exit_rate) - t["cost_usd"]
    return round(total, 4)


def check_max_hold(state: dict, cfg: dict) -> None:
    """Profit-lock time-stop: force-close a tranche held longer than
    risk.max_hold_days, but ONLY if it's already up at least
    risk.max_hold_min_profit_pct -- this is NOT a stop-loss. It exists so an
    ambitious take_profit_pct can still run under normal conditions, without
    letting capital sit tied up indefinitely chasing a stretch target once a
    solid, already-real gain is on the table. A stale tranche that's still
    below the profit floor (including an underwater one) is left alone here
    and keeps waiting for its actual take-profit target, same as before --
    this deliberately does not cut losses, only locks in gains that have
    already arrived. Off unless risk.max_hold_enabled is true."""
    risk = cfg["risk"]
    if not risk.get("max_hold_enabled"):
        return
    max_days = float(risk.get("max_hold_days", 10))
    min_profit_pct = float(risk.get("max_hold_min_profit_pct", 4.0))

    for tranche in list(state["open_tranches"]):
        try:
            opened = datetime.fromisoformat(tranche["opened_at"])
        except (ValueError, KeyError):
            continue
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        age_days = (now_utc() - opened).total_seconds() / 86400.0
        if age_days < max_days:
            continue

        price = state["symbols"].get(tranche["symbol"], {}).get("last_price")
        if price is None:
            continue
        gain_pct = (price - tranche["fill_price"]) / tranche["fill_price"] * 100.0
        if gain_pct < min_profit_pct:
            continue  # not yet at the profit floor -- keep waiting for the real target

        slip = float(cfg["execution"].get("market_slippage_bps", 0)) / 10000.0
        exit_px = price * (1 - slip)
        close_tranche(state, cfg, tranche, exit_px, reason=f"max_hold {age_days:.1f}d", fill_kind="taker")


def check_stop_loss(state: dict, cfg: dict, *,
                    price_bands: dict[str, tuple[float, float]] | None = None) -> None:
    """Real, immediate stop-loss: force-closes a tranche the moment price falls
    risk.stop_loss_pct below its OWN entry price -- a stop order (taker +
    slippage), triggered as soon as the band reaches it, not a probabilistic
    resting fill and not gated by how long it's been held. This is the fast
    crash-protection counterpart to check_max_hold's profit-lock: max_hold
    only ever locks in a gain that's already arrived and deliberately leaves
    losers alone; this is what actually caps the downside on a single
    position. Off unless risk.stop_loss_enabled is true.

    A stop-out fires an @everyone Discord alert regardless of
    notifications.mention_events, since this is a risk event, not routine
    profit-taking, and also starts a price-watch on that symbol (see
    notify_watched_prices) for the rest of the trading day. No cooldown on
    re-entry -- if the symbol's rung is still at/below its (re-armed) level
    on the very next poll, it can re-buy immediately."""
    price_bands = price_bands or {}
    risk = cfg["risk"]
    if not risk.get("stop_loss_enabled"):
        return
    stop_pct = float(risk.get("stop_loss_pct", 10.0)) / 100.0
    if stop_pct <= 0:
        return

    for tranche in list(state["open_tranches"]):
        sym = tranche["symbol"]
        sdata = state["symbols"].get(sym, {})
        price = sdata.get("last_price")
        if price is None:
            continue
        if sym in price_bands:
            lo, _hi = price_bands[sym]
        else:
            prev = sdata.get("prev_price")
            lo = min(prev, price) if prev is not None else price

        stop_price = tranche["fill_price"] * (1 - stop_pct)
        if lo > stop_price:
            continue

        slip = float(cfg["execution"].get("market_slippage_bps", 0)) / 10000.0
        exit_px = min(price, stop_price) * (1 - slip)
        close_tranche(state, cfg, tranche, exit_px, reason="stop_loss", fill_kind="taker")


def process_fills(state: dict, cfg: dict, *,
                  price_bands: dict[str, tuple[float, float]] | None = None) -> None:
    """`price_bands` (symbol -> (low, high)) lets a backtest feed a real OHLC
    range per symbol instead of just the poll-to-poll [prev_price, last_price]
    band -- same idea as grid_bot.py's `price_band` param, just one per symbol.
    Live callers never pass this, so live behavior is unchanged."""
    price_bands = price_bands or {}

    # 1) exits: take-profit for every open tranche, using its OWN symbol's band
    for tranche in list(state["open_tranches"]):
        sym = tranche["symbol"]
        sdata = state["symbols"].get(sym, {})
        price = sdata.get("last_price")
        if price is None:
            continue
        if sym in price_bands:
            lo, hi = price_bands[sym]
        else:
            prev = sdata.get("prev_price")
            lo, hi = (min(prev, price), max(prev, price)) if prev is not None else (price, price)
        tp = tranche["take_profit_price"]
        if hi >= tp and g.resting_order_fills(state, cfg, tp, hi, "sell"):
            close_tranche(state, cfg, tranche, tp, reason="take_profit", fill_kind="maker")

    # 1a) immediate stop-loss (off by default -- see check_stop_loss)
    check_stop_loss(state, cfg, price_bands=price_bands)

    # 1b) profit-lock time-stop (off by default -- see check_max_hold)
    check_max_hold(state, cfg)

    # 2) mark primed rungs across the whole watchlist
    for sdata in state["symbols"].values():
        price = sdata.get("last_price")
        if price is None:
            continue
        for lvl in sdata.get("grid_levels", []):
            if price > lvl["price"]:
                lvl["primed"] = True

    # 3) each symbol's reached rung independently decides whether it "filled"
    #    this poll (same probabilistic resting-order logic as every other bot);
    #    the ranking rule then decides who gets the shared, limited budget
    #    first when more than one symbol filled on the same poll.
    filled: list[tuple[str, float, dict]] = []
    for sym, score, level in eligible_candidates(state, cfg):
        sdata = state["symbols"][sym]
        price = sdata.get("last_price")
        if sym in price_bands:
            lo, _hi = price_bands[sym]
        else:
            prev = sdata.get("prev_price")
            lo = min(prev, price) if prev is not None else price
        if g.resting_order_fills(state, cfg, level["price"], lo, "buy"):
            filled.append((sym, score, level))

    for sym, score, level in filled:  # already best-ranked first
        before = len(state["open_tranches"])
        open_tranche(state, cfg, sym, level, state["symbols"][sym]["last_price"])
        if len(state["open_tranches"]) == before:
            break  # budget exhausted -- remaining filled candidates wait for next poll


# --------------------------------------------------------------------------- #
# daily rollover + risk rails
# --------------------------------------------------------------------------- #
def daily_summary_row(state: dict, cfg: dict) -> dict:
    unreal = compute_unrealized(state, cfg)
    return {
        "date": state["day"]["date"],
        "open_tranches": len(state["open_tranches"]),
        "symbols_held": ",".join(sorted({t["symbol"] for t in state["open_tranches"]})),
        "deployed_usd": deployed_usd(state),
        "realized_pnl_day_usd": round(state["day"]["realized_pnl_usd"], 4),
        "unrealized_pnl_usd": unreal,
        "fees_paid_total_usd": round(state["fees_paid_usd"], 4),
        "buys_today": state["day"]["buy_count"],
        "sells_today": state["day"]["sell_count"],
        "halted": state["halted"], "paused": state["paused"],
        "halt_reason": state["halt_reason"] or "",
        "ranking_mode": cfg["ranking"]["mode"],
    }


def print_summary(state: dict, cfg: dict) -> None:
    r = daily_summary_row(state, cfg)
    print(f"\n=== WATCHLIST SUMMARY {r['date']} (UTC) ===")
    for k, v in r.items():
        print(f"  {k:24s}: {v}")
    tot = r["realized_pnl_day_usd"] + r["unrealized_pnl_usd"]
    print(f"  {'total_pnl_day_usd':24s}: {round(tot, 4)}  "
          f"({round(100 * tot / cfg['allocated_capital_usd'], 2)}% of allocated)")
    print("=" * 40 + "\n")


def append_daily_summary(state: dict, cfg: dict) -> None:
    os.makedirs(os.path.dirname(DAILY_CSV), exist_ok=True)
    row = daily_summary_row(state, cfg)
    new = g._csv_needs_header(DAILY_CSV)
    with open(DAILY_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)


def notify_daily_summary(state: dict, cfg: dict) -> None:
    r = daily_summary_row(state, cfg)
    tot = r["realized_pnl_day_usd"] + r["unrealized_pnl_usd"]
    flags = [f for f, on in (("HALTED", r["halted"]), ("PAUSED", r["paused"])) if on]
    g.discord_send(
        f"📊 Watchlist daily summary {r['date']} (UTC)",
        description=" ".join(flags) if flags else "",
        fields=[
            ("Open positions", f"{r['open_tranches']} ({r['symbols_held'] or 'none'})"),
            ("Deployed", f"${r['deployed_usd']}"),
            ("Realized (day)", f"${r['realized_pnl_day_usd']}"),
            ("Unrealized", f"${r['unrealized_pnl_usd']}"),
            ("Total P&L (day)", f"${round(tot, 4)}  "
             f"({round(100 * tot / cfg['allocated_capital_usd'], 2)}%)"),
            ("Buys / Sells today", f"{r['buys_today']} / {r['sells_today']}"),
            ("Ranking mode", r["ranking_mode"]),
        ],
        color=g.COLOR_ALERT if flags else g.COLOR_INFO,
        event="alert" if flags else "daily",
    )


def daily_rollover(state: dict, cfg: dict) -> None:
    d = g.today_str()
    if state["day"]["date"] == d:
        return
    g.log_event(f"Day rollover {state['day']['date']} -> {d}. "
               f"buys={state['day']['buy_count']} sells={state['day']['sell_count']} "
               f"realized={round(state['day']['realized_pnl_usd'], 4)}")
    append_daily_summary(state, cfg)
    print_summary(state, cfg)
    notify_daily_summary(state, cfg)
    state["day"] = {"date": d, "buy_count": 0, "sell_count": 0, "realized_pnl_usd": 0.0}
    if state["paused"]:
        state["paused"] = False
        g.log_event("Daily-loss pause cleared by day rollover.")
        g.discord_send("▶️ Daily-loss pause cleared by UTC day rollover — trading resumes.",
                       color=g.COLOR_INFO, event="alert")


def check_risk_rails(state: dict, cfg: dict) -> None:
    risk = cfg["risk"]
    alloc = cfg["allocated_capital_usd"]
    unreal = compute_unrealized(state, cfg)
    total_pnl = state["realized_pnl_usd"] + unreal
    drawdown_pct = -100.0 * total_pnl / alloc

    if not state["halted"] and drawdown_pct > risk["hard_stop_loss_pct"]:
        state["halted"] = True
        state["halt_reason"] = "HARD_STOP_LOSS"
        g.alert(f"HARD STOP-LOSS hit (watchlist bot): total drawdown {drawdown_pct:.1f}% > "
               f"{risk['hard_stop_loss_pct']}% of allocated. Halting all BUYING across the "
               f"whole watchlist. Open tranches kept; take-profit sells still allowed. "
               f"Manual review required.")

    day_pnl = state["day"]["realized_pnl_usd"] + unreal
    day_loss_pct = -100.0 * day_pnl / alloc
    if not state["paused"] and day_loss_pct > risk["max_daily_loss_pct"]:
        state["paused"] = True
        g.alert(f"MAX DAILY LOSS hit (watchlist bot): day P&L {day_loss_pct:.1f}% loss > "
               f"{risk['max_daily_loss_pct']}%. Pausing (no buys, no sells) "
               f"until manual review. Clear 'paused' in watchlist_state.json to resume.")


def heartbeat(state: dict, cfg: dict) -> None:
    unreal = compute_unrealized(state, cfg)
    held = sorted({t["symbol"] for t in state["open_tranches"]})
    flags = []
    if state["halted"]:
        flags.append("HALTED")
    if state["paused"]:
        flags.append("PAUSED")
    line = (
        f"{iso(now_utc())} [HEARTBEAT] watchlist={len(cfg['watchlist_symbols'])} "
        f"open={len(state['open_tranches'])} held={held or 'none'} "
        f"deployed=${deployed_usd(state)} realized=${round(state['realized_pnl_usd'], 2)} "
        f"unrealized=${unreal} buys_today={state['day']['buy_count']} "
        f"mode={cfg['ranking']['mode']}"
        + (f"  {' '.join(flags)}" if flags else "")
    )
    print(line, flush=True)
    os.makedirs(os.path.dirname(g.HEARTBEAT_LOG), exist_ok=True)
    with open(g.HEARTBEAT_LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def notify_watched_prices(state: dict, cfg: dict) -> None:
    """Price check-in to Discord, but ONLY for a symbol that had a stop-loss
    TODAY -- sent every execution.price_notify_interval_sec until market close,
    then it stops naturally once the calendar date rolls over (see
    close_tranche, which sets stop_loss_watch_date). On a day with no
    stop-loss, this sends nothing. Purely informational: uses event='silent',
    which never pings regardless of notifications.mention_events."""
    today = g.today_str()
    fields = []
    for sym, sdata in state["symbols"].items():
        if sdata.get("stop_loss_watch_date") != today:
            continue
        price = sdata.get("last_price")
        if price is not None:
            fields.append((sym, f"${price:,.2f}"))
    if not fields:
        return
    g.discord_send("📈 Post-stop-loss price watch", fields=fields, color=g.COLOR_INFO, event="silent")


# --------------------------------------------------------------------------- #
# one iteration
# --------------------------------------------------------------------------- #
def iterate(state: dict, cfg: dict, *,
           price_bands: dict[str, tuple[float, float]] | None = None) -> None:
    """`price_bands` is for backtesting -- see process_fills(). Live callers
    never pass it."""
    symbols = cfg["watchlist_symbols"]
    try:
        quotes = vq.get_quotes_batch(symbols, timeout=cfg["price_feed"]["timeout_sec"])
    except Exception as exc:  # noqa: BLE001
        g.alert(f"PRICE FEED FAILURE (watchlist bot): {exc!r}")
        return

    for sym in symbols:
        price = quotes.get(sym)
        if price is None:
            continue
        sdata = state["symbols"][sym]
        sdata["prev_price"] = sdata.get("last_price")
        sdata["last_price"] = round(price, 2)
        ensure_grid_for_symbol(state, cfg, sym, price)
        refresh_symbol_market_data(state, cfg, sym)

    daily_rollover(state, cfg)
    check_risk_rails(state, cfg)
    process_fills(state, cfg, price_bands=price_bands)
    save_state(state)


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #
_last_closed_log = 0.0


def market_gated_iterate(state: dict, cfg: dict) -> bool:
    global _last_closed_log
    if not market_open_now():
        if time.monotonic() - _last_closed_log > 1800:
            g.log_event("Market closed (outside 9:30-16:00 America/New_York, Mon-Fri) -- idle.")
            _last_closed_log = time.monotonic()
        return False
    iterate(state, cfg)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    g.configure_notifications(cfg)

    if args.test_notify:
        if not g.discord_configured():
            print("No Discord webhook configured.", file=sys.stderr)
            return 2
        g.discord_send("✅ Test notification (watchlist grid bot)",
                       "If you can see this in Discord, the webhook works.",
                       fields=[("mode", cfg["execution"]["mode"]),
                               ("watchlist", ", ".join(cfg["watchlist_symbols"])),
                               ("ranking", cfg["ranking"]["mode"])],
                       color=g.COLOR_INFO, event="test")
        print("Test message sent.")
        return 0

    if cfg["execution"]["mode"] != "dry_run":
        print("REFUSING TO RUN: execution.mode is not 'dry_run'.", file=sys.stderr)
        return 2

    if args.reset:
        if input("Type 'reset' to wipe watchlist_state.json: ").strip() == "reset":
            if os.path.exists(STATE_PATH):
                os.remove(STATE_PATH)
            print("watchlist_state.json removed.")
        return 0

    state = load_state(cfg)

    if args.status:
        print(json.dumps(state, indent=2))
        return 0

    if args.summary:
        print_summary(state, cfg)
        return 0

    g.log_event(
        f"Starting watchlist grid bot ({len(cfg['watchlist_symbols'])} symbols, "
        f"mode={cfg['execution']['mode']}, ranking={cfg['ranking']['mode']}, "
        f"poll={cfg['execution']['poll_interval_sec']}s). THIS BUILD PLACES NO REAL ORDERS."
    )
    g.discord_send(
        f"▶️ Watchlist grid bot started  ({cfg['execution']['mode']})",
        description="Dry run — no real orders.",
        fields=[
            ("Watchlist", ", ".join(cfg["watchlist_symbols"])),
            ("Ranking mode", cfg["ranking"]["mode"]),
            ("Allocated", f"${cfg['allocated_capital_usd']}"),
            ("Grid", f"{cfg['grid']['num_levels']} levels, "
                     f"{cfg['grid']['grid_spacing_pct']}% apart, "
                     f"${cfg['grid']['tranche_size_usd']}/tranche"),
            ("Max per symbol", str(cfg["risk"].get("max_tranches_per_symbol", 1))),
        ],
        color=g.COLOR_INFO, event="start",
    )

    if args.once:
        ran = market_gated_iterate(state, cfg)
        if not ran:
            print("Market is closed right now -- no iteration run.")
        print_summary(state, cfg)
        return 0

    hb_interval = cfg["execution"].get("heartbeat_interval_sec", 0)
    price_notify_interval = cfg["execution"].get("price_notify_interval_sec", 0)
    last_hb = 0.0
    last_price_notify = 0.0
    try:
        while True:
            ran = market_gated_iterate(state, cfg)
            if hb_interval and time.monotonic() - last_hb >= hb_interval:
                heartbeat(state, cfg)
                last_hb = time.monotonic()
            if ran and price_notify_interval and time.monotonic() - last_price_notify >= price_notify_interval:
                notify_watched_prices(state, cfg)
                last_price_notify = time.monotonic()
            time.sleep(cfg["execution"]["poll_interval_sec"])
    except KeyboardInterrupt:
        g.log_event("Stopped by user (KeyboardInterrupt).")
        append_daily_summary(state, cfg)
        print_summary(state, cfg)
        g.discord_send(f"⏹️ Watchlist grid bot stopped by user  ({cfg['execution']['mode']})",
                       fields=[("Realized total", f"${round(state['realized_pnl_usd'], 4)}"),
                               ("Open tranches", str(len(state["open_tranches"])))],
                       color=g.COLOR_INFO, event="stop")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
