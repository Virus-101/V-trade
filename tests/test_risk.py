import pandas as pd
import pytest

from vtrade.config import CostConfig, RiskConfig
from vtrade.risk import RiskManager


def make(**overrides) -> RiskManager:
    return RiskManager(RiskConfig(**overrides), CostConfig())


def test_size_risks_fixed_fraction_of_equity():
    rm = make(risk_per_trade=0.01, max_position_pct=1.0, stop_atr_mult=2.0)
    plan = rm.plan(equity=10_000, cash=10_000, price=100.0, atr=1.0)
    # stop distance = 2, risk = 100 -> 50 units
    assert plan.qty == pytest.approx(50.0)
    assert plan.stop == pytest.approx(98.0)
    assert plan.take_profit == pytest.approx(103.0)


def test_size_is_capped_by_max_position_and_cash():
    rm = make(risk_per_trade=0.05, max_position_pct=0.2)
    plan = rm.plan(equity=10_000, cash=10_000, price=100.0, atr=0.1)
    assert plan.notional == pytest.approx(2_000)
    plan = make(risk_per_trade=0.05, max_position_pct=1.0).plan(equity=10_000, cash=500, price=100.0, atr=0.1)
    assert plan.notional < 500


def test_no_trade_below_min_notional_or_bad_atr():
    rm = make(min_notional=10)
    assert rm.plan(equity=100, cash=5, price=100.0, atr=1.0) is None
    assert rm.plan(equity=10_000, cash=10_000, price=100.0, atr=float("nan")) is None
    assert rm.plan(equity=10_000, cash=10_000, price=100.0, atr=0.0) is None


def test_daily_loss_limit_blocks_until_next_day():
    rm = make(daily_loss_limit=0.03)
    day1 = pd.Timestamp("2024-01-01 00:00", tz="UTC")
    rm.update(10_000, day1)
    rm.update(9_650, day1 + pd.Timedelta(hours=5))
    assert rm.can_open(9_650)[0] is False
    rm.update(9_650, day1 + pd.Timedelta(days=1))
    assert rm.can_open(9_650)[0] is True


def test_max_drawdown_kill_switch_is_sticky_until_reset():
    rm = make(max_drawdown=0.2, daily_loss_limit=0.5)
    t = pd.Timestamp("2024-01-01", tz="UTC")
    rm.update(10_000, t)
    rm.update(7_900, t + pd.Timedelta(days=2))
    assert rm.state.halted
    rm.update(12_000, t + pd.Timedelta(days=3))
    assert rm.can_open(12_000)[0] is False
    rm.reset_halt()
    assert rm.can_open(12_000)[0] is True


def test_state_roundtrip():
    rm = make()
    rm.update(10_000, pd.Timestamp("2024-01-01", tz="UTC"))
    again = RiskManager.from_dict(RiskConfig(), CostConfig(), rm.to_dict())
    assert again.state == rm.state
