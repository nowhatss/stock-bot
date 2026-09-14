#!/usr/bin/env python3
"""
Backtest a simple trend-following rule on ETH-USD:  hold ETH while its daily
close is above the N-day moving average, otherwise hold USDC (earning yield).

Compares several MA lengths against buy-and-hold ETH and all-cash. Uses the same
public Coinbase candles + cache as backtest.py.

Run:  python backtest_trend.py                 # 12 months, MA 50/100/200
      python backtest_trend.py --months 24
      python backtest_trend.py --ma 50 100      # pick lengths
      python backtest_trend.py --refresh

CAVEATS: one historical path = one sample. Signals act on the daily close (next
bar), fees + slippage modelled crudely. Don't tune the MA length to this result.
"""
from __future__ import annotations

import argparse
import os
import statistics
from datetime import datetime, timezone

import backtest as bt  # reuse fetch_candles + cache

ALLOC = 10_000.0
FEE = 0.006          # per switch (taker-ish, conservative)
YIELD_APY = 0.045    # USDC while in cash


def daily_closes(candles: list[list]) -> list[tuple[datetime, float]]:
    by_day: dict = {}
    for ts, _lo, _hi, _o, close, _v in candles:
        d = datetime.fromtimestamp(ts, timezone.utc).date()
        by_day[d] = float(close)          # last close of the day wins
    return [(datetime(d.year, d.month, d.day, tzinfo=timezone.utc), c)
            for d, c in sorted(by_day.items())]


def run_ma(series: list[tuple[datetime, float]], n: int, start_idx: int) -> dict:
    closes = [c for _, c in series]
    cash, units, pos = ALLOC, 0.0, 0          # pos: 0 cash, 1 ETH
    trades, days_in = 0, 0
    eq_start = None
    monthly: dict[str, float] = {}
    peak = ALLOC
    max_dd = 0.0
    dd_when = None

    for i in range(start_idx, len(series)):
        dt, px = series[i]
        ma = sum(closes[i - n + 1:i + 1]) / n
        signal = px > ma

        if signal and pos == 0:
            units = cash * (1 - FEE) / px
            cash, pos = 0.0, 1
            trades += 1
        elif not signal and pos == 1:
            cash = units * px * (1 - FEE)
            units, pos = 0.0, 0
            trades += 1

        if cash > 0:
            cash *= (1 + YIELD_APY / 365)
        if pos == 1:
            days_in += 1

        equity = cash + units * px
        if eq_start is None:
            eq_start = equity
        rel = equity - eq_start
        peak = max(peak, equity)
        if equity - peak < max_dd:
            max_dd, dd_when = equity - peak, dt
        monthly[dt.strftime("%Y-%m")] = rel

    keys = sorted(monthly)
    rows, prev = [], 0.0
    for k in keys:
        rows.append((k, round(monthly[k] - prev, 2), round(monthly[k], 2)))
        prev = monthly[k]

    final = cash + units * closes[-1]
    n_days = len(series) - start_idx
    return {
        "name": f"trend MA-{n}",
        "total": round(final - ALLOC, 2),
        "pct": round((final / ALLOC - 1) * 100, 1),
        "trades": trades,
        "time_in_market": round(100 * days_in / n_days),
        "max_dd": round(max_dd, 2),
        "dd_when": dd_when.strftime("%Y-%m-%d") if dd_when else "-",
        "months": rows,
    }


def run_hold(series, start_idx: int) -> dict:
    _, px0 = series[start_idx]
    units = ALLOC * (1 - FEE) / px0
    peak, max_dd, dd_when = ALLOC, 0.0, None
    monthly, prev_key = {}, None
    for i in range(start_idx, len(series)):
        dt, px = series[i]
        eq = units * px
        peak = max(peak, eq)
        if eq - peak < max_dd:
            max_dd, dd_when = eq - peak, dt
        monthly[dt.strftime("%Y-%m")] = eq - ALLOC
    keys = sorted(monthly)
    rows, prev = [], 0.0
    for k in keys:
        rows.append((k, round(monthly[k] - prev, 2), round(monthly[k], 2)))
        prev = monthly[k]
    final = units * series[-1][1]
    return {"name": "buy & hold ETH", "total": round(final - ALLOC, 2),
            "pct": round((final / ALLOC - 1) * 100, 1), "trades": 1,
            "time_in_market": 100, "max_dd": round(max_dd, 2),
            "dd_when": dd_when.strftime("%Y-%m-%d") if dd_when else "-", "months": rows}


def run_cash(series, start_idx: int) -> dict:
    cash = ALLOC
    monthly = {}
    for i in range(start_idx, len(series)):
        dt, _ = series[i]
        cash *= (1 + YIELD_APY / 365)
        monthly[dt.strftime("%Y-%m")] = cash - ALLOC
    keys = sorted(monthly)
    rows, prev = [], 0.0
    for k in keys:
        rows.append((k, round(monthly[k] - prev, 2), round(monthly[k], 2)))
        prev = monthly[k]
    return {"name": "all cash (USDC 4.5%)", "total": round(cash - ALLOC, 2),
            "pct": round((cash / ALLOC - 1) * 100, 1), "trades": 0,
            "time_in_market": 0, "max_dd": 0.0, "dd_when": "-", "months": rows}


def fmt(r: dict) -> str:
    L = [f"### {r['name']}",
         f"- **{'+' if r['total'] >= 0 else ''}${r['total']:,.2f}  ({r['pct']:+.1f}%)**",
         f"- trades: {r['trades']}   |   time in ETH: {r['time_in_market']}%",
         f"- max drawdown: ${r['max_dd']:,.2f} (trough {r['dd_when']})",
         "",
         "| month | this month | vs start |",
         "|---|---|---|"]
    for m, d, e in r["months"]:
        L.append(f"| {m} | ${d:,.2f} | ${e:,.2f} |")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--ma", type=int, nargs="+", default=[50, 100, 200])
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--pid", default="ETH-USD")
    args = ap.parse_args()

    lead = max(args.ma) + 15
    print(f"Trend-following backtest — {args.pid}, {args.months} months, "
          f"MA lengths {args.ma}  (fee {FEE * 100:.1f}%/switch, cash yield {YIELD_APY * 100:.1f}%)")
    print("Fetching candles ...")
    candles = bt.fetch_candles(args.pid, args.months, args.refresh, lead_days=lead)
    series = daily_closes(candles)
    print(f"{len(series)} daily closes "
          f"({series[0][0].date()} -> {series[-1][0].date()})")

    # reported window starts `months` back; need `max(ma)` days of history before it
    start_dt = datetime.now(timezone.utc).timestamp() - 30 * args.months * 86400
    start_idx = next(i for i, (dt, _) in enumerate(series) if dt.timestamp() >= start_dt)
    if start_idx < max(args.ma):
        print(f"WARNING: only {start_idx} days of lead-in for MA-{max(args.ma)}; "
              f"fetch more history with a bigger --months or accept a late start.")
        start_idx = max(args.ma)

    eth_ret = (series[-1][1] / series[start_idx][1] - 1) * 100
    print(f"ETH over the reported window: {eth_ret:+.1f}%  "
          f"(${series[start_idx][1]:,.0f} -> ${series[-1][1]:,.0f})\n")

    results = ([run_ma(series, n, start_idx) for n in args.ma]
               + [run_hold(series, start_idx), run_cash(series, start_idx)])

    report = (f"# Trend-following backtest — {datetime.now():%Y-%m-%d %H:%M}\n\n"
              f"{args.pid}, {args.months} months, ${ALLOC:,.0f}. Rule: hold ETH while "
              f"daily close > N-day MA, else hold USDC at {YIELD_APY * 100:.1f}%. "
              f"{FEE * 100:.1f}% fee per switch. ETH over the window: {eth_ret:+.1f}%.\n\n"
              "| strategy | P&L | % | trades | time in ETH | max drawdown |\n"
              "|---|---|---|---|---|---|\n"
              + "\n".join(f"| {r['name']} | ${r['total']:,.2f} | {r['pct']:+.1f}% | "
                         f"{r['trades']} | {r['time_in_market']}% | ${r['max_dd']:,.2f} |"
                         for r in results)
              + "\n\n" + "\n\n".join(fmt(r) for r in results)
              + "\n\n---\n*One historical path. Signals act on the daily close. "
                "Don't pick the MA length that looks best here — that's curve-fitting.*\n")

    out = os.path.join(bt.BT_DIR, f"report_trend_{datetime.now():%Y-%m-%d_%H%M}.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(report)
    print(f"[report written to {os.path.relpath(out, bt.HERE)}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
