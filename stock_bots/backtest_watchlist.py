#!/usr/bin/env python3
"""
Historical backtest for the watchlist grid bot. Fetches real Questrade daily
OHLC candles for every symbol in watchlist_config.json, then replays them
through the ACTUAL production code (grid_bot_watchlist.iterate()/process_fills())
one simulated trading day at a time -- same principle as ../backtest.py, just
at daily bar resolution (Questrade's intraday history is much shorter than its
daily history, so daily bars are what's actually available for a multi-symbol,
multi-month replay).

By default this backtests BOTH ranking rules (vol_normalized_dip -- currently
active in watchlist_config.json -- and grid_depth -- built but inactive) over
the same data, so you can compare them before deciding whether to switch.
Nothing here changes watchlist_config.json or the live watchlist_state.json --
this writes its own state/trade files under logs/backtest/.

Needs a working Questrade connection (stock_bots/questrade_refresh_token.txt).

Run:  python backtest_watchlist.py                  (12 months, both rules)
      python backtest_watchlist.py --months 6
      python backtest_watchlist.py --no-ab           (only the currently-active rule)
      python backtest_watchlist.py --refresh         (force re-fetch candles)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT)

import grid_bot as g  # noqa: E402
import grid_bot_watchlist as wl  # noqa: E402
import market_data  # noqa: E402  -- parent module, pure sma/daily_vol_pct math only

CACHE_DIR = os.path.join(HERE, "logs", "backtest")
WARMUP_DAYS = 35   # calendar days of lead-in before the reported window, for the 20-day ranking lookbacks


# --------------------------------------------------------------------------- #
# candle fetch / cache
# --------------------------------------------------------------------------- #
def _cache_path(symbol: str) -> str:
    return os.path.join(CACHE_DIR, f"watchlist_candles_{symbol}.json")


def fetch_symbol_candles(symbol: str, calendar_days: int, refresh: bool, timeout: int = 10) -> list[dict]:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(symbol)
    if not refresh and os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                cached = json.load(fh)
            if cached.get("rows") and cached.get("calendar_days", 0) >= calendar_days:
                return cached["rows"]
        except Exception:
            pass

    import venue_questrade as vq
    candles = vq.get_daily_candles(symbol, days=calendar_days, timeout=timeout)
    dedup: dict[str, dict] = {}
    for c in candles:
        if c.get("close") is None or c.get("low") is None or c.get("high") is None:
            continue
        date = str(c["start"])[:10]
        dedup[date] = {"date": date, "low": float(c["low"]), "high": float(c["high"]),
                       "close": float(c["close"])}
    rows = [dedup[d] for d in sorted(dedup)]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"calendar_days": calendar_days, "rows": rows}, fh)
    return rows


# --------------------------------------------------------------------------- #
# simulated clock (drives g.now_utc() so day-rollover / risk rails / trade
# timestamps all key off the SIMULATED date, not wall-clock time)
# --------------------------------------------------------------------------- #
class Clock:
    def __init__(self) -> None:
        self.now: datetime | None = None

    def now_utc(self) -> datetime:
        return self.now


# --------------------------------------------------------------------------- #
# one full replay, for one ranking mode / take-profit setting
# --------------------------------------------------------------------------- #
def run_one(cfg_base: dict, symbols: list[str], by_date: dict[str, dict[str, dict]],
           replay_dates: list[str], window_start: str, mode: str, *,
           take_profit_pct: float | None = None, stop_loss_pct: float | None = None,
           label: str | None = None) -> dict:
    cfg = json.loads(json.dumps(cfg_base))   # deep copy -- each run is independent
    cfg["watchlist_symbols"] = symbols
    cfg["ranking"]["mode"] = mode
    cfg["notifications"]["enabled"] = False
    if take_profit_pct is not None:
        cfg["grid"]["take_profit_pct"] = take_profit_pct
    if stop_loss_pct is not None:
        cfg["risk"]["stop_loss_enabled"] = True
        cfg["risk"]["stop_loss_pct"] = stop_loss_pct
    label = label or mode

    clock = Clock()
    g.now_utc = clock.now_utc
    g.discord_send = lambda *a, **k: None
    g.log_event = lambda *a, **k: None
    wl.print_summary = lambda *a, **k: None   # daily_rollover() would otherwise print one block per simulated day
    # bypass the wall-clock-cached candle refresh entirely -- we set the ranking
    # inputs (recent_high/vol_pct) directly below, from data available as of
    # each simulated day only, so there's no lookahead and no stale real-time cache.
    wl.refresh_symbol_market_data = lambda *a, **k: None

    wl.TRADES_CSV = os.path.join(CACHE_DIR, f"watchlist_trades_{label}.csv")
    wl.DAILY_CSV = os.path.join(CACHE_DIR, f"watchlist_daily_{label}_backtest.csv")
    wl.STATE_PATH = os.path.join(CACHE_DIR, f"watchlist_state_{label}_backtest.json")
    for p in (wl.TRADES_CSV, wl.DAILY_CSV, wl.STATE_PATH):
        if os.path.exists(p):
            os.remove(p)

    current_closes: dict[str, float] = {}

    def fake_quotes(syms, timeout=10):
        return dict(current_closes)

    import venue_questrade as vq
    vq.get_quotes_batch = fake_quotes
    wl.vq = vq

    clock.now = datetime.strptime(replay_dates[0], "%Y-%m-%d").replace(hour=20, tzinfo=timezone.utc)
    state = wl.default_state(cfg)
    hist_so_far: dict[str, list[dict]] = {s: [] for s in symbols}
    baseline = None
    r = cfg["ranking"]
    high_lb = int(r["high_lookback_days"])
    vol_lb = int(r["vol_lookback_days"])

    for d in replay_dates:
        clock.now = datetime.strptime(d, "%Y-%m-%d").replace(hour=20, tzinfo=timezone.utc)

        current_closes.clear()
        bands: dict[str, tuple[float, float]] = {}
        for s in symbols:
            row = by_date.get(s, {}).get(d)
            if row is None:
                continue
            hist_so_far[s].append(row)
            bands[s] = (row["low"], row["high"])
            current_closes[s] = row["close"]

            highs = [rr["high"] for rr in hist_so_far[s][-high_lb:]]
            closes = [rr["close"] for rr in hist_so_far[s]]
            vol = market_data.daily_vol_pct(closes, vol_lb)
            sdata = state["symbols"].setdefault(s, wl.default_symbol_state())
            if highs:
                sdata["recent_high"] = max(highs)
            if vol is not None:
                sdata["vol_pct"] = vol

        wl.iterate(state, cfg, price_bands=bands)

        if baseline is None and d >= window_start:
            baseline = {"realized_pnl_usd": state["realized_pnl_usd"],
                       "fees_paid_usd": state["fees_paid_usd"]}

    if baseline is None:
        baseline = {"realized_pnl_usd": 0.0, "fees_paid_usd": 0.0}

    closed_in_window = [t for t in state["closed_tranches"] if t.get("closed_at", "") >= window_start]
    wins = sum(1 for t in closed_in_window if t["pnl_usd"] > 0)
    losses = len(closed_in_window) - wins
    realized = round(state["realized_pnl_usd"] - baseline["realized_pnl_usd"], 2)
    fees = round(state["fees_paid_usd"] - baseline["fees_paid_usd"], 2)
    unrealized = wl.compute_unrealized(state, cfg)

    per_symbol: dict[str, dict] = {}
    for t in closed_in_window:
        d_ = per_symbol.setdefault(t["symbol"], {"trades": 0, "wins": 0, "pnl": 0.0})
        d_["trades"] += 1
        d_["wins"] += 1 if t["pnl_usd"] > 0 else 0
        d_["pnl"] += t["pnl_usd"]

    return {
        "mode": mode,
        "take_profit_pct": cfg["grid"]["take_profit_pct"],
        "stop_loss_pct": cfg["risk"].get("stop_loss_pct"),
        "stop_loss_enabled": cfg["risk"].get("stop_loss_enabled", False),
        "closed_trades": len(closed_in_window),
        "wins": wins, "losses": losses,
        "win_rate_pct": round(100 * wins / len(closed_in_window), 1) if closed_in_window else None,
        "realized_pnl_usd": realized,
        "unrealized_pnl_usd": unrealized,
        "fees_paid_usd": fees,
        "total_pnl_usd": round(realized + unrealized, 2),
        "open_at_end": len(state["open_tranches"]),
        "symbols_open_at_end": sorted({t["symbol"] for t in state["open_tranches"]}),
        "per_symbol": per_symbol,
        "allocated_capital_usd": cfg["allocated_capital_usd"],
    }


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def prepare_data(months: int, refresh: bool, max_capital_override: float | None = None,
                 enable_max_hold: bool = False, enable_reanchor: bool = False,
                 enable_stop_loss: bool = False, stop_loss_pct: float | None = None):
    cfg_base = wl.load_config()
    if max_capital_override is not None:
        cfg_base["risk"]["max_capital_deployed_usd"] = max_capital_override
    if enable_max_hold:
        cfg_base["risk"]["max_hold_enabled"] = True
    if enable_reanchor:
        cfg_base.setdefault("reanchor", {})["enabled"] = True
    if enable_stop_loss:
        cfg_base["risk"]["stop_loss_enabled"] = True
        if stop_loss_pct is not None:
            cfg_base["risk"]["stop_loss_pct"] = stop_loss_pct
    symbols = cfg_base["watchlist_symbols"]
    calendar_days = int(months * 31 + WARMUP_DAYS + 15)

    per_symbol_rows: dict[str, list[dict]] = {}
    for sym in symbols:
        try:
            rows = fetch_symbol_candles(sym, calendar_days, refresh,
                                        timeout=cfg_base["price_feed"]["timeout_sec"])
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: failed to fetch candles for {sym}: {exc!r} -- excluding it from this backtest")
            rows = []
        if rows:
            per_symbol_rows[sym] = rows

    if not per_symbol_rows:
        raise SystemExit("No candle data fetched for any watchlist symbol -- aborting.")

    by_date = {s: {r["date"]: r for r in rows} for s, rows in per_symbol_rows.items()}
    all_dates = sorted({r["date"] for rows in per_symbol_rows.values() for r in rows})
    end_date_str = all_dates[-1]
    end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
    window_start_dt = end_date - timedelta(days=int(months * 30.44))
    window_start = window_start_dt.strftime("%Y-%m-%d")
    replay_start = (window_start_dt - timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d")
    replay_dates = [d for d in all_dates if d >= replay_start]
    if not replay_dates:
        raise SystemExit("Not enough candle history for the requested window.")

    return cfg_base, list(per_symbol_rows.keys()), by_date, replay_dates, window_start, end_date_str


def run(months: int, refresh: bool, modes: list[str], max_capital_override: float | None = None,
       enable_reanchor: bool = False, enable_stop_loss: bool = False,
       stop_loss_pct: float | None = None) -> tuple[dict, dict]:
    cfg_base, symbols_used, by_date, replay_dates, window_start, end_date_str = prepare_data(
        months, refresh, max_capital_override, enable_reanchor=enable_reanchor,
        enable_stop_loss=enable_stop_loss, stop_loss_pct=stop_loss_pct)

    results = {mode: run_one(cfg_base, symbols_used, by_date, replay_dates, window_start, mode)
              for mode in modes}
    meta = {"symbols": symbols_used, "window_start": window_start,
            "end_date": end_date_str, "replay_start": replay_dates[0],
            "active_mode": cfg_base["ranking"]["mode"],
            "reanchor_enabled": cfg_base.get("reanchor", {}).get("enabled", False),
            "reanchor_breakout_pct": cfg_base.get("reanchor", {}).get("breakout_pct"),
            "max_hold_enabled": cfg_base["risk"].get("max_hold_enabled", False),
            "max_hold_days": cfg_base["risk"].get("max_hold_days"),
            "max_hold_min_profit_pct": cfg_base["risk"].get("max_hold_min_profit_pct"),
            "stop_loss_enabled": cfg_base["risk"].get("stop_loss_enabled", False),
            "stop_loss_pct": cfg_base["risk"].get("stop_loss_pct"),
            "take_profit_pct": cfg_base["grid"]["take_profit_pct"],
            "max_capital_deployed_usd": cfg_base["risk"]["max_capital_deployed_usd"],
            "tranche_size_usd": cfg_base["grid"]["tranche_size_usd"]}
    return results, meta


def run_tp_sweep(months: int, refresh: bool, tp_values: list[float],
                 enable_max_hold: bool = False, enable_reanchor: bool = False) -> tuple[dict, dict]:
    """Backtests a set of take_profit_pct values against the same data, holding
    everything else (ranking mode, capital, spacing) at whatever's currently in
    watchlist_config.json -- isolates the take-profit question specifically."""
    cfg_base, symbols_used, by_date, replay_dates, window_start, end_date_str = prepare_data(
        months, refresh, enable_max_hold=enable_max_hold, enable_reanchor=enable_reanchor)
    mode = cfg_base["ranking"]["mode"]

    results = {}
    for tp in tp_values:
        label = f"tp_{tp:g}pct"
        results[label] = run_one(cfg_base, symbols_used, by_date, replay_dates, window_start, mode,
                                 take_profit_pct=tp, label=label)
    meta = {"symbols": symbols_used, "window_start": window_start,
            "end_date": end_date_str, "replay_start": replay_dates[0],
            "ranking_mode": mode, "active_tp_pct": cfg_base["grid"]["take_profit_pct"],
            "max_capital_deployed_usd": cfg_base["risk"]["max_capital_deployed_usd"],
            "tranche_size_usd": cfg_base["grid"]["tranche_size_usd"],
            "max_hold_enabled": cfg_base["risk"].get("max_hold_enabled", False),
            "max_hold_days": cfg_base["risk"].get("max_hold_days"),
            "max_hold_min_profit_pct": cfg_base["risk"].get("max_hold_min_profit_pct"),
            "reanchor_enabled": cfg_base.get("reanchor", {}).get("enabled", False),
            "reanchor_breakout_pct": cfg_base.get("reanchor", {}).get("breakout_pct")}
    return results, meta


def run_sl_sweep(months: int, refresh: bool, sl_values: list[float]) -> tuple[dict, dict]:
    """Backtests a set of stop_loss_pct values against the same data, holding
    everything else (take-profit, ranking mode, capital, re-anchor, max-hold)
    at whatever's currently in watchlist_config.json -- isolates the
    stop-loss distance question specifically."""
    cfg_base, symbols_used, by_date, replay_dates, window_start, end_date_str = prepare_data(months, refresh)
    mode = cfg_base["ranking"]["mode"]

    results = {}
    for sl in sl_values:
        label = f"sl_{sl:g}pct"
        results[label] = run_one(cfg_base, symbols_used, by_date, replay_dates, window_start, mode,
                                 stop_loss_pct=sl, label=label)
    meta = {"symbols": symbols_used, "window_start": window_start,
            "end_date": end_date_str, "replay_start": replay_dates[0],
            "ranking_mode": mode, "take_profit_pct": cfg_base["grid"]["take_profit_pct"],
            "max_capital_deployed_usd": cfg_base["risk"]["max_capital_deployed_usd"],
            "tranche_size_usd": cfg_base["grid"]["tranche_size_usd"],
            "max_hold_enabled": cfg_base["risk"].get("max_hold_enabled", False),
            "max_hold_days": cfg_base["risk"].get("max_hold_days"),
            "max_hold_min_profit_pct": cfg_base["risk"].get("max_hold_min_profit_pct"),
            "reanchor_enabled": cfg_base.get("reanchor", {}).get("enabled", False),
            "reanchor_breakout_pct": cfg_base.get("reanchor", {}).get("breakout_pct"),
            "live_stop_loss_enabled": cfg_base["risk"].get("stop_loss_enabled", False),
            "live_stop_loss_pct": cfg_base["risk"].get("stop_loss_pct")}
    return results, meta


def fmt_sl_sweep(results: dict, meta: dict, months: int) -> str:
    lines = [f"# Watchlist grid bot — stop-loss sweep — {months} months", ""]
    lines.append(f"- Symbols: {', '.join(meta['symbols'])}")
    lines.append(f"- Reported window: {meta['window_start']} to {meta['end_date']}")
    lines.append(f"- Warm-up lead-in (not counted in P&L): {meta['replay_start']} to {meta['window_start']}")
    lines.append(f"- Ranking mode held fixed at: {meta['ranking_mode']}")
    lines.append(f"- Take-profit held fixed at: {meta['take_profit_pct']}%")
    lines.append(f"- Capital: ${meta['max_capital_deployed_usd']:,} max deployed / "
                 f"${meta['tranche_size_usd']:,} per tranche")
    if meta.get("max_hold_enabled"):
        lines.append(f"- Profit-lock time-stop: ENABLED -- {meta['max_hold_days']}d / "
                     f"{meta['max_hold_min_profit_pct']}% floor")
    else:
        lines.append("- Profit-lock time-stop: disabled")
    if meta.get("reanchor_enabled"):
        lines.append(f"- Re-anchoring: ENABLED -- {meta['reanchor_breakout_pct']}% breakout threshold")
    else:
        lines.append("- Re-anchoring: disabled")
    if meta.get("live_stop_loss_enabled"):
        lines.append(f"- (Note: watchlist_config.json currently has stop_loss_pct="
                     f"{meta['live_stop_loss_pct']}% live -- each row below overrides it for that run only)")
    lines.append("")
    lines.append(
        "**Methodology**: same replay engine (real Questrade daily OHLC candles through the "
        "actual production code), with ONLY risk.stop_loss_pct varied between runs -- everything "
        "else held fixed at whatever's currently in watchlist_config.json. Daily-bar resolution -- "
        "see the other reports for the same caveat."
    )
    lines.append("")
    lines.append("| stop-loss | trades | wins | losses | win rate | open at end | "
                 "realized P&L | unrealized P&L | total P&L |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for label, r in results.items():
        wr = f"{r['win_rate_pct']}%" if r["win_rate_pct"] is not None else "n/a"
        lines.append(f"| {r['stop_loss_pct']:g}% | {r['closed_trades']} | {r['wins']} | {r['losses']} | "
                     f"{wr} | {r['open_at_end']} | ${r['realized_pnl_usd']:,} | "
                     f"${r['unrealized_pnl_usd']:,} | ${r['total_pnl_usd']:,} |")
    lines.append("")
    for label, r in results.items():
        lines.append(f"## stop_loss_pct = {r['stop_loss_pct']:g}%")
        if r["per_symbol"]:
            for sym, d in sorted(r["per_symbol"].items(), key=lambda kv: -kv[1]["pnl"]):
                wr = round(100 * d["wins"] / d["trades"], 1) if d["trades"] else 0.0
                lines.append(f"    - {sym}: {d['trades']} trades, {wr}% win rate, "
                            f"${round(d['pnl'], 2):,} pnl")
        else:
            lines.append("    - no trades")
        if r["open_at_end"]:
            lines.append(f"    - still open at report end: {', '.join(r['symbols_open_at_end'])} "
                        f"(unrealized ${r['unrealized_pnl_usd']:,})")
        lines.append("")
    return "\n".join(lines)


def fmt_tp_sweep(results: dict, meta: dict, months: int) -> str:
    lines = [f"# Watchlist grid bot — take-profit sweep — {months} months", ""]
    lines.append(f"- Symbols: {', '.join(meta['symbols'])}")
    lines.append(f"- Reported window: {meta['window_start']} to {meta['end_date']}")
    lines.append(f"- Warm-up lead-in (not counted in P&L): {meta['replay_start']} to {meta['window_start']}")
    lines.append(f"- Ranking mode held fixed at: {meta['ranking_mode']} (the currently active one)")
    lines.append(f"- Capital: ${meta['max_capital_deployed_usd']:,} max deployed / "
                 f"${meta['tranche_size_usd']:,} per tranche")
    if meta.get("max_hold_enabled"):
        lines.append(f"- Profit-lock time-stop: ENABLED -- force-closes a tranche after "
                     f"{meta['max_hold_days']} days if it's already up at least "
                     f"{meta['max_hold_min_profit_pct']}% (not a stop-loss; a stale losing "
                     f"position is left alone)")
    else:
        lines.append("- Profit-lock time-stop: disabled -- a tranche only ever exits at its "
                     "take-profit target, however long that takes")
    if meta.get("reanchor_enabled"):
        lines.append(f"- Re-anchoring: ENABLED -- a symbol's grid re-anchors on a "
                     f"{meta['reanchor_breakout_pct']}% breakout above its anchor while flat in it")
    else:
        lines.append("- Re-anchoring: disabled")
    lines.append("")
    lines.append(
        "**Methodology**: same replay engine as the ranking-mode backtest (real Questrade "
        "daily OHLC candles through the actual production code), with ONLY grid.take_profit_pct "
        "varied between runs -- everything else (grid spacing, capital, ranking mode) held fixed "
        "at whatever's currently in watchlist_config.json, so any difference below is isolated to "
        "the take-profit change. Daily-bar resolution -- see the ranking-mode report for the same caveat."
    )
    lines.append("")
    lines.append("| take-profit | trades | open at end | win rate | realized P&L | unrealized P&L | total P&L | fees |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for label, r in results.items():
        tag = " (active)" if abs(r["take_profit_pct"] - meta["active_tp_pct"]) < 1e-9 else ""
        wr = f"{r['win_rate_pct']}%" if r["win_rate_pct"] is not None else "n/a"
        lines.append(f"| {r['take_profit_pct']:g}%{tag} | {r['closed_trades']} | {r['open_at_end']} | {wr} | "
                     f"${r['realized_pnl_usd']:,} | ${r['unrealized_pnl_usd']:,} | "
                     f"${r['total_pnl_usd']:,} | ${r['fees_paid_usd']:,} |")
    lines.append("")
    for label, r in results.items():
        tag = "  (ACTIVE)" if abs(r["take_profit_pct"] - meta["active_tp_pct"]) < 1e-9 else ""
        lines.append(f"## take_profit_pct = {r['take_profit_pct']:g}%{tag}")
        if r["per_symbol"]:
            for sym, d in sorted(r["per_symbol"].items(), key=lambda kv: -kv[1]["pnl"]):
                wr = round(100 * d["wins"] / d["trades"], 1) if d["trades"] else 0.0
                lines.append(f"    - {sym}: {d['trades']} trades, {wr}% win rate, "
                            f"${round(d['pnl'], 2):,} pnl")
        else:
            lines.append("    - no trades")
        if r["open_at_end"]:
            lines.append(f"    - still open at report end: {', '.join(r['symbols_open_at_end'])} "
                        f"(unrealized ${r['unrealized_pnl_usd']:,})")
        lines.append("")
    return "\n".join(lines)


def fmt(results: dict, meta: dict, months: int) -> str:
    lines = [f"# Watchlist grid bot backtest — {months} months", ""]
    lines.append(f"- Symbols: {', '.join(meta['symbols'])}")
    lines.append(f"- Reported window: {meta['window_start']} to {meta['end_date']}")
    lines.append(f"- Warm-up lead-in (not counted in P&L): {meta['replay_start']} to {meta['window_start']}")
    lines.append(f"- Capital: ${meta['max_capital_deployed_usd']:,} max deployed / "
                 f"${meta['tranche_size_usd']:,} per tranche "
                 f"({int(meta['max_capital_deployed_usd'] // meta['tranche_size_usd'])} concurrent slots)")
    lines.append(f"- Take-profit: {meta.get('take_profit_pct')}%")
    if meta.get("reanchor_enabled"):
        lines.append(f"- Re-anchoring: ENABLED -- a symbol's grid re-anchors on a "
                     f"{meta['reanchor_breakout_pct']}% breakout above its anchor while flat in it")
    else:
        lines.append("- Re-anchoring: disabled -- a symbol's anchor is set once and never moves, "
                     "so it stops generating entries if price runs away from it permanently")
    if meta.get("max_hold_enabled"):
        lines.append(f"- Profit-lock time-stop: ENABLED -- {meta['max_hold_days']}d / "
                     f"{meta['max_hold_min_profit_pct']}% floor (never cuts a loss on its own)")
    else:
        lines.append("- Profit-lock time-stop: disabled")
    if meta.get("stop_loss_enabled"):
        lines.append(f"- Stop-loss: ENABLED -- force-closes a tranche immediately (taker + "
                     f"slippage) if price falls {meta['stop_loss_pct']}% below its entry, no time gate")
    else:
        lines.append("- Stop-loss: disabled -- no per-position mechanism caps a loss")
    lines.append("")
    lines.append(
        "**Methodology**: replays the actual production code "
        "(`grid_bot_watchlist.iterate()` / `process_fills()`), fed real Questrade daily "
        "OHLC candles as each symbol's fill band -- not a reimplementation. This is coarser "
        "than the crypto grid backtest, which used hourly bars: Questrade's intraday history "
        "doesn't go back far enough for a multi-month, multi-symbol replay, so this uses one "
        "daily bar per symbol per day. That will understate intraday touches that reverse "
        "before a daily close would show them -- treat these numbers as directional, not exact."
    )
    lines.append("")
    for mode, r in results.items():
        tag = "  (ACTIVE in watchlist_config.json)" if mode == meta["active_mode"] else "  (inactive)"
        lines.append(f"## {mode}{tag}")
        if r["closed_trades"]:
            lines.append(f"- Closed trades: {r['closed_trades']}  "
                         f"(wins {r['wins']}, losses {r['losses']}, win rate {r['win_rate_pct']}%)")
        else:
            lines.append("- No trades triggered in this window.")
        lines.append(f"- Realized P&L (window): ${r['realized_pnl_usd']:,}")
        lines.append(f"- Unrealized P&L (open at report end): ${r['unrealized_pnl_usd']:,}  "
                     f"({r['open_at_end']} open: {', '.join(r['symbols_open_at_end']) or 'none'})")
        tot_pct = round(100 * r["total_pnl_usd"] / r["allocated_capital_usd"], 2)
        lines.append(f"- Total P&L: ${r['total_pnl_usd']:,}  ({tot_pct}% of allocated capital)")
        lines.append(f"- Fees paid (window): ${r['fees_paid_usd']:,}")
        if r["per_symbol"]:
            lines.append("- Per symbol:")
            for sym, d in sorted(r["per_symbol"].items(), key=lambda kv: -kv[1]["pnl"]):
                wr = round(100 * d["wins"] / d["trades"], 1) if d["trades"] else 0.0
                lines.append(f"    - {sym}: {d['trades']} trades, {wr}% win rate, "
                            f"${round(d['pnl'], 2):,} pnl")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--refresh", action="store_true", help="force re-fetch candles instead of using the cache")
    ap.add_argument("--no-ab", action="store_true",
                    help="only backtest the currently-active ranking mode (skip the comparison)")
    ap.add_argument("--max-capital-override", type=float, default=None,
                    help="temporarily override risk.max_capital_deployed_usd for this run only "
                         "(does not touch watchlist_config.json) -- useful for stress-testing "
                         "whether the ranking rule actually matters when capital is scarce enough "
                         "to force a real choice between symbols")
    ap.add_argument("--tp-sweep", type=str, default=None,
                    help="comma-separated take_profit_pct values to compare instead of the "
                         "ranking-mode comparison, e.g. --tp-sweep 3,4,6,8 (ranking mode, "
                         "spacing, and capital are held fixed at whatever's in watchlist_config.json)")
    ap.add_argument("--enable-max-hold", action="store_true",
                    help="temporarily force risk.max_hold_enabled=true for this run only "
                         "(does not touch watchlist_config.json) -- use this with --tp-sweep so a "
                         "high take-profit target can't just sit open forever waiting to be hit")
    ap.add_argument("--enable-reanchor", action="store_true",
                    help="temporarily force reanchor.enabled=true for this run only "
                         "(does not touch watchlist_config.json) -- lets a symbol's grid follow it "
                         "after a breakout instead of going permanently dark once price runs away")
    ap.add_argument("--enable-stop-loss", action="store_true",
                    help="temporarily force risk.stop_loss_enabled=true for this run only "
                         "(does not touch watchlist_config.json)")
    ap.add_argument("--stop-loss-pct", type=float, default=None,
                    help="override risk.stop_loss_pct for this run (implies --enable-stop-loss)")
    ap.add_argument("--sl-sweep", type=str, default=None,
                    help="comma-separated stop_loss_pct values to compare, e.g. --sl-sweep 25,30,35,40 "
                         "(take-profit, ranking mode, capital, re-anchor, and max-hold are held fixed "
                         "at whatever's currently in watchlist_config.json)")
    args = ap.parse_args()

    if args.stop_loss_pct is not None:
        args.enable_stop_loss = True

    cfg_base = wl.load_config()

    if args.sl_sweep:
        sl_values = [float(v.strip()) for v in args.sl_sweep.split(",") if v.strip()]
        results, meta = run_sl_sweep(months=args.months, refresh=args.refresh, sl_values=sl_values)
        report = fmt_sl_sweep(results, meta, args.months)
        out_path = os.path.join(CACHE_DIR, f"watchlist_sl_sweep_{args.months}mo.md")
    elif args.tp_sweep:
        tp_values = [float(v.strip()) for v in args.tp_sweep.split(",") if v.strip()]
        results, meta = run_tp_sweep(months=args.months, refresh=args.refresh, tp_values=tp_values,
                                     enable_max_hold=args.enable_max_hold,
                                     enable_reanchor=args.enable_reanchor)
        report = fmt_tp_sweep(results, meta, args.months)
        suffix = ("_maxhold" if args.enable_max_hold else "") + ("_reanchor" if args.enable_reanchor else "")
        out_path = os.path.join(CACHE_DIR, f"watchlist_tp_sweep_{args.months}mo{suffix}.md")
    else:
        modes = [cfg_base["ranking"]["mode"]] if args.no_ab else ["vol_normalized_dip", "grid_depth"]
        results, meta = run(months=args.months, refresh=args.refresh, modes=modes,
                            max_capital_override=args.max_capital_override,
                            enable_reanchor=args.enable_reanchor,
                            enable_stop_loss=args.enable_stop_loss,
                            stop_loss_pct=args.stop_loss_pct)
        report = fmt(results, meta, args.months)
        suffix = (f"_cap{int(args.max_capital_override)}" if args.max_capital_override is not None else "") \
            + ("_reanchor" if args.enable_reanchor else "") \
            + (f"_sl{int(args.stop_loss_pct or cfg_base['risk'].get('stop_loss_pct', 10))}"
               if args.enable_stop_loss else "")
        out_path = os.path.join(CACHE_DIR, f"watchlist_backtest_{args.months}mo{suffix}.md")

    print(report)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"Saved report to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
