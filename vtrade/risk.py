"""Position sizing and account-level circuit breakers."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import pandas as pd

from vtrade.config import CostConfig, RiskConfig


@dataclass(frozen=True)
class PositionPlan:
    qty: float
    stop: float
    take_profit: float
    notional: float


@dataclass
class RiskState:
    peak_equity: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""


class RiskManager:
    """Fixed-fractional sizing with ATR stops, a daily loss limit and a max-drawdown kill switch."""

    def __init__(self, cfg: RiskConfig, costs: CostConfig, state: RiskState | None = None):
        self.cfg = cfg
        self.costs = costs
        self.state = state or RiskState()

    def update(self, equity: float, ts: pd.Timestamp) -> None:
        """Call on every bar close with the marked-to-market equity."""
        s = self.state
        day = ts.strftime("%Y-%m-%d")
        if day != s.day:
            s.day, s.day_start_equity = day, equity
        s.peak_equity = max(s.peak_equity, equity)
        if not s.halted and s.peak_equity > 0:
            drawdown = 1 - equity / s.peak_equity
            if drawdown >= self.cfg.max_drawdown:
                s.halted = True
                s.halt_reason = f"max drawdown {drawdown:.1%} >= {self.cfg.max_drawdown:.0%} at {ts}"

    def can_open(self, equity: float) -> tuple[bool, str]:
        s = self.state
        if s.halted:
            return False, f"halted: {s.halt_reason}"
        if s.day_start_equity > 0 and equity <= s.day_start_equity * (1 - self.cfg.daily_loss_limit):
            return False, f"daily loss limit {self.cfg.daily_loss_limit:.0%} reached"
        return True, "ok"

    def levels(self, entry_price: float, atr: float) -> tuple[float, float]:
        """Stop-loss and take-profit prices for a long entry."""
        return entry_price - self.cfg.stop_atr_mult * atr, entry_price + self.cfg.take_profit_atr_mult * atr

    def plan(self, equity: float, cash: float, price: float, atr: float) -> PositionPlan | None:
        """Size a long entry so that hitting the stop loses about `risk_per_trade` of equity."""
        if not (atr > 0) or math.isnan(atr) or price <= 0 or equity <= 0:
            return None
        stop, take_profit = self.levels(price, atr)
        if stop <= 0:
            return None
        per_unit_cost = price * (1 + self.costs.fee_rate + self.costs.slippage)
        qty = min(
            equity * self.cfg.risk_per_trade / (price - stop),
            equity * self.cfg.max_position_pct / price,
            cash / per_unit_cost,
        )
        notional = qty * price
        if qty <= 0 or notional < self.cfg.min_notional:
            return None
        return PositionPlan(qty=qty, stop=stop, take_profit=take_profit, notional=notional)

    def reset_halt(self) -> None:
        self.state.halted, self.state.halt_reason = False, ""
        self.state.peak_equity = 0.0

    def to_dict(self) -> dict:
        return asdict(self.state)

    @classmethod
    def from_dict(cls, cfg: RiskConfig, costs: CostConfig, data: dict | None) -> "RiskManager":
        return cls(cfg, costs, RiskState(**data) if data else None)
