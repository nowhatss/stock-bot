#!/usr/bin/env python3
"""
Historical backtest for the trend watchlist bot. Fetches real Questrade daily
OHLC candles for every symbol in trend_watchlist_config.json, then replays
them through the ACTUAL production code (trend_bot_watchlist.iterate() /
evaluate_signals()) one simulated trading day at a time -- same principle as
backtest_watchlist.py, not a reimplementation.

Nothing here changes trend_watchlist_config.json or the live
trend_watchlist_state.json -- this writes its own state/trade files under
logs/backtest/.

Needs a working Questrade connection (stock_bots/questrade_refresh_token.txt).

Run:  python backtest_trend_watchlist.py                  (12 months)
      python backtest_trend_watchlist.py --months 24
      python backtest_trend_watchlist.py --refresh         (force re-fetch candles)
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
import trend_bot_watchlist as tw  # noqa: E402

CACHE_DIR = os.path.join(HERE, "logs", "backtest")
WARMUP_DAYS = 90  # calendar days of lead-in before the reported window, for MA-50 + weekends/holidays


# --------------------------------------------------------------------------- #
# candle fetch / cache (same shape as backtest_watchlist.py's)
# --------------------------------------------------------------------------- #
def _cache_path(symbol: str) -> str:
    return os.path.join(CACHE_DIR, f"trend_watchlist_candles_{symbol}.json")


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
        if c.get("close") is None:
            continue
        date = str(c["start"])[:10]
        dedup[date] = {"date": date, "close": float(c["close"])}
    rows = [dedup[d] for d in sorted(dedup)]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"calendar_days": calendar_days, "rows": rows}, fh)
    return rows


# --------------------------------------------------------------------------- #
# simulated clock -- drives g.now_utc() so day-rollover / trade timestamps key
# off the SIMULATED date, not wall-clock time (see backtest_watchlist.py)
# --------------------------------------------------------------------------- #
class Clock:
    def __init__(self) -> None:
        self.now: datetime | None = None

    def now_utc(self) -> datetime:
        return self.now


# --------------------------------------------------------------------------- #
# one full replay
# --------------------------------------------------------------------------- #
def run_one(cfg_base: dict, symbols: list[str], by_date: dict[str, dict[str, float]],
           replay_dates: list[str], window_start: str, *,
           ma_days: int | None = None, label: str | None = None) -> dict:
    cfg = json.loads(json.dumps(cfg_base))  # deep copy -- independent of the live config
    cfg["watchlist_symbols"] = symbols
    cfg["notifications"]["enabled"] = False
    if ma_days is not None:
        cfg["strategy"]["ma_days"] = ma_days
    label = label or f"ma{cfg['strategy']['ma_days']}"

    clock = Clock()
    g.now_utc = clock.now_utc
    g.discord_send = lambda *a, **k: None
    g.log_event = lambda *a, **k: None

    tw.TRADES_CSV = os.path.join(CACHE_DIR, f"trend_watchlist_trades_{label}_backtest.csv")
    tw.DAILY_CSV = os.path.join(CACHE_DIR, f"trend_watchlist_daily_{label}_backtest.csv")
    tw.STATE_PATH = os.path.join(CACHE_DIR, f"trend_watchlist_state_{label}_backtest.json")
    for p in (tw.TRADES_CSV, tw.DAILY_CSV, tw.STATE_PATH):
        if os.path.exists(p):
            os.remove(p)

    current_closes: dict[str, float] = {}

    def fake_quotes(syms, timeout=10):
        return dict(current_closes)

    import venue_questrade as vq
    vq.get_quotes_batch = fake_quotes
    tw.vq = vq

    hist_so_far: dict[str, list[float]] = {s: [] for s in symbols}

    def fake_closes(cfg_, symbol):
        # only ever sees history UP TO AND INCLUDING the current simulated day --
        # no lookahead into the future, same principle as backtest_watchlist.py
        # setting recent_high/vol_pct directly from hist_so_far.
        closes = hist_so_far.get(symbol, [])
        return closes if len(closes) >= 5 else None

    tw.refresh_symbol_closes = fake_closes

    clock.now = datetime.strptime(replay_dates[0], "%Y-%m-%d").replace(hour=20, tzinfo=timezone.utc)
    state = tw.default_state(cfg)
    baseline = None

    for d in replay_dates:
        clock.now = datetime.strptime(d, "%Y-%m-%d").replace(hour=20, tzinfo=timezone.utc)

        current_closes.clear()
        for s in symbols:
            close = by_date.get(s, {}).get(d)
            if close is None:
                continue
            hist_so_far[s].append(close)
            current_closes[s] = close

        tw.iterate(state, cfg)  # last_signal_date naturally differs each new simulated day

        if baseline is None and d >= window_start:
            # snapshot per-symbol too, so trade counts/P&L can be isolated to the
            # reported window and not inflated by activity during warm-up lead-in
            baseline = {
                "realized_pnl_usd": sum(s["realized_pnl_usd"] for s in state["symbols"].values()),
                "yield_earned_usd": sum(s["yield_earned_usd"] for s in state["symbols"].values()),
                "per_symbol": {sym: {"trades_total": s["trades_total"],
                                     "realized_pnl_usd": s["realized_pnl_usd"]}
                              for sym, s in state["symbols"].items()},
            }

    if baseline is None:
        baseline = {"realized_pnl_usd": 0.0, "yield_earned_usd": 0.0,
                    "per_symbol": {sym: {"trades_total": 0, "realized_pnl_usd": 0.0} for sym in symbols}}

    realized_now = sum(s["realized_pnl_usd"] for s in state["symbols"].values())
    yield_now = sum(s["yield_earned_usd"] for s in state["symbols"].values())
    unrealized = tw.compute_unrealized(state)
    held = sorted(sym for sym, s in state["symbols"].items() if s["position"] == "holding")

    per_symbol: dict[str, dict] = {}
    trades_total = 0
    for sym, sdata in state["symbols"].items():
        b = baseline["per_symbol"].get(sym, {"trades_total": 0, "realized_pnl_usd": 0.0})
        trades_in_window = sdata["trades_total"] - b["trades_total"]
        trades_total += trades_in_window
        per_symbol[sym] = {
            "trades": trades_in_window,
            "realized_pnl": round(sdata["realized_pnl_usd"] - b["realized_pnl_usd"], 2),
            "position_at_end": sdata["position"],
        }

    return {
        "realized_pnl_usd": round(realized_now - baseline["realized_pnl_usd"], 2),
        "yield_earned_usd": round(yield_now - baseline["yield_earned_usd"], 2),
        "unrealized_pnl_usd": unrealized,
        "total_pnl_usd": round((realized_now - baseline["realized_pnl_usd"]) + unrealized, 2),
        "trades_total": trades_total,
        "held_at_end": held,
        "per_symbol": per_symbol,
        "allocated_capital_usd": cfg["allocated_capital_usd"],
        "ma_days": cfg["strategy"]["ma_days"],
    }


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def prepare_data(months: int, refresh: bool, warmup_days: int = WARMUP_DAYS):
    cfg_base = tw.load_config()
    symbols = cfg_base["watchlist_symbols"]
    calendar_days = int(months * 31 + warmup_days + 15)

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

    by_date = {s: {r["date"]: r["close"] for r in rows} for s, rows in per_symbol_rows.items()}
    all_dates = sorted({r["date"] for rows in per_symbol_rows.values() for r in rows})
    end_date_str = all_dates[-1]
    end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
    window_start_dt = end_date - timedelta(days=int(months * 30.44))
    window_start = window_start_dt.strftime("%Y-%m-%d")
    replay_start = (window_start_dt - timedelta(days=warmup_days)).strftime("%Y-%m-%d")
    replay_dates = [d for d in all_dates if d >= replay_start]
    if not replay_dates:
        raise SystemExit("Not enough candle history for the requested window.")

    return cfg_base, list(per_symbol_rows.keys()), by_date, replay_dates, window_start, end_date_str


def fmt(result: dict, meta: dict, months: int) -> str:
    lines = [f"# Trend watchlist bot backtest — {months} months", ""]
    lines.append(f"- Symbols: {', '.join(meta['symbols'])}")
    lines.append(f"- Reported window: {meta['window_start']} to {meta['end_date']}")
    lines.append(f"- Warm-up lead-in (not counted in P&L): {meta['replay_start']} to {meta['window_start']}")
    lines.append(f"- Allocated: ${result['allocated_capital_usd']:,.0f} total, split into "
                 f"{len(meta['symbols'])} equal fixed slots of "
                 f"${result['allocated_capital_usd'] / len(meta['symbols']):,.2f} each")
    lines.append(f"- Rule: hold each symbol while its own close > its own "
                 f"{result['ma_days']}-day moving average, else that slot sits in cash")
    lines.append("")
    lines.append(
        "**Methodology**: replays the actual production code "
        "(`trend_bot_watchlist.iterate()` / `evaluate_signals()`), fed real Questrade daily "
        "closes -- not a reimplementation. Each symbol's slot is fixed and never rebalanced "
        "against the others (see trend_watchlist_config.json's _allocation_note) -- a day with "
        "fewer symbols trending simply deploys less of the total pool, it does not double up "
        "into the ones that do qualify. Daily-bar resolution, same caveat as the grid watchlist "
        "backtest: this will understate intraday reversals a same-day close wouldn't show."
    )
    lines.append("")
    lines.append(f"- Realized P&L (window): ${result['realized_pnl_usd']:,}")
    lines.append(f"- Idle-yield earned (window): ${result['yield_earned_usd']:,}")
    lines.append(f"- Unrealized P&L (open at report end): ${result['unrealized_pnl_usd']:,}  "
                 f"({len(result['held_at_end'])} held: {', '.join(result['held_at_end']) or 'none'})")
    tot_pct = round(100 * result["total_pnl_usd"] / result["allocated_capital_usd"], 2)
    lines.append(f"- Total P&L: ${result['total_pnl_usd']:,}  ({tot_pct}% of allocated capital)")
    lines.append(f"- Trades total: {result['trades_total']}")
    lines.append("")
    lines.append("| symbol | trades | realized P&L | position at report end |")
    lines.append("|---|---|---|---|")
    for sym, d in sorted(result["per_symbol"].items(), key=lambda kv: -kv[1]["realized_pnl"]):
        lines.append(f"| {sym} | {d['trades']} | ${d['realized_pnl']:,} | {d['position_at_end']} |")
    lines.append("")
    return "\n".join(lines)


def run_ma_sweep(months: int, refresh: bool, ma_values: list[int]) -> tuple[dict, dict]:
    """Backtests a set of ma_days values against the same data, holding
    everything else (watchlist, capital, fees) fixed at whatever's currently
    in trend_watchlist_config.json -- isolates the MA-length question."""
    warmup_days = int(max(ma_values) * 1.6) + 30  # enough calendar-day lead-in for the longest MA
    cfg_base, symbols_used, by_date, replay_dates, window_start, end_date_str = prepare_data(
        months, refresh, warmup_days=warmup_days)

    results = {}
    for ma in ma_values:
        label = f"ma{ma}"
        results[ma] = run_one(cfg_base, symbols_used, by_date, replay_dates, window_start,
                              ma_days=ma, label=label)
    meta = {"symbols": symbols_used, "window_start": window_start, "end_date": end_date_str,
            "replay_start": replay_dates[0], "active_ma_days": cfg_base["strategy"]["ma_days"]}
    return results, meta


def fmt_ma_sweep(results: dict, meta: dict, months: int) -> str:
    lines = [f"# Trend watchlist bot — MA-length sweep — {months} months", ""]
    lines.append(f"- Symbols: {', '.join(meta['symbols'])}")
    lines.append(f"- Reported window: {meta['window_start']} to {meta['end_date']}")
    lines.append(f"- Warm-up lead-in (not counted in P&L): {meta['replay_start']} to {meta['window_start']}")
    lines.append(f"- Everything except strategy.ma_days held fixed at whatever's currently in "
                 f"trend_watchlist_config.json (currently ma_days={meta['active_ma_days']})")
    lines.append("")
    lines.append(
        "**Methodology**: same replay engine as the main backtest (real Questrade daily closes "
        "through the actual production code), with ONLY strategy.ma_days varied between runs -- "
        "each run gets its own independent state, so there's no cross-contamination between MA "
        "lengths. Don't just pick whichever number looks best here -- that's curve-fitting to one "
        "historical path."
    )
    lines.append("")
    lines.append("| MA length | trades | realized P&L | unrealized P&L | total P&L | % of allocated |")
    lines.append("|---|---|---|---|---|---|")
    for ma, r in sorted(results.items()):
        tag = "  (active)" if ma == meta["active_ma_days"] else ""
        pct = round(100 * r["total_pnl_usd"] / r["allocated_capital_usd"], 2)
        lines.append(f"| {ma}{tag} | {r['trades_total']} | ${r['realized_pnl_usd']:,} | "
                     f"${r['unrealized_pnl_usd']:,} | ${r['total_pnl_usd']:,} | {pct}% |")
    lines.append("")
    for ma, r in sorted(results.items()):
        tag = "  (ACTIVE)" if ma == meta["active_ma_days"] else ""
        lines.append(f"## MA-{ma}{tag}")
        for sym, d in sorted(r["per_symbol"].items(), key=lambda kv: -kv[1]["realized_pnl"]):
            lines.append(f"    - {sym}: {d['trades']} trades, ${d['realized_pnl']:,} realized, "
                        f"ends {d['position_at_end']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--refresh", action="store_true", help="force re-fetch candles instead of using the cache")
    ap.add_argument("--ma-sweep", type=str, default=None,
                    help="comma-separated ma_days values to compare, e.g. --ma-sweep 50,100,150,200 "
                         "(everything else held fixed at whatever's in trend_watchlist_config.json)")
    args = ap.parse_args()

    if args.ma_sweep:
        ma_values = [int(v.strip()) for v in args.ma_sweep.split(",") if v.strip()]
        results, meta = run_ma_sweep(months=args.months, refresh=args.refresh, ma_values=ma_values)
        report = fmt_ma_sweep(results, meta, args.months)
        out_path = os.path.join(CACHE_DIR, f"trend_watchlist_ma_sweep_{args.months}mo.md")
    else:
        cfg_base, symbols_used, by_date, replay_dates, window_start, end_date_str = prepare_data(
            args.months, args.refresh)
        result = run_one(cfg_base, symbols_used, by_date, replay_dates, window_start)
        meta = {"symbols": symbols_used, "window_start": window_start, "end_date": end_date_str,
                "replay_start": replay_dates[0]}
        report = fmt(result, meta, args.months)
        out_path = os.path.join(CACHE_DIR, f"trend_watchlist_backtest_{args.months}mo.md")

    print(report)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"Saved report to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
