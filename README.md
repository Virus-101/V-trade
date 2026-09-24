# V-trade: AI crypto trading bot

V-trade trades spot crypto on any exchange [ccxt](https://github.com/ccxt/ccxt) supports (Binance by
default). A machine-learning model estimates the probability that price rises over the next few
hours. Fixed rules turn that probability into long/flat trades with ATR stops, and a risk manager
decides how much to put in each trade. If you turn it on, Claude reviews every entry and can veto it
before an order is sent.

It starts in **paper trading** mode. Live trading needs API keys, a command-line flag and a typed
confirmation.

> **Read this first.** This is software, not financial advice. The backtest section below shows
> what the default settings would have done on real data, and it lost money. Crypto is volatile and
> you can lose what you put in. Run it in paper mode for weeks before you risk real money, and
> never risk money you can't afford to lose.

## How it works

```
candles (ccxt) ─► 25 features ─► gradient-boosted model ─► P(up) ─► strategy rules ─► risk manager ─► [Claude review] ─► broker
                   returns, RSI,      HistGradientBoosting      enter ≥ 0.58     size = 1% equity     approve / veto      paper or live
                   MACD, ATR, BB,     retrained walk-forward    exit ≤ 0.45      ATR stop + target
                   EMAs, volume,                                 EMA-200 filter   daily loss limit
                   time of day                                   max hold         drawdown kill switch
```

- **Model** (`vtrade/model.py`): scikit-learn `HistGradientBoostingClassifier`. It labels a bar
  "up" if the close `horizon` bars later is more than `label_threshold` higher.
- **Honest backtests** (`vtrade/backtest.py`): every prediction is out-of-sample. The model is
  retrained on an expanding window every `retrain_every` bars and only ever sees labels that were
  already known at that bar. Orders fill at the *next* bar's open, with fees and slippage. If a bar
  touches both the stop and the target, the stop is assumed to fill first. Tests check that
  rewriting future prices never changes a past prediction.
- **Same code in backtests and live trading**: the strategy (`strategy.py`) and risk manager
  (`risk.py`) are shared by the backtester and the paper/live engine.
- **Risk controls**: each trade risks about 1% of equity at its stop, no position is bigger than
  50% of equity, new entries stop for the day after a 3% daily loss, and trading halts after a 20%
  drawdown from the peak (the kill switch). All of these are set in `config.yaml`.
- **Claude reviewer** (`vtrade/llm.py`, optional): sends Claude the model's probability, the
  proposed stop, target and size, indicator values and the last 24 candles. Claude answers with a
  structured `approve`/`veto`, a confidence and a short reason, and every review is logged. It is
  never used in backtests, because Claude may already know how past prices moved.
- **Engine** (`vtrade/engine.py`): checks the stop and target on every poll (30 s by default) and
  acts once per closed candle. It saves its state to JSON with atomic writes, so a restart picks up
  where it left off. After a restart or an outage it won't trade a candle that closed more than a
  few minutes ago.

## Quick start (Windows)

```powershell
cd D:\V-trade
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

.venv\Scripts\python -m vtrade fetch      # download ~5 years of BTC/USDT 1h candles (public API, no keys)
.venv\Scripts\python -m vtrade backtest   # walk-forward backtest vs buy & hold
.venv\Scripts\python -m vtrade train      # train the model used for trading
.venv\Scripts\python -m vtrade signal     # what does the model say right now?
.venv\Scripts\python -m vtrade paper      # paper trade on live prices (Ctrl+C to stop)
.venv\Scripts\python -m vtrade status     # account, open position, recent fills
```

`run.bat` sets up the virtual environment the first time you run it and then starts paper trading.
You can also pass it any command, for example `run.bat backtest`.

To try it without a network connection, use `backtest --synthetic` or `train --synthetic`. These run
on generated prices, so they test the code, not the strategy.

## Commands

| Command | What it does |
|---|---|
| `fetch [--since 2021-01-01]` | Download candles into `data/`. Later runs only download what's new. |
| `train [--no-eval] [--synthetic]` | Report walk-forward AUC, then fit on all history and save to `models/`. |
| `backtest [--no-kill-switch] [--synthetic]` | Out-of-sample backtest. Writes trades and equity CSVs to `reports/`. |
| `signal` | Show the model's current P(up) and what the strategy would do with it. |
| `paper [--reset] [--reset-halt]` | Paper trading on live prices with a simulated `starting_equity` account. |
| `live [--yes] [--reset-halt]` | Real orders. Needs keys in `.env`, and you type `LIVE` to confirm. |
| `status [--mode paper\|live]` | Cash, position, unrealized P&L, kill switch state, recent fills. |

Every command also accepts `--config path.yaml` and `-v` for debug logging. Logs go to
`logs/vtrade.log`. Fills, equity snapshots, Claude reviews and errors are recorded in
`data/journal.db` (SQLite).

## Configuration

Everything is in [`config.yaml`](config.yaml): exchange, symbol, timeframe, model horizon and
label threshold, entry and exit thresholds, risk limits, fees, the Claude reviewer and polling. The
loader rejects unknown keys and inconsistent values such as an exit threshold above the entry
threshold. Secrets go in `.env` (copy it from [`.env.example`](.env.example)).

To trade another market, change `symbol`/`timeframe` (and `exchange.id` if needed), then run `fetch`,
`backtest` and `train` again. Every exchange/symbol/timeframe combination keeps its own data, model
and state files.

### Turning on the Claude reviewer

1. Put `ANTHROPIC_API_KEY=...` in `.env`.
2. Set `llm.enabled: true` in `config.yaml`.

It uses `claude-opus-5` with adaptive thinking and structured JSON output. Server-side refusal
fallbacks are enabled, so if a request is declined Anthropic re-runs it on its recommended fallback
model. Approvals below `min_confidence` count as vetoes. `on_error: veto` (the default) skips the
trade if the API can't be reached. There is one call per proposed entry, and entries are infrequent.

### Going live (read all of this)

1. Paper trade first and compare the results with the backtest.
2. Create exchange API keys with **trading only, no withdrawals**, and IP-restrict them. Put them in
   `.env`.
3. If your exchange supports it, try `exchange.sandbox: true` (testnet) first.
4. The bot trades from its own ledger, which starts at `risk.starting_equity` and is capped by your
   free balance. Set that to the amount you are willing to lose.
5. Stops and targets are managed by the bot, not placed on the exchange, so **they only work while
   the process is running**. Run it on a machine that stays on.
6. Run `python -m vtrade live` and type `LIVE`.

## Backtest results (real data, default settings)

Walk-forward, out-of-sample, fees and slippage included. Binance BTC/USDT, data from 2021-01-01 to
2026-09-24, run on 2026-09-24:

| | V-trade | Buy & hold |
|---|---|---|
| **1h, default config** (2021-06 → 2026-09) | **−19.7%**, max drawdown −20.2% | +117.8%, max drawdown −77.2% |
| **4h, one test config, kill switch off** (2021-11 → 2026-09) | **−12.2%**, max drawdown −14.7% | +45.0%, max drawdown −73.2% |

On 1h: 273 trades, 42% winners, profit factor 0.72. The kill switch fired in December 2022 and the
bot stayed flat after that. I also ran 48 other 1h variants with the kill switch off (horizons of
12/24/48 bars, different thresholds, stop widths and holding periods). **All 48 lost money**, with
profit factors between 0.57 and 0.87.

**What this means:** the model's out-of-sample AUC is about 0.55, so it predicts direction slightly
better than a coin flip. That edge is real, but it's smaller than the ~0.3% each round trip costs in
fees and slippage, so it doesn't make money. The risk controls did their job: drawdowns stayed near
20%, compared with more than 70% for buy & hold. **Don't expect the default settings to make money
live.** What this project gives you is a trustworthy way to test better ideas (see the list below)
without fooling yourself. The backtests are hard to game, and paper and live trading run the same
code as the backtest. If an idea only works after heavy tuning, treat that as a warning sign.

## Project layout

```
vtrade/
  cli.py          commands
  config.py       typed config loader and validation
  data.py         ccxt download and cache, live feed, synthetic data
  indicators.py   RSI, EMA, MACD, ATR, Bollinger (causal)
  features.py     feature matrix and forward-return labels
  model.py        signal model, walk-forward prediction, evaluation
  strategy.py     probability -> enter / exit / hold
  risk.py         position sizing, daily loss limit, kill switch
  backtest.py     bar-by-bar backtester with fees and slippage
  metrics.py      Sharpe, Sortino, drawdown, profit factor, ...
  engine.py       paper/live loop, state persistence
  llm.py          Claude trade reviewer
  journal.py      SQLite journal
  broker/         paper simulator and ccxt exchange broker
tests/            pytest suite (no network needed)
```

Run the tests with `.venv\Scripts\python -m pytest`.

## Ideas for improving the edge

- Try higher timeframes (`4h`), where fees take a smaller share of each move.
- Add features the model can't compute from price alone: funding rates, open interest, order book
  imbalance, on-chain flows, or the returns of other coins.
- Pick thresholds on a validation period and confirm them on a later period you haven't looked at.
  Tuning on the whole backtest overfits.
- Add short selling on a futures market. The backtester's long-only rules would have to change.
