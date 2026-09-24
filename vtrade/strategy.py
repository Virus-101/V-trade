"""Turns a model probability into an action. Shared by the backtester and the live engine."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from vtrade.config import StrategyConfig


class Action(str, Enum):
    HOLD = "hold"
    ENTER = "enter"
    EXIT = "exit"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


def decide(
    cfg: StrategyConfig,
    prob: float,
    dist_ema_200: float,
    in_position: bool,
    bars_held: int = 0,
) -> Decision:
    """Long/flat rules evaluated at a bar close.

    Stops and take-profits are not handled here: they are price levels checked intrabar by the
    backtester and on every poll by the engine.
    """
    if prob is None or math.isnan(prob):
        return Decision(Action.HOLD, "no prediction")
    if in_position:
        if bars_held >= cfg.max_hold_bars:
            return Decision(Action.EXIT, f"max hold {cfg.max_hold_bars} bars")
        if prob <= cfg.exit_threshold:
            return Decision(Action.EXIT, f"signal faded (p={prob:.2f})")
        return Decision(Action.HOLD, f"holding (p={prob:.2f})")
    if prob < cfg.entry_threshold:
        return Decision(Action.HOLD, f"no edge (p={prob:.2f})")
    if cfg.trend_filter and not (dist_ema_200 > 0):
        return Decision(Action.HOLD, f"below EMA200 (p={prob:.2f})")
    return Decision(Action.ENTER, f"model p={prob:.2f}")
