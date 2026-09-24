"""Performance statistics for an equity curve and its trades."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from vtrade.data import periods_per_year


def equity_stats(equity: pd.Series, timeframe: str) -> dict[str, float]:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return {}
    total_return = equity.iloc[-1] / equity.iloc[0] - 1
    years = (equity.index[-1] - equity.index[0]).total_seconds() / (365 * 86400)
    cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 and equity.iloc[-1] > 0 else float("nan")
    rets = equity.pct_change().dropna()
    ann = math.sqrt(periods_per_year(timeframe))
    std = rets.std()
    downside = math.sqrt(float(np.mean(np.minimum(rets, 0) ** 2))) if len(rets) else 0.0
    max_dd = float((equity / equity.cummax() - 1).min())
    return {
        "total_return": float(total_return),
        "cagr": float(cagr),
        "sharpe": float(rets.mean() / std * ann) if std > 0 else 0.0,
        "sortino": float(rets.mean() / downside * ann) if downside > 0 else 0.0,
        "max_drawdown": max_dd,
        "calmar": float(cagr / abs(max_dd)) if max_dd < 0 and not math.isnan(cagr) else 0.0,
    }


def trade_stats(trades: pd.DataFrame) -> dict[str, float]:
    if trades.empty:
        return {"trades": 0}
    wins, losses = trades[trades["pnl"] > 0], trades[trades["pnl"] <= 0]
    gross_loss = -losses["pnl"].sum()
    return {
        "trades": int(len(trades)),
        "win_rate": float(len(wins) / len(trades)),
        "profit_factor": float(wins["pnl"].sum() / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_trade": float(trades["return_pct"].mean()),
        "best_trade": float(trades["return_pct"].max()),
        "worst_trade": float(trades["return_pct"].min()),
        "avg_bars_held": float(trades["bars_held"].mean()),
        "total_fees": float(trades["fees"].sum()),
    }
