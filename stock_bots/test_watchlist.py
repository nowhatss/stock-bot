#!/usr/bin/env python3
"""
Targeted tests for the watchlist grid bot's ranking/eligibility logic.
No network, no orders. Run:  python test_watchlist.py
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
import grid_bot_watchlist as wl  # noqa: E402

# Redirect every file either module might write so tests never touch real logs/state.
_TMP = tempfile.mkdtemp(prefix="watchlist_test_")
for _attr in ("EVENTS_LOG", "HEARTBEAT_LOG"):
    setattr(g, _attr, os.path.join(_TMP, _attr.lower() + ".txt"))
for _attr in ("TRADES_CSV", "DAILY_CSV", "CANDLE_CACHE"):
    setattr(wl, _attr, os.path.join(_TMP, _attr.lower() + ".txt"))

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


def make_cfg(ranking_mode="vol_normalized_dip", max_tranches_per_symbol=1, tranche_size=1000.0,
            num_levels=3, spacing=5.0, tp=4.0, max_capital=9000.0, max_trades_per_day=10,
            min_dip_pct=1.0, min_vol_pct_floor=0.5, max_hold_enabled=False, max_hold_days=10.0,
            max_hold_min_profit_pct=4.0, reanchor_enabled=False, reanchor_breakout_pct=8.0,
            stop_loss_enabled=False, stop_loss_pct=10.0,
            market_slippage_bps=0) -> dict:
    return {
        "watchlist_symbols": [],
        "allocated_capital_usd": 10000.0,
        "grid": {"num_levels": num_levels, "grid_spacing_pct": spacing,
                 "tranche_size_usd": tranche_size, "take_profit_pct": tp},
        "risk": {"max_capital_deployed_usd": max_capital, "hard_stop_loss_pct": 50.0,
                 "max_trades_per_day": max_trades_per_day, "max_daily_loss_pct": 40.0,
                 "max_tranches_per_symbol": max_tranches_per_symbol,
                 "max_hold_enabled": max_hold_enabled, "max_hold_days": max_hold_days,
                 "max_hold_min_profit_pct": max_hold_min_profit_pct,
                 "stop_loss_enabled": stop_loss_enabled, "stop_loss_pct": stop_loss_pct},
        "reanchor": {"enabled": reanchor_enabled, "breakout_pct": reanchor_breakout_pct},
        "ranking": {"mode": ranking_mode, "min_dip_pct": min_dip_pct,
                    "min_vol_pct_floor": min_vol_pct_floor,
                    "vol_lookback_days": 20, "high_lookback_days": 20},
        "fees": {"fee_rate_per_side": 0.0017, "fee_rate_maker": 0.0017, "fee_rate_taker": 0.0017},
        "execution": {"mode": "dry_run", "poll_interval_sec": 60, "heartbeat_interval_sec": 300,
                      "shallow_touch_pct": 0.15, "shallow_fill_prob": 0.5,
                      "market_slippage_bps": market_slippage_bps, "fill_seed": 1, "log_previews": False},
        "price_feed": {"timeout_sec": 10},
        "notifications": {"enabled": False},
    }


def make_state(symbols_data: dict) -> dict:
    return {
        "symbols": symbols_data,
        "open_tranches": [],
        "closed_tranches": [],
        "realized_pnl_usd": 0.0,
        "fees_paid_usd": 0.0,
        "day": {"date": g.today_str(), "buy_count": 0, "sell_count": 0, "realized_pnl_usd": 0.0},
        "halted": False, "paused": False, "halt_reason": None,
        "_fill_calls": 0, "_last_skips": {},
    }


# --------------------------------------------------------------------------- #
print("\n# dip_pct")
check("at high -> 0", wl.dip_pct(100.0, 100.0) == 0.0)
check("below high -> positive", abs(wl.dip_pct(90.0, 100.0) - 10.0) < 1e-9)
check("above high -> clamped to 0", wl.dip_pct(110.0, 100.0) == 0.0)
check("no high -> 0", wl.dip_pct(90.0, None) == 0.0)
check("no price -> 0", wl.dip_pct(None, 100.0) == 0.0)


# --------------------------------------------------------------------------- #
print("\n# score_vol_normalized_dip (option 3)")
check("dip below min_dip_pct -> None",
      wl.score_vol_normalized_dip(99.5, 100.0, 1.0, 0.5, 1.0) is None)
check("dip/vol score", abs(wl.score_vol_normalized_dip(95.0, 100.0, 2.0, 0.5, 1.0) - 2.5) < 1e-9)
check("near-zero vol floored", abs(wl.score_vol_normalized_dip(95.0, 100.0, 0.01, 0.5, 1.0) - 10.0) < 1e-9)


# --------------------------------------------------------------------------- #
print("\n# score_grid_depth (option 2, built but inactive)")
levels = g.build_levels(100.0, 5.0, 3)   # rungs ~95 / 90.25 / 85.7375
for lvl in levels:
    lvl["primed"] = True
check("no rung reached -> None", wl.score_grid_depth(levels, 96.0) is None)
check("reaches rung 0 -> score 1", wl.score_grid_depth(levels, 94.0) == 1.0)
check("reaches rungs 0+1 -> score 2 (deepest)", wl.score_grid_depth(levels, 90.0) == 2.0)


# --------------------------------------------------------------------------- #
print("\n# eligible_candidates -- vol_normalized_dip ranks by dip/vol, not just dip size")
cfg = make_cfg(ranking_mode="vol_normalized_dip")
levels_a = g.build_levels(100.0, 5.0, 1)
levels_b = g.build_levels(100.0, 5.0, 1)
for lvl in levels_a + levels_b:
    lvl["primed"] = True
state = make_state({
    "A": {"grid_levels": levels_a, "last_price": 95.0, "prev_price": 100.0,
          "recent_high": 100.0, "vol_pct": 2.0},   # same 5% dip, choppier -> lower score
    "B": {"grid_levels": levels_b, "last_price": 95.0, "prev_price": 100.0,
          "recent_high": 100.0, "vol_pct": 1.0},   # same 5% dip, calmer -> higher score
})
cands = wl.eligible_candidates(state, cfg)
check("both symbols eligible", len(cands) == 2)
check("calmer stock (B) ranked first for the same raw dip", cands[0][0] == "B", str(cands))


# --------------------------------------------------------------------------- #
print("\n# eligible_candidates -- grid_depth ranks by rungs reached")
cfg_gd = make_cfg(ranking_mode="grid_depth")
levels_a2 = g.build_levels(100.0, 5.0, 3)
levels_b2 = g.build_levels(100.0, 5.0, 3)
for lvl in levels_a2 + levels_b2:
    lvl["primed"] = True
state_gd = make_state({
    "A": {"grid_levels": levels_a2, "last_price": 94.0, "prev_price": 100.0},   # reaches rung 0 only
    "B": {"grid_levels": levels_b2, "last_price": 89.0, "prev_price": 100.0},   # reaches rungs 0+1
})
cands_gd = wl.eligible_candidates(state_gd, cfg_gd)
check("deeper symbol (B) ranked first", cands_gd[0][0] == "B", str(cands_gd))
check("scores reflect rung depth", cands_gd[0][1] == 2.0 and cands_gd[1][1] == 1.0, str(cands_gd))


# --------------------------------------------------------------------------- #
print("\n# eligible_candidates -- max_tranches_per_symbol excludes a symbol already at cap")
cfg_cap = make_cfg(max_tranches_per_symbol=1)
levels_c = g.build_levels(100.0, 5.0, 1)
for lvl in levels_c:
    lvl["primed"] = True
state_cap = make_state({
    "A": {"grid_levels": levels_c, "last_price": 95.0, "prev_price": 100.0,
          "recent_high": 100.0, "vol_pct": 1.0},
})
state_cap["open_tranches"] = [{"id": "x", "symbol": "A", "level_index": 0, "cost_usd": 1000.0,
                               "qty": 1.0, "fill_price": 95.0, "take_profit_price": 99.0,
                               "opened_at": g.iso(g.now_utc())}]
check("symbol already at max_tranches_per_symbol is excluded",
      len(wl.eligible_candidates(state_cap, cfg_cap)) == 0)


# --------------------------------------------------------------------------- #
print("\n# default config: option 3 active, option 2 built but inactive")
real_cfg = wl.load_config()
check("default ranking mode is vol_normalized_dip",
      real_cfg["ranking"]["mode"] == "vol_normalized_dip")
check("grid_depth is not the default", real_cfg["ranking"]["mode"] != "grid_depth")


# --------------------------------------------------------------------------- #
print("\n# process_fills -- shared capital cap: best-ranked symbol wins, not first-come")
cfg2 = make_cfg(ranking_mode="vol_normalized_dip", tranche_size=1000.0, max_capital=1000.0,
                num_levels=1, spacing=5.0)
levels_a3 = g.build_levels(100.0, 5.0, 1)   # rung @ 95.0
levels_b3 = g.build_levels(100.0, 5.0, 1)   # rung @ 95.0
for lvl in levels_a3 + levels_b3:
    lvl["primed"] = True
state2 = make_state({
    "A": {"grid_levels": levels_a3, "last_price": 90.0, "prev_price": 100.0,
          "recent_high": 100.0, "vol_pct": 5.0},    # choppier -> lower score
    "B": {"grid_levels": levels_b3, "last_price": 90.0, "prev_price": 100.0,
          "recent_high": 100.0, "vol_pct": 1.0},    # calmer -> higher score, should win
})
wl.process_fills(state2, cfg2)
check("only one tranche opened -- capital cap allows just one", len(state2["open_tranches"]) == 1,
      f"open={len(state2['open_tranches'])}")
check("the better-ranked symbol (B) won the shared capital",
      state2["open_tranches"] and state2["open_tranches"][0]["symbol"] == "B",
      str(state2["open_tranches"]))


# --------------------------------------------------------------------------- #
print("\n# process_fills -- take-profit exit uses the tranche's own symbol price")
cfg3 = make_cfg()
state3 = make_state({
    "A": {"grid_levels": [], "last_price": 105.0, "prev_price": 100.0,
          "recent_high": 100.0, "vol_pct": 1.0},
})
state3["open_tranches"] = [{"id": "t1", "symbol": "A", "level_index": 0, "cost_usd": 1000.0,
                            "qty": 10.0, "fill_price": 100.0, "take_profit_price": 104.0,
                            "opened_at": g.iso(g.now_utc())}]
wl.process_fills(state3, cfg3)
check("take-profit exit closes the tranche", len(state3["open_tranches"]) == 0)
check("closed tranche recorded a positive pnl", state3["closed_tranches"][-1]["pnl_usd"] > 0,
      str(state3["closed_tranches"][-1]["pnl_usd"] if state3["closed_tranches"] else None))


# --------------------------------------------------------------------------- #
print("\n# check_max_hold -- profit-lock time-stop (off by default)")
old_ts = g.iso(g.now_utc() - timedelta(days=15))
young_ts = g.iso(g.now_utc() - timedelta(days=1))

cfg_mh_off = make_cfg(max_hold_enabled=False)
state_mh_off = make_state({"A": {"grid_levels": [], "last_price": 110.0}})
state_mh_off["open_tranches"] = [{"id": "t", "symbol": "A", "level_index": 0, "cost_usd": 1000.0,
                                  "qty": 10.0, "fill_price": 100.0, "take_profit_price": 999.0,
                                  "opened_at": old_ts}]
wl.check_max_hold(state_mh_off, cfg_mh_off)
check("disabled by default -- old + profitable tranche left alone",
      len(state_mh_off["open_tranches"]) == 1)

cfg_mh = make_cfg(max_hold_enabled=True, max_hold_days=10.0, max_hold_min_profit_pct=4.0)

state_mh_win = make_state({"A": {"grid_levels": [], "last_price": 106.0}})   # +6% > 4% floor
state_mh_win["open_tranches"] = [{"id": "t", "symbol": "A", "level_index": 0, "cost_usd": 1000.0,
                                  "qty": 10.0, "fill_price": 100.0, "take_profit_price": 999.0,
                                  "opened_at": old_ts}]
wl.check_max_hold(state_mh_win, cfg_mh)
check("old tranche above the profit floor IS force-closed",
      len(state_mh_win["open_tranches"]) == 0)
check("force-close recorded as taker (market exit, not a resting limit)",
      state_mh_win["closed_tranches"][-1]["sell_fill_kind"] == "taker")
check("close reason mentions max_hold",
      state_mh_win["closed_tranches"][-1]["close_reason"].startswith("max_hold"))

state_mh_under_floor = make_state({"A": {"grid_levels": [], "last_price": 102.0}})   # +2% < 4% floor
state_mh_under_floor["open_tranches"] = [{"id": "t", "symbol": "A", "level_index": 0,
                                          "cost_usd": 1000.0, "qty": 10.0, "fill_price": 100.0,
                                          "take_profit_price": 999.0, "opened_at": old_ts}]
wl.check_max_hold(state_mh_under_floor, cfg_mh)
check("old tranche BELOW the profit floor is left alone (not a stop-loss)",
      len(state_mh_under_floor["open_tranches"]) == 1)

state_mh_underwater = make_state({"A": {"grid_levels": [], "last_price": 90.0}})   # -10%, losing
state_mh_underwater["open_tranches"] = [{"id": "t", "symbol": "A", "level_index": 0,
                                         "cost_usd": 1000.0, "qty": 10.0, "fill_price": 100.0,
                                         "take_profit_price": 999.0, "opened_at": old_ts}]
wl.check_max_hold(state_mh_underwater, cfg_mh)
check("old UNDERWATER tranche is left alone (no forced loss)",
      len(state_mh_underwater["open_tranches"]) == 1)

state_mh_young = make_state({"A": {"grid_levels": [], "last_price": 110.0}})   # +10%, but too young
state_mh_young["open_tranches"] = [{"id": "t", "symbol": "A", "level_index": 0, "cost_usd": 1000.0,
                                    "qty": 10.0, "fill_price": 100.0, "take_profit_price": 999.0,
                                    "opened_at": young_ts}]
wl.check_max_hold(state_mh_young, cfg_mh)
check("profitable but not yet held long enough -- left alone",
      len(state_mh_young["open_tranches"]) == 1)


# --------------------------------------------------------------------------- #
print("\n# ensure_grid_for_symbol -- re-anchoring (off by default)")

cfg_ra_off = make_cfg(reanchor_enabled=False)
state_ra_off = make_state({"X": wl.default_symbol_state()})
state_ra_off["symbols"]["X"]["anchor_price"] = 100.0
state_ra_off["symbols"]["X"]["grid_levels"] = g.build_levels(100.0, 5.0, 3)
wl.ensure_grid_for_symbol(state_ra_off, cfg_ra_off, "X", 120.0)   # +20%, way past any breakout
check("disabled by default -- anchor unchanged even on a big breakout",
      state_ra_off["symbols"]["X"]["anchor_price"] == 100.0)

cfg_ra = make_cfg(reanchor_enabled=True, reanchor_breakout_pct=8.0)

state_ra_flat = make_state({"X": wl.default_symbol_state()})
state_ra_flat["symbols"]["X"]["anchor_price"] = 100.0
state_ra_flat["symbols"]["X"]["grid_levels"] = g.build_levels(100.0, 5.0, 3)
wl.ensure_grid_for_symbol(state_ra_flat, cfg_ra, "X", 109.0)   # +9% > 8% breakout, flat
check("enabled + flat + past breakout -- anchor moves to current price",
      state_ra_flat["symbols"]["X"]["anchor_price"] == 109.0)
check("rungs rebuilt off the new anchor",
      abs(state_ra_flat["symbols"]["X"]["grid_levels"][0]["price"] - 109.0 * 0.95) < 0.01)

state_ra_notyet = make_state({"X": wl.default_symbol_state()})
state_ra_notyet["symbols"]["X"]["anchor_price"] = 100.0
state_ra_notyet["symbols"]["X"]["grid_levels"] = g.build_levels(100.0, 5.0, 3)
wl.ensure_grid_for_symbol(state_ra_notyet, cfg_ra, "X", 105.0)   # +5% < 8% breakout
check("enabled but under the breakout threshold -- anchor unchanged",
      state_ra_notyet["symbols"]["X"]["anchor_price"] == 100.0)

state_ra_holding = make_state({"X": wl.default_symbol_state()})
state_ra_holding["symbols"]["X"]["anchor_price"] = 100.0
state_ra_holding["symbols"]["X"]["grid_levels"] = g.build_levels(100.0, 5.0, 3)
state_ra_holding["open_tranches"] = [{"id": "t", "symbol": "X", "level_index": 0, "cost_usd": 1000.0,
                                      "qty": 10.0, "fill_price": 95.0, "take_profit_price": 99.0,
                                      "opened_at": g.iso(g.now_utc())}]
wl.ensure_grid_for_symbol(state_ra_holding, cfg_ra, "X", 120.0)   # well past breakout, but holding
check("enabled + past breakout but HOLDING a position -- never reshapes",
      state_ra_holding["symbols"]["X"]["anchor_price"] == 100.0)


# --------------------------------------------------------------------------- #
print("\n# check_stop_loss -- immediate crash protection (off by default)")

cfg_sl_off = make_cfg(stop_loss_enabled=False)
state_sl_off = make_state({"A": {"grid_levels": [], "last_price": 85.0, "prev_price": 100.0}})
state_sl_off["open_tranches"] = [{"id": "t", "symbol": "A", "level_index": 0, "cost_usd": 1000.0,
                                  "qty": 10.0, "fill_price": 100.0, "take_profit_price": 999.0,
                                  "opened_at": g.iso(g.now_utc())}]
wl.check_stop_loss(state_sl_off, cfg_sl_off)
check("disabled by default -- a 15% crash is left alone", len(state_sl_off["open_tranches"]) == 1)

cfg_sl = make_cfg(stop_loss_enabled=True, stop_loss_pct=10.0, market_slippage_bps=10)

state_sl_hit = make_state({"A": {"grid_levels": [], "last_price": 85.0, "prev_price": 100.0}})
state_sl_hit["open_tranches"] = [{"id": "t", "symbol": "A", "level_index": 0, "cost_usd": 1000.0,
                                  "qty": 10.0, "fill_price": 100.0, "take_profit_price": 999.0,
                                  "opened_at": g.iso(g.now_utc())}]
wl.check_stop_loss(state_sl_hit, cfg_sl)
check("15% drop past a 10% stop IS force-closed immediately", len(state_sl_hit["open_tranches"]) == 0)
closed = state_sl_hit["closed_tranches"][-1] if state_sl_hit["closed_tranches"] else {}
check("stop-loss recorded as taker (a real stop order, not a resting limit)",
      closed.get("sell_fill_kind") == "taker")
check("close reason is stop_loss", closed.get("close_reason") == "stop_loss")
check("gapped-through exit fills at the worse market price (85), not the stop price (90) "
      "-- a real stop order, minus slippage",
      abs(closed.get("sell_price", 0) - 85.0 * 0.999) < 0.01, str(closed.get("sell_price")))

state_sl_notyet = make_state({"A": {"grid_levels": [], "last_price": 92.0, "prev_price": 100.0}})
state_sl_notyet["open_tranches"] = [{"id": "t", "symbol": "A", "level_index": 0, "cost_usd": 1000.0,
                                     "qty": 10.0, "fill_price": 100.0, "take_profit_price": 999.0,
                                     "opened_at": g.iso(g.now_utc())}]
wl.check_stop_loss(state_sl_notyet, cfg_sl)
check("an 8% dip does NOT trigger a 10% stop", len(state_sl_notyet["open_tranches"]) == 1)

state_sl_band = make_state({"A": {"grid_levels": [], "last_price": 95.0}})
state_sl_band["open_tranches"] = [{"id": "t", "symbol": "A", "level_index": 0, "cost_usd": 1000.0,
                                   "qty": 10.0, "fill_price": 100.0, "take_profit_price": 999.0,
                                   "opened_at": g.iso(g.now_utc())}]
wl.check_stop_loss(state_sl_band, cfg_sl, price_bands={"A": (88.0, 96.0)})
check("triggers off the OHLC band's low, not just the close (backtest price_bands)",
      len(state_sl_band["open_tranches"]) == 0)

state_sl_immediate = make_state({"A": {"grid_levels": [], "last_price": 85.0, "prev_price": 100.0}})
state_sl_immediate["open_tranches"] = [{"id": "t", "symbol": "A", "level_index": 0, "cost_usd": 1000.0,
                                        "qty": 10.0, "fill_price": 100.0, "take_profit_price": 999.0,
                                        "opened_at": g.iso(g.now_utc())}]  # opened right now, not stale
wl.check_stop_loss(state_sl_immediate, cfg_sl)
check("fires immediately -- no time gate like max_hold has",
      len(state_sl_immediate["open_tranches"]) == 0)

# no cooldown: a symbol is immediately eligible again right after its own stop-out
levels_no_cd = g.build_levels(100.0, 5.0, 1)
for lvl in levels_no_cd:
    lvl["primed"] = True
state_no_cd = make_state({
    "A": {"grid_levels": levels_no_cd, "last_price": 85.0, "prev_price": 100.0,
          "recent_high": 100.0, "vol_pct": 1.0},
})
state_no_cd["open_tranches"] = [{"id": "t", "symbol": "A", "level_index": 0, "cost_usd": 1000.0,
                                 "qty": 10.0, "fill_price": 100.0, "take_profit_price": 999.0,
                                 "opened_at": g.iso(g.now_utc())}]
wl.check_stop_loss(state_no_cd, cfg_sl)
check("stop-loss fired", len(state_no_cd["open_tranches"]) == 0)
check("no cooldown -- symbol is immediately eligible again after its own stop-out",
      len(wl.eligible_candidates(state_no_cd, cfg_sl)) == 1)
check("stop-out arms today's price-watch flag on that symbol",
      state_no_cd["symbols"]["A"]["stop_loss_watch_date"] == g.today_str())


# --------------------------------------------------------------------------- #
print("\n# stop-loss Discord alert -- always pings, distinct from a routine sell")

sent = []
_orig_discord_send = g.discord_send
g.discord_send = lambda *a, **k: sent.append((a, k))

cfg_alert = make_cfg(stop_loss_enabled=True, stop_loss_pct=10.0)
state_alert = make_state({"A": {"grid_levels": [], "last_price": 85.0, "prev_price": 100.0}})
state_alert["open_tranches"] = [{"id": "t", "symbol": "A", "level_index": 0, "cost_usd": 1000.0,
                                 "qty": 10.0, "fill_price": 100.0, "take_profit_price": 999.0,
                                 "opened_at": g.iso(g.now_utc())}]
wl.check_stop_loss(state_alert, cfg_alert)
check("a Discord message was sent for the stop-out", len(sent) == 1)
sl_kwargs = sent[0][1] if sent else {}
check("stop-loss notification uses event='alert' (always pings, not gated by 'sell')",
      sl_kwargs.get("event") == "alert", str(sl_kwargs))
check("stop-loss notification title flags it clearly",
      "STOP-LOSS" in sent[0][0][0] if sent and sent[0][0] else False,
      str(sent[0][0][0] if sent and sent[0][0] else None))

sent.clear()
state_win = make_state({"A": {"grid_levels": [], "last_price": 110.0, "prev_price": 100.0}})
state_win["open_tranches"] = [{"id": "t2", "symbol": "A", "level_index": 0, "cost_usd": 1000.0,
                               "qty": 10.0, "fill_price": 100.0, "take_profit_price": 104.0,
                               "opened_at": g.iso(g.now_utc())}]
wl.process_fills(state_win, make_cfg())
check("a routine take-profit sell still uses event='sell', not 'alert'",
      sent and sent[0][1].get("event") == "sell", str(sent))

g.discord_send = _orig_discord_send


# --------------------------------------------------------------------------- #
print("\n# notify_watched_prices -- only fires for a symbol stopped out TODAY, never pings")

g._MENTION = "@everyone"
g._MENTION_EVENTS = {"all"}
check("'silent' event never pings, even with mention_events=['all']",
      g._should_mention("silent") is False)
check("a normal 'alert' event WOULD ping under mention_events=['all'] (sanity check)",
      g._should_mention("alert") is True)

sent2 = []
g.discord_send = lambda *a, **k: sent2.append((a, k))
cfg_prices = make_cfg()
cfg_prices["watchlist_symbols"] = ["A", "B"]

# quiet day: nobody has been stopped out -- sends nothing
state_quiet = make_state({"A": {"grid_levels": [], "last_price": 123.45},
                          "B": {"grid_levels": [], "last_price": 67.89}})
wl.notify_watched_prices(state_quiet, cfg_prices)
check("no stop-loss today -- no price check-in sent", len(sent2) == 0)

# A was stopped out today -- only A gets watched
state_watch = make_state({"A": {"grid_levels": [], "last_price": 123.45,
                                "stop_loss_watch_date": g.today_str()},
                          "B": {"grid_levels": [], "last_price": 67.89}})
wl.notify_watched_prices(state_watch, cfg_prices)
check("a price check-in message was sent for the watched symbol", len(sent2) == 1)
check("price check-in uses event='silent'",
      sent2[0][1].get("event") == "silent" if sent2 else False, str(sent2))
fields_sent = sent2[0][1].get("fields", []) if sent2 else []
check("only the stopped-out symbol (A) is included, not the whole watchlist",
      {f[0] for f in fields_sent} == {"A"}, str(fields_sent))

# stop-loss watch from a PRIOR day doesn't carry over
sent2.clear()
state_stale = make_state({"A": {"grid_levels": [], "last_price": 123.45,
                                "stop_loss_watch_date": "2000-01-01"}})
wl.notify_watched_prices(state_stale, cfg_prices)
check("a stop-loss watch from a past day is expired -- nothing sent", len(sent2) == 0)

g.discord_send = _orig_discord_send
g._MENTION = None
g._MENTION_EVENTS = set()


# --------------------------------------------------------------------------- #
print("\n# compute_unrealized -- combines multiple symbols at their own current prices")
cfg4 = make_cfg()
state4 = make_state({
    "A": {"grid_levels": [], "last_price": 110.0},
    "B": {"grid_levels": [], "last_price": 90.0},
})
state4["open_tranches"] = [
    {"id": "a", "symbol": "A", "level_index": 0, "cost_usd": 1000.0, "qty": 10.0,
     "fill_price": 100.0, "take_profit_price": 999.0, "opened_at": ""},
    {"id": "b", "symbol": "B", "level_index": 0, "cost_usd": 1000.0, "qty": 10.0,
     "fill_price": 100.0, "take_profit_price": 999.0, "opened_at": ""},
]
u = wl.compute_unrealized(state4, cfg4)
rate = g.fee_rate(cfg4, "maker")
expected = ((1100.0 - 1100.0 * rate - 1000.0) + (900.0 - 900.0 * rate - 1000.0))
check("multi-symbol unrealized combines both legs correctly", abs(u - expected) < 0.01,
      f"got {u} exp {expected}")


# --------------------------------------------------------------------------- #
print(f"\n{'='*50}\n  {PASS} passed, {FAIL} failed\n{'='*50}")
sys.exit(1 if FAIL else 0)
