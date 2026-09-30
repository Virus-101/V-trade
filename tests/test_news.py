import json

import pandas as pd
import pytest

from vtrade.news import NewsCalendar, parse_events

RAW = [
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-02T08:30:00-04:00", "impact": "High", "forecast": "90K", "previous": "162K"},
    {"title": "Cash Rate", "country": "AUD", "date": "2026-10-01T00:30:00-04:00", "impact": "High", "forecast": "4.60%", "previous": "4.35%"},
    {"title": "ISM Services PMI", "country": "USD", "date": "2026-10-01T10:00:00-04:00", "impact": "Medium", "forecast": "51.0", "previous": "50.8"},
    {"title": "broken", "country": "USD", "date": "not a date", "impact": "High"},
]


@pytest.fixture
def calendar(cfg):
    return NewsCalendar(cfg, fetch=lambda url: RAW)


def test_parse_converts_to_utc_and_sorts():
    events = parse_events(RAW)
    assert [e.title for e in events] == ["Cash Rate", "ISM Services PMI", "Non-Farm Employment Change"]
    nfp = events[-1]
    assert nfp.time == pd.Timestamp("2026-10-02 12:30", tz="UTC")
    assert nfp.currency == "USD" and nfp.impact == "High" and nfp.forecast == "90K"


def test_only_watched_currency_and_impact_block(calendar):
    nfp = pd.Timestamp("2026-10-02 12:30", tz="UTC")
    assert calendar.blackout(nfp - pd.Timedelta(minutes=31)) is None
    assert calendar.blackout(nfp - pd.Timedelta(minutes=29)).title == "Non-Farm Employment Change"
    assert calendar.blackout(nfp + pd.Timedelta(minutes=29)) is not None
    assert calendar.blackout(nfp + pd.Timedelta(minutes=31)) is None
    # AUD high impact and USD medium impact are not watched by default
    assert calendar.blackout(pd.Timestamp("2026-10-01 04:30", tz="UTC")) is None
    assert calendar.blackout(pd.Timestamp("2026-10-01 14:00", tz="UTC")) is None


def test_disabled_never_blocks(cfg):
    cfg.news.enabled = False
    cal = NewsCalendar(cfg, fetch=lambda url: RAW)
    assert cal.blackout(pd.Timestamp("2026-10-02 12:30", tz="UTC")) is None


def test_next_relevant_and_imminent(calendar):
    now = pd.Timestamp("2026-10-01 00:00", tz="UTC")
    assert calendar.next_relevant(now).title == "Non-Farm Employment Change"
    assert calendar.imminent(now) is None
    assert calendar.imminent(pd.Timestamp("2026-10-02 12:10", tz="UTC")).title == "Non-Farm Employment Change"
    assert [e.title for e in calendar.upcoming(now, hours=48, relevant_only=False)] == ["Cash Rate", "ISM Services PMI", "Non-Farm Employment Change"]


def test_cache_is_used_when_download_fails(cfg):
    NewsCalendar(cfg, fetch=lambda url: RAW).events()  # writes the cache
    cache = json.loads((cfg.data_dir / "ff_calendar.json").read_text())
    assert len(cache["events"]) == len(RAW)

    def boom(url):
        raise RuntimeError("rate limited")

    cfg.news.refresh_minutes = 5
    cal = NewsCalendar(cfg, fetch=boom)
    cal._fetched_at = "2000-01-01T00:00:00+00:00"  # force a refresh attempt
    cal._events = parse_events(RAW)
    events = cal.events()
    assert len(events) == 3 and cal.error == "rate limited"
