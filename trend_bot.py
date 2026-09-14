#!/usr/bin/env python3
"""
Trend-following bot -- DRY RUN ONLY.

Hold ETH-USD while yesterday's daily close is above its N-day moving average
(default 50), otherwise hold 100% USDC (earning modelled idle yield). The signal
is evaluated ONCE per UTC day, right after rollover, using the completed previous
day's close -- exactly like backtest_trend.py, so live behaviour matches what was
backtested and there's no intraday whipsaw.

This is a separate bot from grid_bot.py -- its own config, state, and logs -- so
it can run in its own terminal at the same time as the grid dry run. It reuses
grid_bot.py's logging/Discord/price-feed code (same conventions, same channel).

Run:  python trend_bot.py             (continuous loop)
      python trend_bot.py --once      (single check; good for cron)
      python trend_bot.py --status    (print state and exit)
      python trend_bot.py --summary   (print today's status and exit)
      python trend_bot.py --reset     (wipe trend_state.json)
      python trend_bot.py --test-notify

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

import grid_bot as g          # reused: log_event, discord_send, notifications,
                              # now_utc, iso, today_str, get_price, colors, alert
import market_data as md

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "trend_config.json")
STATE_PATH = os.path.join(HERE, "trend_state.json")
TRADES_CSV = os.path.join(HERE, "logs", "trend_trades.csv")
DAILY_CSV = os.path.join(HERE, "logs", "trend_daily_summary.csv")

# point the shared grid_bot logging/notification plumbing at this bot's own files
g.EVENTS_LOG = os.path.join(HERE, "logs", "trend_events.log")
g.HEARTBEAT_LOG = os.path.join(HERE, "logs", "trend_heartbeat.log")


# --------------------------------------------------------------------------- #
# config / state
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
        return json.load(fh)


def default_state(cfg: dict) -> dict:
    return {
        "created_at": g.iso(g.now_utc()),
        "position": "cash",              # "cash" | "eth"
        "cash_usd": cfg["allocated_capital_usd"],
        "eth_qty": 0.0,
        "entry_price": None,
        "entry_cost_usd": None,
        "realized_pnl_usd": 0.0,         # trading P&L only, excludes yield
        "yield_earned_usd": 0.0,
        "trades_total": 0,
        "last_price": None,
        "last_signal_date": None,        # last UTC date the daily signal ran
        "last_ma": None,
        "last_signal_close": None,
        "reference_signals": {},         # "100": true/false, "200": true/false
        "peak_equity": None,
        "dd_alerted": False,
        "_yield_last_accrual_at": None,
        "last_iteration_at": None,
    }


def load_state(cfg: dict) -> dict:
    if not os.path.exists(STATE_PATH):
        return default_state(cfg)
    with open(STATE_PATH, encoding="utf-8-sig") as fh:
        return json.load(fh)


def save_state(state: dict) -> None:
    state["last_iteration_at"] = g.iso(g.now_utc())
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def equity(state: dict, price: float) -> float:
    return state["cash_usd"] + state["eth_qty"] * price


# --------------------------------------------------------------------------- #
# idle-cash yield (modelling only -- see grid_bot's accrue_idle_yield)
# --------------------------------------------------------------------------- #
def accrue_idle_yield(state: dict, cfg: dict) -> None:
    y = cfg.get("idle_yield", {}) or {}
    now = g.now_utc()
    last = state.get("_yield_last_accrual_at")
    state["_yield_last_accrual_at"] = g.iso(now)
    if not y.get("enabled") or state["position"] != "cash" or not last:
        return
    try:
        elapsed_s = (now - datetime.fromisoformat(last)).total_seconds()
    except ValueError:
        return
    if elapsed_s <= 0 or state["cash_usd"] <= 0:
        return
    apy = float(y.get("apy_pct", 4.5)) / 100.0
    earned = state["cash_usd"] * apy * (elapsed_s / (365.0 * 86400.0))
    state["cash_usd"] += earned
    state["yield_earned_usd"] = round(state.get("yield_earned_usd", 0.0) + earned, 8)


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
TRADE_FIELDS = ["timestamp", "mode", "action", "price", "size_usd", "qty_eth",
                "fee_usd", "realized_pnl_delta_usd", "realized_pnl_total_usd", "note"]


def log_trade(row: dict) -> None:
    os.makedirs(os.path.dirname(TRADES_CSV), exist_ok=True)
    new = g._csv_needs_header(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in TRADE_FIELDS})


DAILY_FIELDS = ["date", "position", "last_price", "ma", "signal_close",
                "equity_usd", "total_pnl_usd", "realized_pnl_usd", "yield_earned_usd",
                "peak_equity", "drawdown_pct", "trades_total"]


def daily_summary_row(state: dict, cfg: dict, price: float) -> dict:
    eq = equity(state, price)
    peak = state.get("peak_equity") or cfg["allocated_capital_usd"]
    dd = 100 * (peak - eq) / peak if peak else 0.0
    return {
        "date": g.today_str(), "position": state["position"], "last_price": round(price, 2),
        "ma": round(state["last_ma"], 2) if state["last_ma"] else "",
        "signal_close": round(state["last_signal_close"], 2) if state["last_signal_close"] else "",
        "equity_usd": round(eq, 2),
        "total_pnl_usd": round(eq - cfg["allocated_capital_usd"], 2),
        "realized_pnl_usd": round(state["realized_pnl_usd"], 2),
        "yield_earned_usd": round(state["yield_earned_usd"], 2),
        "peak_equity": round(peak, 2), "drawdown_pct": round(dd, 2),
        "trades_total": state["trades_total"],
    }


def append_daily_summary(state: dict, cfg: dict, price: float) -> None:
    os.makedirs(os.path.dirname(DAILY_CSV), exist_ok=True)
    new = g._csv_needs_header(DAILY_CSV)
    with open(DAILY_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=DAILY_FIELDS)
        if new:
            w.writeheader()
        w.writerow(daily_summary_row(state, cfg, price))


def print_summary(state: dict, cfg: dict, price: float) -> None:
    r = daily_summary_row(state, cfg, price)
    print(f"\n=== TREND SUMMARY {r['date']} (UTC) ===")
    for k in DAILY_FIELDS:
        print(f"  {k:16s}: {r[k]}")
    print("=" * 40 + "\n")


def notify_daily_summary(state: dict, cfg: dict, price: float) -> None:
    r = daily_summary_row(state, cfg, price)
    flagged = r["drawdown_pct"] >= cfg["risk"]["alert_drawdown_pct"]
    g.discord_send(
        f"📈 Trend summary {r['date']} (UTC)",
        fields=[
            ("Position", r["position"].upper()),
            (f"{g.asset_symbol(cfg)} price", f"${r['last_price']:,}"),
            (f"MA-{cfg['strategy']['ma_days']}", f"${r['ma']}" if r["ma"] != "" else "n/a"),
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
    ref = ", ".join(f"MA{k}={'IN' if v else 'OUT'}" for k, v in state.get("reference_signals", {}).items())
    price = state.get("last_price") or 0.0
    eq = equity(state, price)
    line = (
        f"{g.iso(g.now_utc())} [HEARTBEAT] price={price} position={state['position']} "
        f"equity=${round(eq, 2)} pnl=${round(eq - cfg['allocated_capital_usd'], 2)} "
        f"ma{cfg['strategy']['ma_days']}={round(state['last_ma'], 2) if state['last_ma'] else 'n/a'}"
        + (f"  [{ref}]" if ref else "")
    )
    print(line, flush=True)
    os.makedirs(os.path.dirname(g.HEARTBEAT_LOG), exist_ok=True)
    with open(g.HEARTBEAT_LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------------------------- #
# the daily decision
# --------------------------------------------------------------------------- #
def buy(state: dict, cfg: dict, price: float, ma: float, signal_close: float) -> None:
    fee_rate = cfg["fees"]["fee_rate_per_side"]
    spend = state["cash_usd"]
    fee = round(spend * fee_rate, 6)
    qty = (spend - fee) / price

    state["eth_qty"] = qty
    state["cash_usd"] = 0.0
    state["entry_price"] = price
    state["entry_cost_usd"] = spend
    state["position"] = "eth"
    state["trades_total"] += 1

    log_trade({
        "timestamp": g.iso(g.now_utc()), "mode": cfg["execution"]["mode"], "action": "BUY",
        "price": round(price, 2), "size_usd": round(spend, 2), "qty_eth": round(qty, 8),
        "fee_usd": fee, "realized_pnl_delta_usd": 0.0,
        "realized_pnl_total_usd": round(state["realized_pnl_usd"], 4),
        "note": f"ma{cfg['strategy']['ma_days']}={round(ma, 2)} close={round(signal_close, 2)}",
    })
    sym = g.asset_symbol(cfg)
    g.log_event(f"BUY  {qty:.6f} {sym} @ {price:.2f}  fee={fee}  "
               f"(close {signal_close:.2f} > MA{cfg['strategy']['ma_days']} {ma:.2f})")
    g.discord_send(
        f"🟦 BUY  entering {sym} (trend up)  ({cfg['execution']['mode']})",
        fields=[
            ("Fill price", f"${price:,.2f}"), ("Size", f"{qty:.6f} {sym}  (${spend:,.2f})"),
            ("Fee", f"${fee}"), (f"Signal", f"close ${signal_close:,.2f} > MA{cfg['strategy']['ma_days']} ${ma:,.2f}"),
        ],
        color=g.COLOR_BUY, event="buy",
    )


def sell(state: dict, cfg: dict, price: float, ma: float, signal_close: float) -> None:
    fee_rate = cfg["fees"]["fee_rate_per_side"]
    gross = state["eth_qty"] * price
    fee = round(gross * fee_rate, 6)
    proceeds = gross - fee
    pnl = round(proceeds - state["entry_cost_usd"], 6)

    state["realized_pnl_usd"] = round(state["realized_pnl_usd"] + pnl, 6)
    state["cash_usd"] = proceeds
    qty_sold = state["eth_qty"]
    state["eth_qty"] = 0.0
    state["position"] = "cash"
    state["entry_price"] = None
    state["entry_cost_usd"] = None
    state["trades_total"] += 1

    log_trade({
        "timestamp": g.iso(g.now_utc()), "mode": cfg["execution"]["mode"], "action": "SELL",
        "price": round(price, 2), "size_usd": round(gross, 2), "qty_eth": round(qty_sold, 8),
        "fee_usd": fee, "realized_pnl_delta_usd": pnl,
        "realized_pnl_total_usd": round(state["realized_pnl_usd"], 4),
        "note": f"ma{cfg['strategy']['ma_days']}={round(ma, 2)} close={round(signal_close, 2)}",
    })
    g.log_event(f"SELL {qty_sold:.6f} {g.asset_symbol(cfg)} @ {price:.2f}  pnl={pnl:+.4f}  "
               f"realized_total={round(state['realized_pnl_usd'], 4)}  "
               f"(close {signal_close:.2f} < MA{cfg['strategy']['ma_days']} {ma:.2f})")
    g.discord_send(
        f"{'🟩' if pnl >= 0 else '🟧'} SELL  exiting to cash (trend down)  "
        f"({cfg['execution']['mode']})",
        fields=[
            ("Sell price", f"${price:,.2f}"), ("Entry price", f"${state.get('entry_price') or 0:,.2f}"),
            ("P&L this trade", f"{pnl:+.2f} USD"), ("Fee", f"${fee}"),
            ("Realized total", f"${round(state['realized_pnl_usd'], 2)}"),
            ("Signal", f"close ${signal_close:,.2f} < MA{cfg['strategy']['ma_days']} ${ma:,.2f}"),
        ],
        color=g.COLOR_SELL_WIN if pnl >= 0 else g.COLOR_SELL_LOSS, event="sell",
    )


def evaluate_signal(state: dict, cfg: dict, price: float) -> None:
    """Runs once per UTC day: decide in/out, trade on a change, log reference MAs."""
    mkt = md.load(cfg)
    if not mkt or not mkt.get("closes"):
        g.log_event("trend signal: no market data available, skipping today's check", level="WARN")
        return
    closes = mkt["closes"]
    n = int(cfg["strategy"]["ma_days"])
    ma = md.sma(closes, n)
    if ma is None:
        g.log_event(f"trend signal: not enough history yet for MA-{n} "
                    f"({len(closes)} days)", level="WARN")
        return
    signal_close = closes[-1]
    signal_in = signal_close > ma

    state["last_ma"] = ma
    state["last_signal_close"] = signal_close
    state["last_signal_date"] = g.today_str()

    ref = {}
    for rn in cfg["strategy"].get("reference_ma_days", []):
        rma = md.sma(closes, int(rn))
        if rma is not None:
            ref[str(rn)] = bool(signal_close > rma)
    state["reference_signals"] = ref

    if signal_in and state["position"] == "cash":
        buy(state, cfg, price, ma, signal_close)
    elif not signal_in and state["position"] == "eth":
        sell(state, cfg, price, ma, signal_close)
    else:
        g.log_event(f"trend signal: no change ({state['position']}, "
                    f"close {signal_close:.2f} vs MA{n} {ma:.2f})")


def check_drawdown(state: dict, cfg: dict, price: float) -> None:
    eq = equity(state, price)
    peak = state.get("peak_equity") or cfg["allocated_capital_usd"]
    peak = max(peak, eq)
    state["peak_equity"] = peak
    threshold = float(cfg["risk"]["alert_drawdown_pct"])
    dd_pct = 100.0 * (peak - eq) / peak if peak else 0.0
    if dd_pct >= threshold and not state.get("dd_alerted"):
        state["dd_alerted"] = True
        g.alert(f"TREND BOT drawdown {dd_pct:.1f}% from peak equity ${peak:,.2f} "
               f"-> ${eq:,.2f}. Not a stop -- exiting to cash on the next down-signal "
               f"is the strategy's own risk control. Just a heads-up.")
    elif dd_pct < threshold / 2:
        state["dd_alerted"] = False


# --------------------------------------------------------------------------- #
# one iteration
# --------------------------------------------------------------------------- #
def iterate(state: dict, cfg: dict) -> None:
    try:
        price = g.get_price(cfg)
    except Exception as exc:  # noqa: BLE001
        g.alert(f"PRICE FEED FAILURE (trend bot): {exc!r}")
        return

    state["last_price"] = round(price, 2)
    accrue_idle_yield(state, cfg)

    today = g.today_str()
    if state["last_signal_date"] != today:
        evaluate_signal(state, cfg, price)
        append_daily_summary(state, cfg, price)
        if state["last_signal_date"] is not None:  # skip notify on the very first check
            notify_daily_summary(state, cfg, price)

    check_drawdown(state, cfg, price)
    save_state(state)


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #
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
        g.discord_send("✅ Test notification (trend bot)",
                       "If you can see this in Discord, the webhook works.",
                       fields=[("mode", cfg["execution"]["mode"]), ("asset", cfg["asset"])],
                       color=g.COLOR_INFO, event="test")
        print("Test message sent.")
        return 0

    if cfg["execution"]["mode"] != "dry_run":
        print("REFUSING TO RUN: execution.mode is not 'dry_run'.", file=sys.stderr)
        return 2

    if args.reset:
        if input("Type 'reset' to wipe trend_state.json: ").strip() == "reset":
            if os.path.exists(STATE_PATH):
                os.remove(STATE_PATH)
            print("trend_state.json removed.")
        return 0

    state = load_state(cfg)

    if args.status:
        print(json.dumps(state, indent=2))
        return 0

    if args.summary:
        price = state.get("last_price") or g.get_price(cfg)
        print_summary(state, cfg, price)
        return 0

    g.log_event(
        f"Starting trend bot (mode={cfg['execution']['mode']}, "
        f"ma_days={cfg['strategy']['ma_days']}, poll={cfg['execution']['poll_interval_sec']}s). "
        f"THIS BUILD PLACES NO REAL ORDERS."
    )
    g.discord_send(
        f"▶️ Trend bot started  ({cfg['execution']['mode']})",
        description="Dry run — no real orders.",
        fields=[
            ("Asset", cfg["asset"]), ("Allocated", f"${cfg['allocated_capital_usd']:,.0f}"),
            ("Rule", f"hold {g.asset_symbol(cfg)} while close > MA-{cfg['strategy']['ma_days']}, else cash"),
            ("Position", state["position"].upper()),
        ],
        color=g.COLOR_INFO, event="start",
    )

    if args.once:
        iterate(state, cfg)
        price = state.get("last_price") or 0.0
        print_summary(state, cfg, price)
        return 0

    if state.get("last_price"):
        print_summary(state, cfg, state["last_price"])

    hb_interval = cfg["execution"].get("heartbeat_interval_sec", 0)
    last_hb = 0.0
    try:
        while True:
            iterate(state, cfg)
            if hb_interval and time.monotonic() - last_hb >= hb_interval:
                heartbeat(state, cfg)
                last_hb = time.monotonic()
            time.sleep(cfg["execution"]["poll_interval_sec"])
    except KeyboardInterrupt:
        g.log_event("Stopped by user (KeyboardInterrupt).")
        price = state.get("last_price") or 0.0
        append_daily_summary(state, cfg, price)
        print_summary(state, cfg, price)
        g.discord_send(f"⏹️ Trend bot stopped by user  ({cfg['execution']['mode']})",
                       fields=[("Equity", f"${round(equity(state, price), 2)}"),
                               ("Position", state["position"].upper())],
                       color=g.COLOR_INFO, event="stop")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
