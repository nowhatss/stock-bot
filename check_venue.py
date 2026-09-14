#!/usr/bin/env python3
"""
Coinbase Advanced Trade readiness check. Places NO orders.

Verifies, in order:
  1. the API key file loads and authenticates
  2. ETH-USD price + tick sizes are readable
  3. account balances are readable (and, if a portfolio id is set, scoped to it)
  4. a grid-sized limit BUY previews successfully -> shows the REAL maker fee
  5. a grid-sized limit SELL (take-profit) previews successfully

Run:  .venv\\Scripts\\python check_venue.py

Reads the same config.json the bot uses:
  execution.coinbase_key_file   path to the Secret API Key JSON (Trade permission)
  execution.coinbase_portfolio_id   optional; the dedicated portfolio's id
"""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))


def load_cfg() -> dict:
    with open(os.path.join(HERE, "config.json"), encoding="utf-8-sig") as fh:
        return json.load(fh)


def main() -> int:
    cfg = load_cfg()
    ex = cfg["execution"]
    key_file = ex.get("coinbase_key_file", "cdp_api_key.json")
    if not os.path.isabs(key_file):
        key_file = os.path.join(HERE, key_file)
    portfolio_id = ex.get("coinbase_portfolio_id") or None

    grid = cfg["grid"]
    tranche_usd = Decimal(str(grid["tranche_size_usd"]))
    tp_pct = Decimal(str(grid["take_profit_pct"]))

    print("=" * 62)
    print("Coinbase Advanced Trade readiness check  (no orders placed)")
    print("=" * 62)
    print(f"key_file      : {key_file}")
    print(f"portfolio_id  : {portfolio_id or '(none set - will see ALL portfolios)'}")
    print(f"product       : {cfg['asset']}")
    print("-" * 62)

    try:
        from venue_coinbase import CoinbaseVenue
    except ModuleNotFoundError as exc:
        print(f"FAIL  import: {exc}. Run:  .venv\\Scripts\\python -m pip install coinbase-advanced-py")
        return 1

    # 1 + 2: auth + market data
    try:
        v = CoinbaseVenue(key_file, product_id=cfg["asset"],
                          portfolio_id=portfolio_id, allow_trading=False)
        meta = v.get_product_meta()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  auth / market data: {exc!r}")
        print("      -> check the key file, and that the key has at least 'View' permission.")
        return 1
    print(f"OK    auth + market data")
    print(f"      ETH-USD price   : {meta.price}")
    print(f"      base_increment  : {meta.base_increment}  (ETH step)")
    print(f"      quote_increment : {meta.quote_increment}  (USD step)")
    print(f"      base_min_size   : {meta.base_min_size}")

    # 3: balances
    try:
        bals = v.get_balances()
        shown = {k: bals[k] for k in ("USD", "USDC", "ETH") if k in bals}
        print(f"OK    balances readable : {shown or '(no USD/USDC/ETH balance found)'}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARN  balances not readable: {exc!r}")

    # 4 + 5: previews at real grid sizes
    spacing = Decimal(str(grid["grid_spacing_pct"])) / 100
    rung_price = v.round_quote(meta.price * (1 - spacing), meta.quote_increment)
    base_size = v.round_base(tranche_usd / rung_price, meta.base_increment)
    tp_price = v.round_quote(rung_price * (1 + tp_pct / 100), meta.quote_increment)

    print("-" * 62)
    print(f"Simulated grid rung 0 : buy {base_size} ETH @ {rung_price}  (= ${tranche_usd})")
    try:
        pv = v.preview_limit("buy", base_size, rung_price)
        print(f"OK    BUY preview  fee ~ {pv.fees}  "
              f"({(pv.fees / tranche_usd * 100):.2f}% of ${tranche_usd})")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  BUY preview: {exc!r}")
        print("      -> if this is a permission error, the key lacks 'Trade'. "
              "Create a new Secret API Key with Trade enabled.")
        return 1

    try:
        pv2 = v.preview_limit("sell", base_size, tp_price)
        print(f"OK    SELL preview @ {tp_price}  fee ~ {pv2.fees}")
        round_trip = pv.fees + pv2.fees
        gross_tp = base_size * (tp_price - rung_price)
        print("-" * 62)
        print(f"Round-trip fee estimate : {round_trip}  "
              f"vs +4% gross of {gross_tp.quantize(Decimal('0.0001'))}  "
              f"-> net ~ {(gross_tp - round_trip).quantize(Decimal('0.0001'))} per cycle")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  SELL preview: {exc!r}")
        return 1

    print("=" * 62)
    print("READY. Auth, market data, balances, and order previews all work.")
    print("No orders were placed. Next: build the live order lifecycle (Phase 2/3).")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
