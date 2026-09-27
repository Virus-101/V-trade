"""Operations shared by the CLI and the dashboard: fetch, train, backtest, live snapshot, reports."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd

from vtrade.backtest import BacktestResult, run_backtest
from vtrade.config import Config
from vtrade.data import cache_path, fetch_history, load_cached, synthetic_ohlcv
from vtrade.features import build_features, build_labels
from vtrade.model import SignalModel, classification_report, train_final_model, walk_forward_predict
from vtrade.risk import RiskManager
from vtrade.strategy import decide

Progress = Callable[[int, int], None]

SNAPSHOT_FEATURES = ("rsi_14", "dist_ema_200", "dist_ema_50", "ret_24", "vol_ratio", "atr_pct", "volume_z", "bb_pctb")


def clean(obj: Any) -> Any:
    """Make numpy/pandas values JSON-safe (NaN and inf become None)."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return None if math.isnan(obj) or math.isinf(obj) else float(obj)
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    return obj


def market_data(cfg: Config, synthetic: bool = False) -> pd.DataFrame:
    return synthetic_ohlcv(n=12_000, timeframe=cfg.timeframe) if synthetic else load_cached(cfg)


def prepare(cfg: Config, df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    features = build_features(df)
    labels = build_labels(df["close"], cfg.model.horizon, cfg.model.label_threshold)
    return features, labels


def walk_forward(cfg: Config, features: pd.DataFrame, labels: pd.Series, on_progress: Progress | None = None) -> pd.Series:
    m = cfg.model
    return walk_forward_predict(features, labels, m.horizon, m.min_train_bars, m.retrain_every, m.params, on_progress)


# ---------------------------------------------------------------------------- data & model
def data_info(cfg: Config) -> dict[str, Any]:
    path = cache_path(cfg)
    if not path.exists():
        return {"available": False}
    lines = path.read_text(encoding="utf-8").strip().splitlines()  # a few MB; cheaper than parsing with pandas
    if len(lines) < 2:
        return {"available": False}
    return {
        "available": True,
        "rows": len(lines) - 1,
        "start": lines[1].split(",")[0],
        "end": lines[-1].split(",")[0],
        "path": str(path),
    }


def fetch(cfg: Config) -> dict[str, Any]:
    df = fetch_history(cfg)
    return {"rows": len(df), "start": df.index[0].isoformat(), "end": df.index[-1].isoformat()}


def model_info(cfg: Config) -> dict[str, Any]:
    if not cfg.model_path.exists():
        return {"available": False}
    meta = SignalModel.load(cfg.model_path).meta
    return {"available": True, **meta}


def train(cfg: Config, evaluate: bool = True, synthetic: bool = False, on_progress: Progress | None = None) -> dict[str, Any]:
    df = market_data(cfg, synthetic)
    features, labels = prepare(cfg, df)
    report = classification_report(walk_forward(cfg, features, labels, on_progress), labels) if evaluate else None
    meta = {
        "symbol": cfg.symbol,
        "timeframe": cfg.timeframe,
        "exchange": cfg.exchange.id,
        "horizon": cfg.model.horizon,
        "label_threshold": cfg.model.label_threshold,
    }
    if report:
        meta["walk_forward"] = report
    model = train_final_model(features, labels, cfg.model.params, meta)
    if not synthetic:
        model.save(cfg.model_path)
    return {"bars": len(df), "start": df.index[0].isoformat(), "end": df.index[-1].isoformat(),
            "report": report, "meta": model.meta, "saved": not synthetic}


# ---------------------------------------------------------------------------- backtest
def summary_path(cfg: Config):
    return cfg.reports_dir / f"{cfg.slug}_summary.json"


def backtest(
    cfg: Config, ignore_kill_switch: bool = False, synthetic: bool = False, on_progress: Progress | None = None
) -> tuple[BacktestResult, dict[str, Any]]:
    if ignore_kill_switch:
        cfg = _with_risk(cfg, max_drawdown=0.99)
    df = market_data(cfg, synthetic)
    features, labels = prepare(cfg, df)
    probs = walk_forward(cfg, features, labels, on_progress)
    result = run_backtest(df, features, probs, cfg)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": cfg.symbol,
        "timeframe": cfg.timeframe,
        "exchange": cfg.exchange.id,
        "synthetic": synthetic,
        "kill_switch": not ignore_kill_switch,
        "period": {"start": result.equity.index[0].isoformat(), "end": result.equity.index[-1].isoformat()},
        "final_equity": float(result.equity.iloc[-1]),
        "final_benchmark": float(result.benchmark.iloc[-1]),
        "starting_equity": cfg.risk.starting_equity,
        "metrics": result.metrics,
        "benchmark_metrics": result.benchmark_metrics,
        "model_report": classification_report(probs, labels),
        "halted": result.halted,
        "settings": {"model": cfg.to_dict()["model"], "strategy": cfg.to_dict()["strategy"],
                     "risk": cfg.to_dict()["risk"], "costs": cfg.to_dict()["costs"]},
    }
    if not synthetic:
        cfg.reports_dir.mkdir(parents=True, exist_ok=True)
        stem = cfg.reports_dir / cfg.slug
        result.trades.to_csv(f"{stem}_trades.csv", index=False)
        pd.concat([result.equity, result.benchmark], axis=1).to_csv(f"{stem}_equity.csv", index_label="timestamp")
        summary_path(cfg).write_text(json.dumps(clean(summary), indent=2), encoding="utf-8")
    return result, summary


def _with_risk(cfg: Config, **changes) -> Config:
    import copy

    new = copy.deepcopy(cfg)
    for key, value in changes.items():
        setattr(new.risk, key, value)
    return new


def load_backtest(cfg: Config) -> dict[str, Any]:
    """Saved backtest report: summary, weekly equity, trades and two example trades with candles."""
    path = summary_path(cfg)
    if not path.exists():
        return {"available": False}
    summary = json.loads(path.read_text(encoding="utf-8"))
    stem = cfg.reports_dir / cfg.slug
    equity = pd.read_csv(f"{stem}_equity.csv", index_col="timestamp", parse_dates=["timestamp"])
    weekly = equity.resample("W").last().dropna()
    trades = pd.read_csv(f"{stem}_trades.csv", parse_dates=["entry_time", "exit_time"]) if (cfg.reports_dir / f"{cfg.slug}_trades.csv").exists() else pd.DataFrame()
    reasons: dict[str, int] = {}
    if not trades.empty:
        grouped = trades["exit_reason"].str.replace(r" \(p=.*\)", "", regex=True)
        reasons = {k: int(v) for k, v in grouped.value_counts().items()}
    return clean(
        {
            "available": True,
            "summary": summary,
            "equity": {
                "t": [d.strftime("%Y-%m-%d") for d in weekly.index],
                "bot": weekly["equity"].round(2).tolist(),
                "hold": weekly["buy_and_hold"].round(2).tolist(),
            },
            "exit_reasons": reasons,
            "trades": _trade_records(trades),
            "examples": _examples(cfg, trades),
        }
    )


def _trade_records(trades: pd.DataFrame) -> list[dict[str, Any]]:
    if trades.empty:
        return []
    out = trades.copy()
    for col in ("entry_time", "exit_time"):
        out[col] = out[col].dt.strftime("%Y-%m-%d %H:%M")
    return out.round({"entry_price": 2, "exit_price": 2, "qty": 6, "pnl": 2, "return_pct": 5, "fees": 2,
                      "stop": 2, "take_profit": 2}).to_dict("records")


def _examples(cfg: Config, trades: pd.DataFrame) -> list[dict[str, Any]]:
    """One take-profit and one stop-loss trade, with the candles around them."""
    if trades.empty or "stop" not in trades or not cache_path(cfg).exists():
        return []
    hist = load_cached(cfg)
    out = []
    for reason in ("take_profit", "stop_loss"):
        pool = trades[trades["exit_reason"] == reason]
        preferred = pool[pool["bars_held"].between(3, 12)]
        if preferred.empty and pool.empty:
            continue
        t = (preferred if not preferred.empty else pool).iloc[0]
        try:
            i0, i1 = hist.index.get_loc(t.entry_time), hist.index.get_loc(t.exit_time)
        except KeyError:
            continue
        window = hist.iloc[max(0, i0 - 13) : i1 + 7]
        out.append(
            {
                "reason": reason,
                "entry_time": t.entry_time.strftime("%Y-%m-%d %H:%M"),
                "exit_time": t.exit_time.strftime("%Y-%m-%d %H:%M"),
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "stop": t.stop,
                "take_profit": t.take_profit,
                "pnl": t.pnl,
                "return_pct": t.return_pct,
                "fees": t.fees,
                "bars_held": int(t.bars_held),
                "entry_idx": int(i0 - max(0, i0 - 13)),
                "exit_idx": int(i1 - max(0, i0 - 13)),
                "t": [ts.strftime("%m-%d %H:%M") for ts in window.index],
                "close": window["close"].round(2).tolist(),
            }
        )
    return out


# ---------------------------------------------------------------------------- live
def live_snapshot(cfg: Config, model: SignalModel, feed, hours: int = 72) -> dict[str, Any]:
    """Current candle, model probability history, decision and the order a buy would produce."""
    candles = feed.recent_candles(min(cfg.engine.warmup_bars + hours, 998))
    features = build_features(candles)
    probs = model.predict_proba(features)
    last = features.iloc[-1]
    close = candles["close"]
    price = float(close.iloc[-1])
    prob = float(probs.iloc[-1])
    decision = decide(cfg.strategy, prob, float(last["dist_ema_200"]), in_position=False)
    atr = float(last["atr"])
    risk = RiskManager(cfg.risk, cfg.costs)
    plan = risk.plan(cfg.risk.starting_equity, cfg.risk.starting_equity, price, atr)
    stop_distance = cfg.risk.stop_atr_mult * atr
    tail = candles.tail(hours)
    return clean(
        {
            "bar": candles.index[-1].isoformat(),
            "price": price,
            "change_24h": float(close.iloc[-1] / close.iloc[-25] - 1) if len(close) > 25 else None,
            "prob": prob,
            "decision": decision.action.value,
            "reason": decision.reason,
            "history": {
                "t": [ts.isoformat() for ts in tail.index],
                "close": tail["close"].round(2).tolist(),
                "prob": probs.tail(hours).round(4).tolist(),
            },
            "features": {k: float(last[k]) for k in SNAPSHOT_FEATURES},
            "atr": atr,
            "plan": None if plan is None else {
                "qty": plan.qty, "stop": plan.stop, "take_profit": plan.take_profit, "notional": plan.notional,
                "loss_at_stop": plan.qty * (price - plan.stop),
                "uncapped_qty": cfg.risk.starting_equity * cfg.risk.risk_per_trade / stop_distance if stop_distance > 0 else None,
            },
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
