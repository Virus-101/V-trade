"""Typed settings loaded from config.yaml (secrets come from the environment / .env)."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ExchangeConfig:
    id: str = "binance"
    sandbox: bool = False


@dataclass
class ModelConfig:
    horizon: int = 12
    label_threshold: float = 0.004
    min_train_bars: int = 4000
    retrain_every: int = 500
    params: dict[str, Any] = field(
        default_factory=lambda: {
            "max_iter": 300,
            "learning_rate": 0.05,
            "max_leaf_nodes": 31,
            "min_samples_leaf": 50,
            "l2_regularization": 1.0,
        }
    )


@dataclass
class StrategyConfig:
    entry_threshold: float = 0.58
    exit_threshold: float = 0.45
    trend_filter: bool = True
    max_hold_bars: int = 48


@dataclass
class RiskConfig:
    starting_equity: float = 10_000.0
    risk_per_trade: float = 0.01
    max_position_pct: float = 0.5
    stop_atr_mult: float = 2.0
    take_profit_atr_mult: float = 3.0
    daily_loss_limit: float = 0.03
    max_drawdown: float = 0.20
    min_notional: float = 10.0


@dataclass
class CostConfig:
    fee_rate: float = 0.001
    slippage: float = 0.0005


@dataclass
class LLMConfig:
    enabled: bool = False
    model: str = "claude-opus-5"
    effort: str = "high"
    min_confidence: float = 0.5
    on_error: str = "veto"


@dataclass
class EngineConfig:
    poll_seconds: int = 30
    warmup_bars: int = 900


@dataclass
class NewsConfig:
    enabled: bool = True
    currencies: list[str] = field(default_factory=lambda: ["USD"])
    impacts: list[str] = field(default_factory=lambda: ["High"])
    block_before_minutes: int = 30
    block_after_minutes: int = 30
    close_before_event: bool = False
    refresh_minutes: int = 60


@dataclass
class CopyConfig:
    enabled: bool = True
    coin: str = "BTC"
    leaders: list[str] = field(default_factory=list)
    top_n: int = 10
    rank_by: str = "month"
    min_account_value: float = 1_000_000
    mode: str = "off"
    min_bias: float = 0.0
    follow_entry_bias: float = 0.3
    follow_exit_bias: float = 0.0
    leaderboard_refresh_hours: int = 6


@dataclass
class Config:
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    history_since: str = "2021-01-01"
    model: ModelConfig = field(default_factory=ModelConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    copy: CopyConfig = field(default_factory=CopyConfig)

    # Paths are not user-configurable in YAML; they are derived from the project root.
    data_dir: Path = ROOT / "data"
    models_dir: Path = ROOT / "models"
    logs_dir: Path = ROOT / "logs"
    reports_dir: Path = ROOT / "reports"

    @property
    def slug(self) -> str:
        """File-name friendly id for this exchange/symbol/timeframe, e.g. binance_BTC-USDT_1h."""
        return f"{self.exchange.id}_{self.symbol.replace('/', '-').replace(':', '-')}_{self.timeframe}"

    @property
    def model_path(self) -> Path:
        return self.models_dir / f"{self.slug}.joblib"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("data_dir", "models_dir", "logs_dir", "reports_dir"):
            d.pop(key)
        return d


def _build(cls: type, values: dict[str, Any] | None):
    """Build a (possibly nested) dataclass from a dict, rejecting unknown keys so typos surface."""
    values = values or {}
    known = {f.name: f for f in fields(cls)}
    unknown = set(values) - set(known)
    if unknown:
        raise ValueError(f"Unknown config keys for {cls.__name__}: {sorted(unknown)}")
    kwargs = {}
    for name, value in values.items():
        f = known[name]
        default = f.default_factory() if callable(f.default_factory) else f.default  # type: ignore[misc]
        if is_dataclass(default) and isinstance(value, dict):
            kwargs[name] = _build(type(default), value)
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load_config(path: str | Path | None = None) -> Config:
    """Load config.yaml (or the given path) and the .env file next to it."""
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:  # python-dotenv is a hard dependency, but stay usable without it
        pass

    path = Path(path) if path else ROOT / "config.yaml"
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = _build(Config, raw)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    s, r = cfg.strategy, cfg.risk
    problems = []
    if not 0 < s.exit_threshold < s.entry_threshold < 1:
        problems.append("strategy: need 0 < exit_threshold < entry_threshold < 1")
    if not 0 < r.risk_per_trade <= 0.05:
        problems.append("risk.risk_per_trade must be in (0, 0.05]")
    if not 0 < r.max_position_pct <= 1:
        problems.append("risk.max_position_pct must be in (0, 1] (spot, no leverage)")
    if r.stop_atr_mult <= 0 or r.take_profit_atr_mult <= 0:
        problems.append("risk: ATR multipliers must be positive")
    if not 0 < r.max_drawdown < 1 or not 0 < r.daily_loss_limit < 1:
        problems.append("risk: max_drawdown and daily_loss_limit must be in (0, 1)")
    if cfg.model.horizon < 1 or cfg.model.retrain_every < 1:
        problems.append("model: horizon and retrain_every must be >= 1")
    if cfg.llm.on_error not in ("veto", "allow"):
        problems.append("llm.on_error must be 'veto' or 'allow'")
    if cfg.news.refresh_minutes < 5:
        problems.append("news.refresh_minutes must be >= 5 (ForexFactory allows 2 downloads per 5 minutes)")
    if cfg.news.block_before_minutes < 0 or cfg.news.block_after_minutes < 0:
        problems.append("news: block windows can't be negative")
    c = cfg.copy
    if c.mode not in ("off", "filter", "follow"):
        problems.append("copy.mode must be 'off', 'filter' or 'follow'")
    if c.rank_by not in ("day", "week", "month", "allTime"):
        problems.append("copy.rank_by must be day, week, month or allTime")
    if not -1 <= c.follow_exit_bias < c.follow_entry_bias <= 1:
        problems.append("copy: need -1 <= follow_exit_bias < follow_entry_bias <= 1")
    if not -1 <= c.min_bias <= 1:
        problems.append("copy.min_bias must be between -1 and 1")
    if any(not (isinstance(a, str) and a.startswith("0x") and len(a) == 42) for a in c.leaders):
        problems.append("copy.leaders must be 0x wallet addresses (42 characters)")
    if problems:
        raise ValueError("Invalid config:\n  " + "\n  ".join(problems))


def exchange_credentials() -> dict[str, str]:
    """Exchange API credentials from the environment (only needed for live trading)."""
    creds = {
        "apiKey": os.getenv("EXCHANGE_API_KEY", ""),
        "secret": os.getenv("EXCHANGE_API_SECRET", ""),
        "password": os.getenv("EXCHANGE_API_PASSWORD", ""),
    }
    return {k: v for k, v in creds.items() if v}
