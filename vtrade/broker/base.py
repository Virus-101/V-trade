"""Broker interface: the engine keeps its own ledger and asks a broker only to execute fills."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Fill:
    side: str          # "buy" | "sell"
    qty: float         # base units actually received (buy) or sold (sell)
    price: float       # average fill price
    fee: float         # fee in quote currency
    timestamp: pd.Timestamp
    order_id: str = ""


class Broker(ABC):
    name: str = "broker"

    @abstractmethod
    def available(self) -> tuple[float, float]:
        """Free (quote, base) balances the broker can actually trade with."""

    @abstractmethod
    def buy(self, qty: float, price_hint: float) -> Fill:
        """Market-buy about `qty` base units. `price_hint` is the latest known price."""

    @abstractmethod
    def sell(self, qty: float, price_hint: float) -> Fill:
        """Market-sell `qty` base units."""
