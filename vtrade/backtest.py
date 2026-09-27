"""Bar-by-bar backtester that runs the same strategy and risk rules as the live engine.

Timing: a decision is made at the close of bar t and filled at the open of bar t+1 (with slippage
and fees). Stops and take-profits are checked against each bar's high/low; if both are touched in
the same bar the stop is assumed to fill first, which is the conservative choice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from vtrade.config import Config
from vtrade.metrics import equity_stats, trade_stats
from vtrade.risk import RiskManager
from vtrade.strategy import Action, decide


@dataclass
class _Position:
    qty: float
    entry_price: float
    entry_time: pd.Timestamp
    entry_fee: float
    stop: float
    take_profit: float
    bars_held: int = 0


@dataclass
class BacktestResult:
    equity: pd.Series
    benchmark: pd.Series
    trades: pd.DataFrame
    metrics: dict[str, float] = field(default_factory=dict)
    benchmark_metrics: dict[str, float] = field(default_factory=dict)
    halted: str = ""


def run_backtest(df: pd.DataFrame, features: pd.DataFrame, probs: pd.Series, cfg: Config) -> BacktestResult:
    fee, slip = cfg.costs.fee_rate, cfg.costs.slippage
    risk = RiskManager(cfg.risk, cfg.costs)
    valid = np.flatnonzero(probs.notna().to_numpy())
    if len(valid) == 0:
        raise ValueError("No predictions to backtest.")
    start = int(valid[0])

    idx = df.index
    o, h, l, c = (df[col].to_numpy() for col in ("open", "high", "low", "close"))
    p = probs.to_numpy()
    atr = features["atr"].to_numpy()
    trend = features["dist_ema_200"].to_numpy()

    cash = cfg.risk.starting_equity
    pos: _Position | None = None
    pending: tuple[str, float, float] | None = None  # (action, qty, atr) or ("exit", 0, 0)
    pending_reason = ""
    trades: list[dict] = []
    equity = np.empty(len(df) - start)

    def close_position(i: int, raw_price: float, reason: str) -> None:
        nonlocal cash, pos
        assert pos is not None
        fill = raw_price * (1 - slip)
        proceeds = pos.qty * fill
        exit_fee = proceeds * fee
        cash += proceeds - exit_fee
        cost_basis = pos.qty * pos.entry_price + pos.entry_fee
        pnl = proceeds - exit_fee - cost_basis
        trades.append(
            {
                "entry_time": pos.entry_time,
                "exit_time": idx[i],
                "entry_price": pos.entry_price,
                "exit_price": fill,
                "qty": pos.qty,
                "pnl": pnl,
                "return_pct": pnl / cost_basis,
                "fees": pos.entry_fee + exit_fee,
                "bars_held": pos.bars_held,
                "exit_reason": reason,
                "stop": pos.stop,
                "take_profit": pos.take_profit,
            }
        )
        pos = None

    for i in range(start, len(df)):
        # 1) Fill the order decided at the previous close.
        if pending is not None:
            action, qty, entry_atr = pending
            if action == "enter" and pos is None:
                fill = o[i] * (1 + slip)
                qty = min(qty, cash / (fill * (1 + fee)))
                cost = qty * fill
                entry_fee = cost * fee
                cash -= cost + entry_fee
                stop, take_profit = risk.levels(fill, entry_atr)
                pos = _Position(qty, fill, idx[i], entry_fee, stop, take_profit)
            elif action == "exit" and pos is not None:
                close_position(i, o[i], pending_reason)
            pending = None

        # 2) Intrabar stop-loss / take-profit.
        if pos is not None:
            if l[i] <= pos.stop:
                close_position(i, min(o[i], pos.stop), "stop_loss")
            elif h[i] >= pos.take_profit:
                close_position(i, max(o[i], pos.take_profit), "take_profit")

        # 3) Mark to market at the close.
        eq = cash + (pos.qty * c[i] if pos is not None else 0.0)
        risk.update(eq, idx[i])
        equity[i - start] = eq
        if pos is not None:
            pos.bars_held += 1

        # 4) Decide at the close; the last bar has no next open to fill at.
        if i == len(df) - 1:
            break
        if risk.state.halted:
            if pos is not None:
                pending, pending_reason = ("exit", 0.0, 0.0), "kill_switch"
            continue
        decision = decide(cfg.strategy, p[i], trend[i], pos is not None, pos.bars_held if pos else 0)
        if decision.action == Action.ENTER:
            ok, _ = risk.can_open(eq)
            plan = risk.plan(eq, cash, c[i], atr[i]) if ok else None
            if plan is not None:
                pending = ("enter", plan.qty, atr[i])
        elif decision.action == Action.EXIT:
            pending, pending_reason = ("exit", 0.0, 0.0), decision.reason

    if pos is not None:
        close_position(len(df) - 1, c[-1], "end_of_data")
        equity[-1] = cash

    equity_s = pd.Series(equity, index=idx[start:], name="equity")
    benchmark = pd.Series(cfg.risk.starting_equity * c[start:] / c[start], index=idx[start:], name="buy_and_hold")
    trades_df = pd.DataFrame(trades)
    in_market = _exposure(trades_df, idx[start:])
    metrics = {**equity_stats(equity_s, cfg.timeframe), **trade_stats(trades_df), "exposure": in_market}
    return BacktestResult(
        equity=equity_s,
        benchmark=benchmark,
        trades=trades_df,
        metrics=metrics,
        benchmark_metrics=equity_stats(benchmark, cfg.timeframe),
        halted=risk.state.halt_reason,
    )


def _exposure(trades: pd.DataFrame, index: pd.DatetimeIndex) -> float:
    if trades.empty or len(index) == 0:
        return 0.0
    return float(trades["bars_held"].sum() / len(index))
