"""Gradient-boosted signal model and leak-free walk-forward prediction."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, roc_auc_score

from vtrade.features import FEATURE_COLUMNS

log = logging.getLogger(__name__)


@dataclass
class SignalModel:
    """Predicts P(up): the probability that price rises more than the label threshold over the horizon."""

    params: dict[str, Any] = field(default_factory=dict)
    feature_columns: list[str] = field(default_factory=lambda: list(FEATURE_COLUMNS))
    meta: dict[str, Any] = field(default_factory=dict)
    estimator: HistGradientBoostingClassifier | None = None

    def fit(self, features: pd.DataFrame, labels: pd.Series) -> "SignalModel":
        X, y = _training_rows(features[self.feature_columns], labels)
        if len(np.unique(y)) < 2:
            raise ValueError("Training labels contain a single class; need more or different data.")
        self.estimator = HistGradientBoostingClassifier(random_state=42, **self.params)
        self.estimator.fit(X, y)
        return self

    def predict_proba(self, features: pd.DataFrame) -> pd.Series:
        if self.estimator is None:
            raise RuntimeError("Model is not trained.")
        X = features[self.feature_columns]
        valid = features["dist_ema_200"].notna()  # past the warm-up period
        out = pd.Series(np.nan, index=features.index)
        if valid.any():
            out[valid] = self.estimator.predict_proba(X[valid])[:, 1]
        return out

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"params": self.params, "feature_columns": self.feature_columns, "meta": self.meta, "estimator": self.estimator},
            path,
        )

    @classmethod
    def load(cls, path: Path) -> "SignalModel":
        if not path.exists():
            raise FileNotFoundError(f"No trained model at {path}. Run `vtrade train` first.")
        blob = joblib.load(path)
        return cls(**blob)


def _training_rows(X: pd.DataFrame, y: pd.Series) -> tuple[pd.DataFrame, np.ndarray]:
    """Rows with a known label and at least the warm-up features present."""
    mask = y.notna() & X["dist_ema_200"].notna()
    return X[mask], y[mask].to_numpy(dtype=int)


def walk_forward_predict(
    features: pd.DataFrame,
    labels: pd.Series,
    horizon: int,
    min_train_bars: int,
    retrain_every: int,
    params: dict[str, Any],
    on_progress: Callable[[int, int], None] | None = None,
) -> pd.Series:
    """Out-of-sample P(up) for every bar after `min_train_bars`, retraining every `retrain_every` bars.

    For a block that starts at bar s, the model is trained only on rows i <= s - horizon: the label
    of row i needs the close of bar i + horizon, which is only known at bar s when i + horizon <= s.
    That purge is what keeps the backtest honest - no future information reaches any prediction.
    """
    n = len(features)
    probs = pd.Series(np.nan, index=features.index)
    if n <= min_train_bars:
        raise ValueError(f"Need more than {min_train_bars} bars for walk-forward, have {n}.")
    starts = range(min_train_bars, n, retrain_every)
    for done, start in enumerate(starts):
        end = min(start + retrain_every, n)
        train_end = start - horizon + 1  # rows [0, train_end) have labels known at bar `start`
        model = SignalModel(params=params).fit(features.iloc[:train_end], labels.iloc[:train_end])
        probs.iloc[start:end] = model.predict_proba(features.iloc[start:end]).to_numpy()
        log.debug("walk-forward block %s-%s trained on %s rows", start, end, train_end)
        if on_progress:
            on_progress(done + 1, len(starts))
    return probs


def classification_report(probs: pd.Series, labels: pd.Series) -> dict[str, float]:
    mask = probs.notna() & labels.notna()
    y, p = labels[mask].astype(int), probs[mask]
    if mask.sum() == 0 or y.nunique() < 2:
        return {"samples": int(mask.sum())}
    return {
        "samples": int(mask.sum()),
        "base_rate": float(y.mean()),
        "auc": float(roc_auc_score(y, p)),
        "accuracy": float(accuracy_score(y, (p >= 0.5).astype(int))),
        "mean_prob": float(p.mean()),
    }


def train_final_model(features: pd.DataFrame, labels: pd.Series, params: dict[str, Any], meta: dict[str, Any]) -> SignalModel:
    """Fit on all labelled history; this is the model the paper/live engine uses."""
    model = SignalModel(params=params, meta=meta).fit(features, labels)
    mask = labels.notna() & features["dist_ema_200"].notna()
    model.meta.update(
        {
            "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "train_rows": int(mask.sum()),
            "train_start": str(features.index[mask][0]),
            "train_end": str(features.index[mask][-1]),
        }
    )
    return model
