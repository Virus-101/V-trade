import time

import pytest
from fastapi.testclient import TestClient

from vtrade import services
from vtrade.copytrade import TopTraders
from vtrade.data import synthetic_ohlcv
from vtrade.features import build_features, build_labels
from vtrade.model import train_final_model
from vtrade.news import NewsCalendar
from vtrade.web.server import create_app

POST = {"X-VTrade": "1"}


class FakeFeed:
    def __init__(self):
        self.df = synthetic_ohlcv(n=1200, seed=5)

    def recent_candles(self, limit):
        return self.df.iloc[-limit:]

    def last_price(self):
        return float(self.df["close"].iloc[-1])


@pytest.fixture
def trained(cfg):
    df = synthetic_ohlcv(n=2500, seed=3)
    features = build_features(df)
    labels = build_labels(df["close"], cfg.model.horizon, cfg.model.label_threshold)
    train_final_model(features, labels, cfg.model.params, {"symbol": cfg.symbol}).save(cfg.model_path)
    return cfg


NEWS = [{"title": "Non-Farm Employment Change", "country": "USD", "date": "2099-10-02T08:30:00-04:00",
         "impact": "High", "forecast": "90K", "previous": "162K"}]
LEADER = "0x" + "a" * 40
LEADERBOARD = {"leaderboardRows": [{"ethAddress": LEADER, "accountValue": "5000000", "displayName": "whale",
                                    "windowPerformances": [["month", {"pnl": "1000000", "roi": "0.2", "vlm": "5"}]]}]}
LEADER_STATE = {"marginSummary": {"accountValue": "5000000"}, "assetPositions": [
    {"position": {"coin": "BTC", "szi": "3", "positionValue": "250000", "entryPx": "80000", "leverage": {"value": 10}}}]}


def client_for(cfg):
    news = NewsCalendar(cfg, fetch=lambda url: NEWS)  # no network in tests
    traders = TopTraders(cfg, get=lambda url: LEADERBOARD, post=lambda url, payload: LEADER_STATE)
    return TestClient(create_app(cfg, feed_factory=FakeFeed, news=news, traders=traders))


def test_index_and_overview_without_data(cfg):
    with client_for(cfg) as c:
        assert "V-trade" in c.get("/").text
        o = c.get("/api/overview").json()
        assert o["data"]["available"] is False
        assert o["model"]["available"] is False
        assert o["paper"]["running"] is False
        assert c.get("/api/signal").status_code == 409  # no model yet
        assert c.get("/api/backtest").json() == {"available": False}


def test_post_requires_header_and_local_host(cfg):
    with client_for(cfg) as c:
        assert c.post("/api/paper/stop").status_code == 403
        assert c.get("/api/overview", headers={"Host": "evil.example"}).status_code == 403
        assert c.post("/api/paper/stop", headers=POST).status_code == 200


def test_signal_snapshot(trained):
    with client_for(trained) as c:
        s = c.get("/api/signal").json()
        assert 0 <= s["prob"] <= 1
        assert s["decision"] in ("enter", "hold")
        assert len(s["history"]["close"]) == len(s["history"]["prob"]) == 72
        assert s["plan"] is None or s["plan"]["stop"] < s["price"] < s["plan"]["take_profit"]


def test_paper_start_stop_and_reset(trained):
    with client_for(trained) as c:
        assert c.post("/api/paper/start", headers=POST).json()["running"] is True
        assert c.post("/api/paper/reset", headers=POST).status_code == 409  # must stop first
        for _ in range(50):  # the engine writes its state after the first step
            if c.get("/api/paper").json()["state"]:
                break
            time.sleep(0.1)
        paper = c.get("/api/paper").json()
        assert paper["running"] and paper["state"]["cash"] == trained.risk.starting_equity
        assert c.post("/api/paper/stop", headers=POST).json()["running"] is False
        assert c.post("/api/paper/reset", headers=POST).json() == {"ok": True}
        assert c.get("/api/paper").json()["state"] is None
        logs = c.get("/api/logs").json()["lines"]
        assert any("engine started" in line["msg"] for line in logs)


def test_backtest_job_and_report(cfg, monkeypatch):
    monkeypatch.setattr(services, "market_data", lambda c, synthetic=False: synthetic_ohlcv(n=3000, seed=11))
    cfg.model.retrain_every = 750
    # Examples need the cached candles; write the same synthetic data to the cache path.
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    synthetic_ohlcv(n=3000, seed=11).to_csv(services.cache_path(cfg), index_label="timestamp")
    with client_for(cfg) as c:
        job = c.post("/api/jobs/backtest", headers=POST, json={"ignore_kill_switch": True}).json()
        assert job["status"] == "running"
        assert c.post("/api/jobs/train", headers=POST, json={}).status_code == 409  # one task at a time
        for _ in range(300):
            job = c.get("/api/jobs").json()
            if job["status"] != "running":
                break
            time.sleep(0.1)
        assert job["status"] == "done", job
        assert job["result"]["kill_switch"] is False
        report = c.get("/api/backtest").json()
        assert report["available"] and report["summary"]["metrics"]["trades"] == len(report["trades"])
        assert len(report["equity"]["t"]) == len(report["equity"]["bot"])


def test_news_and_traders_endpoints(cfg):
    with client_for(cfg) as c:
        news = c.get("/api/news").json()
        assert news["enabled"] and news["next"]["title"] == "Non-Farm Employment Change"
        assert news["blackout"] is None and news["events"][0]["relevant"] is True
        assert c.get("/api/overview").json()["news"]["next"]["currency"] == "USD"

        t = c.get("/api/traders").json()
        assert t["enabled"] and t["longs"] == 1 and t["bias"] == 1.0
        assert t["traders"][0]["address"] == LEADER and t["traders"][0]["leverage"] == 10


def test_paper_strategy_setting(trained):
    with client_for(trained) as c:
        assert c.get("/api/paper/settings").json()["copy_mode"] == "off"
        assert c.post("/api/paper/settings", headers=POST, json={"copy_mode": "nonsense"}).status_code == 400
        assert c.post("/api/paper/settings", headers=POST, json={"copy_mode": "follow"}).json()["copy_mode"] == "follow"
        assert c.get("/api/overview").json()["paper"]["copy_mode"] == "follow"
        c.post("/api/paper/start", headers=POST)
        assert c.get("/api/overview").json()["paper"]["running_copy_mode"] == "follow"
        c.post("/api/paper/stop", headers=POST)
