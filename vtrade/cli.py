"""Command line interface: `vtrade <command>` or `python -m vtrade <command>`."""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from vtrade import __version__
from vtrade.config import Config, exchange_credentials, load_config

console = Console()
log = logging.getLogger("vtrade")

LIVE_WARNING = """[bold red]LIVE TRADING[/] places real market orders with real money on {exchange} ({symbol}).
- Past backtest results do not guarantee future returns; you can lose money.
- Use API keys with trading permission only (never withdrawals), ideally IP-restricted.
- Stops are managed by the bot: they only work while this process is running.
- The bot trades up to risk.starting_equity = {equity} {quote} of your balance."""


def setup_logging(cfg: Config, verbose: bool) -> None:
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(cfg.logs_dir / "vtrade.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True), file_handler],
    )
    for noisy in ("ccxt", "urllib3", "httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------- helpers
def progress_status(label: str):
    """A rich status spinner plus a callback that shows walk-forward progress in it."""
    status = console.status(label)

    def update(done: int, total: int) -> None:
        status.update(f"{label} {done}/{total} models")

    return status, update


def fmt(key: str, value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    if key in {"trades", "samples"}:
        return f"{int(value)}"
    if key in {"total_return", "cagr", "max_drawdown", "win_rate", "avg_trade", "best_trade", "worst_trade", "exposure", "base_rate", "accuracy"}:
        return f"{value:+.2%}" if key in {"total_return", "cagr", "avg_trade", "best_trade", "worst_trade"} else f"{value:.2%}"
    if isinstance(value, float):
        return f"{value:,.3f}" if abs(value) < 1000 else f"{value:,.2f}"
    return str(value)


# ---------------------------------------------------------------------------- commands
def cmd_fetch(cfg: Config, args) -> int:
    from vtrade.data import fetch_history

    console.print(f"Downloading {cfg.symbol} {cfg.timeframe} from {cfg.exchange.id}...")
    df = fetch_history(cfg, since=args.since)
    console.print(f"[green]{len(df):,} candles[/] {df.index[0]} -> {df.index[-1]}")
    return 0


def cmd_train(cfg: Config, args) -> int:
    from vtrade import services

    if args.synthetic:
        console.print("[yellow]Using synthetic data (offline demo) - results say nothing about real markets.[/]")
    status, update = progress_status("Walk-forward evaluation:")
    with status:
        out = services.train(cfg, evaluate=not args.no_eval, synthetic=args.synthetic, on_progress=update)
    console.print(f"{out['bars']:,} bars  {out['start']} -> {out['end']}")
    if out["report"]:
        table = Table(title="Out-of-sample (walk-forward) prediction quality")
        table.add_column("metric")
        table.add_column("value", justify="right")
        for k, v in out["report"].items():
            table.add_row(k, fmt(k, v))
        console.print(table)
        if out["report"].get("auc", 0.5) < 0.52:
            console.print("[yellow]AUC is close to 0.5: the model has little edge on this data. Be skeptical.[/]")
    if out["saved"]:
        console.print(f"[green]Model saved[/] to {cfg.model_path} ({out['meta']['train_rows']:,} rows)")
    else:
        console.print("[yellow]Synthetic run: model not saved.[/]")
    return 0


def cmd_backtest(cfg: Config, args) -> int:
    from vtrade import services

    if args.synthetic:
        console.print("[yellow]Using synthetic data (offline demo) - results say nothing about real markets.[/]")
    status, update = progress_status("Walk-forward backtest:")
    with status:
        result, summary = services.backtest(cfg, ignore_kill_switch=args.no_kill_switch, synthetic=args.synthetic, on_progress=update)

    period = f"{result.equity.index[0]:%Y-%m-%d} -> {result.equity.index[-1]:%Y-%m-%d}"
    table = Table(title=f"Walk-forward backtest {cfg.symbol} {cfg.timeframe}  ({period}, out-of-sample only)")
    table.add_column("metric")
    table.add_column("V-trade", justify="right")
    table.add_column("buy & hold", justify="right")
    for k, v in result.metrics.items():
        table.add_row(k, fmt(k, v), fmt(k, result.benchmark_metrics.get(k)))
    table.add_row("final equity", f"{result.equity.iloc[-1]:,.2f}", f"{result.benchmark.iloc[-1]:,.2f}")
    table.add_row("model AUC", fmt("auc", summary["model_report"].get("auc")), "")
    console.print(table)
    if result.halted:
        console.print(f"[red]Kill switch fired:[/] {result.halted}")
    console.print("[dim]Includes fees and slippage from config.yaml. Past performance does not predict future results.[/]")
    if not args.synthetic:
        console.print(f"Saved trades, equity and summary to {cfg.reports_dir}")
    return 0


def cmd_signal(cfg: Config, args) -> int:
    from vtrade.data import MarketFeed
    from vtrade.features import build_features
    from vtrade.model import SignalModel
    from vtrade.strategy import decide

    model = SignalModel.load(cfg.model_path)
    candles = MarketFeed(cfg).recent_candles(cfg.engine.warmup_bars)
    features = build_features(candles)
    last = features.iloc[[-1]]
    prob = float(model.predict_proba(last).iloc[0])
    decision = decide(cfg.strategy, prob, float(last["dist_ema_200"].iloc[0]), in_position=False)
    console.print(
        f"{cfg.symbol} {cfg.timeframe} bar {candles.index[-1]:%Y-%m-%d %H:%M} UTC  close {candles['close'].iloc[-1]:,.2f}\n"
        f"P(up > {cfg.model.label_threshold:.2%} in {cfg.model.horizon} bars) = [bold]{prob:.3f}[/]  ->  "
        f"[bold]{decision.action.value.upper()}[/] ({decision.reason})"
    )
    return 0


def _run_engine(cfg: Config, args, mode: str) -> int:
    from vtrade.broker.paper import PaperBroker
    from vtrade.data import MarketFeed, make_exchange
    from vtrade.engine import Engine, load_state, state_path
    from vtrade.journal import Journal
    from vtrade.copytrade import TopTraders
    from vtrade.llm import ClaudeAnalyst
    from vtrade.model import SignalModel
    from vtrade.news import NewsCalendar

    model = SignalModel.load(cfg.model_path)
    if args.reset and state_path(cfg, mode).exists():
        state_path(cfg, mode).unlink()
        console.print(f"Reset {mode} account to {cfg.risk.starting_equity:,.2f}.")

    if mode == "live":
        creds = exchange_credentials()
        if not creds.get("apiKey"):
            console.print("[red]EXCHANGE_API_KEY / EXCHANGE_API_SECRET are not set in .env[/]")
            return 2
        exchange = make_exchange(cfg, creds)
        from vtrade.broker.exchange import ExchangeBroker

        broker = ExchangeBroker(cfg, exchange)
        console.print(LIVE_WARNING.format(exchange=cfg.exchange.id + (" TESTNET" if cfg.exchange.sandbox else ""),
                                          symbol=cfg.symbol, equity=cfg.risk.starting_equity, quote=broker.quote))
        if not args.yes:
            answer = console.input("Type [bold]LIVE[/] to start: ")
            if answer.strip() != "LIVE":
                console.print("Aborted.")
                return 1
        feed = MarketFeed(cfg, exchange)
    else:
        broker = PaperBroker(cfg.costs)
        feed = MarketFeed(cfg)

    analyst = ClaudeAnalyst(cfg.llm) if cfg.llm.enabled else None
    news = NewsCalendar(cfg) if cfg.news.enabled else None
    leaders = TopTraders(cfg) if cfg.copy.enabled and cfg.copy.mode != "off" else None
    journal = Journal(cfg.data_dir / "journal.db", mode, cfg.symbol)
    engine = Engine(cfg, mode, broker, feed, model, journal, analyst, state=load_state(cfg, mode), news=news, leaders=leaders)
    if args.reset_halt:
        engine.risk.reset_halt()
        engine.save()
        console.print("Kill switch reset.")
    trained = model.meta.get("train_end", "?")
    console.print(f"Model trained through {trained}. Claude reviewer: {'on' if analyst else 'off'}. "
                  f"News blackout: {'on' if news else 'off'}. Copy top traders: {cfg.copy.mode}.")
    try:
        engine.run()
    except KeyboardInterrupt:
        engine.save()
        console.print("\nStopped. State saved.")
    finally:
        journal.close()
    return 0


def cmd_paper(cfg: Config, args) -> int:
    return _run_engine(cfg, args, "paper")


def cmd_live(cfg: Config, args) -> int:
    return _run_engine(cfg, args, "live")


def cmd_status(cfg: Config, args) -> int:
    from vtrade.engine import load_state
    from vtrade.journal import Journal

    state = load_state(cfg, args.mode)
    if state is None:
        console.print(f"No {args.mode} state yet for {cfg.slug}. Start with `vtrade {args.mode}`.")
        return 0
    price = None
    try:
        from vtrade.data import MarketFeed

        price = MarketFeed(cfg).last_price()
    except Exception as exc:
        console.print(f"[yellow]Could not fetch live price: {exc}[/]")

    pos = state.position
    table = Table(title=f"{args.mode.upper()} account  {cfg.symbol}")
    table.add_column("field")
    table.add_column("value", justify="right")
    table.add_row("cash", f"{state.cash:,.2f}")
    table.add_row("realized P&L", f"{state.realized_pnl:+,.2f}")
    if pos:
        table.add_row("position", f"{pos.qty:.6f} @ {pos.entry_price:,.2f}")
        table.add_row("stop / target", f"{pos.stop:,.2f} / {pos.take_profit:,.2f}")
        table.add_row("bars held", str(pos.bars_held))
    else:
        table.add_row("position", "flat")
    if price:
        equity = state.cash + (pos.qty * price if pos else 0)
        table.add_row("last price", f"{price:,.2f}")
        table.add_row("equity", f"{equity:,.2f}")
        if pos:
            table.add_row("unrealized P&L", f"{(price - pos.entry_price) * pos.qty - pos.entry_fee:+,.2f}")
    risk = state.risk or {}
    table.add_row("last bar", str(state.last_bar))
    table.add_row("kill switch", f"[red]HALTED[/] {risk.get('halt_reason')}" if risk.get("halted") else "ok")
    console.print(table)

    journal = Journal(cfg.data_dir / "journal.db", args.mode, cfg.symbol)
    fills = journal.recent_fills(10)
    if not fills.empty:
        console.print("Recent fills:")
        console.print(fills.to_string(index=False))
    journal.close()
    return 0


def cmd_dashboard(cfg: Config, args) -> int:
    import threading
    import webbrowser

    import uvicorn

    from vtrade.web.server import create_app

    url = f"http://127.0.0.1:{args.port}"
    console.print(f"V-trade dashboard on [bold]{url}[/] (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_publish(cfg: Config, args) -> int:
    import shutil
    import subprocess

    from vtrade.config import ROOT
    from vtrade.web.export import export_site

    out = ROOT / args.out
    with console.status("Building the read-only snapshot (live signal, news, top traders)..."):
        result = export_site(cfg, out)
    console.print(f"Snapshot written to {out}")
    for name, why in result["failed"].items():
        console.print(f"[yellow]  {name}: {why}[/]")
    if args.no_deploy:
        return 0
    npx = shutil.which("npx")
    if not npx:
        console.print("[red]npx not found. Install Node.js, then run: npx netlify-cli deploy --dir site --prod[/]")
        return 2
    if not (ROOT / ".netlify" / "state.json").exists():
        console.print("[red]This folder isn't linked to a Netlify site yet. Run: npx netlify-cli link  (or sites:create)[/]")
        return 2
    console.print("Deploying to Netlify...")
    for attempt in range(3):  # netlify-cli sometimes fails transiently with "403 fetching extensions"
        done = subprocess.run([npx, "--yes", "netlify-cli", "deploy", "--no-build", "--dir", str(out), "--prod"], cwd=ROOT)
        if done.returncode == 0:
            return 0
        console.print(f"[yellow]Deploy failed (attempt {attempt + 1}/3), retrying...[/]")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vtrade", description="V-trade AI crypto trading bot")
    parser.add_argument("--config", help="path to config.yaml (default: project root)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"vtrade {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="download/refresh historical candles")
    p.add_argument("--since", help="start date, e.g. 2021-01-01 (ignored when a cache exists)")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("train", help="evaluate walk-forward and train the model used for trading")
    p.add_argument("--synthetic", action="store_true", help="use generated data (offline demo)")
    p.add_argument("--no-eval", action="store_true", help="skip the walk-forward evaluation")
    p.set_defaults(func=cmd_train)

    p = sub.add_parser("backtest", help="walk-forward, out-of-sample backtest with fees and slippage")
    p.add_argument("--synthetic", action="store_true", help="use generated data (offline demo)")
    p.add_argument("--no-kill-switch", action="store_true", help="ignore max_drawdown to see the whole period")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("signal", help="show the model's current signal")
    p.set_defaults(func=cmd_signal)

    for name, helptext in (("paper", "trade live prices with a simulated account"), ("live", "trade real money")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--reset", action="store_true", help="delete saved state and start fresh")
        p.add_argument("--reset-halt", action="store_true", help="clear a fired kill switch")
        if name == "live":
            p.add_argument("--yes", action="store_true", help="skip the interactive LIVE confirmation")
        p.set_defaults(func=cmd_paper if name == "paper" else cmd_live)

    p = sub.add_parser("dashboard", help="open the web dashboard (everything in one place)")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("publish", help="publish a read-only snapshot of the dashboard to Netlify")
    p.add_argument("--out", default="site", help="folder for the static site (default: site)")
    p.add_argument("--no-deploy", action="store_true", help="only build the folder, don't deploy")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("status", help="show account, position and recent fills")
    p.add_argument("--mode", choices=["paper", "live"], default="paper")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        return 2
    setup_logging(cfg, args.verbose)
    started = time.perf_counter()
    try:
        code = args.func(cfg, args)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    if args.command in {"train", "backtest", "fetch"}:
        console.print(f"[dim]done in {time.perf_counter() - started:.1f}s[/]")
    return code


if __name__ == "__main__":
    sys.exit(main())
