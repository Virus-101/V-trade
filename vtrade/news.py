"""Economic calendar from ForexFactory and the news blackout rule.

ForexFactory publishes this week's calendar as a free JSON export (served from faireconomy.media,
its parent company). It lists when each release is scheduled, how much it usually moves markets,
and the forecast and previous values. It does not know the actual number before it is released:
what it gives the bot is advance warning of *when* the market is likely to jump, so the bot can
stay out of new trades around it.

The export is rate limited to 2 downloads per 5 minutes, so it is cached on disk.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from vtrade.config import Config

log = logging.getLogger(__name__)

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPACT_ORDER = {"High": 3, "Medium": 2, "Low": 1, "Holiday": 0}


@dataclass(frozen=True)
class NewsEvent:
    title: str
    currency: str
    time: pd.Timestamp  # UTC
    impact: str
    forecast: str
    previous: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "currency": self.currency, "time": self.time.isoformat(),
            "impact": self.impact, "forecast": self.forecast, "previous": self.previous,
        }


def http_get_json(url: str, timeout: float = 20.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "V-trade/0.1 (+https://github.com/Virus-101/V-trade)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    if body.lstrip().startswith("<"):
        # When the limit is exceeded the export returns an HTML "Request Denied" page.
        raise RuntimeError("ForexFactory refused the request (rate limit: 2 downloads per 5 minutes)")
    return json.loads(body)


def parse_events(raw: list[dict[str, Any]]) -> list[NewsEvent]:
    events = []
    for item in raw:
        try:
            when = pd.Timestamp(item["date"]).tz_convert("UTC")
        except (KeyError, ValueError, TypeError):
            continue
        events.append(NewsEvent(
            title=str(item.get("title", "")).strip(),
            currency=str(item.get("country", "")).strip().upper(),
            time=when,
            impact=str(item.get("impact", "")).strip(),
            forecast=str(item.get("forecast", "") or ""),
            previous=str(item.get("previous", "") or ""),
        ))
    return sorted(events, key=lambda e: e.time)


class NewsCalendar:
    def __init__(self, cfg: Config, fetch: Callable[[str], Any] | None = None):
        self.cfg = cfg
        self.settings = cfg.news
        self.path = cfg.data_dir / "ff_calendar.json"
        self.fetch = fetch or http_get_json
        self.lock = threading.Lock()
        self._events: list[NewsEvent] | None = None
        self._fetched_at: str | None = None
        self._last_attempt = 0.0
        self.error: str | None = None

    # ---------------------------------------------------------------- loading
    def _load_cache(self) -> None:
        if self._events is None and self.path.exists():
            try:
                blob = json.loads(self.path.read_text(encoding="utf-8"))
                self._events = parse_events(blob["events"])
                self._fetched_at = blob.get("fetched_at")
            except (ValueError, KeyError) as exc:
                log.warning("Ignoring unreadable calendar cache: %s", exc)

    def _stale(self) -> bool:
        if not self._fetched_at:
            return True
        age = datetime.now(timezone.utc) - datetime.fromisoformat(self._fetched_at)
        return age.total_seconds() > self.settings.refresh_minutes * 60

    def refresh(self, force: bool = False) -> None:
        """Download the calendar if the cache is stale (never more than once a minute)."""
        with self.lock:
            self._load_cache()
            if not force and not self._stale():
                return
            if time.monotonic() - self._last_attempt < 60:
                return
            self._last_attempt = time.monotonic()
            try:
                raw = self.fetch(FF_CALENDAR_URL)
                events = parse_events(raw)
                self._fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"fetched_at": self._fetched_at, "events": raw}), encoding="utf-8")
                self._events = events
                self.error = None
                log.info("Economic calendar updated: %s events this week", len(events))
            except Exception as exc:  # keep using the cached week
                self.error = str(exc)
                log.warning("Could not update the economic calendar: %s", exc)

    def events(self) -> list[NewsEvent]:
        self.refresh()
        return list(self._events or [])

    # ---------------------------------------------------------------- queries
    def relevant(self, event: NewsEvent) -> bool:
        return event.currency in self.settings.currencies and event.impact in self.settings.impacts

    def blackout(self, now: pd.Timestamp | None = None) -> NewsEvent | None:
        """The matching event that currently blocks new entries, if any."""
        if not self.settings.enabled:
            return None
        now = now or pd.Timestamp.now(tz="UTC")
        before = pd.Timedelta(minutes=self.settings.block_before_minutes)
        after = pd.Timedelta(minutes=self.settings.block_after_minutes)
        for event in self.events():
            if self.relevant(event) and event.time - before <= now <= event.time + after:
                return event
        return None

    def imminent(self, now: pd.Timestamp | None = None) -> NewsEvent | None:
        """A matching event starting within the pre-event window (used to close positions early)."""
        now = now or pd.Timestamp.now(tz="UTC")
        before = pd.Timedelta(minutes=self.settings.block_before_minutes)
        for event in self.events():
            if self.relevant(event) and now <= event.time <= now + before:
                return event
        return None

    def upcoming(self, now: pd.Timestamp | None = None, hours: float = 24, relevant_only: bool = True) -> list[NewsEvent]:
        now = now or pd.Timestamp.now(tz="UTC")
        end = now + pd.Timedelta(hours=hours)
        return [e for e in self.events() if now <= e.time <= end and (not relevant_only or self.relevant(e))]

    def next_relevant(self, now: pd.Timestamp | None = None) -> NewsEvent | None:
        now = now or pd.Timestamp.now(tz="UTC")
        return next((e for e in self.events() if e.time >= now and self.relevant(e)), None)

    def status(self) -> dict[str, Any]:
        return {"fetched_at": self._fetched_at, "error": self.error, "source": "ForexFactory", "url": FF_CALENDAR_URL}
