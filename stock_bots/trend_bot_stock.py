#!/usr/bin/env python3
"""
Stock trend-following bot -- DRY RUN ONLY. Same daily-MA rule as ../trend_bot.py
(hold the stock while yesterday's close > N-day MA, else 100% cash) -- this
file only swaps the venue: price/candles come from Questrade instead of
Coinbase, and the loop only actually evaluates while the market is open
(stocks don't trade 24/7 like crypto). The engine itself is imported, not
copied, so it's the exact same code path as the crypto trend bot.

Needs a Questrade refresh token first -- see stock_bots/README.md.

Run:  python trend_bot_stock.py             (continuous loop)
      python trend_bot_stock.py --once      (single check; no-op if market closed)
      python trend_bot_stock.py --status
      python trend_bot_stock.py --summary
      python trend_bot_stock.py --reset
      python trend_bot_stock.py --test-notify

THIS BUILD PLACES NO REAL ORDERS.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT)

import grid_bot as g  # noqa: E402  -- shared logging/Discord/price-feed plumbing
import trend_bot as t  # noqa: E402  -- the tested trend engine, reused not copied
import venue_questrade as vq  # noqa: E402
import market_data_questrade as mdq  # noqa: E402
from market_hours import market_open_now  # noqa: E402

# point the shared engine's file paths at this bot's own folder
t.HERE = HERE
t.CONFIG_PATH = os.path.join(HERE, "trend_config.json")
t.STATE_PATH = os.path.join(HERE, "trend_state.json")
t.TRADES_CSV = os.path.join(HERE, "logs", "trend_trades.csv")
t.DAILY_CSV = os.path.join(HERE, "logs", "trend_daily_summary.csv")
g.EVENTS_LOG = os.path.join(HERE, "logs", "trend_events.log")
g.HEARTBEAT_LOG = os.path.join(HERE, "logs", "trend_heartbeat.log")


def get_price(cfg: dict) -> float:
    return vq.get_last_price(cfg["asset"], timeout=cfg["price_feed"]["timeout_sec"])


g.get_price = get_price
t.md = mdq  # trend_bot.evaluate_signal() calls md.load(cfg) -- redirect to Questrade


_last_closed_log = 0.0


def market_gated_iterate(state: dict, cfg: dict) -> bool:
    global _last_closed_log
    if not market_open_now():
        if time.monotonic() - _last_closed_log > 1800:
            g.log_event("Market closed (outside 9:30-16:00 America/New_York, Mon-Fri) -- idle.")
            _last_closed_log = time.monotonic()
        return False
    t.iterate(state, cfg)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    args = ap.parse_args()

    cfg = t.load_config()
    g.configure_notifications(cfg)

    if args.test_notify:
        if not g.discord_configured():
            print("No Discord webhook configured.", file=sys.stderr)
            return 2
        g.discord_send("✅ Test notification (stock trend bot)",
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
            if os.path.exists(t.STATE_PATH):
                os.remove(t.STATE_PATH)
            print("trend_state.json removed.")
        return 0

    state = t.load_state(cfg)

    if args.status:
        print(json.dumps(state, indent=2))
        return 0

    if args.summary:
        price = state.get("last_price") or get_price(cfg)
        t.print_summary(state, cfg, price)
        return 0

    g.log_event(
        f"Starting stock trend bot (asset={cfg['asset']}, mode={cfg['execution']['mode']}, "
        f"ma_days={cfg['strategy']['ma_days']}, poll={cfg['execution']['poll_interval_sec']}s). "
        f"THIS BUILD PLACES NO REAL ORDERS."
    )
    g.discord_send(
        f"▶️ Stock trend bot started  ({cfg['execution']['mode']})",
        description="Dry run — no real orders.",
        fields=[
            ("Asset", cfg["asset"]), ("Allocated", f"${cfg['allocated_capital_usd']:,.0f}"),
            ("Rule", f"hold {cfg['asset']} while close > MA-{cfg['strategy']['ma_days']}, else cash"),
            ("Position", state["position"].upper()),
        ],
        color=g.COLOR_INFO, event="start",
    )

    if args.once:
        ran = market_gated_iterate(state, cfg)
        if not ran:
            print("Market is closed right now -- no iteration run.")
        price = state.get("last_price") or 0.0
        t.print_summary(state, cfg, price)
        return 0

    if state.get("last_price"):
        t.print_summary(state, cfg, state["last_price"])

    hb_interval = cfg["execution"].get("heartbeat_interval_sec", 0)
    last_hb = 0.0
    try:
        while True:
            market_gated_iterate(state, cfg)
            if hb_interval and time.monotonic() - last_hb >= hb_interval:
                t.heartbeat(state, cfg)
                last_hb = time.monotonic()
            time.sleep(cfg["execution"]["poll_interval_sec"])
    except KeyboardInterrupt:
        g.log_event("Stopped by user (KeyboardInterrupt).")
        price = state.get("last_price") or 0.0
        t.append_daily_summary(state, cfg, price)
        t.print_summary(state, cfg, price)
        g.discord_send(f"⏹️ Stock trend bot stopped by user  ({cfg['execution']['mode']})",
                       fields=[("Equity", f"${round(t.equity(state, price), 2)}"),
                               ("Position", state["position"].upper())],
                       color=g.COLOR_INFO, event="stop")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
