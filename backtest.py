#!/usr/bin/env python3
"""
Backtest the grid bot against real ETH-USD history.

Pulls hourly candles from Coinbase's PUBLIC Exchange API (no auth), caches them,
then replays them through the real grid_bot state machine -- same buy/sell/rail/
adaptation code the live bot runs. Uses each candle's high/low as the fill band.
A simulated clock advances 1 hour per step so the daily rollover, 10-day
max-hold, and idle-yield accrual all fire.

Run:  python backtest.py                    # 12 months, current config, filter A/B
      python backtest.py --months 6
      python backtest.py --refresh           # re-download candles
      python backtest.py --no-ab             # skip the filter on/off comparison

Writes: logs/backtest/<run>_trades.csv, and a report to logs/backtest/report_*.md
Reads config.json for all strategy params. Places no orders, touches no live state.

CAVEATS: one historical path is one sample. Candle high/low fills are optimistic
(assume you'd fill at the exact extreme). No order-book depth or exact fee tier.
Do NOT tune parameters to this -- that's curve-fitting.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid_bot as g

HERE = os.path.dirname(os.path.abspath(__file__))
BT_DIR = os.path.join(HERE, "logs", "backtest")
GRAN = 3600  # 1 hour
WARMUP_DAYS = 25  # lead-in so the trend MA is warm at the start of the reported window
EXCHANGE = "https://api.exchange.coinbase.com/products/{pid}/candles"


# --------------------------------------------------------------------------- #
# candle fetch + cache
# --------------------------------------------------------------------------- #
def _cache_path(pid: str) -> str:
    return os.path.join(BT_DIR, f"candles_{pid}_{GRAN}.json")


def fetch_candles(pid: str, months: int, refresh: bool, lead_days: int = WARMUP_DAYS) -> list[list]:
    os.makedirs(BT_DIR, exist_ok=True)
    need_from = datetime.now(timezone.utc) - timedelta(days=30 * months + lead_days + 2)
    cache = _cache_path(pid)

    have: dict[int, list] = {}
    if os.path.exists(cache) and not refresh:
        with open(cache, encoding="utf-8-sig") as fh:
            have = {int(c[0]): c for c in json.load(fh)}

    earliest = min(have) if have else int(datetime.now(timezone.utc).timestamp())
    latest = max(have) if have else 0
    now_ts = int(datetime.now(timezone.utc).timestamp())

    def _pull(start_ts: int, end_ts: int) -> None:
        cur_end = end_ts
        while cur_end > start_ts:
            cur_start = max(start_ts, cur_end - 300 * GRAN)
            url = (EXCHANGE.format(pid=pid)
                   + f"?granularity={GRAN}&start={cur_start}&end={cur_end}")
            req = Request(url, headers={"User-Agent": "eth-grid-bot/backtest"})
            for attempt in range(5):
                try:
                    with urlopen(req, timeout=20) as resp:
                        rows = json.loads(resp.read().decode("utf-8"))
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt == 4:
                        raise
                    time.sleep(1.5 * (attempt + 1))
            for r in rows:
                if isinstance(r, list) and len(r) >= 5:
                    have[int(r[0])] = r
            print(f"  fetched {datetime.fromtimestamp(cur_start, timezone.utc):%Y-%m-%d %H:%M} "
                  f"..  ({len(have)} candles)", end="\r", flush=True)
            cur_end = cur_start
            time.sleep(0.16)  # be polite to the public endpoint

    if int(need_from.timestamp()) < earliest:
        _pull(int(need_from.timestamp()), earliest)
    if latest < now_ts - GRAN:
        _pull(latest or int(need_from.timestamp()), now_ts)

    print()
    candles = sorted(have.values(), key=lambda c: c[0])
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(candles, fh)
    return candles


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #
class Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t


def run(candles: list[list], cfg: dict, tag: str) -> dict:
    """candles oldest->newest: [time, low, high, open, close, volume]."""
    os.makedirs(BT_DIR, exist_ok=True)

    # only replay [reported window + WARMUP_DAYS lead-in] -- the cache may hold
    # much more history than this run asked for.
    win_start = datetime.now(timezone.utc) - timedelta(days=30 * cfg["_months"] + WARMUP_DAYS)
    candles = [c for c in candles if c[0] >= win_start.timestamp()]
    for attr, fn in (("STATE_PATH", "state.json"), ("TRADES_CSV", "trades.csv"),
                     ("DAILY_CSV", "daily.csv"), ("EVENTS_LOG", "events.log"),
                     ("HEARTBEAT_LOG", "heartbeat.log")):
        p = os.path.join(BT_DIR, f"{tag}_{fn}")
        setattr(g, attr, p)
        if os.path.exists(p):
            os.remove(p)

    cfg = json.loads(json.dumps(cfg))  # deep copy
    cfg["execution"]["mode"] = "dry_run"
    cfg["execution"]["fill_model"] = "resting"
    cfg["execution"]["log_previews"] = False

    clock = Clock(datetime.fromtimestamp(candles[0][0], timezone.utc))
    g.now_utc = clock.now
    g.discord_send = lambda *a, **k: None
    g.save_state = lambda s: None          # skip 8760 json writes
    g.print_summary = lambda *a, **k: None
    g.notify_daily_summary = lambda *a, **k: None

    def _file_log(msg, level="INFO"):
        os.makedirs(os.path.dirname(g.EVENTS_LOG), exist_ok=True)
        with open(g.EVENTS_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{g.iso(clock.now())} [{level}] {msg}\n")
    g.log_event = _file_log

    state = g.default_state()

    daily_closes: list[float] = []
    cur_day = None
    prev_close = float(candles[0][4])
    day_end_close = prev_close

    def market_data(_cfg):
        # completed daily closes + the running day's latest close (matches live:
        # Coinbase's daily candle updates in real time)
        if not daily_closes:
            return None
        return {"closes": daily_closes + [prev_close], "as_of": g.iso(clock.now()),
                "stale": False}

    g.load_market_data = market_data
    g.get_price = lambda _cfg: prev_close

    reported_start = datetime.now(timezone.utc) - timedelta(days=30 * cfg["_months"])
    baseline = None
    monthly_equity: dict[str, float] = {}   # month -> end-of-month equity vs baseline
    monthly_eth: dict[str, float] = {}
    peak = 0.0
    max_dd = 0.0
    dd_when = None
    eth_start_reported = None

    for c in candles:
        ts, low, high, _open, close, _vol = c
        dt = datetime.fromtimestamp(ts, timezone.utc)
        clock.t = dt
        prev_close = float(close)

        d = dt.date()
        if cur_day is None:
            cur_day = d
        elif d != cur_day:
            daily_closes.append(day_end_close)   # close of the day that just ended
            cur_day = d
        day_end_close = float(close)

        g.iterate(state, cfg, price_band=(float(low), float(high)))

        if baseline is None and dt >= reported_start:
            baseline = {"realized": state["realized_pnl_usd"], "yield": state["yield_earned_usd"],
                        "fees": state["fees_paid_usd"], "cycles": len(state["closed_tranches"])}
            eth_start_reported = prev_close

        if baseline is not None:
            unreal = g.compute_unrealized(state, prev_close, cfg)
            equity = (state["realized_pnl_usd"] - baseline["realized"]
                      + state["yield_earned_usd"] - baseline["yield"] + unreal)
            peak = max(peak, equity)
            if equity - peak < max_dd:
                max_dd = equity - peak
                dd_when = dt
            mk = dt.strftime("%Y-%m")
            monthly_equity[mk] = equity
            monthly_eth[mk] = prev_close

    price_end = float(candles[-1][4])
    unreal = g.compute_unrealized(state, price_end, cfg)
    b = baseline or {"realized": 0.0, "yield": 0.0, "fees": 0.0, "cycles": 0}
    realized = state["realized_pnl_usd"] - b["realized"]
    yield_e = state["yield_earned_usd"] - b["yield"]
    fees = state["fees_paid_usd"] - b["fees"]
    cycles = len(state["closed_tranches"]) - b["cycles"]

    # month rows: end-of-month equity vs baseline, the month's P&L delta, ETH close
    keys = sorted(monthly_equity)
    rows = []
    prev = 0.0
    for k in keys:
        eq = monthly_equity[k]
        rows.append((k, round(eq - prev, 2), round(eq, 2), round(monthly_eth[k])))
        prev = eq

    eth_ret = (price_end / eth_start_reported - 1.0) * 100 if eth_start_reported else 0.0

    return {
        "tag": tag,
        "cycles": cycles,
        "realized": round(realized, 2),
        "unrealized": round(unreal, 2),
        "yield": round(yield_e, 2),
        "fees": round(fees, 2),
        "total": round(realized + unreal + yield_e, 2),
        "max_dd": round(max_dd, 2),
        "dd_when": dd_when.strftime("%Y-%m-%d") if dd_when else "-",
        "peak": round(peak, 2),
        "open_end": len(state["open_tranches"]),
        "halted": state["halted"],
        "eth_ret": round(eth_ret, 1),
        "months": rows,
        "reshapes": g._grep_count_bt("Grid reshaped"),
        "trend_switches": g._grep_count_bt("Trend filter:"),
        "maxhold": g._grep_count_bt("[max_hold"),
        "stopouts": g._grep_count_bt("HARD STOP-LOSS hit"),
        "breakout_buys": g._grep_count_bt("BUY  breakout"),
        "breakout_stops": g._grep_count_bt("[breakeven_stop]"),
    }


def _grep_count(needle: str) -> int:
    p = g.EVENTS_LOG
    if not os.path.exists(p):
        return 0
    with open(p, encoding="utf-8") as fh:
        return sum(1 for ln in fh if needle in ln)


g._grep_count_bt = _grep_count


# --------------------------------------------------------------------------- #
def fmt(r: dict, alloc: float) -> str:
    L = [f"### {r['tag']}",
         f"- **Total P&L: ${r['total']:,.2f}  ({r['total'] / alloc * 100:+.1f}% of allocated)**",
         f"  - realized ${r['realized']:,.2f}  +  unrealized ${r['unrealized']:,.2f}  "
         f"+  idle yield ${r['yield']:,.2f}",
         f"- ETH over the window: {r['eth_ret']:+.1f}%",
         f"- completed cycles: {r['cycles']}   |   fees paid: ${r['fees']:,.2f}",
         f"- peak equity: ${r['peak']:,.2f}   |   max drawdown: ${r['max_dd']:,.2f} "
         f"(trough {r['dd_when']})",
         f"- open tranches at end: {r['open_end']}   |   halted at end: {r['halted']}",
         f"- activity: {r['reshapes']} reshapes, {r['trend_switches']} trend-filter switches, "
         f"{r['maxhold']} max-hold closes, {r['stopouts']} stop-loss trips",
         f"- breakout leg: {r['breakout_buys']} entries, {r['breakout_stops']} breakeven-stopped",
         "",
         "| month | this month | equity vs start | ETH close |",
         "|---|---|---|---|"]
    for m, delta, eq, eth in r["months"]:
        L.append(f"| {m} | ${delta:,.2f} | ${eq:,.2f} | ${eth:,} |")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-ab", action="store_true")
    ap.add_argument("--breakout", action="store_true",
                    help="add a run with the breakout-buy leg force-enabled")
    ap.add_argument("--pid", default=None)
    args = ap.parse_args()

    cfg = g.load_config()
    cfg["_months"] = args.months
    pid = args.pid or cfg.get("asset", "ETH-USD")

    print(f"Backtest {pid}  {args.months} months  @ 1h  (+{WARMUP_DAYS}d MA warm-up)")
    print("Fetching candles from Coinbase public API ...")
    candles = fetch_candles(pid, args.months, args.refresh)
    span_d = (candles[-1][0] - candles[0][0]) / 86400
    print(f"{len(candles)} hourly candles, {span_d:.0f} days "
          f"({datetime.fromtimestamp(candles[0][0], timezone.utc):%Y-%m-%d} -> "
          f"{datetime.fromtimestamp(candles[-1][0], timezone.utc):%Y-%m-%d})")
    print(f"ETH: start ${candles[0][4]:,.0f}  end ${candles[-1][4]:,.0f}  "
          f"low ${min(c[1] for c in candles):,.0f}  high ${max(c[2] for c in candles):,.0f}\n")

    runs = [run(candles, cfg, "as-configured")]
    if not args.no_ab:
        off = json.loads(json.dumps(cfg))
        off["adaptations"]["trend_filter_enabled"] = False
        runs.append(run(candles, off, "trend-filter-OFF"))
    if args.breakout:
        bo = json.loads(json.dumps(cfg))
        bo["adaptations"]["breakout_buy_enabled"] = True
        runs.append(run(candles, bo, "breakout-buy-ON"))
        bo_off_trend = json.loads(json.dumps(cfg))
        bo_off_trend["adaptations"]["breakout_buy_enabled"] = True
        bo_off_trend["adaptations"]["trend_filter_enabled"] = False
        runs.append(run(candles, bo_off_trend, "breakout-buy-ON, trend-filter-OFF"))

    report = (f"# Grid-bot backtest — {datetime.now():%Y-%m-%d %H:%M}\n\n"
              f"{pid}, {args.months} months hourly, ${cfg['allocated_capital_usd']:,.0f} allocated, "
              f"{cfg['grid']['num_levels']} rungs @ {cfg['grid']['grid_spacing_pct']}%, "
              f"TP +{cfg['grid']['take_profit_pct']}%, fee model resting "
              f"({g.fee_rate(cfg, 'maker') * 100:.2f}% maker).\n\n"
              + "\n\n".join(fmt(r, cfg["allocated_capital_usd"]) for r in runs)
              + "\n\n---\n*One historical path = one sample. Candle high/low fills are "
                "optimistic. Don't tune parameters to this.*\n")

    os.makedirs(BT_DIR, exist_ok=True)
    out = os.path.join(BT_DIR, f"report_{datetime.now():%Y-%m-%d_%H%M}.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(report)
    print(f"[report written to {os.path.relpath(out, HERE)}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
