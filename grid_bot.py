#!/usr/bin/env python3
"""
ETH grid trading bot -- PHASE 1: DRY RUN ONLY.

This program NEVER places a real order. It:
  * polls a public ETH-USD price,
  * runs the full grid + risk-rail state machine,
  * simulates fills using a configurable per-side fee assumption,
  * logs every simulated order and every risk event,
  * prints / appends a daily P&L summary.

Live trading (Phase 2/3) is intentionally not implemented. The functions
`preview_order()` and `place_order()` are the seam where a real venue adapter
(Coinbase Advanced Trade or CDP swaps) would plug in later.

Stdlib only. Run:  python grid_bot.py             (continuous loop)
                   python grid_bot.py --once      (single iteration; good for cron)
                   python grid_bot.py --status    (print state and exit)
                   python grid_bot.py --summary   (print today's summary and exit)
                   python grid_bot.py --reset     (wipe state.json after confirmation)
                   python grid_bot.py --test-notify  (send a test Discord message)

Discord notifications: set config notifications.discord_webhook_url (or the env
var GRID_BOT_DISCORD_WEBHOOK). Fires on start/stop, every BUY and SELL, every
alert, and the daily summary. Used by both the dry run and (later) the live bot.
A notification failure never interrupts the trading loop.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
TRADES_CSV = os.path.join(HERE, "logs", "trades.csv")
DAILY_CSV = os.path.join(HERE, "logs", "daily_summary.csv")
EVENTS_LOG = os.path.join(HERE, "logs", "events.log")
HEARTBEAT_LOG = os.path.join(HERE, "logs", "heartbeat.log")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def today_str(dt: datetime | None = None) -> str:
    return (dt or now_utc()).strftime("%Y-%m-%d")


def log_event(msg: str, level: str = "INFO") -> None:
    line = f"{iso(now_utc())} [{level}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(EVENTS_LOG), exist_ok=True)
    with open(EVENTS_LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def alert(msg: str) -> None:
    """A loud log line + a red Discord notification (if configured)."""
    log_event(f"*** ALERT *** {msg}", level="ALERT")
    discord_send("⚠️ ALERT", msg, color=COLOR_ALERT, event="alert")


# --------------------------------------------------------------------------- #
# Discord notifications
# --------------------------------------------------------------------------- #
COLOR_BUY = 0x3498DB      # blue
COLOR_SELL_WIN = 0x2ECC71  # green
COLOR_SELL_LOSS = 0xE67E22  # orange
COLOR_ALERT = 0xE74C3C    # red
COLOR_INFO = 0x95A5A6     # grey

_DISCORD_WEBHOOK: str | None = None
_NOTIFY_ENABLED: bool = True
_MENTION: str | None = None          # e.g. "@everyone", "@here", "<@&ROLE_ID>"
_MENTION_EVENTS: set[str] = set()    # which event types get the ping; {"all"} = every one
_BOT_LABEL: str = "Grid Bot"         # Discord username; derived from cfg["asset"]


def asset_symbol(cfg: dict) -> str:
    """Human-readable unit label for Discord/log text, derived from config
    (e.g. 'ETH-USD' -> 'ETH', 'AAPL' -> 'AAPL')."""
    raw = str(cfg.get("asset", "")).strip()
    return raw.split("-")[0] or "units"


def configure_notifications(cfg: dict) -> None:
    """Resolve notification settings once at startup: env var wins over config."""
    global _DISCORD_WEBHOOK, _NOTIFY_ENABLED, _MENTION, _MENTION_EVENTS, _BOT_LABEL
    _BOT_LABEL = f"{asset_symbol(cfg)} Grid Bot"
    n = cfg.get("notifications", {}) or {}
    _NOTIFY_ENABLED = bool(n.get("enabled", True))
    url = os.environ.get("GRID_BOT_DISCORD_WEBHOOK") or n.get("discord_webhook_url")
    _DISCORD_WEBHOOK = url.strip() if isinstance(url, str) and url.strip() else None

    mention = os.environ.get("GRID_BOT_DISCORD_MENTION") or n.get("mention")
    _MENTION = mention.strip() if isinstance(mention, str) and mention.strip() else None
    events = n.get("mention_events", ["alert", "buy", "sell"])
    _MENTION_EVENTS = {str(e).lower() for e in events} if isinstance(events, list) else set()


def discord_configured() -> bool:
    return bool(_DISCORD_WEBHOOK and _NOTIFY_ENABLED)


def _should_mention(event: str) -> bool:
    if not _MENTION:
        return False
    if event == "silent":     # purely informational (e.g. a periodic price check-in) -- never pings,
        return False          # regardless of mention_events, even if it's set to ["all"]
    if event == "test":       # --test-notify always pings, so you can verify it
        return True
    return "all" in _MENTION_EVENTS or event.lower() in _MENTION_EVENTS


def discord_send(title: str, description: str = "",
                 fields: list[tuple[str, str]] | None = None,
                 color: int = COLOR_INFO, event: str = "") -> None:
    """POST one embed to the Discord webhook. Never raises.

    If `event` is in notifications.mention_events (or that list contains "all"),
    the configured mention string (e.g. @everyone) is put in the message content
    so Discord actually pings -- embed text never triggers a ping on its own.
    """
    if not discord_configured():
        return
    embed = {
        "title": title[:256],
        "description": (description or "")[:4000],
        "color": color,
        "timestamp": now_utc().isoformat(),
        "fields": [
            {"name": str(n)[:256], "value": str(v)[:1024], "inline": True}
            for n, v in (fields or [])
        ],
    }
    payload: dict = {"username": _BOT_LABEL, "embeds": [embed]}
    if _should_mention(event):
        payload["content"] = _MENTION
        parse = []
        if "@everyone" in _MENTION or "@here" in _MENTION:
            parse.append("everyone")
        if "<@&" in _MENTION:
            parse.append("roles")
        if "<@" in _MENTION and "<@&" not in _MENTION:
            parse.append("users")
        payload["allowed_mentions"] = {"parse": parse}
    data = json.dumps(payload).encode("utf-8")
    req = Request(_DISCORD_WEBHOOK, data=data, method="POST",
                  headers={"Content-Type": "application/json",
                           "User-Agent": "eth-grid-bot"})
    try:
        with urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001 -- notifications must never break the loop
        line = f"{iso(now_utc())} [WARN] Discord notify failed: {exc!r}"
        print(line, flush=True)
        try:
            os.makedirs(os.path.dirname(EVENTS_LOG), exist_ok=True)
            with open(EVENTS_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def heartbeat(state: dict, cfg: dict) -> None:
    """One-line 'still alive' status. Console + logs/heartbeat.log (kept separate
    from events.log so real events stay easy to scan)."""
    unreal = compute_unrealized(state, state.get("last_price") or 0.0, cfg)
    armed = sum(1 for lvl in state.get("grid_levels", []) if not lvl["held"])
    flags = []
    if state["halted"]:
        flags.append("HALTED")
    if state["paused"]:
        flags.append("PAUSED")
    if not state.get("trend_ok", True):
        flags.append("TREND-OFF")
    mult = float(state.get("spacing_mult", 1.0))
    line = (
        f"{iso(now_utc())} [HEARTBEAT] price={state.get('last_price')} "
        f"open={len(state['open_tranches'])} rungs_armed={armed} "
        f"deployed=${deployed_usd(state)} "
        f"realized=${round(state['realized_pnl_usd'], 2)} "
        f"unrealized=${unreal} "
        f"buys_today={state['day']['buy_count']} sells_today={state['day']['sell_count']}"
        + (f" spacing_mult={mult:.2f}" if abs(mult - 1.0) > 1e-9 else "")
        + (f" yield=${float(state.get('yield_earned_usd', 0.0)):.2f}"
           if float(state.get("yield_earned_usd", 0.0)) >= 0.01 else "")
        + (f"  {' '.join(flags)}" if flags else "")
    )
    print(line, flush=True)
    os.makedirs(os.path.dirname(HEARTBEAT_LOG), exist_ok=True)
    with open(HEARTBEAT_LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------------------------- #
# config / state
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    # utf-8-sig tolerates a BOM if config.json is edited in a Windows editor.
    with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
        return json.load(fh)


def default_state() -> dict:
    return {
        "created_at": iso(now_utc()),
        "anchor_price": None,
        "grid_levels": [],          # [{index, price, held: bool}]
        "open_tranches": [],        # see open a tranche below
        "closed_tranches": [],
        "realized_pnl_usd": 0.0,
        "fees_paid_usd": 0.0,
        "day": {"date": today_str(), "buy_count": 0, "sell_count": 0, "realized_pnl_usd": 0.0},
        "halted": False,            # hard stop-loss: stop BUYING, keep protective sells
        "paused": False,            # max daily loss: stop everything until manual clear
        "halt_reason": None,
        "last_price": None,
        "prev_price": None,                     # price at the previous poll (fill band)
        "last_iteration_at": None,
        "_fill_calls": 0,                       # seeds the reproducible fill RNG
        # --- adaptive features (all no-ops unless enabled in config.adaptations) ---
        "spacing_mult": 1.0,                    # volatility multiplier on grid spacing / TP
        "trend_ok": True,                       # trend filter verdict: is buying allowed?
        "reanchor_pending_after_stopout": False,
        "last_reshape_at": None,
        "market_as_of": None,
        "_last_skips": {},                      # level_index -> last-logged skip reason
        "yield_earned_usd": 0.0,                # simulated USDC yield on idle cash (if enabled)
        "_yield_last_accrual_at": None,
        "_price_hist": [],                      # rolling [iso, price] window for breakout_buy
        "breakout_cooldown_until": None,        # set after a breakeven-stop exit
    }


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return default_state()
    with open(STATE_PATH, encoding="utf-8-sig") as fh:
        return json.load(fh)


def save_state(state: dict) -> None:
    state["last_iteration_at"] = iso(now_utc())
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------- #
# price feed
# --------------------------------------------------------------------------- #
def get_price(cfg: dict) -> float:
    url = cfg["price_feed"]["url"]
    req = Request(url, headers={"User-Agent": "eth-grid-bot/phase1"})
    with urlopen(req, timeout=cfg["price_feed"]["timeout_sec"]) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    # Coinbase v2 spot: {"data": {"amount": "1234.56", ...}}
    return float(data["data"]["amount"])


def load_market_data(cfg: dict):
    """Trailing daily closes for the adaptive features. None on total failure --
    callers must degrade gracefully. Monkeypatched by simulate.py for offline runs."""
    if not _adaptations_need_market_data(cfg):
        return None
    try:
        import market_data
        return market_data.load(cfg)
    except Exception as exc:  # noqa: BLE001
        log_event(f"market data unavailable ({exc!r}); adaptive features idle", level="WARN")
        return None


# --------------------------------------------------------------------------- #
# adaptive features -- grid geometry, trend filter, time-stop
#   all controlled by config["adaptations"]; absent/false == classic behaviour
# --------------------------------------------------------------------------- #
def _adapt(cfg: dict) -> dict:
    return cfg.get("adaptations", {}) or {}


def _adaptations_need_market_data(cfg: dict) -> bool:
    a = _adapt(cfg)
    return bool(a.get("trend_filter_enabled") or a.get("vol_spacing_enabled"))


def _sma(mkt, n: int):
    if not mkt:
        return None
    import market_data
    return market_data.sma(mkt.get("closes", []), n)


def _daily_vol_pct(mkt, n: int):
    if not mkt:
        return None
    import market_data
    return market_data.daily_vol_pct(mkt.get("closes", []), n)


def spacing_mult_from_vol(cfg: dict, mkt) -> float:
    """Multiplier applied to grid spacing (and optionally take-profit). 1.0 when
    disabled or data missing."""
    a = _adapt(cfg)
    if not a.get("vol_spacing_enabled") or not mkt:
        return 1.0
    vol = _daily_vol_pct(mkt, int(a.get("vol_lookback_days", 20)))
    ref = float(a.get("vol_reference_daily_pct", 3.5))
    if not vol or ref <= 0:
        return 1.0
    raw = vol / ref
    lo = float(a.get("vol_mult_min", 0.6))
    hi = float(a.get("vol_mult_max", 1.8))
    return max(lo, min(hi, raw))


def effective_spacing_pct(state: dict, cfg: dict) -> float:
    return cfg["grid"]["grid_spacing_pct"] * float(state.get("spacing_mult", 1.0))


def effective_tp_pct(state: dict, cfg: dict) -> float:
    a = _adapt(cfg)
    if a.get("vol_spacing_enabled") and a.get("vol_scales_take_profit"):
        return cfg["grid"]["take_profit_pct"] * float(state.get("spacing_mult", 1.0))
    return cfg["grid"]["take_profit_pct"]


def build_levels(anchor: float, spacing_pct: float, n: int) -> list[dict]:
    levels, p, s = [], anchor, spacing_pct / 100.0
    for i in range(n):
        p = p * (1 - s)
        # primed = has price been above this rung since it was (re)armed? A
        # resting maker buy can only sit here once the market is above it.
        levels.append({"index": i, "price": round(p, 4), "held": False, "primed": False})
    return levels


# --------------------------------------------------------------------------- #
# fill modelling
# --------------------------------------------------------------------------- #
def fee_rate(cfg: dict, kind: str) -> float:
    f = cfg["fees"]
    base = float(f.get("fee_rate_per_side", 0.006))
    if kind == "taker":
        return float(f.get("fee_rate_taker", base * 2))
    return float(f.get("fee_rate_maker", base))


def _fill_rng(state: dict, cfg: dict) -> "random.Random":
    import random
    n = int(state.get("_fill_calls", 0))
    state["_fill_calls"] = n + 1
    return random.Random(f"{int(cfg['execution'].get('fill_seed', 0))}:{n}")


def resting_order_fills(state: dict, cfg: dict, order_price: float,
                        band_extreme: float, side: str) -> bool:
    """Did a resting limit at order_price fill this poll?  band_extreme is the
    favourable end of [prev_price, price]: the LOW for a buy, the HIGH for a sell.
    A decisive move through fills for sure; a shallow touch fills with a
    probability that stands in for queue position."""
    if side == "buy":
        depth = (order_price - band_extreme) / order_price
    else:
        depth = (band_extreme - order_price) / order_price
    if depth < 0:
        return False                       # band never reached the order
    shallow = float(cfg["execution"].get("shallow_touch_pct", 0.15)) / 100.0
    if depth >= shallow:
        return True
    prob = float(cfg["execution"].get("shallow_fill_prob", 0.5))
    return _fill_rng(state, cfg).random() < prob


def level_in_cooldown(level: dict) -> bool:
    cd = level.get("cooldown_until")
    if not cd:
        return False
    try:
        return now_utc() < datetime.fromisoformat(cd)
    except ValueError:
        return False


def level_buyable(level: dict, price: float) -> bool:
    """at_level / at_poll models: buy when not held, price at/below the rung, not
    in a max-hold cooldown. (The 'resting' model has its own logic.)"""
    return not level["held"] and price <= level["price"] and not level_in_cooldown(level)


def trend_status(state: dict, cfg: dict, price: float, mkt) -> tuple[bool, float | None]:
    """(buying_allowed, ma). Symmetric dead zone around the MA: turn OFF only
    below MA*(1-buffer), turn ON only above MA*(1+buffer); hold state in between."""
    a = _adapt(cfg)
    if not a.get("trend_filter_enabled") or not mkt:
        return True, None
    ma = _sma(mkt, int(a.get("trend_ma_days", 20)))
    if not ma:
        return True, None
    sym = a.get("trend_buffer_pct")
    pause_buf = float(sym if sym is not None else a.get("trend_pause_buffer_pct", 3.0)) / 100.0
    resume_buf = float(sym if sym is not None else
                       a.get("trend_resume_buffer_pct", 1.0)) / 100.0
    if state.get("trend_ok", True):
        return price >= ma * (1 - pause_buf), ma    # pause only in a real downtrend
    return price >= ma * (1 + resume_buf), ma       # resume once clearly back above


# --------------------------------------------------------------------------- #
# order seam -- Phase 1 stubs. A real adapter replaces the bodies below.
# --------------------------------------------------------------------------- #
def preview_order(cfg, side: str, price: float, size_usd: float, client_order_id: str,
                  fee_rate_override: float | None = None) -> dict:
    """Return an estimated fill. Phase 1: pure calculation, no network call."""
    rate = fee_rate_override if fee_rate_override is not None else cfg["fees"]["fee_rate_per_side"]
    fee_usd = round(size_usd * rate, 6)
    if side == "buy":
        qty_eth = (size_usd - fee_usd) / price
    else:
        qty_eth = size_usd / price  # size_usd here is the tranche's ETH qty * price
    preview = {
        "client_order_id": client_order_id,
        "side": side,
        "limit_price": round(price, 2),
        "est_fill_price": round(price, 2),
        "est_size_usd": round(size_usd, 6),
        "est_qty_eth": round(qty_eth, 8),
        "est_fee_usd": fee_usd,
        "mode": cfg["execution"]["mode"],
    }
    if cfg["execution"].get("log_previews", True):
        # spec: log the estimated fill/fees before every order. One concise line.
        log_event(f"preview {side} {client_order_id}: "
                  f"~{preview['est_qty_eth']} {asset_symbol(cfg)} @ {preview['est_fill_price']}  "
                  f"fee ~${fee_usd}")
    return preview


def place_order(cfg, preview: dict) -> dict:
    """Phase 1: 'fill' the preview exactly. Live adapter would submit + poll here."""
    if cfg["execution"]["mode"] != "dry_run":
        raise RuntimeError(
            "Live mode is not implemented in this Phase 1 build. "
            "This bot only simulates fills."
        )
    fill = dict(preview)
    fill["status"] = "filled_simulated"
    fill["filled_at"] = iso(now_utc())
    return fill


# --------------------------------------------------------------------------- #
# logging of trades / daily summary
# --------------------------------------------------------------------------- #
TRADE_FIELDS = [
    "timestamp", "mode", "action", "level_index", "client_order_id",
    "price", "size_usd", "qty_eth", "fee_usd", "realized_pnl_delta_usd",
    "realized_pnl_total_usd", "note",
]


def _csv_needs_header(path: str) -> bool:
    return not os.path.exists(path) or os.path.getsize(path) == 0


def log_trade(row: dict) -> None:
    os.makedirs(os.path.dirname(TRADES_CSV), exist_ok=True)
    new = _csv_needs_header(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in TRADE_FIELDS})


DAILY_FIELDS = [
    "date", "last_price", "open_tranches", "deployed_usd",
    "realized_pnl_day_usd", "unrealized_pnl_usd", "fees_paid_total_usd",
    "buys_today", "sells_today", "halted", "paused", "halt_reason",
    "trend_ok", "spacing_mult", "yield_earned_usd",
]


def compute_unrealized(state: dict, price: float, cfg: dict) -> float:
    exit_rate = fee_rate(cfg, "maker")     # take-profit exits are maker limit orders
    total = 0.0
    for t in state["open_tranches"]:
        gross = t["qty_eth"] * price
        total += (gross - gross * exit_rate) - t["cost_usd"]
    return round(total, 4)


def deployed_usd(state: dict) -> float:
    return round(sum(t["cost_usd"] for t in state["open_tranches"]), 4)


def daily_summary_row(state: dict, price: float, cfg: dict) -> dict:
    return {
        "date": state["day"]["date"],
        "last_price": round(price, 2),
        "open_tranches": len(state["open_tranches"]),
        "deployed_usd": deployed_usd(state),
        "realized_pnl_day_usd": round(state["day"]["realized_pnl_usd"], 4),
        "unrealized_pnl_usd": compute_unrealized(state, price, cfg),
        "fees_paid_total_usd": round(state["fees_paid_usd"], 4),
        "buys_today": state["day"]["buy_count"],
        "sells_today": state["day"]["sell_count"],
        "halted": state["halted"],
        "paused": state["paused"],
        "halt_reason": state["halt_reason"] or "",
        "trend_ok": state.get("trend_ok", True),
        "spacing_mult": round(float(state.get("spacing_mult", 1.0)), 3),
        "yield_earned_usd": round(float(state.get("yield_earned_usd", 0.0)), 4),
    }


def print_summary(state: dict, price: float, cfg: dict) -> None:
    r = daily_summary_row(state, price, cfg)
    print("\n=== DAILY SUMMARY " + r["date"] + " (UTC) ===")
    for k in DAILY_FIELDS:
        print(f"  {k:24s}: {r[k]}")
    tot = r["realized_pnl_day_usd"] + r["unrealized_pnl_usd"]
    print(f"  {'total_pnl_day_usd':24s}: {round(tot, 4)}  "
          f"({round(100 * tot / cfg['allocated_capital_usd'], 2)}% of allocated)")
    if r["yield_earned_usd"]:
        grand = state["realized_pnl_usd"] + r["unrealized_pnl_usd"] + r["yield_earned_usd"]
        print(f"  {'idle_yield_total_usd':24s}: {r['yield_earned_usd']}")
        print(f"  {'grand_total_incl_yield':24s}: {round(grand, 4)}  "
              f"({round(100 * grand / cfg['allocated_capital_usd'], 2)}% of allocated)")
    print("=" * 40 + "\n")


def append_daily_summary(state: dict, price: float, cfg: dict) -> None:
    os.makedirs(os.path.dirname(DAILY_CSV), exist_ok=True)
    # If an older file has a different column set, retire it so the CSV stays
    # aligned (a schema change added trend_ok / spacing_mult).
    if os.path.exists(DAILY_CSV):
        with open(DAILY_CSV, encoding="utf-8-sig") as fh:
            header = fh.readline().strip()
        if header and header != ",".join(DAILY_FIELDS):
            bak = DAILY_CSV + ".bak"
            os.replace(DAILY_CSV, bak)
            log_event(f"daily_summary.csv schema changed; old file moved to {os.path.basename(bak)}")
    new = _csv_needs_header(DAILY_CSV)
    with open(DAILY_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=DAILY_FIELDS)
        if new:
            w.writeheader()
        w.writerow(daily_summary_row(state, price, cfg))


def notify_daily_summary(state: dict, price: float, cfg: dict) -> None:
    r = daily_summary_row(state, price, cfg)
    tot = r["realized_pnl_day_usd"] + r["unrealized_pnl_usd"]
    flags = [f for f, on in (("HALTED", r["halted"]), ("PAUSED", r["paused"])) if on]
    discord_send(
        f"📊 Daily summary {r['date']} (UTC)  ({cfg['execution']['mode']})",
        description=" ".join(flags) if flags else "",
        fields=[
            (f"{asset_symbol(cfg)} price", f"${r['last_price']:,}"),
            ("Open tranches", str(r["open_tranches"])),
            ("Deployed", f"${r['deployed_usd']}"),
            ("Realized (day)", f"${r['realized_pnl_day_usd']}"),
            ("Unrealized", f"${r['unrealized_pnl_usd']}"),
            ("Total P&L (day)", f"${round(tot, 4)}  "
             f"({round(100 * tot / cfg['allocated_capital_usd'], 2)}%)"),
            ("Fees paid (all-time)", f"${r['fees_paid_total_usd']}"),
            ("Buys / Sells today", f"{r['buys_today']} / {r['sells_today']}"),
        ] + ([("Idle yield (all-time)", f"${r['yield_earned_usd']}")]
             if r["yield_earned_usd"] else []),
        color=COLOR_ALERT if flags else COLOR_INFO,
        event="alert" if flags else "daily",
    )


# --------------------------------------------------------------------------- #
# grid setup
# --------------------------------------------------------------------------- #
def ensure_or_reshape_grid(state: dict, cfg: dict, price: float, mkt=None) -> None:
    """First run: build the grid. Later: re-anchor / re-space it, but ONLY when
    no tranches are open (reshaping while holding orphans their take-profits)."""
    a = _adapt(cfg)
    n = cfg["grid"]["num_levels"]
    if mkt and mkt.get("as_of"):
        state["market_as_of"] = mkt["as_of"]

    if state["anchor_price"] is None:                       # ---- first init ----
        anchor = cfg["grid"]["anchor_price"] or price
        state["spacing_mult"] = spacing_mult_from_vol(cfg, mkt)
        state["anchor_price"] = round(anchor, 4)
        state["grid_levels"] = build_levels(anchor, effective_spacing_pct(state, cfg), n)
        log_event(
            f"Grid initialised. anchor={state['anchor_price']} "
            f"spacing={effective_spacing_pct(state, cfg):.3f}% (vol mult {state['spacing_mult']:.2f}) "
            f"levels={[l['price'] for l in state['grid_levels']]}"
        )
        return

    if state["open_tranches"]:                              # never reshape while holding
        return

    new_anchor = state["anchor_price"]
    reasons: list[str] = []

    if a.get("reanchor_enabled") and not state.get("halted"):
        breakout = float(a.get("reanchor_breakout_pct", 8.0)) / 100.0
        if breakout > 0 and price >= state["anchor_price"] * (1 + breakout):
            new_anchor = price
            reasons.append(f"breakout +{breakout * 100:.0f}%")

    if (a.get("reanchor_after_stopout") and state.get("reanchor_pending_after_stopout")
            and not state.get("halted")):
        new_anchor = price
        state["reanchor_pending_after_stopout"] = False
        reasons.append("post-stopout")

    new_mult = spacing_mult_from_vol(cfg, mkt)
    old_mult = float(state.get("spacing_mult", 1.0))
    mult_changed = abs(new_mult - old_mult) / max(old_mult, 1e-9) > 0.10

    # rate-limit vol-only reshapes (breakout / stopout are immediate)
    if mult_changed and not reasons:
        min_h = float(a.get("min_reshape_hours", 6))
        last = state.get("last_reshape_at")
        if last:
            try:
                age_h = (now_utc() - datetime.fromisoformat(last)).total_seconds() / 3600
                if age_h < min_h:
                    mult_changed = False
            except ValueError:
                pass

    if reasons or mult_changed:
        state["anchor_price"] = round(new_anchor, 4)
        state["spacing_mult"] = new_mult
        state["grid_levels"] = build_levels(new_anchor, effective_spacing_pct(state, cfg), n)
        state["last_reshape_at"] = iso(now_utc())
        msg = (f"Grid reshaped [{', '.join(reasons) or 'volatility'}]. "
               f"anchor={state['anchor_price']} "
               f"spacing={effective_spacing_pct(state, cfg):.3f}% "
               f"(vol mult {old_mult:.2f}->{new_mult:.2f}) "
               f"levels={[l['price'] for l in state['grid_levels']]}")
        log_event(msg)
        discord_send("♻️ Grid reshaped", msg, color=COLOR_INFO, event="reshape")


def update_trend_filter(state: dict, cfg: dict, price: float, mkt) -> None:
    """Refresh state['trend_ok']; log + notify on a transition."""
    if not _adapt(cfg).get("trend_filter_enabled"):
        state["trend_ok"] = True
        return
    ok, ma = trend_status(state, cfg, price, mkt)
    if ma is None:
        return  # filter enabled but no MA yet -> hold current verdict, don't flip
    if ok == state.get("trend_ok", True):
        return
    state["trend_ok"] = ok
    ma_days = int(_adapt(cfg).get("trend_ma_days", 20))
    msg = (f"Trend filter: new buys {'ENABLED' if ok else 'PAUSED'} "
           f"(price {price:.2f} vs MA{ma_days} {ma:.2f})" if ma else
           f"Trend filter: new buys {'ENABLED' if ok else 'PAUSED'}")
    log_event(msg)
    discord_send(("▶️ " if ok else "⏸️ ") + "Trend filter", msg,
                 color=COLOR_INFO if ok else COLOR_ALERT, event="alert")


def accrue_idle_yield(state: dict, cfg: dict) -> None:
    """Simulate USDC rewards on the undeployed capital. Modelling only -- no
    trade is placed. Off unless config.idle_yield.enabled."""
    y = cfg.get("idle_yield", {}) or {}
    now = now_utc()
    last = state.get("_yield_last_accrual_at")
    state["_yield_last_accrual_at"] = iso(now)
    if not y.get("enabled"):
        return
    if not last:
        return  # first tick just sets the clock
    try:
        elapsed_s = (now - datetime.fromisoformat(last)).total_seconds()
    except ValueError:
        return
    if elapsed_s <= 0:
        return
    free_cash = cfg["allocated_capital_usd"] + state["realized_pnl_usd"] - deployed_usd(state)
    if free_cash <= 0:
        return
    apy = float(y.get("apy_pct", 4.5)) / 100.0
    earned = free_cash * apy * (elapsed_s / (365.0 * 86400.0))
    state["yield_earned_usd"] = round(state.get("yield_earned_usd", 0.0) + earned, 8)


def check_max_hold(state: dict, cfg: dict, price: float) -> None:
    """Force-close tranches held longer than adaptations.max_hold_days."""
    a = _adapt(cfg)
    if not a.get("max_hold_enabled"):
        return
    max_days = float(a.get("max_hold_days", 10))
    only_underwater = bool(a.get("max_hold_only_if_underwater", True))
    for tranche in list(state["open_tranches"]):
        try:
            opened = datetime.fromisoformat(tranche["opened_at"])
        except (ValueError, KeyError):
            continue
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        age_days = (now_utc() - opened).total_seconds() / 86400.0
        if age_days < max_days:
            continue
        if only_underwater and price >= tranche["fill_price"]:
            continue
        slip = float(cfg["execution"].get("market_slippage_bps", 0)) / 10000.0
        exit_px = price * (1 - slip)          # market sell -> you receive less
        close_tranche(state, cfg, tranche, price, exit_price=exit_px,
                      reason=f"max_hold {age_days:.1f}d", fill_kind="taker")


# --------------------------------------------------------------------------- #
# breakout-buy leg -- buys strength instead of dips, stops at breakeven
# --------------------------------------------------------------------------- #
def _record_price_history(state: dict, price: float, hours: float) -> None:
    """Rolling window of recent prices, used to find the breakout reference low.
    Bounded by `hours`, so state.json growth stays modest."""
    hist = state.setdefault("_price_hist", [])
    hist.append([iso(now_utc()), price])
    cutoff = now_utc() - timedelta(hours=hours)
    state["_price_hist"] = [
        [t, p] for t, p in hist if datetime.fromisoformat(t) >= cutoff
    ][-2000:]  # hard cap regardless of `hours`, in case of a config mistake


def _breakout_reference_low(state: dict, price: float) -> float:
    hist = state.get("_price_hist", [])
    return min([p for _, p in hist] + [price]) if hist else price


def breakout_cooldown_active(state: dict) -> bool:
    cd = state.get("breakout_cooldown_until")
    if not cd:
        return False
    try:
        return now_utc() < datetime.fromisoformat(cd)
    except ValueError:
        return False


def check_breakout_buy(state: dict, cfg: dict, price: float, hi: float) -> None:
    """Buy strength: price up `breakout_buy_pct` from its recent low. Exits are
    handled in process_fills -- the normal take-profit on the way up, or a
    breakeven stop (sell at entry) if it fails and gives the move back. Shares
    the portfolio capital/day-trade budget with the dip rungs; independent of
    the trend filter (a breakout is the opposite signal from a dip-buy)."""
    a = _adapt(cfg)
    if not a.get("breakout_buy_enabled"):
        return
    if any(t.get("entry_kind") == "breakout" for t in state["open_tranches"]):
        return  # only one breakout position open at a time
    if breakout_cooldown_active(state):
        return

    size_usd = float(a.get("breakout_tranche_size_usd") or cfg["grid"]["tranche_size_usd"])
    ok, reason = _budget_ok(state, cfg, size_usd)
    if not ok:
        skips = state.setdefault("_last_skips", {})
        if skips.get("breakout") != reason:
            skips["breakout"] = reason
            log_event(f"Breakout buy skipped: {reason}")
        return
    state.setdefault("_last_skips", {}).pop("breakout", None)

    ref_low = _breakout_reference_low(state, price)
    pct = float(a.get("breakout_buy_pct", 8.0)) / 100.0
    trigger_price = ref_low * (1 + pct)
    if hi < trigger_price:
        return  # hasn't risen far enough yet

    # a buy-the-breakout order is a taker/market fill triggered on the way up,
    # not a resting maker limit -- you can't rest a buy above the market.
    slip = float(cfg["execution"].get("market_slippage_bps", 0)) / 10000.0
    fill_price = round(trigger_price * (1 + slip), 4)
    tp_pct = effective_tp_pct(state, cfg)
    coid = f"grid-buy-breakout-{uuid.uuid4().hex[:12]}"

    preview = preview_order(cfg, "buy", fill_price, size_usd, coid,
                            fee_rate_override=fee_rate(cfg, "taker"))
    fill = place_order(cfg, preview)

    tranche = {
        "id": coid,
        "level_index": "breakout",
        "entry_kind": "breakout",
        "fill_price": fill["est_fill_price"],
        "fill_kind": "taker",
        "qty_eth": fill["est_qty_eth"],
        "cost_usd": round(size_usd, 6),
        "buy_fee_usd": fill["est_fee_usd"],
        "take_profit_pct": round(tp_pct, 4),
        "take_profit_price": round(fill["est_fill_price"] * (1 + tp_pct / 100.0), 4),
        "breakeven_stop_price": fill["est_fill_price"],
        "opened_at": fill["filled_at"],
    }
    state["open_tranches"].append(tranche)
    state["fees_paid_usd"] += fill["est_fee_usd"]
    state["day"]["buy_count"] += 1

    log_trade({
        "timestamp": fill["filled_at"], "mode": cfg["execution"]["mode"], "action": "BUY",
        "level_index": "breakout", "client_order_id": coid,
        "price": fill["est_fill_price"], "size_usd": size_usd,
        "qty_eth": fill["est_qty_eth"], "fee_usd": fill["est_fee_usd"],
        "realized_pnl_delta_usd": 0.0,
        "realized_pnl_total_usd": round(state["realized_pnl_usd"], 4),
        "note": f"breakout ref_low={round(ref_low, 2)} take_profit_at={tranche['take_profit_price']} "
                f"stop={tranche['breakeven_stop_price']} taker",
    })
    log_event(
        f"BUY  breakout @ {fill['est_fill_price']} (taker)  qty={fill['est_qty_eth']}  "
        f"fee={fill['est_fee_usd']}  ref_low={round(ref_low, 2)}  "
        f"TP={tranche['take_profit_price']}  stop={tranche['breakeven_stop_price']}  "
        f"deployed=${deployed_usd(state)}"
    )
    discord_send(
        f"🟨 BUY  breakout entry  ({cfg['execution']['mode']})",
        fields=[
            ("Fill price", f"${fill['est_fill_price']:,}"),
            ("Reference low", f"${round(ref_low, 2):,}"),
            ("Size", f"{fill['est_qty_eth']} {asset_symbol(cfg)}  (${size_usd})"),
            ("Fee", f"${fill['est_fee_usd']}"),
            ("Take-profit at", f"${tranche['take_profit_price']:,}"),
            ("Breakeven stop at", f"${tranche['breakeven_stop_price']:,}"),
            ("Deployed", f"${deployed_usd(state)} / ${cfg['risk']['max_capital_deployed_usd']}"),
        ],
        color=COLOR_BUY, event="buy",
    )


# --------------------------------------------------------------------------- #
# daily rollover + risk rails
# --------------------------------------------------------------------------- #
def daily_rollover(state: dict, cfg: dict, price: float) -> None:
    d = today_str()
    if state["day"]["date"] == d:
        return
    # Emit the summary for the day that just ENDED (state["day"] still holds it),
    # then reset the per-day counters.
    log_event(
        f"Day rollover {state['day']['date']} -> {d}. "
        f"buys={state['day']['buy_count']} sells={state['day']['sell_count']} "
        f"realized={round(state['day']['realized_pnl_usd'], 4)}"
    )
    append_daily_summary(state, price, cfg)
    print_summary(state, price, cfg)
    notify_daily_summary(state, price, cfg)
    state["day"] = {"date": d, "buy_count": 0, "sell_count": 0, "realized_pnl_usd": 0.0}
    # A new day lifts the daily-loss pause but NOT the hard stop-loss halt.
    if state["paused"]:
        state["paused"] = False
        log_event("Daily-loss pause cleared by day rollover.")
        discord_send("▶️ Daily-loss pause cleared by UTC day rollover — trading resumes.",
                     color=COLOR_INFO, event="alert")


def check_risk_rails(state: dict, price: float, cfg: dict) -> None:
    risk = cfg["risk"]
    alloc = cfg["allocated_capital_usd"]

    unreal = compute_unrealized(state, price, cfg)
    total_pnl = state["realized_pnl_usd"] + unreal
    drawdown_pct = -100.0 * total_pnl / alloc  # positive number == a loss

    if not state["halted"] and drawdown_pct > risk["hard_stop_loss_pct"]:
        state["halted"] = True
        state["halt_reason"] = "HARD_STOP_LOSS"
        if _adapt(cfg).get("reanchor_after_stopout"):
            state["reanchor_pending_after_stopout"] = True
        alert(
            f"HARD STOP-LOSS hit: total drawdown {drawdown_pct:.1f}% > "
            f"{risk['hard_stop_loss_pct']}% of allocated. Halting all BUYING. "
            f"Open tranches kept; take-profit sells still allowed. Manual review required."
        )

    day_pnl = state["day"]["realized_pnl_usd"] + unreal
    day_loss_pct = -100.0 * day_pnl / alloc
    if not state["paused"] and day_loss_pct > risk["max_daily_loss_pct"]:
        state["paused"] = True
        alert(
            f"MAX DAILY LOSS hit: day P&L {day_loss_pct:.1f}% loss > "
            f"{risk['max_daily_loss_pct']}%. Pausing agent (no buys, no sells) "
            f"until manual review. Clear 'paused' in state.json to resume."
        )


def _budget_ok(state: dict, cfg: dict, size_usd: float) -> tuple[bool, str]:
    """Checks shared by every new-buy path (dip rungs and the breakout leg):
    the portfolio-wide halts and the capital / daily-trade caps."""
    if state["halted"]:
        return False, "halted (hard stop-loss)"
    if state["paused"]:
        return False, "paused (max daily loss)"
    if state["day"]["buy_count"] >= cfg["risk"]["max_trades_per_day"]:
        return False, "max buys/day reached"
    if deployed_usd(state) + size_usd > cfg["risk"]["max_capital_deployed_usd"] + 1e-9:
        return False, "would exceed max capital deployed"
    return True, ""


def can_open_new_tranche(state: dict, cfg: dict) -> tuple[bool, str]:
    ok, reason = _budget_ok(state, cfg, cfg["grid"]["tranche_size_usd"])
    if not ok:
        return False, reason
    if _adapt(cfg).get("trend_filter_enabled") and not state.get("trend_ok", True):
        return False, "trend filter (price below MA)"
    dip_tranches = sum(1 for t in state["open_tranches"] if t.get("entry_kind", "dip") == "dip")
    if dip_tranches >= cfg["grid"]["num_levels"]:
        return False, "all grid levels held"
    return True, ""


# --------------------------------------------------------------------------- #
# core actions
# --------------------------------------------------------------------------- #
def open_tranche(state: dict, cfg: dict, level: dict, price: float,
                 *, fill_price: float | None = None, fill_kind: str = "maker") -> None:
    ok, reason = can_open_new_tranche(state, cfg)
    if not ok:
        # log a skip only when the reason for this level changes -- otherwise a
        # weeks-long trend pause or a full grid would write a line every poll.
        key = str(level["index"])
        skips = state.setdefault("_last_skips", {})
        if skips.get(key) != reason:
            skips[key] = reason
            if "trend filter" not in reason:   # trend transitions are logged separately
                log_event(f"BUY skipped at level {level['index']} ({level['price']}): {reason}")
        return
    state.setdefault("_last_skips", {}).pop(str(level["index"]), None)

    if fill_price is None:
        fill_price = level["price"] if cfg["execution"]["fill_model"] == "at_level" else price
    size_usd = cfg["grid"]["tranche_size_usd"]
    tp_pct = effective_tp_pct(state, cfg)
    coid = f"grid-buy-L{level['index']}-{uuid.uuid4().hex[:12]}"

    preview = preview_order(cfg, "buy", fill_price, size_usd, coid,
                            fee_rate_override=fee_rate(cfg, fill_kind))
    fill = place_order(cfg, preview)

    tranche = {
        "id": coid,
        "level_index": level["index"],
        "entry_kind": "dip",
        "fill_price": fill["est_fill_price"],
        "fill_kind": fill_kind,
        "qty_eth": fill["est_qty_eth"],
        "cost_usd": round(size_usd, 6),          # includes buy fee
        "buy_fee_usd": fill["est_fee_usd"],
        "take_profit_pct": round(tp_pct, 4),
        "take_profit_price": round(fill["est_fill_price"] * (1 + tp_pct / 100.0), 4),
        "opened_at": fill["filled_at"],
    }
    state["open_tranches"].append(tranche)
    level["held"] = True
    state["fees_paid_usd"] += fill["est_fee_usd"]
    state["day"]["buy_count"] += 1

    log_trade({
        "timestamp": fill["filled_at"], "mode": cfg["execution"]["mode"], "action": "BUY",
        "level_index": level["index"], "client_order_id": coid,
        "price": fill["est_fill_price"], "size_usd": size_usd,
        "qty_eth": fill["est_qty_eth"], "fee_usd": fill["est_fee_usd"],
        "realized_pnl_delta_usd": 0.0,
        "realized_pnl_total_usd": round(state["realized_pnl_usd"], 4),
        "note": f"take_profit_at={tranche['take_profit_price']} {fill_kind}",
    })
    log_event(
        f"BUY  L{level['index']} @ {fill['est_fill_price']} ({fill_kind})  "
        f"qty={fill['est_qty_eth']}  fee={fill['est_fee_usd']}  "
        f"TP={tranche['take_profit_price']}  deployed=${deployed_usd(state)}"
    )
    discord_send(
        f"🟦 BUY  grid level {level['index']}  ({cfg['execution']['mode']})",
        fields=[
            ("Fill price", f"${fill['est_fill_price']:,}"),
            ("Size", f"{fill['est_qty_eth']} {asset_symbol(cfg)}  (${size_usd})"),
            ("Fee", f"${fill['est_fee_usd']}"),
            ("Take-profit at", f"${tranche['take_profit_price']:,}"),
            ("Deployed", f"${deployed_usd(state)} / ${cfg['risk']['max_capital_deployed_usd']}"),
            ("Open tranches", f"{len(state['open_tranches'])}/{cfg['grid']['num_levels']}"),
        ],
        color=COLOR_BUY,
        event="buy",
    )


def close_tranche(state: dict, cfg: dict, tranche: dict, price: float,
                  *, exit_price: float | None = None, reason: str = "take_profit",
                  fill_kind: str = "maker") -> None:
    if state["paused"]:
        return  # max-daily-loss pause blocks everything
    if exit_price is not None:                       # forced exit (e.g. max-hold)
        sell_price = exit_price
    elif cfg["execution"]["fill_model"] == "at_level":
        sell_price = tranche["take_profit_price"]
    else:
        sell_price = price
    gross = tranche["qty_eth"] * sell_price
    coid = f"grid-sell-L{tranche['level_index']}-{uuid.uuid4().hex[:12]}"

    preview = preview_order(cfg, "sell", gross, gross, coid,
                            fee_rate_override=fee_rate(cfg, fill_kind))
    fill = place_order(cfg, preview)

    net_proceeds = gross - fill["est_fee_usd"]
    pnl_delta = round(net_proceeds - tranche["cost_usd"], 6)

    state["realized_pnl_usd"] = round(state["realized_pnl_usd"] + pnl_delta, 6)
    state["day"]["realized_pnl_usd"] = round(state["day"]["realized_pnl_usd"] + pnl_delta, 6)
    state["fees_paid_usd"] += fill["est_fee_usd"]
    state["day"]["sell_count"] += 1

    is_max_hold = reason.startswith("max_hold")
    for lvl in state["grid_levels"]:
        if lvl["index"] == tranche["level_index"]:
            lvl["held"] = False  # reset condition: level becomes buyable again
            if is_max_hold:
                # don't let a time-stopped level immediately rebuy the same dip
                cd_h = float(_adapt(cfg).get("max_hold_cooldown_hours", 48))
                lvl["cooldown_until"] = iso(now_utc() + timedelta(hours=cd_h))
            else:
                lvl.pop("cooldown_until", None)
    if reason == "breakeven_stop":
        # a failed breakout: don't immediately re-chase the same level
        cd_h = float(_adapt(cfg).get("breakout_cooldown_hours", 24))
        state["breakout_cooldown_until"] = iso(now_utc() + timedelta(hours=cd_h))
    state["open_tranches"] = [t for t in state["open_tranches"] if t["id"] != tranche["id"]]
    state["closed_tranches"].append({
        **tranche, "sell_price": round(sell_price, 4), "sell_fill_kind": fill_kind,
        "sell_fee_usd": fill["est_fee_usd"], "pnl_usd": pnl_delta,
        "closed_at": fill["filled_at"], "close_reason": reason,
    })
    state["closed_tranches"] = state["closed_tranches"][-250:]  # cap state.json growth

    log_trade({
        "timestamp": fill["filled_at"], "mode": cfg["execution"]["mode"], "action": "SELL",
        "level_index": tranche["level_index"], "client_order_id": coid,
        "price": round(sell_price, 4), "size_usd": round(gross, 6),
        "qty_eth": tranche["qty_eth"], "fee_usd": fill["est_fee_usd"],
        "realized_pnl_delta_usd": pnl_delta,
        "realized_pnl_total_usd": round(state["realized_pnl_usd"], 4),
        "note": f"bought_at={tranche['fill_price']} reason={reason} {fill_kind}",
    })
    tag = "" if reason == "take_profit" else f"  [{reason}]"
    log_event(
        f"SELL L{tranche['level_index']} @ {round(sell_price, 4)}  "
        f"pnl={pnl_delta:+.4f}  realized_total={round(state['realized_pnl_usd'], 4)}  "
        f"level re-armed{tag}"
    )
    discord_send(
        f"{'🟩' if pnl_delta >= 0 else '🟧'} SELL  grid level {tranche['level_index']}"
        f"{tag}  ({cfg['execution']['mode']})",
        fields=[
            ("Sell price", f"${round(sell_price, 2):,}"),
            ("Bought at", f"${tranche['fill_price']:,}"),
            ("Reason", reason),
            ("P&L this tranche", f"{pnl_delta:+.4f} USD"),
            ("Fee", f"${fill['est_fee_usd']}"),
            ("Realized total", f"${round(state['realized_pnl_usd'], 4)}"),
            ("Open tranches", f"{len(state['open_tranches'])}/{cfg['grid']['num_levels']}"),
        ],
        color=COLOR_SELL_WIN if pnl_delta >= 0 else COLOR_SELL_LOSS,
        event="sell",
    )


# --------------------------------------------------------------------------- #
# one iteration
# --------------------------------------------------------------------------- #
def iterate(state: dict, cfg: dict, *, price_band: tuple[float, float] | None = None) -> None:
    """One poll. `price_band` (low, high) is for backtesting from OHLC candles --
    the resting fill model uses it instead of the poll-to-poll band."""
    try:
        price = get_price(cfg)
    except Exception as exc:  # noqa: BLE001
        alert(f"PRICE FEED FAILURE: {exc!r}")
        return

    state["last_price"] = round(price, 2)
    mkt = load_market_data(cfg)                       # cached hourly; None on failure
    accrue_idle_yield(state, cfg)                     # modelling only; no trade
    daily_rollover(state, cfg, price)
    ensure_or_reshape_grid(state, cfg, price, mkt)    # first-init or re-anchor/re-space (only when flat)
    check_risk_rails(state, price, cfg)
    update_trend_filter(state, cfg, price, mkt)       # refresh state["trend_ok"]

    process_fills(state, cfg, price, band=price_band)

    state["prev_price"] = round(price, 6)
    save_state(state)


def process_fills(state: dict, cfg: dict, price: float,
                  *, band: tuple[float, float] | None = None) -> None:
    """Sells (take-profit), then time-stops, then buys. The 'resting' fill model
    uses a price band + queue-position probability -- `band` (an OHLC low/high for
    backtests) if given, else the poll-to-poll [prev_price, price] range. The
    legacy 'at_level' / 'at_poll' models use simple point checks."""
    model = cfg["execution"].get("fill_model", "resting")

    if model == "resting":
        if band is not None:
            lo, hi = min(band), max(band)
        else:
            prev = state.get("prev_price")
            lo, hi = (min(prev, price), max(prev, price)) if prev is not None else (price, price)

        # a rung is "primed" once the market has been above it since it was armed
        for lvl in state["grid_levels"]:
            if price > lvl["price"]:
                lvl["primed"] = True

        # 1) take-profit sells (resting limit at tp_price; fills if the band's high reached it)
        for tranche in list(state["open_tranches"]):
            tp = tranche["take_profit_price"]
            if hi >= tp and resting_order_fills(state, cfg, tp, hi, "sell"):
                close_tranche(state, cfg, tranche, price, exit_price=tp,
                              reason="take_profit", fill_kind="maker")

        # 1b) breakeven stop on breakout tranches -- a stop order, not a resting
        # limit: triggers for certain once the band reaches it (taker + slippage).
        for tranche in list(state["open_tranches"]):
            if tranche.get("entry_kind") != "breakout":
                continue
            stop = tranche["breakeven_stop_price"]
            if lo <= stop:
                slip = float(cfg["execution"].get("market_slippage_bps", 0)) / 10000.0
                exit_px = min(price, stop) * (1 - slip)
                close_tranche(state, cfg, tranche, price, exit_price=exit_px,
                              reason="breakeven_stop", fill_kind="taker")

        # 2) time-stops (taker market exits)
        check_max_hold(state, cfg, price)

        # 3) buys at armed + primed rungs whose price the band reached
        for level in state["grid_levels"]:
            if level["held"] or level_in_cooldown(level) or not level.get("primed"):
                continue
            P = level["price"]
            if lo <= P and resting_order_fills(state, cfg, P, lo, "buy"):
                open_tranche(state, cfg, level, price, fill_price=P, fill_kind="maker")

        # 4) breakout leg: buy strength (independent of the dip rungs above)
        lookback_h = float(_adapt(cfg).get("breakout_lookback_hours", 24.0))
        _record_price_history(state, price, lookback_h)
        check_breakout_buy(state, cfg, price, hi)
        return

    # legacy optimistic models -------------------------------------------------
    for tranche in list(state["open_tranches"]):
        if price >= tranche["take_profit_price"]:
            close_tranche(state, cfg, tranche, price)
    check_max_hold(state, cfg, price)
    for level in state["grid_levels"]:
        if level_buyable(level, price):
            open_tranche(state, cfg, level, price)


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="run a single iteration and exit")
    ap.add_argument("--status", action="store_true", help="print state.json and exit")
    ap.add_argument("--summary", action="store_true", help="print today's summary and exit")
    ap.add_argument("--reset", action="store_true", help="wipe state.json")
    ap.add_argument("--test-notify", action="store_true",
                    help="send a test Discord message and exit")
    args = ap.parse_args()

    cfg = load_config()
    configure_notifications(cfg)

    if args.test_notify:
        if not discord_configured():
            print("No Discord webhook configured. Set notifications.discord_webhook_url "
                  "in config.json or the GRID_BOT_DISCORD_WEBHOOK env var.", file=sys.stderr)
            return 2
        discord_send("✅ Test notification",
                     "If you can see this in Discord, the webhook works.",
                     fields=[("mode", cfg["execution"]["mode"]), ("asset", cfg["asset"]),
                             ("mention", _MENTION or "(none)")],
                     color=COLOR_INFO, event="test")
        print("Test message sent"
              + (f" (pinging {_MENTION})" if _should_mention("test") else "")
              + ".")
        return 0

    if cfg["execution"]["mode"] != "dry_run":
        print("REFUSING TO RUN: config execution.mode is not 'dry_run'. "
              "This Phase 1 build only simulates.", file=sys.stderr)
        return 2

    if args.reset:
        if input("Type 'reset' to wipe state.json: ").strip() == "reset":
            if os.path.exists(STATE_PATH):
                os.remove(STATE_PATH)
            print("state.json removed.")
        return 0

    state = load_state()

    if args.status:
        print(json.dumps(state, indent=2))
        return 0

    if args.summary:
        price = state.get("last_price") or get_price(cfg)
        print_summary(state, price, cfg)
        return 0

    log_event(
        f"Starting grid bot (mode={cfg['execution']['mode']}, "
        f"fill_model={cfg['execution']['fill_model']}, "
        f"poll={cfg['execution']['poll_interval_sec']}s). "
        f"THIS BUILD PLACES NO REAL ORDERS."
    )
    discord_send(
        f"▶️ Grid bot started  ({cfg['execution']['mode']})",
        description="Dry run — no real orders." if cfg["execution"]["mode"] == "dry_run" else "",
        fields=[
            ("Asset", cfg["asset"]),
            ("Allocated", f"${cfg['allocated_capital_usd']}"),
            ("Grid", f"{cfg['grid']['num_levels']} levels, "
                     f"{cfg['grid']['grid_spacing_pct']}% apart, "
                     f"${cfg['grid']['tranche_size_usd']}/tranche"),
            ("Take-profit", f"+{cfg['grid']['take_profit_pct']}%"),
            ("Anchor", f"${state['anchor_price']:,}" if state.get("anchor_price") else "sets on first poll"),
        ],
        color=COLOR_INFO,
        event="start",
    )

    if args.once:
        iterate(state, cfg)
        print_summary(state, state.get("last_price") or 0.0, cfg)
        return 0

    # show initial state once; per-day summaries are emitted by daily_rollover()
    if state.get("last_price"):
        print_summary(state, state["last_price"], cfg)

    hb_interval = cfg["execution"].get("heartbeat_interval_sec", 0)
    last_hb = 0.0
    try:
        while True:
            iterate(state, cfg)
            if hb_interval and time.monotonic() - last_hb >= hb_interval:
                heartbeat(state, cfg)
                last_hb = time.monotonic()
            time.sleep(cfg["execution"]["poll_interval_sec"])
    except KeyboardInterrupt:
        log_event("Stopped by user (KeyboardInterrupt).")
        price = state.get("last_price") or 0.0
        append_daily_summary(state, price, cfg)
        print_summary(state, price, cfg)
        discord_send(f"⏹️ Grid bot stopped by user  ({cfg['execution']['mode']})",
                     fields=[("Realized total", f"${round(state['realized_pnl_usd'], 4)}"),
                             ("Open tranches", str(len(state["open_tranches"])))],
                     color=COLOR_INFO, event="stop")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
