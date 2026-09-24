import pandas as pd
import pytest

from vtrade.broker.paper import PaperBroker
from vtrade.engine import Engine, load_state
from vtrade.journal import Journal
from vtrade.llm import Review


class FakeFeed:
    def __init__(self, df):
        self.df = df
        self.end = 1000
        self.price = float(df["close"].iloc[self.end - 1])

    def recent_candles(self, limit):
        return self.df.iloc[max(0, self.end - limit) : self.end]

    def last_price(self):
        return self.price

    def advance(self):
        self.end += 1
        self.price = float(self.df["close"].iloc[self.end - 1])


class FixedModel:
    def __init__(self, p):
        self.p = p
        self.meta = {}

    def predict_proba(self, features):
        return pd.Series(self.p, index=features.index)


class StubAnalyst:
    def __init__(self, approve):
        self.approve = approve
        self.calls = 0

    def review(self, context):
        self.calls += 1
        assert context["proposed_trade"]["side"] == "buy"
        return Review(self.approve, 0.8, "stub")


def _engine(cfg, ohlcv, model, analyst=None):
    cfg.strategy.trend_filter = False
    feed = FakeFeed(ohlcv)
    journal = Journal(cfg.data_dir / "journal.db", "paper", cfg.symbol)
    engine = Engine(cfg, "paper", PaperBroker(cfg.costs), feed, model, journal, analyst)
    return engine, feed


def _now_after_close(feed):
    return feed.df.index[feed.end - 1] + pd.Timedelta(hours=1, seconds=20)


def test_first_bar_after_start_is_not_traded(cfg, ohlcv):
    engine, feed = _engine(cfg, ohlcv, FixedModel(0.9))
    engine.step(_now_after_close(feed))
    assert engine.state.position is None
    assert engine.state.last_bar is not None


def test_enters_on_fresh_bar_and_exits_when_signal_fades(cfg, ohlcv):
    model = FixedModel(0.9)
    engine, feed = _engine(cfg, ohlcv, model)
    engine.step(_now_after_close(feed))  # startup bar: observe only
    feed.advance()
    engine.step(_now_after_close(feed))
    pos = engine.state.position
    assert pos is not None and pos.qty > 0
    assert engine.state.cash < cfg.risk.starting_equity

    model.p = 0.1
    feed.advance()
    engine.step(_now_after_close(feed))
    assert engine.state.position is None
    fills = engine.journal.recent_fills()
    assert list(fills["side"]) == ["sell", "buy"]


def test_stale_bar_is_not_traded(cfg, ohlcv):
    engine, feed = _engine(cfg, ohlcv, FixedModel(0.9))
    engine.step(_now_after_close(feed))
    feed.advance()
    engine.step(_now_after_close(feed) + pd.Timedelta(minutes=30))
    assert engine.state.position is None


def test_stop_loss_checked_on_every_poll(cfg, ohlcv):
    engine, feed = _engine(cfg, ohlcv, FixedModel(0.9))
    engine.step(_now_after_close(feed))
    feed.advance()
    engine.step(_now_after_close(feed))
    stop = engine.state.position.stop
    feed.price = stop * 0.99
    engine.check_exits(feed.price)
    assert engine.state.position is None
    assert engine.journal.recent_fills()["reason"].iloc[0] == "stop_loss"


def test_state_survives_restart(cfg, ohlcv):
    engine, feed = _engine(cfg, ohlcv, FixedModel(0.9))
    engine.step(_now_after_close(feed))
    feed.advance()
    engine.step(_now_after_close(feed))
    saved = load_state(cfg, "paper")
    assert saved.position.qty == pytest.approx(engine.state.position.qty)
    assert saved.cash == pytest.approx(engine.state.cash)


def test_llm_veto_blocks_entry(cfg, ohlcv):
    analyst = StubAnalyst(approve=False)
    engine, feed = _engine(cfg, ohlcv, FixedModel(0.9), analyst)
    engine.step(_now_after_close(feed))
    feed.advance()
    engine.step(_now_after_close(feed))
    assert analyst.calls == 1
    assert engine.state.position is None
