# Trading bots (dry run only)

Two families of automated trading bots — crypto (this folder) and stocks
(`stock_bots/`) — that scan a market, buy dips, and sell at a target. **None
of them place a real order, ever, in any mode built so far.** Every bot only
simulates fills against real, live (or historical, for backtests) prices and
logs everything, so the strategies can be watched and tuned risk-free before
anyone decides whether to trade for real.

pls let me know if you have any suggestions

## Repo layout

| Folder | Venue | What's in it |
|---|---|---|
| `.` (this folder) | Coinbase (ETH) | `grid_bot.py` (grid trading) + `trend_bot.py` (moving-average trend following), backtests, analysis tools, live-venue readiness check. Documented in full below. |
| `stock_bots/` | Questrade (equities) | `grid_bot_stock.py` / `trend_bot_stock.py` (single fixed symbol, same engine as the crypto bots) and `grid_bot_watchlist.py` (scans a list of stocks and buys the best dip across all of them, with re-anchoring, a profit-lock time-stop, and a real stop-loss). Backtesting tools and a 50+ test suite included. See [`stock_bots/README.md`](stock_bots/README.md) for full detail. |

Both families share the same engine philosophy: a grid buys fixed-percentage
dips and sells at a fixed take-profit, risk rails cap capital/losses at the
portfolio level, every new feature defaults **off** until it's been backtested
and someone deliberately turns it on, and nothing runs live without an
explicit, separate decision later.

## Coinbase ETH grid bot — Phase 1 (dry run)

A rule-based grid-trading agent for ETH. **This build never places a real
order.** It polls a public ETH‑USD price, runs the full grid + risk-rail
state machine, simulates fills with a fee assumption, and logs everything.
It implements Phase 1 of a three-phase rollout: dry run → confirmed live
orders → fully automatic (see `execution.mode` ladder below) — only Phase 1
is built.

## Files

| File | Purpose |
|---|---|
| `grid_bot.py` | The agent. Loop / `--once` / `--status` / `--summary` / `--reset`. |
| `config.json` | All strategy + risk + fee parameters. Edit here, not in code. |
| `simulate.py` | Offline harness — pushes synthetic price paths through the real state machine. |
| `market_data.py` | Trailing daily closes from Coinbase's public candles endpoint (no auth). Powers the trend filter + vol spacing. Cached hourly to `logs/market_cache.json`. |
| `test_adaptations.py` | Unit tests for the adaptive + fill-model features (52 checks). `python test_adaptations.py` |
| `analyze.py` | Performance report from the logs → `logs/reports/`. `--discord` posts a summary. Read-only. |
| `venue_coinbase.py` | Coinbase Advanced Trade adapter (live modes only; not used by the dry run). |
| `check_venue.py` | Readiness check against the real Coinbase API. Places no orders. |
| `checkin.py` | Reads logs + state, posts a status summary to Discord. Run manually or via Task Scheduler. Changes nothing. |
| `requirements.txt` | Deps for the live path only (`coinbase-advanced-py`). |
| `backtest.py` | Backtests the grid against real ETH-USD history (public Coinbase candles). `python backtest.py --months 12` |
| `backtest_trend.py` | Backtests the trend-following rule below over the same history. |
| `trend_bot.py` / `trend_config.json` | A second, separate dry-run bot — see "Trend-following bot" below. Runs alongside `grid_bot.py`. |
| `state.json` | Persisted state (created on first run). Restart-safe. |
| `logs/trades.csv` | One row per simulated order: level, size, price, fee, P&L, `client_order_id`. |
| `logs/daily_summary.csv` | One row per UTC day. |
| `logs/events.log` | Human-readable log of every decision, skip, and alert. |
| `logs/heartbeat.log` | One "still alive" status line every `heartbeat_interval_sec` (default 300 s / 5 min). |

## Discord notifications

Set a channel webhook URL — either in `config.json` → `notifications.discord_webhook_url`,
or the env var `GRID_BOT_DISCORD_WEBHOOK` (env wins). Works for both the dry run
and the live bot; the message title always shows the current `mode`.

Test it:

```
python grid_bot.py --test-notify
```

Fires a Discord message on:
- ▶️ bot start / ⏹️ bot stop
- 🟦 every BUY  (fill price, size, fee, take-profit target, deployed capital)
- 🟩 / 🟧 every SELL  (green if the tranche closed in profit, orange if not; P&L, running total)
- ⚠️ every alert — hard stop-loss, max daily loss, order failure, price-feed failure
- 📊 daily summary at UTC-day rollover
- ▶️ daily-loss pause cleared

The heartbeat is **not** sent to Discord (too noisy) — it stays terminal + `heartbeat.log`.
A webhook URL is a secret; prefer the env var over the config file.
A notification failure is logged and swallowed; it never interrupts trading.

### Pinging @everyone

`config.json` → `notifications`:
- `mention`: `"@everyone"` (current), `"@here"`, a role `"<@&ROLE_ID>"`, or `null` to disable
- `mention_events`: which events actually ping — currently `["alert", "buy", "sell"]`.
  Add `"daily"` / `"start"` / `"stop"`, or set `["all"]` to ping on every message.

So right now: **every BUY, every SELL, and every alert ping @everyone**; daily
summaries and start/stop do not. `--test-notify` always pings so you can confirm it.
The ping needs the webhook's channel to allow @everyone mentions (default yes).

## Run the 2-day dry run

**Option A — leave it running** (simplest):

```
python grid_bot.py
```

Polls every 60 s. What it prints to that terminal:
- startup lines, then a **heartbeat line every 5 min**
  (`[HEARTBEAT] price=… open=… rungs_armed=… realized=… unrealized=…`)
- every simulated `BUY` / `SELL` as it happens
- any alert (stop-loss, daily-loss, order/feed failure)
- a full daily summary once per UTC day

`Ctrl+C` stops it; state is saved, so you can restart and it resumes.
Set `execution.heartbeat_interval_sec` to `0` in `config.json` to silence the
heartbeat.

**Option B — Windows Task Scheduler**, every 5 minutes:

```
schtasks /create /tn "eth-grid-dryrun" /tr "python \"%CD%\grid_bot.py\" --once" /sc minute /mo 5
```

(`--once` does a single iteration and exits — good for scheduled runs.)

Check in any time:

```
python grid_bot.py --status      # full state
python grid_bot.py --summary      # today's P&L
```

## Design decisions

1. **Grid levels are fixed prices off an anchor.** The anchor is the ETH price
   at first start (or set `grid.anchor_price` in config to pin it). Levels are
   `anchor × 0.95`, `× 0.95²`, `× 0.95³` — a "buy every 5% drop" rule
   implemented as three static rungs rather than a per-fill recalculation.
   - *Consequence:* if you start the bot when ETH is near a local low, price may
     never reach the rungs and nothing trades. That's fine for a dry run — it's
     what you're observing. Re-pin the anchor if needed.
2. **Fill model = `resting`** (see "Fill modelling" below). `at_level` (fills
   exactly at the rung) and `at_poll` (fills at the polled price) are still
   selectable for A/B comparison.
3. **Fees:** a flat `fee_rate_per_side` (default **0.6%**, Coinbase Advanced
   maker, lowest tier) is charged on every simulated side. Set to `0.012` to
   model taker fills. Real fees depend on the venue you pick — this is an
   assumption for the dry run only.
4. **Take-profit** is per tranche, at +4% from *that tranche's* fill price.
5. **Reset:** when a tranche sells, its rung is re-armed and will buy again if
   price drops back to it.
6. **`max_trades_per_day` (7) caps BUYS only.** Take-profit sells are always
   allowed, so a trading cap never blocks taking profit or (later) stopping
   out. Both counts are shown separately in the summary.

## Adaptive features (`config.json` → `adaptations`)

All rule-based, no learning. **Grid geometry (anchor + spacing) only ever changes
while no tranches are open** — reshaping mid-position would orphan take-profits.
Each block is inert unless its `*_enabled` flag is true.

| Feature | Default | What it does |
|---|---|---|
| **Trend filter** | **ON** | Pause opening *new* buys while ETH < its 20-day moving average. Existing tranches + their take-profits are untouched. Hysteresis: once paused, price must reclaim `MA × (1 + 1%)` to resume. In the `downtrend` sim this took the loss from **−$3,107 to $0**. |
| **Re-anchor (breakout)** | **ON** | If price rises ≥ 8% above the anchor while flat, move the anchor to current price and rebuild rungs — fixes the "price ran away up, bot idles forever" state. |
| **Re-anchor (post-stopout)** | **ON** | After a hard stop-loss clears and the position is flat, re-anchor at current price instead of resuming on a stale grid. |
| **Max-hold time-stop** | **ON** | Force-close a tranche held > 10 days (at current price, not the take-profit) if it's underwater. That grid level then won't rebuy for 48 h (`max_hold_cooldown_hours`). |
| **Vol-scaled spacing** | **OFF** | Would make your fixed 5% spacing dynamic: `5% × clamp(realized_vol / 3.0%, 0.75, 1.6)`. Left off so you enable it deliberately — turn on `vol_spacing_enabled` after you've seen how it moves the rungs. |

Trend filter + vol spacing need daily candles from `market_data.py` (Coinbase
public endpoint, no auth, cached hourly). If that fetch fails the features go
idle — the filter **holds its current verdict**, it doesn't flip to permissive.

`simulate.py` now runs an A/B on the `downtrend` scenario (adaptations on vs off)
and has a `recovery` scenario that exercises the trend-resume + re-anchor path.

### Idle-cash yield (`config.json` → `idle_yield`, **ON**)

Models the USDC rewards on the *undeployed* capital — a grid is in cash most of
the time. Each poll accrues `(allocated + realized_pnl − deployed) × apy_pct`,
pro-rated by elapsed time, into `state.yield_earned_usd`. Shown as a separate
line in the summary / heartbeat / check-in and folded into "grand total incl
yield". Places **no trade** (for live you'd hold USDC and Coinbase pays it).
~$1.2/day on $10k idle at 4.5% ≈ **$37/month**.

## Fill modelling (`config.json` → `execution.fill_model`, now `resting`)

`resting` models actual resting limit orders instead of assuming perfect fills:

- an order fills only when the price **band between two polls** crosses its price
  (catches fast intra-poll moves the old point-check missed)
- a **shallow touch** — within `shallow_touch_pct` of the order — fills only with
  probability `shallow_fill_prob` (0.5), standing in for queue position
- a grid level **re-armed below the market** stays inactive until price trades
  back above it (a `post_only` maker order can't fill immediately)
- **max-hold exits are taker** market orders: `fee_rate_taker` + `market_slippage_bps`
- reproducible via `fill_seed`

`simulate.py` A/B on the `kiss` scenario (price repeatedly grazing a rung):
**at_level: 45 cycles / +$3,720** vs **resting: 33 cycles / +$2,728** — the old
model overstates by ~25% when fills are marginal. `at_level` / `at_poll` are kept
for comparison.

Still simplified: no order-book depth, no partial fills, 60-second granularity.

## Trend-following bot (`trend_bot.py`)

A second, independent dry-run bot you can run **at the same time** as the grid
in another terminal — separate config (`trend_config.json`), state
(`trend_state.json`), and logs (`logs/trend_*`), no interference either way.

**Rule:** hold 100% ETH while yesterday's daily close is above its 50-day moving
average (`strategy.ma_days`), otherwise hold 100% USDC (earning the same
modelled idle yield as the grid). Evaluated **once per UTC day**, right after
rollover, using the completed previous day's close — this matches
`backtest_trend.py` exactly, so live behaviour matches what was backtested and
there's no intraday whipsaw. `strategy.reference_ma_days` (100, 200) are logged
each day for comparison but never traded.

```
python trend_bot.py             # continuous loop
python trend_bot.py --once      # single daily check
python trend_bot.py --status    # dump state
python trend_bot.py --summary   # today's status
python trend_bot.py --reset     # wipe trend_state.json
python trend_bot.py --test-notify
```

Shares the grid bot's Discord webhook (same env var / same channel) and
notification style — 🟦 BUY / 🟩🟧 SELL / ⚠️ ALERT / 📊 daily summary — so both
bots' activity shows up together, clearly labelled. Its drawdown "rail" is just
an alert (`risk.alert_drawdown_pct`, default 20%) — unlike the grid, exiting to
cash on the next down-signal **is** this strategy's risk control, so there's no
hard stop to trip.

## Performance analysis — `analyze.py`

```
python analyze.py             # print + write logs/reports/<date>.md
python analyze.py --discord    # also post a summary
```

Computes: P&L breakdown (realized / unrealized / yield / fees / **fee drag %**),
cycle stats (win rate, P&L distribution, hold time, exit reasons, taker share),
per-rung fills + P&L, adaptation/rail activity, max drawdown — plus **flags**
("rung 2 never filled", "fees are 31% of gross → widen TP or use ETH-USDC",
"only N cycles — not conclusive").

**Run it monthly, or after ~50 completed cycles — not every few days.** Fewer
data points than that and any "pattern" is noise. Use the flagged issues to
decide what to test next — a targeted A/B backtest is usually the fastest way
to confirm whether a change actually helps. `analyze.py` changes nothing itself.

## What the risk rails actually do

| Rail | Trigger | Action |
|---|---|---|
| Max capital deployed | open tranches + next tranche > **$90** | that buy is skipped |
| Hard stop-loss | total drawdown (realized + unrealized, incl. est. exit fees) > **50%** of $100 | `halted = true`: **stop all buying**, alert. Open tranches are **kept intentionally**, to avoid averaging down further into a losing position. This is a *buying halt, not a liquidation*: funds stay at risk in the open tranches. |
| Max daily loss | day P&L loss > **40%** of $100 | `paused = true`: stop **everything** (no buys, no sells), alert. Auto-clears at the next UTC day rollover, or clear `"paused": false` in `state.json` after review. |
| Max buys/day | 7 buys in a UTC day | further buys skipped until rollover |

The hard stop-loss `halted` flag does **not** auto-clear — it requires you to set
`"halted": false` in `state.json` after a manual review.

## Simulation harness

```
python simulate.py                    # oscillate + downtrend + crash
python simulate.py oscillate
```

- **oscillate** — ±12% sine wave. Grid completes buy→sell cycles, small net gain
  after fees. This is the case the strategy is built for.
- **downtrend** — slow 35% grind down. Bot buys all 3 rungs and sits underwater
  with no sells. This is the fixed-grid design's known weakness — see
  "Known limitations" below.
- **crash** — 55% straight down. Both the max-daily-loss and hard-stop-loss
  rails fire — useful for confirming the alerts trigger as expected.

Writes separate `logs/sim_<scenario>_*` files; does not touch live state.

## Venue: Coinbase Advanced Trade

Chosen venue. Adapter lives in `venue_coinbase.py` (wraps `coinbase-advanced-py`).
Only used by the live modes — the dry run stays stdlib-only.

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python check_venue.py        # readiness check, places NO orders
```

`check_venue.py` verifies auth, market data, balances, and previews a real grid
buy + take-profit sell so you see the **real** maker fee (~0.60% / side at the
lowest tier — $0.18 on a $30 tranche).

### `execution.mode` ladder

| mode | price | orders | rollout phase |
|---|---|---|---|
| `dry_run` | public feed | simulated | **Phase 1** — the only mode built so far |
| `live_preview` | real Coinbase | real *previews* only, never places | pre-Phase-2 connectivity soak |
| `confirm` | real Coinbase | real orders, each needs `y/n` in terminal | **Phase 2** |
| `live` | real Coinbase | fully automatic | **Phase 3** |

`live_preview` / `confirm` / `live` need the live order lifecycle (real limit
orders → poll for fill → place take-profit → cancel-on-halt). **Not built yet** —
that's the next increment, after `check_venue.py` passes.

### What's needed before going live

1. **API key with Trade permission.** A Coinbase Advanced Trade Secret API
   Key JSON (`cdp_api_key.json`) authenticates and can preview orders. Confirm
   it has *Trade* (not just View) at https://portal.cdp.coinbase.com/access/api
   — or create a new Secret API Key with Trade enabled and point
   `execution.coinbase_key_file` at it.
2. **Dedicated portfolio.** Create a separate Coinbase portfolio, move only
   the capital intended for this strategy into it, and put its id in
   `execution.coinbase_portfolio_id`. This scopes the bot so it can never
   touch funds outside that portfolio.
3. Complete Phases 1–2 before setting `mode: "live"`.

This build intentionally stops at Phase 1: it never places a real order,
under any mode or configuration, with or without per-order confirmation.
The live order lifecycle (Phases 2/3) is deliberately not implemented here.

## Known limitations

- Single fixed anchor; no automatic re-anchoring on a sustained rally.
- Price feed is a single unauthenticated endpoint; a feed outage just skips the
  iteration and alerts (no redundant source in Phase 1).
- `at_level` fills are optimistic — they assume your limit order always fills at
  the exact rung with no partial fills or queue position.
- No slippage model in `at_level` mode.
