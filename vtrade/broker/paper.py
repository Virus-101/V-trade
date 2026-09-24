"""Simulated execution at the live price with the same fees and slippage as the backtester."""

from __future__ import annotations

import uuid

import pandas as pd

from vtrade.broker.base import Broker, Fill
from vtrade.config import CostConfig


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, costs: CostConfig):
        self.costs = costs

    def available(self) -> tuple[float, float]:
        # The engine's ledger is the paper account; the simulator itself has no balance limit.
        return float("inf"), float("inf")

    def _fill(self, side: str, qty: float, price_hint: float) -> Fill:
        if qty <= 0 or price_hint <= 0:
            raise ValueError(f"Invalid paper order: {side} {qty} @ {price_hint}")
        sign = 1 if side == "buy" else -1
        price = price_hint * (1 + sign * self.costs.slippage)
        return Fill(
            side=side,
            qty=qty,
            price=price,
            fee=qty * price * self.costs.fee_rate,
            timestamp=pd.Timestamp.now(tz="UTC"),
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
        )

    def buy(self, qty: float, price_hint: float) -> Fill:
        return self._fill("buy", qty, price_hint)

    def sell(self, qty: float, price_hint: float) -> Fill:
        return self._fill("sell", qty, price_hint)
