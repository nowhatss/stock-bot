#!/usr/bin/env python3
"""
Grid-bot performance analysis. Reads logs/, computes a metrics report, writes it
to logs/reports/, and (optionally) posts a condensed version to Discord.

Run:  python analyze.py            # print + write report file
      python analyze.py --discord  # also post a summary to Discord
      python analyze.py --quiet    # write the file only, no stdout

Intended cadence: run it after ~50 completed cycles or once a month -- NOT every
couple of days. Fewer data points than that and any 'pattern' is noise.
This changes nothing about the bot. It only reads.
"""
from __future__ import annotations

import csv
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

import grid_bot as g

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")
REPORTS = os.path.join(LOGS, "reports")


def _read_csv(name: str) -> list[dict]:
    p = os.path.join(LOGS, name)
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return []
    with open(p, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _parse_dt(s: str):
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _pct(part, whole):
    return (100.0 * part / whole) if whole else 0.0


# --------------------------------------------------------------------------- #
def build_report(cfg: dict) -> tuple[str, dict]:
    trades = _read_csv("trades.csv")
    daily = _read_csv("daily_summary.csv")
    state = g.load_state()

    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]

    # pair buys->sells per level, in order (only one tranche per level at a time)
    per_level_buys: dict[str, list[dict]] = defaultdict(list)
    for b in buys:
        per_level_buys[b["level_index"]].append(b)
    cursor: dict[str, int] = defaultdict(int)

    cycles = []
    for s in sells:
        lv = s["level_index"]
        i = cursor[lv]
        b = per_level_buys[lv][i] if i < len(per_level_buys[lv]) else None
        cursor[lv] += 1
        bt, st = _parse_dt(b["timestamp"]) if b else None, _parse_dt(s["timestamp"])
        hold_h = ((st - bt).total_seconds() / 3600.0) if (b and bt and st) else None
        reason = "take_profit"
        note = s.get("note", "")
        if "reason=" in note:
            reason = note.split("reason=", 1)[1].split()[0]
        kind = "taker" if "taker" in note else "maker"
        cycles.append({
            "level": lv,
            "buy_price": _f(b["price"]) if b else None,
            "sell_price": _f(s["price"]),
            "pnl": _f(s["realized_pnl_delta_usd"]),
            "fee": _f(s["fee_usd"]) + (_f(b["fee_usd"]) if b else 0.0),
            "hold_h": hold_h,
            "reason": reason,
            "sell_kind": kind,
        })

    wins = [c for c in cycles if c["pnl"] > 0]
    losses = [c for c in cycles if c["pnl"] <= 0]
    pnls = [c["pnl"] for c in cycles]
    holds = [c["hold_h"] for c in cycles if c["hold_h"] is not None]
    gross_win = sum(c["pnl"] for c in wins)
    total_fees = _f(state.get("fees_paid_usd"))
    gross_before_fees = sum(pnls) + sum(c["fee"] for c in cycles)

    # per-rung stats
    rungs: dict[str, dict] = {}
    for lv in sorted(set([b["level_index"] for b in buys] + [str(i) for i in range(cfg["grid"]["num_levels"])])):
        lc = [c for c in cycles if c["level"] == lv]
        rungs[lv] = {
            "fills": sum(1 for b in buys if b["level_index"] == lv),
            "cycles": len(lc),
            "wins": sum(1 for c in lc if c["pnl"] > 0),
            "pnl": round(sum(c["pnl"] for c in lc), 2),
            "avg_hold_h": round(statistics.mean([c["hold_h"] for c in lc if c["hold_h"] is not None]), 1)
            if any(c["hold_h"] is not None for c in lc) else None,
        }

    # events
    ev_path = os.path.join(LOGS, "events.log")
    ev = open(ev_path, encoding="utf-8").read() if os.path.exists(ev_path) else ""
    counts = {
        "grid reshapes": ev.count("Grid reshaped"),
        "trend-filter switches": ev.count("Trend filter:"),
        "hard stop-loss trips": ev.count("HARD STOP-LOSS hit"),
        "max-daily-loss trips": ev.count("MAX DAILY LOSS hit"),
        "price-feed failures": ev.count("PRICE FEED FAILURE"),
        "max-hold closes": ev.count("[max_hold"),
    }

    # drawdown from daily summary (unrealized + realized-day)
    dd = 0.0
    for d in daily:
        tot = _f(d.get("realized_pnl_day_usd")) + _f(d.get("unrealized_pnl_usd"))
        dd = min(dd, tot)

    alloc = cfg["allocated_capital_usd"]
    realized = _f(state.get("realized_pnl_usd"))
    yield_earned = _f(state.get("yield_earned_usd"))
    unreal = g.compute_unrealized(state, state.get("last_price") or 0.0, cfg)

    span = ""
    if trades:
        a, b_ = _parse_dt(trades[0]["timestamp"]), _parse_dt(trades[-1]["timestamp"])
        if a and b_:
            span = f"{(b_ - a).days} d ({a.date()} -> {b_.date()})"

    # ---- render ----
    L = []
    L.append(f"# Grid-bot analysis — {datetime.now().strftime('%Y-%m-%d %H:%M')} local")
    L.append("")
    L.append(f"- mode `{cfg['execution']['mode']}`, fill_model `{cfg['execution'].get('fill_model')}`, "
             f"allocated ${alloc:,.0f}")
    L.append(f"- trade span: {span or 'no trades yet'}")
    L.append(f"- completed cycles: **{len(cycles)}**"
             + ("  — too few to draw conclusions (target ~50)" if len(cycles) < 50 else ""))
    L.append("")
    L.append("## P&L")
    L.append(f"| | |")
    L.append(f"|---|---|")
    L.append(f"| realized | ${realized:,.2f} |")
    L.append(f"| unrealized (open) | ${unreal:,.2f} |")
    L.append(f"| idle-cash yield | ${yield_earned:,.2f} |")
    L.append(f"| **grand total** | **${realized + unreal + yield_earned:,.2f}** "
             f"({_pct(realized + unreal + yield_earned, alloc):+.2f}% of allocated) |")
    L.append(f"| fees paid (all-time) | ${total_fees:,.2f} |")
    if gross_before_fees:
        L.append(f"| fee drag | {_pct(sum(c['fee'] for c in cycles), gross_before_fees):.1f}% "
                 f"of gross cycle P&L |")
    L.append(f"| max drawdown (daily marks) | ${dd:,.2f} ({_pct(dd, alloc):.1f}%) |")
    L.append("")
    if cycles:
        L.append("## Cycles")
        L.append(f"- win rate: **{_pct(len(wins), len(cycles)):.0f}%** ({len(wins)}W / {len(losses)}L)")
        L.append(f"- cycle P&L: mean ${statistics.mean(pnls):.2f}, "
                 f"median ${statistics.median(pnls):.2f}, "
                 f"best ${max(pnls):.2f}, worst ${min(pnls):.2f}")
        if holds:
            L.append(f"- hold time: mean {statistics.mean(holds):.1f} h, median {statistics.median(holds):.1f} h, "
                     f"max {max(holds):.1f} h")
        by_reason = defaultdict(int)
        for c in cycles:
            by_reason[c["reason"]] += 1
        L.append(f"- exit reason: " + ", ".join(f"{k} {v}" for k, v in by_reason.items()))
        taker_sells = sum(1 for c in cycles if c["sell_kind"] == "taker")
        L.append(f"- taker (market) exits: {taker_sells} / {len(cycles)}")
        L.append("")
    L.append("## Per rung")
    L.append("| rung | buy fills | cycles | wins | P&L | avg hold |")
    L.append("|---|---|---|---|---|---|")
    for lv, r in rungs.items():
        L.append(f"| {lv} | {r['fills']} | {r['cycles']} | {r['wins']} | "
                 f"${r['pnl']:,.2f} | {r['avg_hold_h'] if r['avg_hold_h'] is not None else '—'} h |")
    L.append("")
    L.append("## Activity")
    for k, v in counts.items():
        L.append(f"- {k}: {v}")
    L.append("")
    L.append("## Flags")
    flags = _flags(cfg, cycles, rungs, total_fees, gross_before_fees, dd, alloc)
    L.extend(f"- {x}" for x in flags) if flags else L.append("- nothing notable")

    data = {"cycles": len(cycles), "win_rate": _pct(len(wins), len(cycles)),
            "grand_total": round(realized + unreal + yield_earned, 2),
            "fees": round(total_fees, 2), "max_dd": round(dd, 2), "flags": flags}
    return "\n".join(L), data


def _flags(cfg, cycles, rungs, fees, gross, dd, alloc) -> list[str]:
    out = []
    n = cfg["grid"]["num_levels"]
    for lv in [str(i) for i in range(n)]:
        if rungs.get(lv, {}).get("fills", 0) == 0:
            out.append(f"rung {lv} has never filled — grid may be too wide for this regime, "
                       f"or price hasn't reached it")
    if gross and _pct(sum(c["fee"] for c in cycles), gross) > 25:
        out.append(f"fees are {_pct(sum(c['fee'] for c in cycles), gross):.0f}% of gross — "
                   f"consider a wider take-profit or a lower-fee pair (ETH-USDC)")
    if cycles:
        wr = _pct(sum(1 for c in cycles if c["pnl"] > 0), len(cycles))
        if wr < 55:
            out.append(f"win rate {wr:.0f}% is low for a grid — check whether entries are being "
                       f"filled on the wrong side of momentum")
        holds = [c["hold_h"] for c in cycles if c["hold_h"] is not None]
        if holds and statistics.mean(holds) > 24 * 5:
            out.append(f"mean hold {statistics.mean(holds) / 24:.1f} days — capital is turning over "
                       f"slowly; a tighter take-profit or spacing would cycle faster")
    if _pct(dd, alloc) < -30:
        out.append(f"max drawdown hit {_pct(dd, alloc):.0f}% — the trend filter / max-hold settings "
                   f"may need tightening")
    if len(cycles) < 50:
        out.append(f"only {len(cycles)} cycles — treat everything above as directional, not conclusive")
    return out


def main() -> int:
    quiet = "--quiet" in sys.argv
    do_discord = "--discord" in sys.argv
    cfg = g.load_config()
    g.configure_notifications(cfg)

    report, data = build_report(cfg)

    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, datetime.now().strftime("%Y-%m-%d_%H%M") + ".md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(report + "\n")

    if not quiet:
        print(report)
        print(f"\n[written to {os.path.relpath(path, HERE)}]")

    if do_discord and g.discord_configured():
        flags = data["flags"]
        g.discord_send(
            f"📈 Grid-bot analysis — {data['cycles']} cycles",
            description=("**Flags:**\n" + "\n".join(f"• {x}" for x in flags)) if flags else "No flags.",
            fields=[
                ("Grand total", f"${data['grand_total']:,.2f}"),
                ("Win rate", f"{data['win_rate']:.0f}%"),
                ("Fees paid", f"${data['fees']:,.2f}"),
                ("Max drawdown", f"${data['max_dd']:,.2f}"),
            ],
            color=g.COLOR_INFO, event="analysis",
        )
        if not quiet:
            print("[posted to Discord]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
