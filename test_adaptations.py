#!/usr/bin/env python3
"""
Targeted tests for the adaptive features. No network, no orders.
Run:  python test_adaptations.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid_bot as g
import market_data as md

# Redirect every file the bot might write so tests never touch real logs/state.
_TMP = tempfile.mkdtemp(prefix="gridbot_test_")
for _attr in ("STATE_PATH", "TRADES_CSV", "DAILY_CSV", "EVENTS_LOG", "HEARTBEAT_LOG"):
    setattr(g, _attr, os.path.join(_TMP, _attr.lower() + ".txt"))

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


# silence side effects
g.discord_send = lambda *a, **k: None
g.log_event = lambda *a, **k: None

CFG = g.load_config()


# --------------------------------------------------------------------------- #
print("\n# market_data indicators")
closes = [100, 102, 101, 103, 105, 104, 106, 108, 107, 110, 109, 111, 113, 112, 114]
check("sma(5) of last 5", abs(md.sma(closes, 5) - sum(closes[-5:]) / 5) < 1e-9)
check("sma too-short returns None", md.sma([1, 2, 3], 5) is None)
check("daily_vol positive", md.daily_vol_pct(closes, 10) > 0)
check("daily_vol too-short None", md.daily_vol_pct([1, 2], 10) is None)
flat = [100.0] * 30
check("zero vol on flat series", md.daily_vol_pct(flat, 14) == 0.0)


# --------------------------------------------------------------------------- #
print("\n# spacing multiplier from volatility")
cfg = g.load_config()
cfg["adaptations"].update({"vol_spacing_enabled": True, "vol_lookback_days": 10,
                           "vol_reference_daily_pct": 2.0, "vol_mult_min": 0.6, "vol_mult_max": 1.8})
hi = [100 * (1.05 if i % 2 else 0.96) ** 1 for i in range(20)]   # very choppy
lo = [100 + i * 0.01 for i in range(20)]                          # nearly flat
m_hi = g.spacing_mult_from_vol(cfg, {"closes": hi})
m_lo = g.spacing_mult_from_vol(cfg, {"closes": lo})
check("high vol -> mult clamped high", m_hi == 1.8, f"got {m_hi}")
check("low vol -> mult clamped low", m_lo == 0.6, f"got {m_lo}")
check("no market data -> mult 1.0", g.spacing_mult_from_vol(cfg, None) == 1.0)
cfg["adaptations"]["vol_spacing_enabled"] = False
check("disabled -> mult 1.0", g.spacing_mult_from_vol(cfg, {"closes": hi}) == 1.0)


# --------------------------------------------------------------------------- #
print("\n# effective spacing / take-profit")
st = g.default_state()
st["spacing_mult"] = 0.8
cfg = g.load_config()
cfg["adaptations"]["vol_spacing_enabled"] = True
cfg["adaptations"]["vol_scales_take_profit"] = True
check("effective spacing scales", abs(g.effective_spacing_pct(st, cfg) - cfg["grid"]["grid_spacing_pct"] * 0.8) < 1e-9)
check("effective TP scales", abs(g.effective_tp_pct(st, cfg) - cfg["grid"]["take_profit_pct"] * 0.8) < 1e-9)
cfg["adaptations"]["vol_scales_take_profit"] = False
check("TP not scaled when flag off", g.effective_tp_pct(st, cfg) == cfg["grid"]["take_profit_pct"])


# --------------------------------------------------------------------------- #
print("\n# trend filter — asymmetric dead zone (pause wider than resume)")
mkt = {"closes": [100.0] * 10}      # MA5 = 100
cfg = g.load_config()
cfg["adaptations"].update({"trend_filter_enabled": True, "trend_ma_days": 5,
                           "trend_pause_buffer_pct": 3.0, "trend_resume_buffer_pct": 2.0})
cfg["adaptations"].pop("trend_buffer_pct", None)
st = g.default_state()
st["trend_ok"] = True
check("ON + price 98 (>97 pause floor) -> stays ON", g.trend_status(st, cfg, 98.0, mkt)[0] is True)
check("ON + price 96 (<97) -> turns OFF", g.trend_status(st, cfg, 96.0, mkt)[0] is False)
st["trend_ok"] = False
check("OFF + price 101 (<102 resume) -> stays OFF", g.trend_status(st, cfg, 101.0, mkt)[0] is False)
check("OFF + price 103 (>102) -> turns ON", g.trend_status(st, cfg, 103.0, mkt)[0] is True)
check("no mkt -> always on", g.trend_status(st, cfg, 1.0, None)[0] is True)

# symmetric override
sym = g.load_config()
sym["adaptations"].update({"trend_filter_enabled": True, "trend_ma_days": 5,
                           "trend_buffer_pct": 2.0})   # dead zone 98..102
check("trend_buffer_pct forces symmetric: ON + 99 stays ON",
      g.trend_status({"trend_ok": True}, sym, 99.0, mkt)[0] is True)
check("trend_buffer_pct forces symmetric: ON + 97 turns OFF",
      g.trend_status({"trend_ok": True}, sym, 97.0, mkt)[0] is False)

cfg["adaptations"]["trend_filter_enabled"] = False
check("disabled -> always on", g.trend_status(st, cfg, 1.0, mkt)[0] is True)


# --------------------------------------------------------------------------- #
print("\n# can_open_new_tranche blocks on trend filter")
cfg = g.load_config()
cfg["adaptations"]["trend_filter_enabled"] = True
st = g.default_state()
st["anchor_price"] = 2500.0
st["grid_levels"] = g.build_levels(2500.0, 5.0, 3)
st["trend_ok"] = False
ok, reason = g.can_open_new_tranche(st, cfg)
check("trend off -> cannot open", ok is False and "trend" in reason, reason)
st["trend_ok"] = True
ok, _ = g.can_open_new_tranche(st, cfg)
check("trend on -> can open", ok is True)


# --------------------------------------------------------------------------- #
print("\n# grid reshape only when flat")
cfg = g.load_config()
cfg["adaptations"].update({"reanchor_enabled": True, "reanchor_breakout_pct": 8.0,
                           "vol_spacing_enabled": False})
st = g.default_state()
st["anchor_price"] = 2000.0
st["grid_levels"] = g.build_levels(2000.0, 5.0, 3)
st["open_tranches"] = [{"id": "x", "level_index": 0, "fill_price": 1900.0, "qty_eth": 1.0,
                        "cost_usd": 1900.0, "take_profit_price": 1976.0, "opened_at": g.iso(g.now_utc())}]
g.ensure_or_reshape_grid(st, cfg, 2400.0, None)      # +20% breakout but holding
check("no reshape while holding a tranche", st["anchor_price"] == 2000.0)
st["open_tranches"] = []
g.ensure_or_reshape_grid(st, cfg, 2400.0, None)      # now flat -> should re-anchor
check("re-anchors on breakout when flat", st["anchor_price"] == 2400.0, f"anchor {st['anchor_price']}")


# --------------------------------------------------------------------------- #
print("\n# re-anchor after stop-out")
cfg = g.load_config()
cfg["adaptations"]["reanchor_after_stopout"] = True
st = g.default_state()
st["anchor_price"] = 3000.0
st["grid_levels"] = g.build_levels(3000.0, 5.0, 3)
st["reanchor_pending_after_stopout"] = True
st["halted"] = True
g.ensure_or_reshape_grid(st, cfg, 2000.0, None)
check("still halted -> no re-anchor", st["anchor_price"] == 3000.0)
st["halted"] = False
g.ensure_or_reshape_grid(st, cfg, 2000.0, None)
check("halt cleared + flat -> re-anchor at price", st["anchor_price"] == 2000.0)
check("pending flag consumed", st["reanchor_pending_after_stopout"] is False)


# --------------------------------------------------------------------------- #
print("\n# max-hold time-stop")
cfg = g.load_config()
cfg["adaptations"].update({"max_hold_enabled": True, "max_hold_days": 10,
                           "max_hold_only_if_underwater": True})
cfg["execution"]["fill_model"] = "at_level"
st = g.default_state()
st["anchor_price"] = 2500.0
st["grid_levels"] = g.build_levels(2500.0, 5.0, 3)
st["grid_levels"][0]["held"] = True
old = (g.now_utc() - timedelta(days=15)).replace(microsecond=0).isoformat()
young = g.now_utc().replace(microsecond=0).isoformat()
st["open_tranches"] = [
    {"id": "old_uw", "level_index": 0, "fill_price": 2400.0, "qty_eth": 1.25, "cost_usd": 3000.0,
     "buy_fee_usd": 18.0, "take_profit_price": 2496.0, "opened_at": old},
]
g.check_max_hold(st, cfg, 2200.0)         # 15 days old, underwater (2200 < 2400)
check("old underwater tranche force-closed", len(st["open_tranches"]) == 0)
check("close logged with reason", st["closed_tranches"][-1]["close_reason"].startswith("max_hold"))

st["open_tranches"] = [
    {"id": "old_profit", "level_index": 1, "fill_price": 2400.0, "qty_eth": 1.25, "cost_usd": 3000.0,
     "buy_fee_usd": 18.0, "take_profit_price": 2496.0, "opened_at": old},
]
g.check_max_hold(st, cfg, 2450.0)         # 15 days old but in profit -> left alone
check("old but profitable tranche kept (only_if_underwater)", len(st["open_tranches"]) == 1)

st["open_tranches"] = [
    {"id": "young_uw", "level_index": 2, "fill_price": 2400.0, "qty_eth": 1.25, "cost_usd": 3000.0,
     "buy_fee_usd": 18.0, "take_profit_price": 2496.0, "opened_at": young},
]
g.check_max_hold(st, cfg, 2000.0)         # young, underwater -> kept
check("young underwater tranche kept", len(st["open_tranches"]) == 1)


# --------------------------------------------------------------------------- #
print("\n# max-hold cooldown blocks immediate rebuy")
cfg = g.load_config()
cfg["adaptations"].update({"max_hold_enabled": True, "max_hold_days": 10,
                           "max_hold_cooldown_hours": 48})
cfg["execution"]["fill_model"] = "at_level"
st = g.default_state()
st["anchor_price"] = 2500.0
st["grid_levels"] = g.build_levels(2500.0, 5.0, 3)
lvl0 = st["grid_levels"][0]
lvl0["held"] = True
st["open_tranches"] = [
    {"id": "t", "level_index": 0, "fill_price": 2375.0, "qty_eth": 1.26, "cost_usd": 3000.0,
     "buy_fee_usd": 18.0, "take_profit_price": 2470.0,
     "opened_at": (g.now_utc() - timedelta(days=15)).replace(microsecond=0).isoformat()},
]
g.check_max_hold(st, cfg, 2200.0)                       # force-close
check("level not held after time-stop", lvl0["held"] is False)
check("cooldown set on the level", "cooldown_until" in lvl0)
check("level_buyable False during cooldown", g.level_buyable(lvl0, 2200.0) is False)
lvl0["cooldown_until"] = (g.now_utc() - timedelta(hours=1)).replace(microsecond=0).isoformat()
check("level_buyable True after cooldown expires", g.level_buyable(lvl0, 2200.0) is True)


# --------------------------------------------------------------------------- #
print("\n# idle-cash yield modelling")
cfg = g.load_config()
cfg["idle_yield"] = {"enabled": True, "apy_pct": 4.5}
cfg["allocated_capital_usd"] = 10000.0
st = g.default_state()
st["realized_pnl_usd"] = 0.0
st["_yield_last_accrual_at"] = (g.now_utc() - timedelta(days=30)).replace(microsecond=0).isoformat()
g.accrue_idle_yield(st, cfg)
# 10000 * 4.5% * (30/365) ~= 36.99
exp = 10000 * 0.045 * (30 / 365)
check("30d yield on full idle ~ expected", abs(st["yield_earned_usd"] - exp) < 0.5,
      f"got {st['yield_earned_usd']:.2f} exp {exp:.2f}")
check("accrual clock advanced", st["_yield_last_accrual_at"] is not None)

st2 = g.default_state()
st2["_yield_last_accrual_at"] = (g.now_utc() - timedelta(days=30)).replace(microsecond=0).isoformat()
st2["open_tranches"] = [{"id": "a", "level_index": 0, "cost_usd": 10000.0, "qty_eth": 4.0,
                         "fill_price": 2500.0, "take_profit_price": 2600.0,
                         "opened_at": g.iso(g.now_utc())}]
g.accrue_idle_yield(st2, cfg)
check("no yield when fully deployed", st2["yield_earned_usd"] == 0.0, f"got {st2['yield_earned_usd']}")

cfg["idle_yield"]["enabled"] = False
st3 = g.default_state()
st3["_yield_last_accrual_at"] = (g.now_utc() - timedelta(days=30)).isoformat()
g.accrue_idle_yield(st3, cfg)
check("disabled -> no yield", st3["yield_earned_usd"] == 0.0)


# --------------------------------------------------------------------------- #
print("\n# fill modelling: fee rates")
cfg = g.load_config()
check("maker rate", g.fee_rate(cfg, "maker") == cfg["fees"]["fee_rate_maker"])
check("taker rate", g.fee_rate(cfg, "taker") == cfg["fees"]["fee_rate_taker"])
check("taker > maker", g.fee_rate(cfg, "taker") > g.fee_rate(cfg, "maker"))


# --------------------------------------------------------------------------- #
print("\n# fill modelling: resting_order_fills")
cfg = g.load_config()
cfg["execution"].update({"shallow_touch_pct": 0.15, "shallow_fill_prob": 0.5, "fill_seed": 1})
st = g.default_state()
# BUY at 2000: band low 1980 = 1% below -> decisive -> fills
check("decisive buy touch fills", g.resting_order_fills(st, cfg, 2000.0, 1980.0, "buy") is True)
# BUY at 2000: band low 2001 -> never reached -> no fill
check("band above buy price -> no fill", g.resting_order_fills(st, cfg, 2000.0, 2001.0, "buy") is False)
# SELL at 2000: band high 2020 -> decisive -> fills
check("decisive sell touch fills", g.resting_order_fills(st, cfg, 2000.0, 2020.0, "sell") is True)
# shallow buy touch (0.05% below, < 0.15% threshold): probabilistic, seeded -> stable result
r1 = g.resting_order_fills(st, cfg, 2000.0, 1999.0, "buy")
st2 = g.default_state()
r2 = g.resting_order_fills(st2, cfg, 2000.0, 1999.0, "buy")
check("shallow touch is deterministic under a fixed seed", r1 == r2)
# over many shallow touches the fill rate ~ shallow_fill_prob
hits = sum(g.resting_order_fills(st, cfg, 2000.0, 1999.0, "buy") for _ in range(400))
check("shallow fill rate ~ 0.5", 120 <= hits <= 280, f"hits={hits}/400")


# --------------------------------------------------------------------------- #
print("\n# fill modelling: process_fills (resting)")
cfg = g.load_config()
cfg["execution"]["fill_model"] = "resting"
cfg["adaptations"] = {}
cfg["idle_yield"] = {"enabled": False}
st = g.default_state()
st["anchor_price"] = 2500.0
st["grid_levels"] = g.build_levels(2500.0, 5.0, 3)   # rungs 2375 / 2256.25 / 2143.44
st["prev_price"] = 2500.0
# unprimed rungs: even though price dips to 2370, nothing fills yet
g.process_fills(st, cfg, 2370.0)
check("unprimed rung does not fill on first cross", len(st["open_tranches"]) == 0,
      f"open={len(st['open_tranches'])}")
# prime by trading above, then dip through rung 0
st["prev_price"] = 2500.0
g.process_fills(st, cfg, 2500.0)                     # primes all rungs above price... rungs are below, so primed
st["prev_price"] = 2400.0
g.process_fills(st, cfg, 2360.0)                     # band 2360..2400 crosses rung 0 (2375)
check("primed rung fills when band crosses it", len(st["open_tranches"]) == 1,
      f"open={len(st['open_tranches'])}")
check("buy recorded as maker", st["open_tranches"][0]["fill_kind"] == "maker")
check("buy filled at the rung price, not the poll price",
      abs(st["open_tranches"][0]["fill_price"] - 2375.0) < 0.01)


# --------------------------------------------------------------------------- #
print("\n# fill modelling: max-hold exit is taker + slippage")
cfg = g.load_config()
cfg["execution"]["market_slippage_bps"] = 10
cfg["adaptations"].update({"max_hold_enabled": True, "max_hold_days": 5,
                           "max_hold_only_if_underwater": True})
st = g.default_state()
st["anchor_price"] = 2500.0
st["grid_levels"] = g.build_levels(2500.0, 5.0, 3)
st["grid_levels"][0]["held"] = True
st["open_tranches"] = [{"id": "z", "level_index": 0, "fill_price": 2400.0, "qty_eth": 1.25,
                        "cost_usd": 3000.0, "buy_fee_usd": 18.0, "take_profit_price": 2496.0,
                        "opened_at": (g.now_utc() - timedelta(days=9)).replace(microsecond=0).isoformat()}]
g.check_max_hold(st, cfg, 2200.0)
cl = st["closed_tranches"][-1]
check("max-hold close recorded as taker", cl["sell_fill_kind"] == "taker")
check("max-hold exit price slipped below spot", cl["sell_price"] < 2200.0,
      f"sell_price={cl['sell_price']}")


# --------------------------------------------------------------------------- #
print("\n# breakout-buy: reference low + trigger")
cfg = g.load_config()
cfg["adaptations"].update({"breakout_buy_enabled": True, "breakout_buy_pct": 8.0,
                          "breakout_lookback_hours": 24.0, "breakout_tranche_size_usd": None})
cfg["execution"]["market_slippage_bps"] = 0
st = g.default_state()
st["anchor_price"] = 2500.0
st["grid_levels"] = g.build_levels(2500.0, 5.0, 3)

check("empty history -> reference low is current price",
      g._breakout_reference_low(st, 2000.0) == 2000.0)
g._record_price_history(st, 1900.0, 24.0)
g._record_price_history(st, 1950.0, 24.0)
check("reference low = min of recent history + current price",
      g._breakout_reference_low(st, 2100.0) == 1900.0)

# trigger needs price >= ref_low * 1.08 = 2052
g.check_breakout_buy(st, cfg, 2000.0, 2040.0)   # band high below trigger -> no buy
check("no buy below the breakout trigger", len(st["open_tranches"]) == 0)
g.check_breakout_buy(st, cfg, 2060.0, 2060.0)   # band high above trigger -> buys
check("buy fires once band crosses the trigger", len(st["open_tranches"]) == 1)
bo = st["open_tranches"][0]
check("breakout tranche tagged entry_kind", bo["entry_kind"] == "breakout")
check("breakout fill recorded as taker", bo["fill_kind"] == "taker")
check("breakeven stop == fill price", bo["breakeven_stop_price"] == bo["fill_price"])
check("breakout has a normal take-profit target too",
      bo["take_profit_price"] > bo["fill_price"])


# --------------------------------------------------------------------------- #
print("\n# breakout-buy: only one at a time, disabled by default, cooldown")
cfg = g.load_config()
check("breakout disabled by default", cfg["adaptations"]["breakout_buy_enabled"] is False)
cfg["adaptations"].update({"breakout_buy_enabled": True, "breakout_buy_pct": 5.0})
st = g.default_state()
st["open_tranches"] = [{"id": "x", "level_index": "breakout", "entry_kind": "breakout",
                       "fill_price": 2000.0, "qty_eth": 1.0, "cost_usd": 2000.0,
                       "take_profit_price": 2100.0, "breakeven_stop_price": 2000.0,
                       "opened_at": g.iso(g.now_utc())}]
g.check_breakout_buy(st, cfg, 3000.0, 3000.0)   # would trigger, but one is already open
check("no second breakout while one is open", len(st["open_tranches"]) == 1)

st2 = g.default_state()
st2["breakout_cooldown_until"] = g.iso(g.now_utc() + timedelta(hours=1))
check("cooldown_active is True right after being set", g.breakout_cooldown_active(st2) is True)
g.check_breakout_buy(st2, cfg, 3000.0, 3000.0)
check("no buy during breakout cooldown", len(st2["open_tranches"]) == 0)


# --------------------------------------------------------------------------- #
print("\n# breakout-buy: breakeven-stop exit vs take-profit exit")
cfg = g.load_config()
cfg["adaptations"].update({"breakout_buy_enabled": True, "breakout_cooldown_hours": 6})
cfg["execution"]["market_slippage_bps"] = 5
st = g.default_state()
st["anchor_price"] = 2500.0
st["grid_levels"] = []   # isolate the breakout exit from any dip-rung interaction
st["open_tranches"] = [{"id": "bo1", "level_index": "breakout", "entry_kind": "breakout",
                       "fill_price": 2200.0, "qty_eth": 1.0, "cost_usd": 2200.0,
                       "buy_fee_usd": 13.2, "take_profit_price": 2288.0,
                       "breakeven_stop_price": 2200.0,
                       "opened_at": g.iso(g.now_utc())}]
g.process_fills(st, cfg, 2150.0, band=(2140.0, 2210.0))   # band crosses the 2200 stop
check("breakout closed on breakeven stop", len(st["open_tranches"]) == 0)
cl = st["closed_tranches"][-1]
check("close reason is breakeven_stop", cl["close_reason"] == "breakeven_stop")
check("stop exit recorded as taker", cl["sell_fill_kind"] == "taker")
check("stop exit slipped below the stop price", cl["sell_price"] < 2200.0, f"{cl['sell_price']}")
check("breakout cooldown armed after a stop-out", g.breakout_cooldown_active(st) is True)

st2 = g.default_state()
st2["anchor_price"] = 2500.0
st2["grid_levels"] = []
st2["open_tranches"] = [{"id": "bo2", "level_index": "breakout", "entry_kind": "breakout",
                        "fill_price": 2200.0, "qty_eth": 1.0, "cost_usd": 2200.0,
                        "buy_fee_usd": 13.2, "take_profit_price": 2288.0,
                        "breakeven_stop_price": 2200.0,
                        "opened_at": g.iso(g.now_utc())}]
g.process_fills(st2, cfg, 2300.0, band=(2260.0, 2300.0))  # band crosses the 2288 take-profit
check("breakout closed on take-profit instead", len(st2["open_tranches"]) == 0)
cl2 = st2["closed_tranches"][-1]
check("take-profit exit reason", cl2["close_reason"] == "take_profit")
check("take-profit exit recorded as maker", cl2["sell_fill_kind"] == "maker")
check("no breakout cooldown after a take-profit exit", g.breakout_cooldown_active(st2) is False)


# --------------------------------------------------------------------------- #
print("\n# breakout-buy: doesn't crowd out the dip rungs' capacity")
cfg = g.load_config()
cfg["adaptations"] = {}
st = g.default_state()
st["anchor_price"] = 2500.0
st["grid_levels"] = g.build_levels(2500.0, 5.0, 3)
st["open_tranches"] = [{"id": "bo3", "level_index": "breakout", "entry_kind": "breakout",
                       "fill_price": 2000.0, "qty_eth": 1.0, "cost_usd": 2000.0,
                       "take_profit_price": 2100.0, "breakeven_stop_price": 2000.0,
                       "opened_at": g.iso(g.now_utc())}]
ok, reason = g.can_open_new_tranche(st, cfg)
check("a dip rung can still open with a breakout tranche already held", ok is True, reason)


# --------------------------------------------------------------------------- #
print("\n# skip-log dedup")
cfg = g.load_config()
cfg["adaptations"]["trend_filter_enabled"] = True
logged = []
g.log_event = lambda msg, level="INFO": logged.append(msg)
st = g.default_state()
st["anchor_price"] = 2500.0
st["grid_levels"] = g.build_levels(2500.0, 5.0, 3)
st["halted"] = True
lvl = st["grid_levels"][0]
for _ in range(5):
    g.open_tranche(st, cfg, lvl, 2300.0)
check("repeated same-reason skip logged once", sum("BUY skipped" in m for m in logged) == 1,
      f"logged {sum('BUY skipped' in m for m in logged)}")
g.log_event = lambda *a, **k: None


# --------------------------------------------------------------------------- #
print(f"\n{'='*50}\n  {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
