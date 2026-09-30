"""Top traders to copy: live positions of the most profitable Hyperliquid wallets.

Copy-trading apps such as Invo (app.invoapp.com) follow traders on Hyperliquid, a perpetual
futures exchange that runs on its own blockchain. Every account's positions there are public, so
the bot reads them straight from Hyperliquid's public API: no Invo account, login or private API.

- The leaderboard (all accounts with their day/week/month/all-time profit) picks who to watch,
  unless `copy.leaders` lists specific wallet addresses.
- Their current positions in `copy.coin` are combined into a "bias" from -1 (everyone short)
  to +1 (everyone long), weighted by position size.

The bot trades spot and only goes long, so copying means "be long when the leaders are net
long, flat otherwise". Leaders often use 10-40x leverage; the bot keeps its own position sizing.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from vtrade.config import Config

log = logging.getLogger(__name__)

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"
WINDOWS = ("day", "week", "month", "allTime")
_HEADERS = {"User-Agent": "V-trade/0.1 (+https://github.com/Virus-101/V-trade)", "Content-Type": "application/json"}


@dataclass
class Leader:
    address: str
    name: str | None = None
    account_value: float = 0.0
    pnl: dict[str, float] = field(default_factory=dict)
    roi: dict[str, float] = field(default_factory=dict)


def http_get_json(url: str, timeout: float = 90.0) -> Any:
    with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_post_json(url: str, payload: dict[str, Any], timeout: float = 20.0) -> Any:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=_HEADERS, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _leader_from_row(row: dict[str, Any]) -> Leader:
    windows = dict(row.get("windowPerformances") or [])
    return Leader(
        address=row["ethAddress"].lower(),
        name=row.get("displayName"),
        account_value=float(row.get("accountValue") or 0),
        pnl={w: float((windows.get(w) or {}).get("pnl") or 0) for w in WINDOWS},
        roi={w: float((windows.get(w) or {}).get("roi") or 0) for w in WINDOWS},
    )


def pick_leaders(rows: list[dict[str, Any]], rank_by: str, top_n: int, min_account_value: float) -> list[Leader]:
    """Most profitable *active* accounts: traded this month (vaults and idle wallets have no volume)."""
    active = []
    for row in rows:
        windows = dict(row.get("windowPerformances") or [])
        volume = float((windows.get("month") or {}).get("vlm") or 0)
        if volume > 0 and float(row.get("accountValue") or 0) >= min_account_value:
            active.append(_leader_from_row(row))
    active.sort(key=lambda leader: leader.pnl.get(rank_by, 0), reverse=True)
    return active[:top_n]


def position_in(state: dict[str, Any], coin: str) -> dict[str, Any]:
    """Summarise one account's clearinghouse state for `coin`."""
    positions = state.get("assetPositions") or []
    out: dict[str, Any] = {
        "account_value": float((state.get("marginSummary") or {}).get("accountValue") or 0),
        "open_positions": len(positions),
        "side": "flat", "size": 0.0, "notional": 0.0, "entry": None, "leverage": None,
        "unrealized_pnl": 0.0, "liquidation": None,
    }
    for item in positions:
        p = item.get("position") or {}
        if p.get("coin") != coin:
            continue
        size = float(p.get("szi") or 0)
        out.update({
            "side": "long" if size > 0 else "short" if size < 0 else "flat",
            "size": size,
            "notional": abs(float(p.get("positionValue") or 0)),
            "entry": float(p["entryPx"]) if p.get("entryPx") else None,
            "leverage": (p.get("leverage") or {}).get("value"),
            "unrealized_pnl": float(p.get("unrealizedPnl") or 0),
            "liquidation": float(p["liquidationPx"]) if p.get("liquidationPx") else None,
        })
    return out


def bias_of(rows: list[dict[str, Any]]) -> float | None:
    """Size-weighted net direction: +1 all long, -1 all short, None if nobody holds the coin."""
    long_notional = sum(r["notional"] for r in rows if r.get("side") == "long")
    short_notional = sum(r["notional"] for r in rows if r.get("side") == "short")
    total = long_notional + short_notional
    return None if total == 0 else (long_notional - short_notional) / total


class TopTraders:
    def __init__(
        self,
        cfg: Config,
        get: Callable[[str], Any] | None = None,
        post: Callable[[str, dict[str, Any]], Any] | None = None,
    ):
        self.cfg = cfg
        self.settings = cfg.copy
        self.path = cfg.data_dir / "hl_leaders.json"
        self.get = get or http_get_json
        self.post = post or http_post_json
        self.lock = threading.Lock()
        self._cache: dict[str, Any] | None = None
        self._consensus: dict[str, Any] | None = None
        self._consensus_at = 0.0
        self.error: str | None = None

    # ---------------------------------------------------------------- leaders
    def _load_cache(self) -> dict[str, Any] | None:
        if self._cache is None and self.path.exists():
            try:
                self._cache = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                self._cache = None
        return self._cache

    def _cache_stale(self) -> bool:
        cache = self._load_cache()
        if not cache or cache.get("settings") != self._cache_key():
            return True
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cache["fetched_at"])
        return age.total_seconds() > self.settings.leaderboard_refresh_hours * 3600

    def _cache_key(self) -> dict[str, Any]:
        s = self.settings
        return {"rank_by": s.rank_by, "top_n": s.top_n, "min_account_value": s.min_account_value,
                "leaders": sorted(a.lower() for a in s.leaders)}

    def refresh_leaderboard(self, force: bool = False) -> None:
        """Download the full leaderboard (~40 MB) and keep only the leaders we watch."""
        with self.lock:
            if not force and not self._cache_stale():
                return
            try:
                rows = self.get(LEADERBOARD_URL).get("leaderboardRows", [])
                wanted = {a.lower() for a in self.settings.leaders}
                manual = [_leader_from_row(r) for r in rows if r.get("ethAddress", "").lower() in wanted]
                auto = pick_leaders(rows, self.settings.rank_by, self.settings.top_n, self.settings.min_account_value)
                self._cache = {
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "settings": self._cache_key(),
                    "accounts_ranked": len(rows),
                    "auto": [asdict(x) for x in auto],
                    "manual": [asdict(x) for x in manual],
                }
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._cache), encoding="utf-8")
                self.error = None
                log.info("Top traders updated from %s ranked Hyperliquid accounts", len(rows))
            except Exception as exc:
                self.error = f"Leaderboard unavailable: {exc}"
                log.warning(self.error)

    def leaders(self) -> list[Leader]:
        if self.settings.leaders:
            known = {x["address"]: Leader(**x) for x in (self._load_cache() or {}).get("manual", [])}
            if not known and self._cache_stale():
                self.refresh_leaderboard()
                known = {x["address"]: Leader(**x) for x in (self._cache or {}).get("manual", [])}
            return [known.get(a.lower(), Leader(address=a.lower())) for a in self.settings.leaders]
        self.refresh_leaderboard()
        return [Leader(**x) for x in (self._cache or {}).get("auto", [])]

    # ---------------------------------------------------------------- positions
    def _account(self, leader: Leader) -> dict[str, Any]:
        row = {"address": leader.address, "name": leader.name, "pnl": leader.pnl, "roi": leader.roi,
               "leaderboard_account_value": leader.account_value, "error": None}
        try:
            state = self.post(INFO_URL, {"type": "clearinghouseState", "user": leader.address})
            row.update(position_in(state, self.settings.coin))
        except Exception as exc:
            row.update({"side": "unknown", "notional": 0.0, "error": str(exc)})
        return row

    def consensus(self, max_age: float = 60.0) -> dict[str, Any]:
        """Leaders' current positions in the coin and their size-weighted bias (cached for `max_age`)."""
        if self._consensus is not None and time.monotonic() - self._consensus_at < max_age:
            return self._consensus
        leaders = self.leaders()
        with ThreadPoolExecutor(max_workers=5) as pool:
            rows = list(pool.map(self._account, leaders))
        ok = [r for r in rows if not r["error"]]
        result = {
            "coin": self.settings.coin,
            "source": "manual" if self.settings.leaders else f"top {self.settings.top_n} by {self.settings.rank_by} profit",
            "bias": bias_of(ok),
            "longs": sum(r["side"] == "long" for r in ok),
            "shorts": sum(r["side"] == "short" for r in ok),
            "flat": sum(r["side"] == "flat" for r in ok),
            "failed": len(rows) - len(ok),
            "long_notional": sum(r["notional"] for r in ok if r["side"] == "long"),
            "short_notional": sum(r["notional"] for r in ok if r["side"] == "short"),
            "traders": rows,
            "leaderboard_fetched_at": (self._cache or {}).get("fetched_at"),
            "accounts_ranked": (self._cache or {}).get("accounts_ranked"),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": self.error if not rows else (None if ok else "Could not read any leader's positions"),
        }
        self._consensus, self._consensus_at = result, time.monotonic()
        return result
