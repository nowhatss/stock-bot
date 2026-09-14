#!/usr/bin/env python3
"""
Stock grid trading bot -- DRY RUN ONLY. Same grid engine as ../grid_bot.py
(rungs below an anchor, take-profit sells, risk rails, resting-order fill
modelling, all the adaptive features) -- this file only swaps the venue:
price/candles come from Questrade instead of Coinbase, and a market-hours
gate stops the bot from doing anything while the exchange is closed (stocks
don't trade 24/7 like crypto). The engine itself is imported, not copied, so
it's the exact same tested code as the crypto bot (see ../test_adaptations.py).

Needs a Questrade refresh token first -- see stock_bots/README.md.

Run:  python grid_bot_stock.py             (continuous loop)
      python grid_bot_stock.py --once      (single iteration; no-op if market closed)
      python grid_bot_stock.py --status    (print state and exit)
      python grid_bot_stock.py --summary   (print today's summary and exit)
      python grid_bot_stock.py --reset     (wipe state.json)
      python grid_bot_stock.py --test-notify

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

import grid_bot as g  # noqa: E402  -- the tested grid engine, reused not copied
import venue_questrade as vq  # noqa: E402
import market_data_questrade as mdq  # noqa: E402
from market_hours import market_open_now  # noqa: E402

# point the shared engine's file paths at this bot's own folder
g.HERE = HERE
g.CONFIG_PATH = os.path.join(HERE, "config.json")
g.STATE_PATH = os.path.join(HERE, "state.json")
g.TRADES_CSV = os.path.join(HERE, "logs", "trades.csv")
g.DAILY_CSV = os.path.join(HERE, "logs", "daily_summary.csv")
g.EVENTS_LOG = os.path.join(HERE, "logs", "events.log")
g.HEARTBEAT_LOG = os.path.join(HERE, "logs", "heartbeat.log")


def get_price(cfg: dict) -> float:
    return vq.get_last_price(cfg["asset"], timeout=cfg["price_feed"]["timeout_sec"])


g.get_price = get_price


def load_market_data(cfg: dict):
    if not g._adaptations_need_market_data(cfg):
        return None
    try:
        return mdq.load(cfg)
    except Exception as exc:  # noqa: BLE001
        g.log_event(f"market data unavailable ({exc!r}); adaptive features idle", level="WARN")
        return None


g.load_market_data = load_market_data


_last_closed_log = 0.0


def market_gated_iterate(state: dict, cfg: dict) -> bool:
    """Runs g.iterate() only while the market is open. Returns True if it ran."""
    global _last_closed_log
    if not market_open_now():
        if time.monotonic() - _last_closed_log > 1800:  # log at most every 30 min
            g.log_event("Market closed (outside 9:30-16:00 America/New_York, Mon-Fri) -- idle.")
            _last_closed_log = time.monotonic()
        return False
    g.iterate(state, cfg)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="run a single iteration and exit")
    ap.add_argument("--status", action="store_true", help="print state.json and exit")
    ap.add_argument("--summary", action="store_true", help="print today's summary and exit")
    ap.add_argument("--reset", action="store_true", help="wipe state.json")
    ap.add_argument("--test-notify", action="store_true",
                    help="send a test Discord message and exit")
    args = ap.parse_args()

    cfg = g.load_config()
    g.configure_notifications(cfg)

    if args.test_notify:
        if not g.discord_configured():
            print("No Discord webhook configured. Set notifications.discord_webhook_url "
                  "in config.json or the GRID_BOT_DISCORD_WEBHOOK env var.", file=sys.stderr)
            return 2
        g.discord_send("✅ Test notification (stock grid bot)",
                       "If you can see this in Discord, the webhook works.",
                       fields=[("mode", cfg["execution"]["mode"]), ("asset", cfg["asset"])],
                       color=g.COLOR_INFO, event="test")
        print("Test message sent.")
        return 0

    if cfg["execution"]["mode"] != "dry_run":
        print("REFUSING TO RUN: config execution.mode is not 'dry_run'. "
              "This Phase 1 build only simulates.", file=sys.stderr)
        return 2

    if args.reset:
        if input("Type 'reset' to wipe state.json: ").strip() == "reset":
            if os.path.exists(g.STATE_PATH):
                os.remove(g.STATE_PATH)
            print("state.json removed.")
        return 0

    state = g.load_state()

    if args.status:
        print(json.dumps(state, indent=2))
        return 0

    if args.summary:
        price = state.get("last_price") or get_price(cfg)
        g.print_summary(state, price, cfg)
        return 0

    g.log_event(
        f"Starting stock grid bot (asset={cfg['asset']}, mode={cfg['execution']['mode']}, "
        f"fill_model={cfg['execution']['fill_model']}, poll={cfg['execution']['poll_interval_sec']}s). "
        f"THIS BUILD PLACES NO REAL ORDERS."
    )
    g.discord_send(
        f"▶️ Stock grid bot started  ({cfg['execution']['mode']})",
        description="Dry run — no real orders.",
        fields=[
            ("Asset", cfg["asset"]),
            ("Allocated", f"${cfg['allocated_capital_usd']}"),
            ("Grid", f"{cfg['grid']['num_levels']} levels, "
                     f"{cfg['grid']['grid_spacing_pct']}% apart, "
                     f"${cfg['grid']['tranche_size_usd']}/tranche"),
            ("Take-profit", f"+{cfg['grid']['take_profit_pct']}%"),
            ("Anchor", f"${state['anchor_price']:,}" if state.get("anchor_price") else "sets on first poll"),
        ],
        color=g.COLOR_INFO,
        event="start",
    )

    if args.once:
        ran = market_gated_iterate(state, cfg)
        if not ran:
            print("Market is closed right now -- no iteration run. "
                  "(9:30-16:00 America/New_York, Mon-Fri; holidays not modeled.)")
        g.print_summary(state, state.get("last_price") or 0.0, cfg)
        return 0

    if state.get("last_price"):
        g.print_summary(state, state["last_price"], cfg)

    hb_interval = cfg["execution"].get("heartbeat_interval_sec", 0)
    last_hb = 0.0
    try:
        while True:
            market_gated_iterate(state, cfg)
            if hb_interval and time.monotonic() - last_hb >= hb_interval:
                g.heartbeat(state, cfg)
                last_hb = time.monotonic()
            time.sleep(cfg["execution"]["poll_interval_sec"])
    except KeyboardInterrupt:
        g.log_event("Stopped by user (KeyboardInterrupt).")
        price = state.get("last_price") or 0.0
        g.append_daily_summary(state, price, cfg)
        g.print_summary(state, price, cfg)
        g.discord_send(f"⏹️ Stock grid bot stopped by user  ({cfg['execution']['mode']})",
                       fields=[("Realized total", f"${round(state['realized_pnl_usd'], 4)}"),
                               ("Open tranches", str(len(state["open_tranches"])))],
                       color=g.COLOR_INFO, event="stop")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
