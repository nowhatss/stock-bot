#!/usr/bin/env python3
"""
Coinbase Advanced Trade venue adapter for the grid bot.

Wraps `coinbase-advanced-py` (RESTClient) behind a small, explicit interface the
grid bot uses. Every method that would change account state (`place_*`,
`cancel_order`) is blocked unless the adapter was constructed with
`allow_trading=True` AND the caller passes the matching confirmation. Read-only
methods (price, product metadata, balances, previews) are always available.

This module never runs on its own. It is imported by the grid bot only when
config `execution.mode` is one of: live_preview | confirm | live.

Requires:  pip install coinbase-advanced-py   (already in .venv)
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN


# --------------------------------------------------------------------------- #
# data holders
# --------------------------------------------------------------------------- #
@dataclass
class ProductMeta:
    product_id: str
    price: Decimal
    base_increment: Decimal      # smallest ETH step
    quote_increment: Decimal     # smallest USD step
    base_min_size: Decimal
    quote_min_size: Decimal


@dataclass
class OrderResult:
    client_order_id: str
    order_id: str | None
    status: str                  # PENDING | OPEN | FILLED | CANCELLED | EXPIRED | FAILED | PREVIEW
    side: str
    limit_price: Decimal
    base_size: Decimal
    filled_size: Decimal
    avg_fill_price: Decimal
    fees: Decimal
    raw: dict


# --------------------------------------------------------------------------- #
# adapter
# --------------------------------------------------------------------------- #
class CoinbaseVenue:
    TERMINAL = {"FILLED", "CANCELLED", "EXPIRED", "FAILED"}

    def __init__(
        self,
        key_file: str,
        product_id: str = "ETH-USD",
        portfolio_id: str | None = None,
        allow_trading: bool = False,
    ) -> None:
        self.product_id = product_id
        self.portfolio_id = portfolio_id
        self.allow_trading = allow_trading
        self._client = self._make_client(key_file)

    # ---- construction ---------------------------------------------------- #
    @staticmethod
    def _make_client(key_file: str):
        from coinbase.rest import RESTClient  # lazy import

        if not os.path.exists(key_file):
            raise FileNotFoundError(
                f"Coinbase API key file not found: {key_file}. Download a "
                f"'Secret API Key' with the *Trade* permission from "
                f"https://portal.cdp.coinbase.com/access/api and point "
                f"config execution.coinbase_key_file at it."
            )
        with open(key_file, encoding="utf-8-sig") as fh:
            blob = json.load(fh)
        name = blob.get("name") or blob.get("id")
        secret = blob.get("privateKey") or blob.get("private_key")
        if not name or not secret:
            raise ValueError(
                f"{key_file} is missing 'name'/'privateKey'. Use the JSON file "
                f"downloaded from the Coinbase developer portal as-is."
            )
        return RESTClient(api_key=name, api_secret=secret)

    # ---- read-only ----------------------------------------------------- #
    def get_product_meta(self) -> ProductMeta:
        p = self._client.get_product(self.product_id)
        d = p if isinstance(p, dict) else p.__dict__
        return ProductMeta(
            product_id=self.product_id,
            price=Decimal(str(d["price"])),
            base_increment=Decimal(str(d["base_increment"])),
            quote_increment=Decimal(str(d["quote_increment"])),
            base_min_size=Decimal(str(d.get("base_min_size", "0"))),
            quote_min_size=Decimal(str(d.get("quote_min_size", "0"))),
        )

    def get_price(self) -> float:
        return float(self.get_product_meta().price)

    def get_balances(self) -> dict[str, float]:
        kwargs = {"retail_portfolio_id": self.portfolio_id} if self.portfolio_id else {}
        resp = self._client.get_accounts(**kwargs)
        d = resp if isinstance(resp, dict) else resp.__dict__
        out: dict[str, float] = {}
        for acct in d.get("accounts", []):
            a = acct if isinstance(acct, dict) else acct.__dict__
            bal = a.get("available_balance", {})
            b = bal if isinstance(bal, dict) else bal.__dict__
            cur = b.get("currency")
            val = float(b.get("value", 0) or 0)
            if cur and val:
                out[cur] = out.get(cur, 0.0) + val
        return out

    # ---- previews (no state change) ----------------------------------- #
    def preview_limit(self, side: str, base_size: Decimal, limit_price: Decimal) -> OrderResult:
        fn = (
            self._client.preview_limit_order_gtc_buy
            if side == "buy"
            else self._client.preview_limit_order_gtc_sell
        )
        kwargs = {"retail_portfolio_id": self.portfolio_id} if self.portfolio_id else {}
        resp = fn(
            product_id=self.product_id,
            base_size=str(base_size),
            limit_price=str(limit_price),
            post_only=True,          # maker only -> lowest fee tier; rejects if it would take
            **kwargs,
        )
        d = resp if isinstance(resp, dict) else resp.__dict__
        fees = Decimal(str(d.get("commission_total") or d.get("total_fees") or "0"))
        return OrderResult(
            client_order_id="(preview)", order_id=None, status="PREVIEW", side=side,
            limit_price=limit_price, base_size=base_size, filled_size=Decimal("0"),
            avg_fill_price=Decimal("0"), fees=fees, raw=d,
        )

    # ---- state-changing (guarded) ------------------------------------- #
    def _require_trading(self, confirm_token: str) -> None:
        if not self.allow_trading:
            raise PermissionError(
                "CoinbaseVenue was created with allow_trading=False. "
                "No order will be placed."
            )
        if confirm_token != "I_UNDERSTAND_THIS_PLACES_A_REAL_ORDER":
            raise PermissionError(
                "place_* requires the explicit confirm_token. Refusing to trade."
            )

    def place_limit(
        self,
        side: str,
        base_size: Decimal,
        limit_price: Decimal,
        client_order_id: str,
        confirm_token: str,
    ) -> OrderResult:
        self._require_trading(confirm_token)
        fn = (
            self._client.limit_order_gtc_buy
            if side == "buy"
            else self._client.limit_order_gtc_sell
        )
        kwargs = {"retail_portfolio_id": self.portfolio_id} if self.portfolio_id else {}
        resp = fn(
            client_order_id=client_order_id,
            product_id=self.product_id,
            base_size=str(base_size),
            limit_price=str(limit_price),
            post_only=True,
            **kwargs,
        )
        d = resp if isinstance(resp, dict) else resp.__dict__
        if not d.get("success", False):
            raise RuntimeError(f"Coinbase rejected order: {d.get('error_response') or d}")
        oid = (d.get("success_response") or {}).get("order_id") or d.get("order_id")
        return self.get_order(oid, client_order_id=client_order_id, side=side,
                              limit_price=limit_price, base_size=base_size)

    def get_order(
        self, order_id: str, *, client_order_id: str = "", side: str = "",
        limit_price: Decimal = Decimal("0"), base_size: Decimal = Decimal("0"),
    ) -> OrderResult:
        resp = self._client.get_order(order_id)
        d = resp if isinstance(resp, dict) else resp.__dict__
        o = d.get("order", d)
        o = o if isinstance(o, dict) else o.__dict__
        return OrderResult(
            client_order_id=client_order_id or o.get("client_order_id", ""),
            order_id=order_id,
            status=(o.get("status") or "UNKNOWN").upper(),
            side=side or (o.get("side") or "").lower(),
            limit_price=limit_price,
            base_size=base_size or Decimal(str(o.get("base_size", "0") or "0")),
            filled_size=Decimal(str(o.get("filled_size", "0") or "0")),
            avg_fill_price=Decimal(str(o.get("average_filled_price", "0") or "0")),
            fees=Decimal(str(o.get("total_fees", "0") or "0")),
            raw=o,
        )

    def cancel_order(self, order_id: str, confirm_token: str) -> bool:
        self._require_trading(confirm_token)
        resp = self._client.cancel_orders(order_ids=[order_id])
        d = resp if isinstance(resp, dict) else resp.__dict__
        results = d.get("results", [])
        return bool(results and (results[0].get("success") if isinstance(results[0], dict) else False))

    # ---- helpers ------------------------------------------------------- #
    @staticmethod
    def round_base(size: Decimal, increment: Decimal) -> Decimal:
        return size.quantize(increment, rounding=ROUND_DOWN)

    @staticmethod
    def round_quote(price: Decimal, increment: Decimal) -> Decimal:
        return price.quantize(increment, rounding=ROUND_DOWN)

    @staticmethod
    def new_client_order_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:16]}"
