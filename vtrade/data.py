"""Market data: ccxt OHLCV download with a CSV cache, live feed, and synthetic data for offline use."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from vtrade.config import Config

log = logging.getLogger(__name__)

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def timeframe_seconds(timeframe: str) -> int:
    """'15m' -> 900, '1h' -> 3600, '1d' -> 86400."""
    unit = timeframe[-1]
    if unit not in _UNIT_SECONDS or not timeframe[:-1].isdigit():
        raise ValueError(f"Unsupported timeframe: {timeframe!r}")
    return int(timeframe[:-1]) * _UNIT_SECONDS[unit]


def periods_per_year(timeframe: str) -> float:
    return 365 * 86400 / timeframe_seconds(timeframe)


def to_frame(rows: list[list[float]]) -> pd.DataFrame:
    """ccxt OHLCV rows ([ms, o, h, l, c, v]) -> DataFrame indexed by UTC timestamp."""
    df = pd.DataFrame(rows, columns=["timestamp", *OHLCV_COLUMNS])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates("timestamp").set_index("timestamp").sort_index()
    return df.astype(float)


def make_exchange(cfg: Config, credentials: dict[str, str] | None = None):
    import ccxt

    if not hasattr(ccxt, cfg.exchange.id):
        raise ValueError(f"Unknown ccxt exchange id: {cfg.exchange.id!r}")
    exchange = getattr(ccxt, cfg.exchange.id)({"enableRateLimit": True, **(credentials or {})})
    if cfg.exchange.sandbox:
        exchange.set_sandbox_mode(True)
    return exchange


def cache_path(cfg: Config) -> Path:
    return cfg.data_dir / f"{cfg.slug}.csv"


def load_cached(cfg: Config) -> pd.DataFrame:
    path = cache_path(cfg)
    if not path.exists():
        raise FileNotFoundError(
            f"No market data at {path}. Run `vtrade fetch` first (or pass --synthetic)."
        )
    df = pd.read_csv(path, index_col="timestamp", parse_dates=["timestamp"])
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df[OHLCV_COLUMNS].astype(float)


def fetch_history(cfg: Config, since: str | None = None, exchange=None) -> pd.DataFrame:
    """Download OHLCV from `since` (or resume from the cache) up to now and update the CSV cache."""
    exchange = exchange or make_exchange(cfg)
    tf_ms = timeframe_seconds(cfg.timeframe) * 1000
    path = cache_path(cfg)
    cached = None
    if path.exists():
        cached = load_cached(cfg)
        start_ms = int(cached.index[-1].timestamp() * 1000) + tf_ms
        log.info("Resuming %s from %s", cfg.slug, cached.index[-1])
    else:
        start_ms = int(pd.Timestamp(since or cfg.history_since, tz="UTC").timestamp() * 1000)

    rows: list[list[float]] = []
    cursor = start_ms
    now_ms = exchange.milliseconds()
    while cursor < now_ms:
        batch = exchange.fetch_ohlcv(cfg.symbol, cfg.timeframe, since=cursor, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1][0] + tf_ms
        log.info("  %s rows, up to %s", len(rows), pd.Timestamp(batch[-1][0], unit="ms", tz="UTC"))
        if len(batch) < 2:
            break

    fresh = to_frame(rows) if rows else pd.DataFrame(columns=OHLCV_COLUMNS)
    df = pd.concat([cached, fresh]) if cached is not None else fresh
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = drop_unclosed(df, cfg.timeframe)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index_label="timestamp")
    return df


def drop_unclosed(df: pd.DataFrame, timeframe: str, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Remove candles that have not closed yet (exchanges return the forming candle last)."""
    if df.empty:
        return df
    now = now or pd.Timestamp.now(tz="UTC")
    closes_at = df.index + pd.Timedelta(seconds=timeframe_seconds(timeframe))
    return df[closes_at <= now]


class MarketFeed:
    """Live public market data for the paper and live engines."""

    def __init__(self, cfg: Config, exchange=None):
        self.cfg = cfg
        self.exchange = exchange or make_exchange(cfg)

    def recent_candles(self, limit: int) -> pd.DataFrame:
        rows = self.exchange.fetch_ohlcv(self.cfg.symbol, self.cfg.timeframe, limit=limit + 1)
        return drop_unclosed(to_frame(rows), self.cfg.timeframe)

    def last_price(self) -> float:
        ticker = self.exchange.fetch_ticker(self.cfg.symbol)
        price = ticker.get("last") or ticker.get("close")
        if not price:
            raise RuntimeError(f"No last price in ticker for {self.cfg.symbol}")
        return float(price)


def synthetic_ohlcv(
    n: int = 8000,
    timeframe: str = "1h",
    seed: int = 7,
    start: str = "2022-01-01",
    start_price: float = 30_000.0,
) -> pd.DataFrame:
    """Regime-switching random walk with momentum, for tests and offline demos.

    Returns have a slowly varying drift (trend regimes) and some autocorrelation, so a model
    has something learnable - unlike pure noise - while still being far from easy money.
    """
    rng = np.random.default_rng(seed)
    vol = 0.006
    drift = np.zeros(n)
    regime = 0.0
    for i in range(n):
        if rng.random() < 0.004:  # switch regime every ~250 bars on average
            regime = rng.choice([-1.0, 0.0, 1.0]) * vol * 0.12
        drift[i] = regime
    noise = rng.standard_normal(n) * vol
    rets = np.empty(n)
    prev = 0.0
    for i in range(n):
        prev = drift[i] + 0.08 * prev + noise[i]
        rets[i] = prev
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]])
    wiggle = np.abs(rng.standard_normal(n)) * vol * close * 0.6
    high = np.maximum(open_, close) + wiggle
    low = np.minimum(open_, close) - np.abs(rng.standard_normal(n)) * vol * close * 0.6
    volume = rng.lognormal(mean=3.0, sigma=0.4, size=n) * (1 + 20 * np.abs(rets))
    index = pd.date_range(start, periods=n, freq=pd.Timedelta(seconds=timeframe_seconds(timeframe)), tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=index
    ).rename_axis("timestamp")
