#!/usr/bin/env python3
"""
One-shot status check for the grid bot dry run. Reads the logs + state and posts
a summary to Discord (same webhook the bot uses: env GRID_BOT_DISCORD_WEBHOOK or
config notifications.discord_webhook_url). Also prints the summary to stdout.

Run:  python checkin.py            (post to Discord + print)
      python checkin.py --print    (print only, no Discord)

Intended to be fired by Windows Task Scheduler. Places no trades, changes nothing.
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timezone

import grid_bot as g

HERE = os.path.dirname(os.path.abspath(__file__))


def _age(ts_iso: str | None) -> str:
    if not ts_iso:
        return "never"
    try:
        t = datetime.fromisoformat(ts_iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - t).total_seconds()
    except ValueError:
        return f"?({ts_iso})"
    if secs < 90:
        return f"{secs:.0f}s ago"
    if secs < 5400:
        return f"{secs / 60:.0f} min ago"
    return f"{secs / 3600:.1f} h ago"


def _last_heartbeat_ts() -> str | None:
    p = os.path.join(HERE, "logs", "heartbeat.log")
    if not os.path.exists(p):
        return None
    last = None
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            if "[HEARTBEAT]" in line:
                last = line.split(" [HEARTBEAT]", 1)[0].strip()
    return last


def _count_trades() -> tuple[int, int, list[str]]:
    p = os.path.join(HERE, "logs", "trades.csv")
    buys = sells = 0
    recent: list[str] = []
    if not os.path.exists(p):
        return 0, 0, []
    with open(p, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["action"] == "BUY":
                buys += 1
            elif row["action"] == "SELL":
                sells += 1
            recent.append(
                f"{row['timestamp'][5:16]}  {row['action']} L{row['level_index']} "
                f"@ {row['price']}  pnl {row['realized_pnl_delta_usd']}"
            )
    return buys, sells, recent[-6:]


def _alerts() -> list[str]:
    p = os.path.join(HERE, "logs", "events.log")
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            if "[ALERT]" in line:
                out.append(line.strip())
    return out[-8:]


def build_summary() -> tuple[str, list[tuple[str, str]], str, int]:
    cfg = g.load_config()
    state = g.load_state()
    price = state.get("last_price") or 0.0

    hb = _last_heartbeat_ts()
    hb_age = _age(hb)
    running = hb is not None and ("s ago" in hb_age or ("min ago" in hb_age and int(hb_age.split()[0]) <= 15))

    buys, sells, recent = _count_trades()
    unreal = g.compute_unrealized(state, price, cfg) if price else 0.0
    realized = round(state.get("realized_pnl_usd", 0.0), 2)
    yield_earned = round(float(state.get("yield_earned_usd", 0.0)), 2)
    alloc = cfg["allocated_capital_usd"]
    total = realized + unreal + yield_earned

    alerts = _alerts()

    fields = [
        ("Bot status", ("🟢 running" if running else "🔴 NO FRESH HEARTBEAT") + f" (last {hb_age})"),
        ("Mode", f"{cfg['execution']['mode']}  (scaled to ${alloc:,.0f})"),
        ("ETH price / anchor", f"${price:,.2f}  /  ${state.get('anchor_price') or 0:,.2f}"),
        ("Rungs", ", ".join(f"${l['price']:,.0f}{'*' if l['held'] else ''}"
                            for l in state.get("grid_levels", [])) or "not set"),
        ("Simulated buys / sells", f"{buys} / {sells}"),
        ("Open tranches", f"{len(state.get('open_tranches', []))} / {cfg['grid']['num_levels']}  "
                          f"(deployed ${g.deployed_usd(state):,.0f})"),
        ("Realized P&L", f"${realized:,.2f}"),
        ("Unrealized P&L", f"${unreal:,.2f}"),
    ] + ([("Idle-cash yield", f"${yield_earned:,.2f}")] if yield_earned else []) + [
        ("Total P&L", f"${total:,.2f}  ({100 * total / alloc:+.2f}% of allocated)"),
        ("Flags", " ".join(f for f, on in (("HALTED", state.get("halted")),
                                            ("PAUSED", state.get("paused"))) if on) or "none"),
        ("Alerts logged", str(len(alerts))),
    ]
    desc_lines = []
    if recent:
        desc_lines.append("**Recent fills:**\n" + "\n".join(recent))
    if alerts:
        desc_lines.append("**Alerts:**\n" + "\n".join(a.split("] ", 1)[-1] for a in alerts))
    description = "\n\n".join(desc_lines)

    color = g.COLOR_ALERT if (not running or alerts or state.get("halted") or state.get("paused")) else (
        g.COLOR_SELL_WIN if total >= 0 else g.COLOR_SELL_LOSS)
    title = f"🗓️ Dry-run check-in — {datetime.now().strftime('%Y-%m-%d %H:%M')} local"
    return title, fields, description, color


def main() -> int:
    cfg = g.load_config()
    g.configure_notifications(cfg)
    title, fields, description, color = build_summary()

    print(title)
    for name, val in fields:
        print(f"  {name:24s}: {val}")
    if description:
        print("\n" + description)

    if "--print" in sys.argv:
        return 0
    if not g.discord_configured():
        print("\n(no Discord webhook configured — not posting)")
        return 0
    g.discord_send(title, description, fields, color=color, event="checkin")
    print("\nPosted to Discord.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
