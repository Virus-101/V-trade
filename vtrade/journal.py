"""SQLite trade journal: fills, equity snapshots and notable events (vetoes, halts, errors)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

from vtrade.broker.base import Fill

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, mode TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    qty REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL,
    reason TEXT, pnl REAL, order_id TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT NOT NULL, mode TEXT NOT NULL, symbol TEXT NOT NULL,
    equity REAL NOT NULL, price REAL NOT NULL, prob REAL,
    PRIMARY KEY (ts, mode, symbol)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, mode TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT
);
"""


class Journal:
    def __init__(self, path: Path, mode: str, symbol: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.mode, self.symbol = mode, symbol

    def record_fill(self, fill: Fill, reason: str, pnl: float | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO fills (ts, mode, symbol, side, qty, price, fee, reason, pnl, order_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (fill.timestamp.isoformat(), self.mode, self.symbol, fill.side, fill.qty, fill.price, fill.fee, reason, pnl, fill.order_id),
            )

    def record_equity(self, ts: pd.Timestamp, equity: float, price: float, prob: float | None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO equity (ts, mode, symbol, equity, price, prob) VALUES (?,?,?,?,?,?)",
                (ts.isoformat(), self.mode, self.symbol, equity, price, prob),
            )

    def event(self, kind: str, detail: dict | str) -> None:
        text = detail if isinstance(detail, str) else json.dumps(detail, default=str)
        with self.conn:
            self.conn.execute(
                "INSERT INTO events (ts, mode, kind, detail) VALUES (?,?,?,?)",
                (pd.Timestamp.now(tz="UTC").isoformat(), self.mode, kind, text),
            )

    def recent_fills(self, limit: int = 20) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT ts, side, qty, price, fee, reason, pnl FROM fills WHERE mode = ? AND symbol = ? ORDER BY id DESC LIMIT ?",
            self.conn,
            params=(self.mode, self.symbol, limit),
        )

    def equity_history(self, limit: int = 2000) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT ts, equity, price, prob FROM (SELECT * FROM equity WHERE mode = ? AND symbol = ? ORDER BY ts DESC LIMIT ?) ORDER BY ts",
            self.conn,
            params=(self.mode, self.symbol, limit),
        )

    def recent_events(self, limit: int = 10) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT ts, kind, detail FROM events WHERE mode = ? ORDER BY id DESC LIMIT ?",
            self.conn,
            params=(self.mode, limit),
        )

    def close(self) -> None:
        self.conn.close()
