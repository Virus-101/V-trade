import numpy as np
import pandas as pd
import pytest

from vtrade.backtest import run_backtest
from vtrade.features import build_features


def _bars(prices_ohlc, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(prices_ohlc), freq="h", tz="UTC")
    df = pd.DataFrame(prices_ohlc, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1.0
    return df


def _features(df, atr=1.0, trend=0.1):
    return pd.DataFrame({"atr": atr, "dist_ema_200": trend}, index=df.index)


def test_no_signal_means_flat_equity(cfg, ohlcv):
    features = build_features(ohlcv)
    probs = pd.Series(0.0, index=ohlcv.index)
    probs.iloc[:300] = np.nan
    result = run_backtest(ohlcv, features, probs, cfg)
    assert result.trades.empty
    assert np.allclose(result.equity, cfg.risk.starting_equity)


def test_entry_fills_next_open_and_stop_loss_exits(cfg):
    cfg.costs.fee_rate = 0.0
    cfg.costs.slippage = 0.0
    cfg.risk.max_position_pct = 1.0
    df = _bars([
        [100, 101, 99, 100],   # signal at this close
        [100, 101, 99.5, 100], # entry at open 100, stop = 98
        [99, 99, 95, 96],      # stop hit -> exit at 98
        [96, 97, 95, 96],
    ])
    probs = pd.Series([0.9, 0.5, 0.5, 0.5], index=df.index)
    result = run_backtest(df, _features(df), probs, cfg)
    t = result.trades.iloc[0]
    assert t.entry_price == pytest.approx(100.0)
    assert t.exit_price == pytest.approx(98.0)
    assert t.exit_reason == "stop_loss"
    # risked 1% of equity on a 2-point stop: loss should be ~1% of equity
    assert t.pnl == pytest.approx(-0.01 * cfg.risk.starting_equity)


def test_gap_through_stop_fills_at_open(cfg):
    cfg.costs.fee_rate = cfg.costs.slippage = 0.0
    df = _bars([
        [100, 101, 99, 100],
        [100, 100.5, 99.5, 100],
        [90, 91, 89, 90],       # gaps below the 98 stop
        [90, 91, 89, 90],
    ])
    probs = pd.Series([0.9, 0.5, 0.5, 0.5], index=df.index)
    t = run_backtest(df, _features(df), probs, cfg).trades.iloc[0]
    assert t.exit_price == pytest.approx(90.0)


def test_take_profit_and_fees(cfg):
    cfg.costs.fee_rate = 0.001
    cfg.costs.slippage = 0.0
    df = _bars([
        [100, 101, 99, 100],
        [100, 101, 99.5, 101],
        [101, 104, 100.5, 103.5],   # target = 103 -> exit at 103
        [103, 104, 102, 103],
    ])
    probs = pd.Series([0.9, 0.5, 0.5, 0.5], index=df.index)
    result = run_backtest(df, _features(df), probs, cfg)
    t = result.trades.iloc[0]
    assert t.exit_reason == "take_profit"
    assert t.exit_price == pytest.approx(103.0)
    gross = t.qty * 3.0
    assert t.pnl == pytest.approx(gross - t.fees)
    assert result.equity.iloc[-1] == pytest.approx(cfg.risk.starting_equity + t.pnl)


def test_trend_filter_blocks_entries(cfg):
    df = _bars([[100, 101, 99, 100]] * 5)
    probs = pd.Series(0.9, index=df.index)
    result = run_backtest(df, _features(df, trend=-0.05), probs, cfg)
    assert result.trades.empty


def test_synthetic_backtest_runs_and_reports(cfg, ohlcv):
    from vtrade.features import build_labels
    from vtrade.model import walk_forward_predict

    features = build_features(ohlcv)
    labels = build_labels(ohlcv["close"], cfg.model.horizon, cfg.model.label_threshold)
    probs = walk_forward_predict(features, labels, cfg.model.horizon, cfg.model.min_train_bars, 500, cfg.model.params)
    result = run_backtest(ohlcv, features, probs, cfg)
    assert len(result.equity) == len(ohlcv) - cfg.model.min_train_bars
    for key in ("total_return", "sharpe", "max_drawdown", "trades", "exposure"):
        assert key in result.metrics
    assert (result.equity > 0).all()
