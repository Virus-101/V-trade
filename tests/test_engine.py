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


class FakeNews:
    def __init__(self, event=None):
        self.event = event

    def blackout(self, now=None):
        return self.event

    def imminent(self, now=None):
        return self.event

    def upcoming(self, now=None, hours=24, relevant_only=True):
        return []


class FakeLeaders:
    def __init__(self, bias):
        self.bias = bias

    def consensus(self, max_age=60):
        return {"coin": "BTC", "bias": self.bias, "longs": 1, "shorts": 0, "flat": 0, "error": None}


def _with(cfg, ohlcv, model, news=None, leaders=None):
    engine, feed = _engine(cfg, ohlcv, model)
    engine.news, engine.leaders = news, leaders
    return engine, feed


def _two_bars(engine, feed):
    engine.step(_now_after_close(feed))  # startup bar: observe only
    feed.advance()
    engine.step(_now_after_close(feed))


def test_news_blackout_blocks_entries(cfg, ohlcv):
    from vtrade.news import NewsEvent

    event = NewsEvent("CPI m/m", "USD", pd.Timestamp("2026-10-01 12:30", tz="UTC"), "High", "0.3%", "0.2%")
    engine, feed = _with(cfg, ohlcv, FixedModel(0.9), news=FakeNews(event))
    _two_bars(engine, feed)
    assert engine.state.position is None
    events = engine.journal.recent_events()
    assert "news blackout" in events["detail"].iloc[0]


def test_close_before_event_sells_open_position(cfg, ohlcv):
    from vtrade.news import NewsEvent

    engine, feed = _with(cfg, ohlcv, FixedModel(0.9), news=FakeNews(None))
    _two_bars(engine, feed)
    assert engine.state.position is not None
    cfg.news.close_before_event = True
    engine.news = FakeNews(NewsEvent("FOMC Statement", "USD", pd.Timestamp("2026-10-01 18:00", tz="UTC"), "High", "", ""))
    engine.step(_now_after_close(feed))
    assert engine.state.position is None
    assert engine.journal.recent_fills()["reason"].iloc[0].startswith("news ahead")


@pytest.mark.parametrize("bias, enters", [(-0.4, False), (None, False), (0.2, True)])
def test_filter_mode_needs_leaders_net_long(cfg, ohlcv, bias, enters):
    cfg.copy.mode = "filter"
    engine, feed = _with(cfg, ohlcv, FixedModel(0.9), leaders=FakeLeaders(bias))
    _two_bars(engine, feed)
    assert (engine.state.position is not None) == enters


def test_follow_mode_copies_leaders_regardless_of_model(cfg, ohlcv):
    cfg.copy.mode = "follow"
    leaders = FakeLeaders(0.6)
    engine, feed = _with(cfg, ohlcv, FixedModel(0.1), leaders=leaders)  # model says no, leaders say long
    _two_bars(engine, feed)
    assert engine.state.position is not None
    assert "copying leaders" in engine.journal.recent_fills()["reason"].iloc[0]

    leaders.bias = -0.2
    feed.advance()
    engine.step(_now_after_close(feed))
    assert engine.state.position is None


def test_follow_mode_holds_when_leader_data_fails(cfg, ohlcv):
    cfg.copy.mode = "follow"

    class Broken:
        def consensus(self, max_age=60):
            raise TimeoutError("hyperliquid down")

    engine, feed = _with(cfg, ohlcv, FixedModel(0.9), leaders=Broken())
    _two_bars(engine, feed)
    assert engine.state.position is None


def test_follow_mode_exits_when_all_leaders_close(cfg, ohlcv):
    cfg.copy.mode = "follow"
    leaders = FakeLeaders(0.6)
    engine, feed = _with(cfg, ohlcv, FixedModel(0.5), leaders=leaders)
    _two_bars(engine, feed)
    assert engine.state.position is not None
    leaders.bias = None  # everyone went flat
    feed.advance()
    engine.step(_now_after_close(feed))
    assert engine.state.position is None
    assert engine.journal.recent_fills()["reason"].iloc[0] == "leaders closed their positions"
