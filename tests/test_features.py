import numpy as np
import pandas as pd

from vtrade.features import FEATURE_COLUMNS, WARMUP_BARS, build_features, build_labels


def test_features_do_not_look_ahead(ohlcv):
    """Features at bar t must be identical whether or not later bars exist."""
    full = build_features(ohlcv)
    t = 1200
    truncated = build_features(ohlcv.iloc[: t + 1])
    pd.testing.assert_series_equal(full.iloc[t], truncated.iloc[t], check_names=False)


def test_features_do_not_change_when_future_is_altered(ohlcv):
    t = 1500
    altered = ohlcv.copy()
    altered.iloc[t + 1 :, :4] *= 3.0  # wild future prices
    a, b = build_features(ohlcv), build_features(altered)
    pd.testing.assert_frame_equal(a.iloc[: t + 1], b.iloc[: t + 1])


def test_warmup_rows_are_empty_and_rest_filled(ohlcv):
    f = build_features(ohlcv)
    assert f.iloc[:WARMUP_BARS].isna().all().all()
    assert f.iloc[WARMUP_BARS + 50 :][FEATURE_COLUMNS].notna().mean().min() > 0.99


def test_labels_are_forward_returns():
    close = pd.Series([100.0, 101.0, 99.0, 103.0, 104.0])
    labels = build_labels(close, horizon=2, threshold=0.01)
    # 100->99 (no), 101->103 (+1.98% yes), 99->104 (yes), last two unknown
    assert labels.iloc[:3].tolist() == [0.0, 1.0, 1.0]
    assert labels.iloc[3:].isna().all()
