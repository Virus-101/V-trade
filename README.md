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

Double-click **`run.bat`**. The first time, it creates a virtual environment and installs the
dependencies. Then it opens the dashboard at http://127.0.0.1:8766, where everything happens:

| Page | What it's for |
|---|---|
| **Dashboard** | Live price, the model's current P(up) and decision, the order it would place, paper account, open position, risk status, recent activity |
| **How it works** | A six-step walkthrough of one decision using live data and real backtest trades |
| **Paper trading** | Start/stop the paper engine, pick its strategy (ML model, ML model + top traders, or copy top traders), reset the account, account value chart, open position, trades, Claude reviews, live log |
| **News** | This week's economic calendar from ForexFactory, the next big release, and whether the news blackout is active |
| **Top traders** | Live BTC positions of the most profitable Hyperliquid traders (the ones Invo copies), their combined long/short bias, and the copy settings |
| **Backtest** | Run the walk-forward backtest (with or without the kill switch), metrics against buy & hold, account value chart, why trades ended, every trade |
| **Model & data** | Update candles, retrain the model (optionally measuring out-of-sample accuracy first), task progress |
| **Settings** | Everything in `config.yaml`, the Claude reviewer's status, and how to go live |

On a fresh install the dashboard walks you through the setup: **download candles**, then
**train model**. After that the signal appears and you can start paper trading.

The dashboard only listens on your own computer (127.0.0.1). Live trading with real money is
deliberately not a button there; it stays in the terminal (see below).

### Online snapshot (Netlify)

A read-only copy of the dashboard is published at **https://v-trade-dashboard.netlify.app**.
Netlify only hosts static files, so it can't run the Python server, the model or the paper
engine. The online page shows the numbers from the moment it was published, and its trading
controls are hidden. To update it, run this on your computer:

```powershell
run.bat publish
```

That rebuilds the snapshot (live signal, news, top traders, paper account, backtest) into `site/`
and deploys it with the Netlify CLI (`npx netlify-cli`, logged in to your account). The folder is
linked to the site in `.netlify/state.json`. The page is public but marked `noindex`, so search
engines skip it. It contains no keys or local paths.

### Online copy account (runs on Netlify)

The Netlify site also runs a **$10,000 paper account that copies the top traders around the
clock**, even when your computer is off. It lives in `netlify/` as four JavaScript Netlify
Functions:

| Function | What it does |
|---|---|
| `copy-tick` | Every 5 minutes: reads the leaders' positions, then buys or sells the paper account |
| `leaders-refresh` | Every 6 hours: re-ranks the Hyperliquid leaderboard |
| `live-copy` | `/api/live/copy`: the account (public); Start / Stop / Check / Reset need the admin key |
| `live-traders` | `/api/live/traders`: the leaders' live positions and bias |

It uses the same rules as `copy.mode: follow` in the local app. It buys spot BTC when the
leaders' bias reaches `follow_entry_bias` and sells at `follow_exit_bias` or when they all close.
It also keeps ATR stop and target, 1% risk per trade, the daily loss limit, the kill switch and
the news blackout, and waits an hour after a sale before buying again. State is kept in Netlify
Blobs. Settings come from `config.yaml` through the published snapshot, so `run.bat publish` keeps
the online account in sync. It uses paper money only; it never touches an exchange account.

**Admin key (one-time setup).** Anyone with the link can watch the account, but only someone with
the admin key can start, stop or reset it. A random key was generated into `.env`
(`VTRADE_ADMIN_KEY`, not committed). Add the same value on Netlify, then publish once more:

1. Netlify → v-trade-dashboard → Project configuration → Environment variables → Add variable:
   key `VTRADE_ADMIN_KEY`, the value from `.env`, scope Functions.
2. `run.bat publish`
3. On the site's Top traders page, press **Start copying** and paste the key when asked. The
   browser remembers it.

Function tests: `cd netlify && npm install && npm test`.

### Terminal

Every dashboard action is also a command:

```powershell
cd D:\V-trade
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

.venv\Scripts\python -m vtrade dashboard  # the web dashboard (what run.bat starts)
.venv\Scripts\python -m vtrade fetch      # download ~5 years of BTC/USDT 1h candles (public API, no keys)
.venv\Scripts\python -m vtrade backtest   # walk-forward backtest vs buy & hold
.venv\Scripts\python -m vtrade train      # train the model used for trading
.venv\Scripts\python -m vtrade signal     # what does the model say right now?
.venv\Scripts\python -m vtrade paper      # paper trade on live prices (Ctrl+C to stop)
.venv\Scripts\python -m vtrade status     # account, open position, recent fills
```

`run.bat` also passes commands through, for example `run.bat backtest`.

To try it without a network connection, use `backtest --synthetic` or `train --synthetic`. These run
on generated prices, so they test the code, not the strategy.

## Commands

| Command | What it does |
|---|---|
| `dashboard [--port 8766] [--no-browser]` | Start the web dashboard and open it in your browser. |
| `publish [--no-deploy]` | Build a read-only snapshot of the dashboard in `site/` and deploy it to Netlify. |
| `fetch [--since 2021-01-01]` | Download candles into `data/`. Later runs only download what's new. |
| `train [--no-eval] [--synthetic]` | Report walk-forward AUC, then fit on all history and save to `models/`. |
| `backtest [--no-kill-switch] [--synthetic]` | Out-of-sample backtest. Writes trades, equity and a summary to `reports/`. |
| `signal` | Show the model's current P(up) and what the strategy would do with it. |
| `paper [--reset] [--reset-halt]` | Paper trading on live prices with a simulated `starting_equity` account. |
| `live [--yes] [--reset-halt]` | Real orders. Needs keys in `.env`, and you type `LIVE` to confirm. |
| `status [--mode paper\|live]` | Cash, position, unrealized P&L, kill switch state, recent fills. |

Every command also accepts `--config path.yaml` and `-v` for debug logging. Logs go to
`logs/vtrade.log`. Fills, equity snapshots, Claude reviews and errors are recorded in
`data/journal.db` (SQLite).

## News, top traders and Fortrade

**ForexFactory (news blackout).** The bot downloads this week's economic calendar from
ForexFactory's free export (at most once an hour; ForexFactory allows 2 downloads per 5 minutes).
By default it opens no new trades from 30 minutes before to 30 minutes after a High-impact USD
release (CPI, jobs report, Fed decisions), and can also sell ahead of them
(`news.close_before_event`). The calendar tells the bot *when* a release is due. It can't know
the actual number before it comes out, and there's no history to backtest this rule with. The
upcoming releases are also passed to the Claude reviewer when it's on.

**Invo / top traders (copy trading).** Invo (app.invoapp.com) lets people copy traders on
Hyperliquid, a crypto futures exchange where every account's positions are public. V-trade reads
the same data from Hyperliquid's public API, so it needs no Invo account, login or private API. It
watches the 10 most profitable active accounts this month (or the wallets you list in
`copy.leaders`), and combines their BTC positions into a bias from −1 (all short) to +1 (all long).
There are three strategies. Pick one on the Paper trading or Top traders page, or set `copy.mode`:

| Mode | What the bot does |
|---|---|
| `off` | ML model only (default) |
| `filter` | ML model entries, but only while the top traders are net long |
| `follow` | Copies the top traders: buys when their bias reaches `follow_entry_bias`, sells at `follow_exit_bias` |

The leaders often use 10–40× leverage, and some of their positions are hedges. V-trade copies only
the direction: spot BTC, its own position size and stops, no shorts, no leverage. None of this can
be backtested, because past leader positions aren't available.

**Fortrade (practice account).** Fortrade (pro.fortrade.com) has no public API. Its only automated
route is MetaTrader 4, which would need a paid third-party bridge holding your login. So V-trade
doesn't place orders there. Instead the dashboard shows every trade idea with the buy price, stop
and target, next to a "Practice this on Fortrade" button, so you can place it on your Fortrade demo
account by hand. V-trade's own paper trading already practices automatically.

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
  services.py     fetch / train / backtest / live snapshot, shared by the CLI and the dashboard
  broker/         paper simulator and ccxt exchange broker
  news.py         ForexFactory economic calendar and the news blackout
  copytrade.py    top Hyperliquid traders, their positions and bias
  web/server.py   dashboard API (FastAPI), paper engine thread, background tasks
  web/export.py   read-only static snapshot for Netlify
  web/static/     dashboard page (HTML, CSS, JS with Chart.js)
tests/            pytest suite (no network needed)
netlify/          online copy account (Netlify Functions + tests)
```

Run the tests with `.venv\Scripts\python -m pytest`.

## Ideas for improving the edge

- Try higher timeframes (`4h`), where fees take a smaller share of each move.
- Add features the model can't compute from price alone: funding rates, open interest, order book
  imbalance, on-chain flows, or the returns of other coins.
- Pick thresholds on a validation period and confirm them on a later period you haven't looked at.
  Tuning on the whole backtest overfits.
- Add short selling on a futures market. The backtester's long-only rules would have to change.
