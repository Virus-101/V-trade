import numpy as np
import pandas as pd

from vtrade import indicators as ind


def test_rsi_is_bounded(ohlcv):
    r = ind.rsi(ohlcv["close"]).dropna()
    assert r.between(0, 100).all()


def test_rsi_is_100_when_price_only_rises():
    close = pd.Series(np.arange(1, 60, dtype=float))
    assert ind.rsi(close).dropna().eq(100).all()


def test_atr_positive_and_matches_constant_range():
    n = 100
    close = pd.Series(np.full(n, 100.0))
    high, low = close + 1, close - 1
    atr = ind.atr(high, low, close, period=14).dropna()
    assert np.allclose(atr, 2.0)


def test_bollinger_band_order(ohlcv):
    bb = ind.bollinger(ohlcv["close"]).dropna()
    assert (bb["upper"] >= bb["mid"]).all() and (bb["lower"] <= bb["mid"]).all()


def test_ema_has_warmup_nans():
    s = pd.Series(np.arange(300, dtype=float))
    e = ind.ema(s, 200)
    assert e.iloc[:199].isna().all() and e.iloc[199:].notna().all()
