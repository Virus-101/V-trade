"""Paper / live trading loop.

Every poll: check the open position's stop-loss and take-profit against the latest price. When a
new candle closes: recompute features, ask the model for P(up), update the risk manager and act on
the strategy's decision. All state (ledger, open position, risk counters) is persisted to JSON so
the bot resumes cleanly after a restart.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from vtrade.broker.base import Broker, Fill
from vtrade.config import Config
from vtrade.data import timeframe_seconds
from vtrade.features import build_features
from vtrade.journal import Journal
from vtrade.llm import ClaudeAnalyst
from vtrade.model import SignalModel
from vtrade.risk import RiskManager
from vtrade.strategy import Action, decide

log = logging.getLogger(__name__)


@dataclass
class OpenPosition:
    qty: float
    entry_price: float
    entry_time: str
    entry_fee: float
    stop: float
    take_profit: float
    bars_held: int = 0


@dataclass
class EngineState:
    mode: str
    symbol: str
    cash: float
    position: OpenPosition | None = None
    last_bar: str | None = None
    realized_pnl: float = 0.0
    risk: dict[str, Any] | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "EngineState":
        data = json.loads(text)
        if data.get("position"):
            data["position"] = OpenPosition(**data["position"])
        return cls(**data)


def state_path(cfg: Config, mode: str) -> Path:
    return cfg.data_dir / f"state_{mode}_{cfg.slug}.json"


def load_state(cfg: Config, mode: str) -> EngineState | None:
    path = state_path(cfg, mode)
    return EngineState.from_json(path.read_text(encoding="utf-8")) if path.exists() else None


class Engine:
    def __init__(
        self,
        cfg: Config,
        mode: str,
        broker: Broker,
        feed,
        model: SignalModel,
        journal: Journal,
        analyst: ClaudeAnalyst | None = None,
        state: EngineState | None = None,
    ):
        self.cfg, self.mode = cfg, mode
        self.broker, self.feed, self.model = broker, feed, model
        self.journal, self.analyst = journal, analyst
        self.state = state or load_state(cfg, mode) or EngineState(mode, cfg.symbol, cfg.risk.starting_equity)
        if self.state.symbol != cfg.symbol:
            raise ValueError(f"State file is for {self.state.symbol}, config is {cfg.symbol}")
        self.risk = RiskManager.from_dict(cfg.risk, cfg.costs, self.state.risk)
        self.tf_seconds = timeframe_seconds(cfg.timeframe)

    # ---------------------------------------------------------------- persistence
    def save(self) -> None:
        self.state.risk = self.risk.to_dict()
        path = state_path(self.cfg, self.mode)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.state.to_json(), encoding="utf-8")
        os.replace(tmp, path)  # atomic: a crash never leaves a half-written state file

    def equity(self, price: float) -> float:
        pos = self.state.position
        return self.state.cash + (pos.qty * price if pos else 0.0)

    # ---------------------------------------------------------------- live reconciliation
    def reconcile(self) -> None:
        """Live only: make sure the position we think we hold still exists on the exchange."""
        pos = self.state.position
        if self.mode != "live" or pos is None:
            return
        _, base_free = self.broker.available()
        if base_free + 1e-12 < pos.qty * 0.995:
            log.warning("Exchange holds %.8f but state says %.8f; shrinking tracked position.", base_free, pos.qty)
            self.journal.event("reconcile", {"state_qty": pos.qty, "exchange_qty": base_free})
            pos.qty = base_free
            if pos.qty * pos.entry_price < self.cfg.risk.min_notional:
                log.warning("Tracked position is now below min_notional; forgetting it.")
                self.state.position = None
            self.save()

    # ---------------------------------------------------------------- trading actions
    def _enter(self, price: float, atr: float, prob: float, features: pd.DataFrame, candles: pd.DataFrame) -> None:
        equity = self.equity(price)
        ok, why = self.risk.can_open(equity)
        if not ok:
            log.info("Entry blocked: %s", why)
            self.journal.event("blocked", why)
            return
        quote_free, _ = self.broker.available()
        plan = self.risk.plan(equity, min(self.state.cash, quote_free), price, atr)
        if plan is None:
            log.info("Entry skipped: position size below minimum or invalid ATR.")
            return
        if self.analyst is not None:
            review = self.analyst.review(review_context(self.cfg, prob, plan, equity, features, candles))
            self.journal.event(
                "llm_review",
                {"approved": review.approved, "confidence": review.confidence, "reasoning": review.reasoning, "error": review.error},
            )
            log.info("Claude %s (%.2f): %s", "APPROVED" if review.approved else "VETOED", review.confidence, review.reasoning)
            if not review.approved:
                return
        fill = self.broker.buy(plan.qty, price)
        self.state.cash -= fill.qty * fill.price + fill.fee
        stop, take_profit = self.risk.levels(fill.price, atr)
        self.state.position = OpenPosition(
            qty=fill.qty,
            entry_price=fill.price,
            entry_time=fill.timestamp.isoformat(),
            entry_fee=fill.fee,
            stop=stop,
            take_profit=take_profit,
        )
        self.journal.record_fill(fill, f"entry p={prob:.2f}")
        log.info("BUY %.6f @ %.2f  stop %.2f  target %.2f", fill.qty, fill.price, stop, take_profit)
        self.save()

    def _exit(self, price: float, reason: str) -> Fill | None:
        pos = self.state.position
        if pos is None:
            return None
        fill = self.broker.sell(pos.qty, price)
        proceeds = fill.qty * fill.price - fill.fee
        pnl = proceeds - (fill.qty * pos.entry_price + pos.entry_fee * fill.qty / pos.qty)
        self.state.cash += proceeds
        self.state.realized_pnl += pnl
        remaining = pos.qty - fill.qty
        if remaining * price >= self.cfg.risk.min_notional:
            log.warning("Partial exit: %.8f left open", remaining)
            pos.entry_fee *= remaining / pos.qty
            pos.qty = remaining
        else:
            self.state.position = None
        self.journal.record_fill(fill, reason, pnl)
        log.info("SELL %.6f @ %.2f  (%s)  pnl %+.2f", fill.qty, fill.price, reason, pnl)
        self.save()
        return fill

    # ---------------------------------------------------------------- loop steps
    def check_exits(self, price: float) -> None:
        pos = self.state.position
        if pos is None:
            return
        if price <= pos.stop:
            self._exit(price, "stop_loss")
        elif price >= pos.take_profit:
            self._exit(price, "take_profit")

    def on_bar(self, candles: pd.DataFrame, price: float, now: pd.Timestamp) -> None:
        """Handle the newest closed candle in `candles`."""
        bar_time = candles.index[-1]
        close_time = bar_time + pd.Timedelta(seconds=self.tf_seconds)
        features = build_features(candles)
        last = features.iloc[[-1]]
        prob = float(self.model.predict_proba(last).iloc[0])
        close = float(candles["close"].iloc[-1])
        atr = float(last["atr"].iloc[0])
        trend = float(last["dist_ema_200"].iloc[0])

        first_bar = self.state.last_bar is None
        self.state.last_bar = bar_time.isoformat()
        equity = self.equity(close)
        self.risk.update(equity, bar_time)
        self.journal.record_equity(bar_time, equity, close, None if math.isnan(prob) else prob)
        pos = self.state.position
        if pos is not None:
            pos.bars_held += 1

        # Only act on a bar that just closed. After a restart or outage, a stale bar is logged, not traded.
        max_age = pd.Timedelta(seconds=max(3 * self.cfg.engine.poll_seconds, 180))
        fresh = (now - close_time) <= max_age and not first_bar
        decision = decide(self.cfg.strategy, prob, trend, pos is not None, pos.bars_held if pos else 0)
        log.info(
            "Bar %s close %.2f | P(up)=%.3f | equity %.2f | %s%s",
            bar_time.strftime("%Y-%m-%d %H:%M"), close, prob, equity, decision.reason,
            "" if fresh else " | not trading: startup or stale bar",
        )
        if not fresh:
            self.save()
            return

        if self.risk.state.halted:
            if pos is not None:
                self._exit(price, "kill_switch")
            log.error("Trading halted: %s", self.risk.state.halt_reason)
            self.journal.event("halted", self.risk.state.halt_reason)
        elif decision.action == Action.ENTER:
            self._enter(price, atr, prob, features, candles)
        elif decision.action == Action.EXIT:
            self._exit(price, decision.reason)
        self.save()

    def step(self, now: pd.Timestamp | None = None) -> None:
        now = now or pd.Timestamp.now(tz="UTC")
        price = self.feed.last_price()
        self.check_exits(price)
        candles = self.feed.recent_candles(self.cfg.engine.warmup_bars)
        if candles.empty:
            return
        newest = candles.index[-1].isoformat()
        if newest != self.state.last_bar:
            self.on_bar(candles, price, now)

    def run(self) -> None:
        import ccxt

        self.reconcile()
        log.info("V-trade %s engine started for %s %s (Ctrl+C to stop)", self.mode.upper(), self.cfg.symbol, self.cfg.timeframe)
        failures = 0
        while True:
            try:
                self.step()
                failures = 0
            except KeyboardInterrupt:
                raise
            except ccxt.NetworkError as exc:
                failures += 1
                log.warning("Network error (%s in a row): %s", failures, exc)
            except Exception as exc:  # keep the loop alive; stops are still checked next poll
                failures += 1
                log.exception("Engine step failed (%s in a row): %s", failures, exc)
                self.journal.event("error", str(exc))
            delay = self.cfg.engine.poll_seconds * min(2 ** max(failures - 1, 0), 8)
            time.sleep(delay)


def review_context(cfg: Config, prob: float, plan, equity: float, features: pd.DataFrame, candles: pd.DataFrame) -> dict[str, Any]:
    """Compact, numbers-only summary of the proposed trade for the Claude reviewer."""
    last = features.iloc[-1]
    recent = candles.tail(24)
    return {
        "symbol": cfg.symbol,
        "timeframe": cfg.timeframe,
        "bar_time_utc": str(candles.index[-1]),
        "model": {
            "p_up": round(prob, 4),
            "meaning": f"probability price rises more than {cfg.model.label_threshold:.2%} within {cfg.model.horizon} bars",
            "entry_threshold": cfg.strategy.entry_threshold,
        },
        "proposed_trade": {
            "side": "buy",
            "price": round(float(candles["close"].iloc[-1]), 2),
            "stop_loss": round(plan.stop, 2),
            "take_profit": round(plan.take_profit, 2),
            "position_pct_of_equity": round(plan.notional / equity, 4),
            "risk_pct_of_equity": cfg.risk.risk_per_trade,
        },
        "indicators": {
            k: round(float(last[k]), 5)
            for k in ("rsi_14", "atr_pct", "vol_ratio", "bb_pctb", "bb_width", "dist_ema_20", "dist_ema_50", "dist_ema_200", "ret_24", "volume_z")
        },
        "last_24_candles": [
            [r.Index.strftime("%m-%d %H:%M"), round(r.open, 2), round(r.high, 2), round(r.low, 2), round(r.close, 2), round(r.volume, 2)]
            for r in recent.itertuples()
        ],
        "candle_columns": ["time", "open", "high", "low", "close", "volume"],
    }
