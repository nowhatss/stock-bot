#!/usr/bin/env python3
"""
Watchlist trend bot -- DRY RUN ONLY.

Runs the same daily-MA trend rule as ../trend_bot.py across a whole watchlist
of stocks at once instead of one fixed symbol: each symbol gets its own FIXED,
EQUAL slice of allocated_capital_usd (allocated_capital_usd / number of
watchlist symbols), and independently holds that symbol while yesterday's
daily close is above its own N-day moving average, otherwise sits in cash
(earning modelled idle yield, if enabled). So this can hold anywhere from 0 to
every symbol in the watchlist at once -- a diversified trend-following
portfolio, not a single rotating position. See trend_watchlist_config.json's
_allocation_note for why slots are fixed rather than continuously rebalanced.

Each symbol's slot is otherwise independent of the others -- there is no
shared capital pool or cross-symbol ranking like grid_bot_watchlist.py, since
every symbol trades its own fixed sleeve regardless of what the others are
doing. What IS shared: one process, one state file, one Discord channel, and
one combined equity/drawdown figure across the whole watchlist.

The signal is evaluated ONCE per UTC day, gated to when the market's open
(stocks don't trade 24/7) -- exactly like trend_bot_stock.py -- using each
symbol's own completed previous daily close, so live behaviour matches what
backtest_trend_watchlist.py tests.

Needs a Questrade refresh token -- see stock_bots/README.md.

Run:  python trend_bot_watchlist.py             (continuous loop)
      python trend_bot_watchlist.py --once      (single check; no-op if market closed)
      python trend_bot_watchlist.py --status
      python trend_bot_watchlist.py --summary
      python trend_bot_watchlist.py --reset
      python trend_bot_watchlist.py --test-notify

THIS BUILD PLACES NO REAL ORDERS.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT)

import grid_bot as g  # noqa: E402  -- reused: logging, Discord, now_utc/iso/today_str
import market_data  # noqa: E402  -- parent module, pure sma() math only, asset-agnostic
import venue_questrade as vq  # noqa: E402
from market_hours import market_open_now  # noqa: E402

CONFIG_PATH = os.path.join(HERE, "trend_watchlist_config.json")
STATE_PATH = os.path.join(HERE, "trend_watchlist_state.json")
TRADES_CSV = os.path.join(HERE, "logs", "trend_watchlist_trades.csv")
DAILY_CSV = os.path.join(HERE, "logs", "trend_watchlist_daily_summary.csv")
CANDLE_CACHE = os.path.join(HERE, "logs", "trend_watchlist_candle_cache.json")

g.EVENTS_LOG = os.path.join(HERE, "logs", "trend_watchlist_events.log")
g.HEARTBEAT_LOG = os.path.join(HERE, "logs", "trend_watchlist_heartbeat.log")


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


def slot_capital(cfg: dict) -> float:
    n = len(cfg["watchlist_symbols"])
    return cfg["allocated_capital_usd"] / n if n else 0.0


def default_symbol_state(cfg: dict) -> dict:
    return {
        "position": "cash",              # "cash" | "holding"
        "cash_usd": slot_capital(cfg),
        "qty": 0.0,
        "entry_price": None,
        "entry_cost_usd": None,
        "last_price": None,
        "prev_price": None,
        "last_ma": None,
        "last_signal_close": None,
        "realized_pnl_usd": 0.0,
        "yield_earned_usd": 0.0,
        "trades_total": 0,
        "_yield_last_accrual_at": None,
    }


def default_state(cfg: dict) -> dict:
    return {
        "created_at": iso(now_utc()),
        "symbols": {sym: default_symbol_state(cfg) for sym in cfg["watchlist_symbols"]},
        "last_signal_date": None,
        "peak_equity": None,
        "dd_alerted": False,
        "last_iteration_at": None,
    }


def load_state(cfg: dict) -> dict:
    if not os.path.exists(STATE_PATH):
        return default_state(cfg)
    with open(STATE_PATH, encoding="utf-8-sig") as fh:
        state = json.load(fh)
    # symbols added to the watchlist since the last run get a fresh slot on the fly
    for sym in cfg["watchlist_symbols"]:
        state["symbols"].setdefault(sym, default_symbol_state(cfg))
    return state


def save_state(state: dict) -> None:
    state["last_iteration_at"] = iso(now_utc())
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    for attempt in range(5):
        try:
            os.replace(tmp, STATE_PATH)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def symbol_equity(sdata: dict, price: float | None) -> float:
    return sdata["cash_usd"] + sdata["qty"] * (price or sdata.get("last_price") or 0.0)


def total_equity(state: dict) -> float:
    return sum(symbol_equity(sdata, sdata.get("last_price")) for sdata in state["symbols"].values())


def compute_unrealized(state: dict) -> float:
    """Mark-to-market P&L on currently-held positions only; a symbol sitting
    in cash contributes 0 regardless of how its own price has moved since."""
    total = 0.0
    for sdata in state["symbols"].values():
        if sdata["position"] != "holding":
            continue
        price = sdata.get("last_price")
        if price is None or sdata.get("entry_cost_usd") is None:
            continue
        total += sdata["qty"] * price - sdata["entry_cost_usd"]
    return round(total, 4)


# --------------------------------------------------------------------------- #
# idle-cash yield, per symbol slot (see trend_bot.py's accrue_idle_yield)
# --------------------------------------------------------------------------- #
def accrue_idle_yield(state: dict, cfg: dict) -> None:
    y = cfg.get("idle_yield", {}) or {}
    now = now_utc()
    apy = float(y.get("apy_pct", 4.5)) / 100.0
    enabled = bool(y.get("enabled"))
    for sdata in state["symbols"].values():
        last = sdata.get("_yield_last_accrual_at")
        sdata["_yield_last_accrual_at"] = iso(now)
        if not enabled or sdata["position"] != "cash" or not last:
            continue
        try:
            elapsed_s = (now - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            continue
        if elapsed_s <= 0 or sdata["cash_usd"] <= 0:
            continue
        earned = sdata["cash_usd"] * apy * (elapsed_s / (365.0 * 86400.0))
        sdata["cash_usd"] += earned
        sdata["yield_earned_usd"] = round(sdata.get("yield_earned_usd", 0.0) + earned, 8)


# --------------------------------------------------------------------------- #
# per-symbol candle cache -> closes, refreshed at most hourly (see
# grid_bot_watchlist.py's refresh_symbol_market_data for the same pattern)
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


def refresh_symbol_closes(cfg: dict, symbol: str) -> list[float] | None:
    """Oldest->newest daily closes for `symbol`, refreshed at most once an
    hour per symbol (cached to disk). Returns None if no data is available at
    all (neither fresh nor a stale cache to fall back on)."""
    lookback = int(cfg["strategy"]["ma_days"]) + 30
    cache = _read_candle_cache()
    entry = cache.get(symbol)
    fresh = entry and (time.time() - float(entry.get("fetched_at_epoch", 0)) < 3600)

    if not fresh:
        try:
            candles = vq.get_daily_candles(symbol, days=lookback,
                                           timeout=cfg["price_feed"]["timeout_sec"])
            rows = [(c["start"], float(c["close"])) for c in candles if c.get("close") is not None]
            rows.sort(key=lambda r: r[0])
            closes = [c for _, c in rows]
            if len(closes) >= 5:
                entry = {"fetched_at_epoch": time.time(), "closes": closes}
                cache[symbol] = entry
                _write_candle_cache(cache)
        except Exception as exc:  # noqa: BLE001
            g.log_event(f"[{symbol}] candle refresh failed ({exc!r}); "
                        f"using stale/no data for trend signal", level="WARN")

    return entry["closes"] if entry else None


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
TRADE_FIELDS = ["timestamp", "mode", "action", "symbol", "price", "size_usd", "qty",
                "fee_usd", "realized_pnl_delta_usd", "realized_pnl_total_usd", "note"]


def log_trade(row: dict) -> None:
    os.makedirs(os.path.dirname(TRADES_CSV), exist_ok=True)
    new = g._csv_needs_header(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in TRADE_FIELDS})


# --------------------------------------------------------------------------- #
# per-symbol buy/sell -- same mechanics as trend_bot.py's buy()/sell(), just
# scoped to one symbol's own slot instead of the whole bot's state
# --------------------------------------------------------------------------- #
def buy(sdata: dict, cfg: dict, symbol: str, price: float, ma: float, signal_close: float) -> None:
    fee_rate = cfg["fees"]["fee_rate_per_side"]
    spend = sdata["cash_usd"]
    fee = round(spend * fee_rate, 6)
    qty = (spend - fee) / price

    sdata["qty"] = qty
    sdata["cash_usd"] = 0.0
    sdata["entry_price"] = price
    sdata["entry_cost_usd"] = spend
    sdata["position"] = "holding"
    sdata["trades_total"] += 1

    n = cfg["strategy"]["ma_days"]
    log_trade({
        "timestamp": iso(now_utc()), "mode": cfg["execution"]["mode"], "action": "BUY",
        "symbol": symbol, "price": round(price, 2), "size_usd": round(spend, 2),
        "qty": round(qty, 8), "fee_usd": fee, "realized_pnl_delta_usd": 0.0,
        "realized_pnl_total_usd": round(sdata["realized_pnl_usd"], 4),
        "note": f"ma{n}={round(ma, 2)} close={round(signal_close, 2)}",
    })
    g.log_event(f"[{symbol}] BUY  {qty:.6f} @ {price:.2f}  fee={fee}  "
                f"(close {signal_close:.2f} > MA{n} {ma:.2f})")
    g.discord_send(
        f"🟦 BUY  {symbol}  (trend up, watchlist)  ({cfg['execution']['mode']})",
        fields=[
            ("Symbol", symbol), ("Fill price", f"${price:,.2f}"),
            ("Size", f"{qty:.6f} shares  (${spend:,.2f})"), ("Fee", f"${fee}"),
            ("Signal", f"close ${signal_close:,.2f} > MA{n} ${ma:,.2f}"),
        ],
        color=g.COLOR_BUY, event="buy",
    )


def sell(sdata: dict, cfg: dict, symbol: str, price: float, ma: float, signal_close: float) -> None:
    fee_rate = cfg["fees"]["fee_rate_per_side"]
    gross = sdata["qty"] * price
    fee = round(gross * fee_rate, 6)
    proceeds = gross - fee
    pnl = round(proceeds - sdata["entry_cost_usd"], 6)

    sdata["realized_pnl_usd"] = round(sdata["realized_pnl_usd"] + pnl, 6)
    sdata["cash_usd"] = proceeds
    qty_sold = sdata["qty"]
    sdata["qty"] = 0.0
    sdata["position"] = "cash"
    sdata["entry_price"] = None
    sdata["entry_cost_usd"] = None
    sdata["trades_total"] += 1

    n = cfg["strategy"]["ma_days"]
    log_trade({
        "timestamp": iso(now_utc()), "mode": cfg["execution"]["mode"], "action": "SELL",
        "symbol": symbol, "price": round(price, 2), "size_usd": round(gross, 2),
        "qty": round(qty_sold, 8), "fee_usd": fee, "realized_pnl_delta_usd": pnl,
        "realized_pnl_total_usd": round(sdata["realized_pnl_usd"], 4),
        "note": f"ma{n}={round(ma, 2)} close={round(signal_close, 2)}",
    })
    g.log_event(f"[{symbol}] SELL {qty_sold:.6f} @ {price:.2f}  pnl={pnl:+.4f}  "
                f"realized_total={round(sdata['realized_pnl_usd'], 4)}  "
                f"(close {signal_close:.2f} < MA{n} {ma:.2f})")
    g.discord_send(
        f"{'🟩' if pnl >= 0 else '🟧'} SELL  {symbol}  exiting to cash (trend down, watchlist)  "
        f"({cfg['execution']['mode']})",
        fields=[
            ("Symbol", symbol), ("Sell price", f"${price:,.2f}"),
            ("Entry price", f"${sdata.get('entry_price') or 0:,.2f}"),
            ("P&L this trade", f"{pnl:+.2f} USD"), ("Fee", f"${fee}"),
            ("Realized total", f"${round(sdata['realized_pnl_usd'], 2)}"),
            ("Signal", f"close ${signal_close:,.2f} < MA{n} ${ma:,.2f}"),
        ],
        color=g.COLOR_SELL_WIN if pnl >= 0 else g.COLOR_SELL_LOSS, event="sell",
    )


def evaluate_signals(state: dict, cfg: dict) -> None:
    """Runs once per UTC day: decide in/out for EVERY symbol independently,
    trade on a change, using each symbol's own MA and its own slot."""
    n = int(cfg["strategy"]["ma_days"])
    for sym in cfg["watchlist_symbols"]:
        sdata = state["symbols"][sym]
        closes = refresh_symbol_closes(cfg, sym)
        if not closes:
            g.log_event(f"[{sym}] trend signal: no market data available, skipping "
                        f"today's check", level="WARN")
            continue
        ma = market_data.sma(closes, n)
        if ma is None:
            g.log_event(f"[{sym}] trend signal: not enough history yet for MA-{n} "
                        f"({len(closes)} days)", level="WARN")
            continue
        signal_close = closes[-1]
        signal_in = signal_close > ma
        sdata["last_ma"] = ma
        sdata["last_signal_close"] = signal_close

        price = sdata.get("last_price")
        if price is None:
            continue
        if signal_in and sdata["position"] == "cash":
            buy(sdata, cfg, sym, price, ma, signal_close)
        elif not signal_in and sdata["position"] == "holding":
            sell(sdata, cfg, sym, price, ma, signal_close)
    state["last_signal_date"] = g.today_str()


def check_drawdown(state: dict, cfg: dict) -> None:
    eq = total_equity(state)
    peak = state.get("peak_equity") or cfg["allocated_capital_usd"]
    peak = max(peak, eq)
    state["peak_equity"] = peak
    threshold = float(cfg["risk"]["alert_drawdown_pct"])
    dd_pct = 100.0 * (peak - eq) / peak if peak else 0.0
    if dd_pct >= threshold and not state.get("dd_alerted"):
        state["dd_alerted"] = True
        g.alert(f"TREND WATCHLIST drawdown {dd_pct:.1f}% from peak equity ${peak:,.2f} "
                f"-> ${eq:,.2f}. Not a stop -- exiting each symbol to cash on its own "
                f"down-signal is that symbol's risk control. Just a heads-up.")
    elif dd_pct < threshold / 2:
        state["dd_alerted"] = False


# --------------------------------------------------------------------------- #
# daily summary / reporting
# --------------------------------------------------------------------------- #
def daily_summary_row(state: dict, cfg: dict) -> dict:
    eq = total_equity(state)
    peak = state.get("peak_equity") or cfg["allocated_capital_usd"]
    dd = 100 * (peak - eq) / peak if peak else 0.0
    held = sorted(sym for sym, s in state["symbols"].items() if s["position"] == "holding")
    realized = sum(s["realized_pnl_usd"] for s in state["symbols"].values())
    yield_earned = sum(s["yield_earned_usd"] for s in state["symbols"].values())
    trades_total = sum(s["trades_total"] for s in state["symbols"].values())
    return {
        "date": g.today_str(),
        "held_symbols": ",".join(held) or "none",
        "positions_held": len(held),
        "watchlist_size": len(cfg["watchlist_symbols"]),
        "equity_usd": round(eq, 2),
        "total_pnl_usd": round(eq - cfg["allocated_capital_usd"], 2),
        "realized_pnl_usd": round(realized, 2),
        "yield_earned_usd": round(yield_earned, 2),
        "peak_equity": round(peak, 2),
        "drawdown_pct": round(dd, 2),
        "trades_total": trades_total,
    }


def print_summary(state: dict, cfg: dict) -> None:
    r = daily_summary_row(state, cfg)
    print(f"\n=== TREND WATCHLIST SUMMARY {r['date']} (UTC) ===")
    for k, v in r.items():
        print(f"  {k:16s}: {v}")
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
    flagged = r["drawdown_pct"] >= cfg["risk"]["alert_drawdown_pct"]
    g.discord_send(
        f"📈 Trend watchlist summary {r['date']} (UTC)",
        fields=[
            ("Holding", r["held_symbols"]),
            ("Positions held", f"{r['positions_held']} / {r['watchlist_size']}"),
            ("Equity", f"${r['equity_usd']:,}"),
            ("Total P&L", f"${r['total_pnl_usd']:,}  "
             f"({100 * r['total_pnl_usd'] / cfg['allocated_capital_usd']:+.2f}%)"),
            ("Realized / Yield", f"${r['realized_pnl_usd']:,} / ${r['yield_earned_usd']:,}"),
            ("Drawdown from peak", f"{r['drawdown_pct']:.1f}%"),
            ("Trades total", str(r["trades_total"])),
        ],
        color=g.COLOR_ALERT if flagged else g.COLOR_INFO,
        event="alert" if flagged else "daily",
    )


def heartbeat(state: dict, cfg: dict) -> None:
    held = sorted(sym for sym, s in state["symbols"].items() if s["position"] == "holding")
    eq = total_equity(state)
    line = (
        f"{iso(now_utc())} [HEARTBEAT] watchlist={len(cfg['watchlist_symbols'])} "
        f"held={held or 'none'} equity=${round(eq, 2)} "
        f"pnl=${round(eq - cfg['allocated_capital_usd'], 2)}"
    )
    print(line, flush=True)
    os.makedirs(os.path.dirname(g.HEARTBEAT_LOG), exist_ok=True)
    with open(g.HEARTBEAT_LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------------------------- #
# one iteration
# --------------------------------------------------------------------------- #
def iterate(state: dict, cfg: dict) -> None:
    try:
        quotes = vq.get_quotes_batch(cfg["watchlist_symbols"], timeout=cfg["price_feed"]["timeout_sec"])
    except Exception as exc:  # noqa: BLE001
        g.alert(f"PRICE FEED FAILURE (trend watchlist bot): {exc!r}")
        return

    for sym in cfg["watchlist_symbols"]:
        price = quotes.get(sym)
        if price is None:
            continue
        sdata = state["symbols"][sym]
        sdata["prev_price"] = sdata.get("last_price")
        sdata["last_price"] = round(price, 2)

    accrue_idle_yield(state, cfg)

    today = g.today_str()
    if state["last_signal_date"] != today:
        was_first = state["last_signal_date"] is None
        evaluate_signals(state, cfg)
        append_daily_summary(state, cfg)
        if not was_first:  # skip notify on the very first check
            notify_daily_summary(state, cfg)

    check_drawdown(state, cfg)
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
        g.discord_send("✅ Test notification (trend watchlist bot)",
                       "If you can see this in Discord, the webhook works.",
                       fields=[("mode", cfg["execution"]["mode"]),
                               ("watchlist", ", ".join(cfg["watchlist_symbols"]))],
                       color=g.COLOR_INFO, event="test")
        print("Test message sent.")
        return 0

    if cfg["execution"]["mode"] != "dry_run":
        print("REFUSING TO RUN: execution.mode is not 'dry_run'.", file=sys.stderr)
        return 2

    if args.reset:
        if input("Type 'reset' to wipe trend_watchlist_state.json: ").strip() == "reset":
            if os.path.exists(STATE_PATH):
                os.remove(STATE_PATH)
            print("trend_watchlist_state.json removed.")
        return 0

    state = load_state(cfg)

    if args.status:
        print(json.dumps(state, indent=2))
        return 0

    if args.summary:
        print_summary(state, cfg)
        return 0

    g.log_event(
        f"Starting trend watchlist bot ({len(cfg['watchlist_symbols'])} symbols, "
        f"mode={cfg['execution']['mode']}, ma_days={cfg['strategy']['ma_days']}, "
        f"poll={cfg['execution']['poll_interval_sec']}s). THIS BUILD PLACES NO REAL ORDERS."
    )
    g.discord_send(
        f"▶️ Trend watchlist bot started  ({cfg['execution']['mode']})",
        description="Dry run — no real orders.",
        fields=[
            ("Watchlist", ", ".join(cfg["watchlist_symbols"])),
            ("Allocated", f"${cfg['allocated_capital_usd']:,.0f}  "
             f"(${slot_capital(cfg):,.2f} / symbol)"),
            ("Rule", f"hold each symbol while close > MA-{cfg['strategy']['ma_days']}, else cash"),
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
    last_hb = 0.0
    try:
        while True:
            market_gated_iterate(state, cfg)
            if hb_interval and time.monotonic() - last_hb >= hb_interval:
                heartbeat(state, cfg)
                last_hb = time.monotonic()
            time.sleep(cfg["execution"]["poll_interval_sec"])
    except KeyboardInterrupt:
        g.log_event("Stopped by user (KeyboardInterrupt).")
        append_daily_summary(state, cfg)
        print_summary(state, cfg)
        g.discord_send(f"⏹️ Trend watchlist bot stopped by user  ({cfg['execution']['mode']})",
                       fields=[("Equity", f"${round(total_equity(state), 2)}"),
                               ("Positions held", str(sum(
                                   1 for s in state["symbols"].values()
                                   if s["position"] == "holding")))],
                       color=g.COLOR_INFO, event="stop")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
