# Stock bots (Questrade) — DRY RUN ONLY

Copies of the two crypto bots (`../grid_bot.py` and `../trend_bot.py`), pointed
at the stock market via Questrade's API instead of Coinbase, plus a third bot
that scans a *watchlist* of stocks instead of trading one fixed symbol. Same
rule for all three: **these place no real orders, under any circumstance.**
They only simulate.

The trading engine itself is *imported*, not duplicated — `grid_bot_stock.py`
and `trend_bot_stock.py` pull in `grid_bot.py` / `trend_bot.py` from the parent
folder and swap out the price feed and file paths. That means these bots run
the exact same, already-tested grid/trend logic (see `../test_adaptations.py`,
76 passing tests) — the only new code here is the Questrade venue adapter, a
market-hours gate, and each bot's own config/state.

## 1. Get a Questrade refresh token

1. Log into questrade.com → **My Accounts → App Hub → Personal apps**.
2. Generate a new refresh token.
3. Save **just the token string** (nothing else — no quotes, no JSON) into a
   new file: `stock_bots/questrade_refresh_token.txt`.

That file is gitignored, same as the Coinbase API key file. Questrade
**rotates** the refresh token every time it's used — `venue_questrade.py`
automatically overwrites the file with the newest token after every refresh,
so don't hand-edit it while a bot is running, and don't run two bots that
would refresh it at the exact same instant.

Test the connection once your token is in place:

```bash
python venue_questrade.py AAPL
```

That prints the symbol ID, last price, and a candle count with no order risk
— read-only calls only.

## 2. Pick a symbol

Both `config.json` (grid bot) and `trend_config.json` (trend bot) default
`asset` to `"AAPL"` as a placeholder. Change it to whatever stock you want to
dry-run — any symbol Questrade can quote. The two bots are independent, so
they don't need to trade the same symbol.

## 3. Run them

```bash
python grid_bot_stock.py
```

```bash
python trend_bot_stock.py
```

Same flags as the crypto bots: `--once`, `--status`, `--summary`, `--reset`,
`--test-notify`. They log to `stock_bots/logs/` and share the crypto bots'
Discord webhook/channel conventions (`GRID_BOT_DISCORD_WEBHOOK` env var, or
`notifications.discord_webhook_url` in each config).

## 4. The watchlist bot (`grid_bot_watchlist.py`)

Instead of one fixed symbol, this scans a list of stocks (`watchlist_symbols`
in `watchlist_config.json`) every poll and buys the dip in whichever one
currently looks best — a shared capital pool across the whole list, rather
than one grid per symbol.

```bash
python grid_bot_watchlist.py
```

Every symbol in the watchlist still gets its own grid (anchor + rungs, same
mechanics as the other bots) — a rung has to be genuinely armed, primed, and
reached before a symbol is "eligible" at all. What's new is the ranking step
that decides *which* eligible symbol gets the next tranche when more than one
qualifies on the same poll, plus a `max_tranches_per_symbol` cap (default 1)
so the shared capital pool naturally spreads across the watchlist instead of
piling into one symbol that happens to keep ranking best.

Two ranking rules are built into `watchlist_config.json`'s `ranking.mode`,
only one active at a time:

- **`"vol_normalized_dip"` (ACTIVE by default)** — ranks eligible symbols by
  how far each has fallen from its own recent high, divided by its own recent
  volatility, so a 5% drop in a calm stock outranks the same 5% drop in a
  choppy one.
- **`"grid_depth"` (built, INACTIVE)** — ranks by how many grid rungs deep
  each symbol has fallen. Pure grid mechanics, no volatility involved. Switch
  `ranking.mode` to `"grid_depth"` in `watchlist_config.json` to try it
  instead — it's fully implemented and unit-tested (`test_watchlist.py`),
  just not the default while option 3 is what's being evaluated.

Run `python test_watchlist.py` any time to re-verify both ranking rules —
including a scenario that proves the shared-capital contention case: when two
symbols both qualify in the same poll but there's only enough budget for one,
the better-ranked symbol wins, not whichever happened to be checked first.

Same flags (`--once`/`--status`/`--summary`/`--reset`/`--test-notify`), same
market-hours gate, logs to `stock_bots/logs/watchlist_*`.

## What's actually different from the crypto bots

- **Price/candle feed**: Questrade's OAuth-authenticated quote and candle
  endpoints (`venue_questrade.py`), instead of Coinbase's public endpoints.
  Quotes are real-time only if your Questrade account has a real-time data
  package — otherwise expect ~15-minute-delayed data. Fine for a dry run,
  just don't mistake it for live pricing.
- **Market hours gate** (`market_hours.py`): stocks don't trade 24/7 like
  crypto. Both bots only poll/trade during 9:30–16:00 America/New_York,
  Mon–Fri, and otherwise sit idle (logging "market closed" at most once every
  30 minutes so it doesn't spam). **This check does not know about market
  holidays** (Christmas, Thanksgiving, etc.) — on those days it will
  incorrectly think the market is open, but since nothing is actually trading
  that day either, no harm done (nothing to fill).
- **Fees**: Questrade's real equity commission is roughly $0.01/share with a
  $4.95 minimum and $9.95 maximum *per trade* — not a clean percentage like
  Coinbase's maker/taker split. `config.json`/`trend_config.json` approximate
  this as a flat percentage rate calibrated to the default trade size; if you
  change `tranche_size_usd` / `allocated_capital_usd` a lot, or trade a very
  cheap/expensive stock, re-check against Questrade's published schedule for
  your account and adjust.
- **Adaptive features default OFF**: the crypto grid bot's `adaptations`
  block (trend filter, re-anchor, max-hold, breakout-buy, vol-spacing) has
  been tuned/backtested for ETH specifically. None of that carries over to
  equities automatically, so every adaptation here starts `false` — same
  "off until you deliberately test it" rule used for the crypto bot's
  breakout-buy feature.

## Canada-specific notes (recap from the earlier conversation)

- **No US Pattern Day Trader rule**: PDT (the $25k / 3-day-trade cap) is a
  FINRA rule for US broker-dealers. Trading through Questrade puts you under
  IIROC rules instead, which don't impose that same cap — so a
  frequent-trading grid strategy isn't blocked here the way it would be at a
  small US broker account.
- **Superficial loss rule**: Canada's version of the US wash-sale rule — a
  capital loss is disallowed if you sell at a loss and buy back the same/an
  identical security within 30 days before or after.
- **TFSA day-trading risk**: if you ever considered running something like
  this inside a TFSA specifically, the CRA has reclassified frequent/day-trading
  activity in TFSAs as fully-taxable *business income* (plus penalties) in
  real cases — worth a conversation with an accountant before doing that, not
  a decision to make from this README.
- **Currency**: if you trade US-listed stocks from a CAD account, factor in
  USD/CAD conversion spread as a real cost, the same way maker/taker fees are
  modelled for the crypto bot.

## Known cosmetic detail

Internally, tranche/trade records still use field names inherited from the
crypto engine (e.g. `qty_eth` in `state.json`/`trades.csv`) — these represent
**shares of whatever `asset` you configured**, not literal ETH. Discord/log
messages were generalized to show the real symbol (`asset_symbol()` in
`grid_bot.py`), but the underlying JSON/CSV key names were left alone to stay
byte-for-byte compatible with the already-tested engine code.
