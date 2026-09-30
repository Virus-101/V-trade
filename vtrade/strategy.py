"""Turns a model probability into an action. Shared by the backtester and the live engine."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from vtrade.config import CopyConfig, StrategyConfig


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


def decide_follow(cfg: CopyConfig, bias: float | None, in_position: bool) -> Decision:
    """Copy mode: long while the top traders are net long, flat otherwise (spot can't short)."""
    if in_position:
        if bias is None and cfg.follow_exit_bias >= 0:
            return Decision(Action.EXIT, "leaders closed their positions")
        if bias is not None and bias <= cfg.follow_exit_bias:
            return Decision(Action.EXIT, f"leaders' bias fell to {bias:+.2f}")
        return Decision(Action.HOLD, "leaders still net long" if bias is None else f"leaders still net long ({bias:+.2f})")
    if bias is None:
        return Decision(Action.HOLD, "leaders hold no position in this coin")
    if bias >= cfg.follow_entry_bias:
        return Decision(Action.ENTER, f"copying leaders, net long {bias:+.2f}")
    return Decision(Action.HOLD, f"leaders' bias {bias:+.2f} below {cfg.follow_entry_bias:+.2f}")
