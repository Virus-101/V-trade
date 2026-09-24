import pytest

from vtrade.config import Config
from vtrade.data import synthetic_ohlcv


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config()
    c.data_dir = tmp_path / "data"
    c.models_dir = tmp_path / "models"
    c.logs_dir = tmp_path / "logs"
    c.reports_dir = tmp_path / "reports"
    c.model.min_train_bars = 1500
    c.model.retrain_every = 500
    c.model.params = {"max_iter": 50, "learning_rate": 0.1, "max_leaf_nodes": 15}
    return c


@pytest.fixture(scope="session")
def ohlcv():
    return synthetic_ohlcv(n=3000, seed=11)
