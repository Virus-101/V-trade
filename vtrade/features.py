"""Feature matrix and labels for the signal model.

Features at bar t use only data up to and including the close of bar t. Labels look forward
`horizon` bars, so the last `horizon` rows have no label (NaN) - they are what we predict live.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from vtrade import indicators as ind

FEATURE_COLUMNS = [
    "ret_1",
    "ret_3",
    "ret_6",
    "ret_12",
    "ret_24",
    "vol_12",
    "vol_48",
    "vol_ratio",
    "rsi_7",
    "rsi_14",
    "macd_hist",
    "bb_pctb",
    "bb_width",
    "atr_pct",
    "dist_ema_20",
    "dist_ema_50",
    "dist_ema_200",
    "ema_20_50",
    "volume_z",
    "range_pct",
    "close_pos",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
]

# Longest look-back used above (EMA 200); rows before this are dropped as warm-up.
WARMUP_BARS = 200


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV -> FEATURE_COLUMNS plus `atr` (absolute, used for stop sizing, not fed to the model)."""
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]
    log_close = np.log(close)
    ret_1 = log_close.diff()

    out = pd.DataFrame(index=df.index)
    for n in (1, 3, 6, 12, 24):
        out[f"ret_{n}"] = log_close.diff(n)
    out["vol_12"] = ret_1.rolling(12).std()
    out["vol_48"] = ret_1.rolling(48).std()
    out["vol_ratio"] = out["vol_12"] / out["vol_48"]
    out["rsi_7"] = ind.rsi(close, 7) / 100
    out["rsi_14"] = ind.rsi(close, 14) / 100
    out["macd_hist"] = ind.macd(close)["hist"] / close
    bb = ind.bollinger(close)
    out["bb_pctb"] = bb["pctb"]
    out["bb_width"] = bb["width"]
    atr = ind.atr(high, low, close)
    out["atr_pct"] = atr / close
    ema20, ema50, ema200 = ind.ema(close, 20), ind.ema(close, 50), ind.ema(close, 200)
    out["dist_ema_20"] = close / ema20 - 1
    out["dist_ema_50"] = close / ema50 - 1
    out["dist_ema_200"] = close / ema200 - 1
    out["ema_20_50"] = ema20 / ema50 - 1
    vol_mean, vol_std = volume.rolling(48).mean(), volume.rolling(48).std()
    out["volume_z"] = (volume - vol_mean) / vol_std.replace(0, np.nan)
    bar_range = (high - low).replace(0, np.nan)
    out["range_pct"] = (high - low) / close
    out["close_pos"] = (close - low) / bar_range
    hours = df.index.hour + df.index.minute / 60
    out["hour_sin"] = np.sin(2 * np.pi * hours / 24)
    out["hour_cos"] = np.cos(2 * np.pi * hours / 24)
    out["dow_sin"] = np.sin(2 * np.pi * df.index.dayofweek / 7)
    out["dow_cos"] = np.cos(2 * np.pi * df.index.dayofweek / 7)
    out["atr"] = atr
    out = out.replace([np.inf, -np.inf], np.nan)
    out.iloc[:WARMUP_BARS] = np.nan
    return out


def forward_returns(close: pd.Series, horizon: int) -> pd.Series:
    return close.shift(-horizon) / close - 1


def build_labels(close: pd.Series, horizon: int, threshold: float) -> pd.Series:
    """1.0 if the return over the next `horizon` bars beats `threshold`, else 0.0; NaN when unknown."""
    fwd = forward_returns(close, horizon)
    return (fwd > threshold).astype(float).where(fwd.notna())
