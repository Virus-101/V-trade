import numpy as np
import pandas as pd

from vtrade.features import build_features, build_labels
from vtrade.model import SignalModel, classification_report, walk_forward_predict


def _wf(cfg, df):
    features = build_features(df)
    labels = build_labels(df["close"], cfg.model.horizon, cfg.model.label_threshold)
    probs = walk_forward_predict(
        features, labels, cfg.model.horizon, cfg.model.min_train_bars, cfg.model.retrain_every, cfg.model.params
    )
    return features, labels, probs


def test_walk_forward_only_predicts_after_min_train(cfg, ohlcv):
    _, _, probs = _wf(cfg, ohlcv)
    assert probs.iloc[: cfg.model.min_train_bars].isna().all()
    assert probs.iloc[cfg.model.min_train_bars :].notna().all()
    assert probs.dropna().between(0, 1).all()


def test_walk_forward_has_no_future_leakage(cfg, ohlcv):
    """Rewriting prices after bar k must not change any prediction made before bar k."""
    k = 2300
    altered = ohlcv.copy()
    rng = np.random.default_rng(0)
    altered.iloc[k:, :4] *= rng.uniform(0.5, 1.5, size=(len(altered) - k, 1))
    _, _, a = _wf(cfg, ohlcv)
    _, _, b = _wf(cfg, altered)
    pd.testing.assert_series_equal(a.iloc[:k], b.iloc[:k])
    assert not a.iloc[k:].equals(b.iloc[k:])


def test_model_save_load_roundtrip(cfg, ohlcv, tmp_path):
    features = build_features(ohlcv)
    labels = build_labels(ohlcv["close"], 12, 0.004)
    model = SignalModel(params=cfg.model.params, meta={"symbol": "X"}).fit(features, labels)
    path = tmp_path / "m.joblib"
    model.save(path)
    loaded = SignalModel.load(path)
    pd.testing.assert_series_equal(model.predict_proba(features), loaded.predict_proba(features))
    assert loaded.meta["symbol"] == "X"


def test_classification_report_fields():
    labels = pd.Series([0, 1, 0, 1, 1.0])
    probs = pd.Series([0.2, 0.8, 0.3, 0.7, 0.6])
    rep = classification_report(probs, labels)
    assert rep["auc"] == 1.0 and rep["samples"] == 5
