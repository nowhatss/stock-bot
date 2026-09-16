#!/usr/bin/env python3
"""
Targeted tests for the trend watchlist bot's per-symbol signal/slot logic.
No network, no orders. Run:  python test_trend_watchlist.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT)
sys.path.insert(0, HERE)

import grid_bot as g  # noqa: E402
import trend_bot_watchlist as tw  # noqa: E402

# Redirect every file the module might write so tests never touch real logs/state.
_TMP = tempfile.mkdtemp(prefix="trend_watchlist_test_")
for _attr in ("EVENTS_LOG", "HEARTBEAT_LOG"):
    setattr(g, _attr, os.path.join(_TMP, _attr.lower() + ".txt"))
for _attr in ("TRADES_CSV", "DAILY_CSV", "CANDLE_CACHE", "STATE_PATH"):
    setattr(tw, _attr, os.path.join(_TMP, _attr.lower() + ".txt"))

g.discord_send = lambda *a, **k: None
g.log_event = lambda *a, **k: None

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def make_cfg(symbols=("AAA", "BBB"), alloc=10000.0, ma_days=50, fee=0.0017,
            idle_yield_enabled=False, idle_apy=4.5, alert_drawdown_pct=20.0) -> dict:
    return {
        "watchlist_symbols": list(symbols),
        "allocated_capital_usd": alloc,
        "strategy": {"ma_days": ma_days},
        "fees": {"fee_rate_per_side": fee},
        "risk": {"alert_drawdown_pct": alert_drawdown_pct},
        "idle_yield": {"enabled": idle_yield_enabled, "apy_pct": idle_apy},
        "execution": {"mode": "dry_run", "poll_interval_sec": 60, "heartbeat_interval_sec": 300},
        "price_feed": {"timeout_sec": 10},
        "notifications": {"enabled": False},
    }


# --------------------------------------------------------------------------- #
print("\n# slot_capital / default_state")
cfg2 = make_cfg(symbols=("AAA", "BBB"), alloc=10000.0)
check("slot_capital splits evenly", tw.slot_capital(cfg2) == 5000.0)
st = tw.default_state(cfg2)
check("every symbol starts in cash", all(s["position"] == "cash" for s in st["symbols"].values()))
check("every symbol starts with its own equal slot",
     all(s["cash_usd"] == 5000.0 for s in st["symbols"].values()))
check("global last_signal_date starts None", st["last_signal_date"] is None)

# --------------------------------------------------------------------------- #
print("\n# buy() / sell() -- per-symbol slot mechanics")
cfg3 = make_cfg(symbols=("AAA",), alloc=1000.0, fee=0.01)
sdata = tw.default_symbol_state(cfg3)
tw.buy(sdata, cfg3, "AAA", price=100.0, ma=90.0, signal_close=95.0)
check("buy spends the whole slot", sdata["cash_usd"] == 0.0)
check("buy sets position to holding", sdata["position"] == "holding")
check("buy computes qty net of fee", abs(sdata["qty"] - (1000.0 * 0.99 / 100.0)) < 1e-9)
check("buy records entry_price", sdata["entry_price"] == 100.0)
check("buy increments trades_total", sdata["trades_total"] == 1)

tw.sell(sdata, cfg3, "AAA", price=110.0, ma=100.0, signal_close=95.0)
check("sell returns to cash position", sdata["position"] == "cash")
check("sell zeroes qty", sdata["qty"] == 0.0)
check("sell realizes a profit on a price rise", sdata["realized_pnl_usd"] > 0)
check("sell increments trades_total again", sdata["trades_total"] == 2)

# a loss is realized correctly too
sdata2 = tw.default_symbol_state(cfg3)
tw.buy(sdata2, cfg3, "AAA", price=100.0, ma=90.0, signal_close=95.0)
tw.sell(sdata2, cfg3, "AAA", price=90.0, ma=95.0, signal_close=85.0)
check("sell realizes a loss on a price drop", sdata2["realized_pnl_usd"] < 0)

# --------------------------------------------------------------------------- #
print("\n# evaluate_signals -- per-symbol independence")
cfg4 = make_cfg(symbols=("UP", "DOWN"), alloc=2000.0, ma_days=5)


def fake_closes(cfg, symbol):
    # UP: rising series, closes above its own MA -> should trigger a BUY
    # DOWN: falling series, closes below its own MA -> stays in cash
    if symbol == "UP":
        return [10.0, 10.5, 11.0, 11.5, 12.0, 13.0]
    return [20.0, 19.5, 19.0, 18.5, 18.0, 15.0]


tw.refresh_symbol_closes = fake_closes
state4 = tw.default_state(cfg4)
state4["symbols"]["UP"]["last_price"] = 13.0
state4["symbols"]["DOWN"]["last_price"] = 15.0
tw.evaluate_signals(state4, cfg4)
check("symbol trending up gets bought", state4["symbols"]["UP"]["position"] == "holding")
check("symbol trending down stays in cash", state4["symbols"]["DOWN"]["position"] == "cash")
check("last_signal_date is set for the whole bot", state4["last_signal_date"] == g.today_str())

# a later day where UP's own close drops below its own MA -> sold back to cash,
# completely independent of DOWN (which never held anything to begin with)
def fake_closes_flip(cfg, symbol):
    if symbol == "UP":
        return [13.0, 12.5, 12.0, 11.0, 10.0, 8.0]  # now below its own MA
    return [15.0, 15.5, 16.0, 16.5, 17.0, 18.0]      # DOWN now trending up


tw.refresh_symbol_closes = fake_closes_flip
state4["symbols"]["UP"]["last_price"] = 8.0
state4["symbols"]["DOWN"]["last_price"] = 18.0
state4["last_signal_date"] = None  # force re-evaluation as if it's a new day
tw.evaluate_signals(state4, cfg4)
check("UP sells back to cash on a down-flip", state4["symbols"]["UP"]["position"] == "cash")
check("DOWN buys in on an up-flip", state4["symbols"]["DOWN"]["position"] == "holding")

# --------------------------------------------------------------------------- #
print("\n# accrue_idle_yield")
cfg5 = make_cfg(symbols=("AAA",), alloc=1000.0, idle_yield_enabled=True, idle_apy=36.5)
sdata5 = tw.default_symbol_state(cfg5)
now = tw.now_utc()
sdata5["_yield_last_accrual_at"] = tw.iso(now - timedelta(days=1))
tw.accrue_idle_yield({"symbols": {"AAA": sdata5}}, cfg5)
# 36.5%/yr APY over exactly 1 day ~= 0.1%/day -> ~$1 on $1000
check("idle cash earns yield while sitting in cash",
     0.5 < sdata5["yield_earned_usd"] < 2.0, extra=str(sdata5["yield_earned_usd"]))

sdata6 = tw.default_symbol_state(cfg5)
sdata6["position"] = "holding"
sdata6["_yield_last_accrual_at"] = tw.iso(now - timedelta(days=1))
tw.accrue_idle_yield({"symbols": {"AAA": sdata6}}, cfg5)
check("no yield accrues while holding a position", sdata6["yield_earned_usd"] == 0.0)

cfg5b = make_cfg(symbols=("AAA",), alloc=1000.0, idle_yield_enabled=False)
sdata7 = tw.default_symbol_state(cfg5b)
sdata7["_yield_last_accrual_at"] = tw.iso(now - timedelta(days=1))
tw.accrue_idle_yield({"symbols": {"AAA": sdata7}}, cfg5b)
check("no yield accrues when idle_yield is disabled", sdata7["yield_earned_usd"] == 0.0)

# --------------------------------------------------------------------------- #
print("\n# check_drawdown")
cfg6 = make_cfg(symbols=("AAA",), alloc=1000.0, alert_drawdown_pct=10.0)
state6 = tw.default_state(cfg6)
state6["symbols"]["AAA"]["cash_usd"] = 1000.0
tw.check_drawdown(state6, cfg6)
check("no alert at peak", state6["dd_alerted"] is False)

state6["symbols"]["AAA"]["cash_usd"] = 850.0  # 15% down from peak 1000
tw.check_drawdown(state6, cfg6)
check("alert fires once past threshold", state6["dd_alerted"] is True)

state6["dd_alerted"] = "sentinel"  # prove it's not re-triggered while still above threshold
tw.check_drawdown(state6, cfg6)
check("alert does not re-fire every poll while still down", state6["dd_alerted"] == "sentinel")

state6["symbols"]["AAA"]["cash_usd"] = 960.0  # recovered to within half the threshold
tw.check_drawdown(state6, cfg6)
check("alert clears once recovered past half the threshold", state6["dd_alerted"] is False)

# --------------------------------------------------------------------------- #
print("\n# daily_summary_row")
cfg7 = make_cfg(symbols=("AAA", "BBB"), alloc=2000.0)
state7 = tw.default_state(cfg7)
state7["symbols"]["AAA"]["position"] = "holding"
state7["symbols"]["AAA"]["qty"] = 10.0
state7["symbols"]["AAA"]["last_price"] = 105.0
state7["symbols"]["AAA"]["cash_usd"] = 0.0
state7["symbols"]["AAA"]["realized_pnl_usd"] = 5.0
state7["symbols"]["AAA"]["trades_total"] = 1
state7["symbols"]["BBB"]["last_price"] = 50.0
row = tw.daily_summary_row(state7, cfg7)
check("held_symbols lists only symbols currently holding", row["held_symbols"] == "AAA")
check("positions_held counts correctly", row["positions_held"] == 1)
check("equity sums cash + market value across all symbols",
     abs(row["equity_usd"] - (10.0 * 105.0 + 1000.0)) < 1e-6)
check("trades_total aggregates across symbols", row["trades_total"] == 1)

# --------------------------------------------------------------------------- #
print("\n# load_state -- watchlist grown since last save")
cfg8 = make_cfg(symbols=("AAA",), alloc=1000.0)
state8 = tw.default_state(cfg8)
state8["symbols"]["AAA"]["cash_usd"] = 42.0  # simulate some activity
tw.save_state(state8)
cfg8b = make_cfg(symbols=("AAA", "NEW"), alloc=2000.0)
reloaded = tw.load_state(cfg8b)
check("existing symbol's state is preserved on reload", reloaded["symbols"]["AAA"]["cash_usd"] == 42.0)
check("newly added symbol gets initialised on the fly", "NEW" in reloaded["symbols"])
check("newly added symbol gets a slot sized off the CURRENT config",
     reloaded["symbols"]["NEW"]["cash_usd"] == tw.slot_capital(cfg8b))

# --------------------------------------------------------------------------- #
print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
