#!/usr/bin/env python3
"""
Offline simulation harness for the grid bot.

Feeds a synthetic ETH price path through the real state machine in grid_bot.py
(no network, no orders) so you can watch buys, take-profits, level re-arming,
every risk rail, and the adaptive features (trend filter, vol spacing, re-anchor)
before committing to a live dry run.

Each simulation step is treated as one "day" for the market-data indicators, so
a 20-step moving average stands in for a 20-day MA. The whole run happens inside
one real second, so the daily rollover and the max-hold time-stop (which are
wall-clock based) don't fire here -- they're covered by test_adaptations.py.

Usage:
    python simulate.py                       # all scenarios
    python simulate.py oscillate             # one scenario
    python simulate.py downtrend --no-adapt  # disable adaptations for A/B
Scenarios: oscillate | downtrend | crash | recovery
"""
from __future__ import annotations

import math
import os
import sys

import grid_bot as gb

HERE = os.path.dirname(os.path.abspath(__file__))


def price_path(name: str) -> list[float]:
    start = 2500.0
    if name == "oscillate":
        return [start * (1 + 0.12 * math.sin(i / 8.0)) for i in range(220)]
    if name == "downtrend":
        return [start * (1 - 0.0018 * i) * (1 + 0.02 * math.sin(i / 3.0)) for i in range(220)]
    if name == "crash":
        return [start * (1 - 0.006 * i) for i in range(110)]
    if name == "recovery":
        out = [start * (1 - 0.005 * i) for i in range(60)]          # -30%
        out += [out[-1] * (1 + 0.001 * math.sin(i)) for i in range(20)]  # flat
        base = out[-1]
        out += [base * (1 + 0.006 * i) for i in range(80)]          # recover past start
        return out
    if name == "choppy":
        out = []
        for i in range(260):
            env = start * (1 + 0.10 * math.sin(i / 11.0))
            noise = 1 + 0.035 * math.sin(i * 2.3) * math.cos(i * 0.7)
            out.append(env * noise)
        return out
    if name == "kiss":
        # first hold at `start` so the anchor lands at 2500 (rung 0 = 2375), then
        # repeatedly come down to *just barely* touch rung 0 (~0.05% below, inside
        # shallow_touch_pct) and bounce. at_level fills every kiss; 'resting' fills
        # only ~half (queue position).
        rung0 = start * 0.95
        out = [start] * 5
        for i in range(180):
            if i % 4 == 0:
                out.append(rung0 * 0.9995)       # shallow kiss just below rung 0
            elif i % 4 == 2:
                out.append(rung0 * 1.045)         # bounce above rung0*1.04 -> hits TP, re-primes
            else:
                out.append(rung0 * 1.02)
        return out
    raise SystemExit(f"unknown scenario {name!r}")


def _grep_count(path: str, needle: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as fh:
        return sum(1 for line in fh if needle in line)


def run(name: str, adapt: bool = True, force_on: dict | None = None,
        fill_model: str = "at_level") -> dict:
    cfg = gb.load_config()
    cfg["execution"]["mode"] = "dry_run"
    cfg["execution"]["fill_model"] = fill_model
    # the whole sim runs inside one wall-clock second, so the daily rollover never
    # fires -- lift the per-day buy cap so it doesn't dominate every scenario.
    cfg["risk"]["max_trades_per_day"] = 10_000_000
    if not adapt:
        cfg["adaptations"] = {}
    elif force_on:
        cfg["adaptations"].update(force_on)   # override config for a clean A/B

    state = gb.default_state()
    suffix = ("_noadapt" if not adapt else "") + (f"_{fill_model}" if fill_model != "at_level" else "")
    for attr, fn in (("STATE_PATH", "state.json"), ("TRADES_CSV", "trades.csv"),
                     ("DAILY_CSV", "daily.csv"), ("EVENTS_LOG", "events.log"),
                     ("HEARTBEAT_LOG", "heartbeat.log")):
        p = os.path.join(HERE, "logs", f"sim_{name}{suffix}_{fn}")
        setattr(gb, attr, p)
        if os.path.exists(p):
            os.remove(p)

    path = price_path(name)
    prices_seen: list[float] = []
    it = iter(path)

    def fake_get_price(_cfg):
        p = next(it)
        prices_seen.append(p)
        return p

    def fake_market_data(_cfg):
        if len(prices_seen) < 25:          # need > lookback+1 "daily" closes first
            return None
        return {"closes": prices_seen[-60:], "as_of": gb.iso(gb.now_utc()), "stale": False}

    gb.get_price = fake_get_price
    gb.load_market_data = fake_market_data
    gb.discord_send = lambda *a, **k: None      # never post during a sim

    print(f"\n########## SCENARIO: {name}{'  (adaptations OFF)' if not adapt else ''}  "
          f"start={path[0]:.0f} end={path[-1]:.0f} min={min(path):.0f} max={max(path):.0f} ##########")
    steps = 0
    for _ in path:
        try:
            gb.iterate(state, cfg)
            steps += 1
        except StopIteration:
            break

    price = state["last_price"]
    unreal = gb.compute_unrealized(state, price, cfg)
    taker = _grep_count(gb.TRADES_CSV, "taker")
    print(f"  closed_tranches        : {len(state['closed_tranches'])}")
    print(f"  realized / unrealized  : {round(state['realized_pnl_usd'], 2)} / {round(unreal, 2)}")
    print(f"  total P&L              : {round(state['realized_pnl_usd'] + unreal, 2)}")
    print(f"  fees paid (all-time)   : {round(state['fees_paid_usd'], 2)}")
    print(f"  open_tranches (end)    : {len(state['open_tranches'])}")
    print(f"  grid reshapes          : {_grep_count(gb.EVENTS_LOG, 'Grid reshaped')}")
    print(f"  trend-filter switches  : {_grep_count(gb.EVENTS_LOG, 'Trend filter')}")
    print(f"  taker fills             : {taker}")
    print(f"  halted / paused (end)  : {state['halted']} / {state['paused']}")
    print(f"  steps_run              : {steps}")
    return {"name": name, "total": round(state["realized_pnl_usd"] + unreal, 2),
            "closed": len(state["closed_tranches"]), "fees": round(state["fees_paid_usd"], 2)}


if __name__ == "__main__":
    pos = [a for a in sys.argv[1:] if not a.startswith("-")]
    no_adapt = "--no-adapt" in sys.argv
    scenarios = pos or ["oscillate", "downtrend", "crash", "recovery", "choppy"]
    for s in scenarios:
        run(s, adapt=not no_adapt)

    if not no_adapt and "downtrend" in scenarios:
        print("\n=== A/B: 'downtrend' with trend filter ON vs OFF ===")
        on = run("downtrend", adapt=True, force_on={"trend_filter_enabled": True})
        off = run("downtrend", adapt=False)
        print(f"\n  downtrend total P&L   trend-filter ON {on['total']:+.2f}   "
              f"OFF {off['total']:+.2f}")

    print("\n=== A/B: fill model  resting (honest) vs at_level (optimistic) ===")
    for sc in ("choppy", "kiss"):
        rest = run(sc, fill_model="resting")
        opt = run(sc, fill_model="at_level")
        print(f"  {sc:8} resting : {rest['closed']:3} cycles  ${rest['total']:+9.2f}  "
              f"${rest['fees']:7.2f} fees")
        print(f"  {sc:8} at_level: {opt['closed']:3} cycles  ${opt['total']:+9.2f}  "
              f"${opt['fees']:7.2f} fees")
