"""Real order execution on a ccxt exchange (spot, market orders)."""

from __future__ import annotations

import logging

import pandas as pd

from vtrade.broker.base import Broker, Fill
from vtrade.config import Config

log = logging.getLogger(__name__)


class ExchangeBroker(Broker):
    name = "live"

    def __init__(self, cfg: Config, exchange):
        self.cfg = cfg
        self.exchange = exchange
        self.exchange.load_markets()
        market = self.exchange.market(cfg.symbol)
        if not market.get("spot", True):
            raise ValueError(f"{cfg.symbol} is not a spot market; V-trade only trades spot (no leverage).")
        self.base, self.quote = market["base"], market["quote"]
        limits = market.get("limits") or {}
        self.min_amount = (limits.get("amount") or {}).get("min") or 0.0
        self.min_cost = (limits.get("cost") or {}).get("min") or 0.0

    def available(self) -> tuple[float, float]:
        free = self.exchange.fetch_balance().get("free", {})
        return float(free.get(self.quote) or 0.0), float(free.get(self.base) or 0.0)

    def _amount(self, qty: float, price_hint: float) -> float:
        amount = float(self.exchange.amount_to_precision(self.cfg.symbol, qty))
        if amount <= 0 or amount < self.min_amount or amount * price_hint < self.min_cost:
            raise ValueError(
                f"Order too small for {self.cfg.symbol}: {amount} (min amount {self.min_amount}, min cost {self.min_cost})"
            )
        return amount

    def _to_fill(self, side: str, order: dict, requested: float, price_hint: float) -> Fill:
        # Market orders usually come back filled; if not, fetch the final state once.
        if order.get("status") not in ("closed", "filled") and order.get("id"):
            try:
                order = self.exchange.fetch_order(order["id"], self.cfg.symbol)
            except Exception as exc:  # the order was placed; report it with our best estimate
                log.warning("Could not fetch order %s: %s", order.get("id"), exc)
        filled = float(order.get("filled") or requested)
        price = float(order.get("average") or order.get("price") or price_hint)
        qty, fee_quote = filled, filled * price * self.cfg.costs.fee_rate
        fee = order.get("fee") or {}
        if fee.get("cost") is not None:
            cost = float(fee["cost"])
            if fee.get("currency") == self.quote:
                fee_quote = cost
            elif fee.get("currency") == self.base:
                fee_quote = cost * price
                if side == "buy":
                    qty = filled - cost  # fee taken out of the coins we received
        return Fill(side, qty, price, fee_quote, pd.Timestamp.now(tz="UTC"), str(order.get("id", "")))

    def buy(self, qty: float, price_hint: float) -> Fill:
        amount = self._amount(qty, price_hint)
        log.warning("LIVE BUY %s %s @ ~%s", amount, self.cfg.symbol, price_hint)
        order = self.exchange.create_order(self.cfg.symbol, "market", "buy", amount)
        return self._to_fill("buy", order, amount, price_hint)

    def sell(self, qty: float, price_hint: float) -> Fill:
        _, base_free = self.available()
        amount = self._amount(min(qty, base_free), price_hint)
        log.warning("LIVE SELL %s %s @ ~%s", amount, self.cfg.symbol, price_hint)
        order = self.exchange.create_order(self.cfg.symbol, "market", "sell", amount)
        return self._to_fill("sell", order, amount, price_hint)
