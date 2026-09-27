"""FastAPI backend for the dashboard. Serves the single-page app in ./static and a small JSON API.

The server only listens on 127.0.0.1. POST requests must carry an `X-VTrade: 1` header (a custom
header forces a CORS preflight, so other websites can't trigger actions) and the Host header must be
local (blocks DNS-rebinding). Live trading is deliberately not exposed here: it stays in the CLI.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from vtrade import __version__, services
from vtrade.config import Config
from vtrade.engine import EngineState, load_state, state_path
from vtrade.journal import Journal

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "testserver"}
SIGNAL_TTL = 30.0

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LogBuffer(logging.Handler):
    """Keeps the most recent log lines in memory so the dashboard can show a live log."""

    def __init__(self, size: int = 400):
        super().__init__(level=logging.INFO)
        self.lines: deque[dict[str, Any]] = deque(maxlen=size)
        self.counter = 0
        # Not `self.lock`: logging.Handler already uses that name for the lock it holds around emit().
        self._buffer_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        with self._buffer_lock:
            self.counter += 1
            self.lines.append({
                "id": self.counter,
                "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
                "level": record.levelname,
                "msg": message,
            })

    def since(self, after: int) -> list[dict[str, Any]]:
        with self._buffer_lock:
            return [line for line in self.lines if line["id"] > after]


class JobRunner:
    """Runs one long task (fetch, train, backtest) at a time in a background thread."""

    def __init__(self, on_done: Callable[[str], None]):
        self.lock = threading.Lock()
        self.job: dict[str, Any] | None = None
        self.on_done = on_done

    def start(self, kind: str, fn: Callable[[Callable[[int, int], None]], Any]) -> dict[str, Any]:
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise HTTPException(409, f"'{self.job['kind']}' is still running")
            job: dict[str, Any] = {"kind": kind, "status": "running", "progress": None, "started": _now(),
                                   "finished": None, "result": None, "error": None}
            self.job = job

        def progress(done: int, total: int) -> None:
            job["progress"] = {"done": done, "total": total}

        def run() -> None:
            try:
                job["result"] = services.clean(fn(progress))
                job["status"] = "done"
                log.info("%s finished", kind)
            except Exception as exc:
                log.exception("%s failed", kind)
                job["error"] = str(exc)
                job["status"] = "error"
            finally:
                job["finished"] = _now()
                self.on_done(kind)

        log.info("%s started", kind)
        threading.Thread(target=run, daemon=True, name=f"job-{kind}").start()
        return job


class PaperRunner:
    """The paper-trading engine, running in a background thread of the dashboard process."""

    def __init__(self, cfg: Config, feed_factory: Callable[[], Any]):
        self.cfg = cfg
        self.feed_factory = feed_factory
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.started_at: str | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        from vtrade.model import SignalModel

        if self.running:
            return
        model = SignalModel.load(self.cfg.model_path)  # fail fast (no model yet) in the request
        self.stop_event = threading.Event()
        self.error = None
        self.started_at = _now()
        self.thread = threading.Thread(target=self._run, args=(model,), daemon=True, name="paper-engine")
        self.thread.start()

    def _run(self, model) -> None:
        from vtrade.broker.paper import PaperBroker
        from vtrade.engine import Engine
        from vtrade.llm import ClaudeAnalyst

        journal = Journal(self.cfg.data_dir / "journal.db", "paper", self.cfg.symbol)
        try:
            analyst = ClaudeAnalyst(self.cfg.llm) if self.cfg.llm.enabled else None
            engine = Engine(self.cfg, "paper", PaperBroker(self.cfg.costs), self.feed_factory(), model, journal, analyst)
            engine.run(self.stop_event)
        except Exception as exc:
            log.exception("Paper engine crashed")
            self.error = str(exc)
        finally:
            journal.close()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=15)
        self.started_at = None


def create_app(cfg: Config, feed_factory: Callable[[], Any] | None = None) -> FastAPI:
    from vtrade.data import MarketFeed
    from vtrade.model import SignalModel

    feed_factory = feed_factory or (lambda: MarketFeed(cfg))
    paper = PaperRunner(cfg, feed_factory)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if paper.running:  # stop the engine thread cleanly when the server shuts down
            paper.stop()

    app = FastAPI(title="V-trade", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)

    logbuf = LogBuffer()
    logging.getLogger().addHandler(logbuf)
    if logging.getLogger().level > logging.INFO or logging.getLogger().level == logging.NOTSET:
        logging.getLogger().setLevel(logging.INFO)

    cache: dict[str, Any] = {"signal": None, "signal_at": 0.0, "signal_error": None,
                             "model": None, "model_mtime": None, "backtest": None, "backtest_mtime": None}
    signal_lock = threading.Lock()
    feed_holder: dict[str, Any] = {}

    def invalidate(kind: str) -> None:
        cache["signal_at"] = 0.0
        if kind in ("train",):
            cache["model_mtime"] = None
        if kind in ("backtest",):
            cache["backtest_mtime"] = None

    jobs = JobRunner(on_done=invalidate)
    app.state.paper = paper
    app.state.jobs = jobs

    def model() -> SignalModel | None:
        path = cfg.model_path
        if not path.exists():
            return None
        mtime = path.stat().st_mtime
        if cache["model_mtime"] != mtime:
            cache["model"], cache["model_mtime"] = SignalModel.load(path), mtime
        return cache["model"]

    # ------------------------------------------------------------------ security
    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]")
        if host not in LOCAL_HOSTS:
            return JSONResponse({"detail": "Forbidden host"}, status_code=403)
        if request.method == "POST" and request.headers.get("x-vtrade") != "1":
            return JSONResponse({"detail": "Missing X-VTrade header"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"  # always revalidate after an update
        return response

    # ------------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ------------------------------------------------------------------ overview & config
    @app.get("/api/overview")
    def overview():
        m = model()
        state = load_state(cfg, "paper")
        live_state = load_state(cfg, "live")
        summary = services.summary_path(cfg)
        return services.clean({
            "version": __version__,
            "market": {"exchange": cfg.exchange.id, "symbol": cfg.symbol, "timeframe": cfg.timeframe},
            "config": cfg.to_dict(),
            "data": services.data_info(cfg),
            "model": {"available": m is not None, **(m.meta if m else {})},
            "paper": {"running": paper.running, "started_at": paper.started_at, "error": paper.error,
                      "state": _state_dict(state)},
            "live": {"state": _state_dict(live_state)},
            "llm": {"enabled": cfg.llm.enabled, "model": cfg.llm.model, "key_set": bool(os.getenv("ANTHROPIC_API_KEY"))},
            "backtest": {"available": summary.exists(),
                         "generated_at": _mtime_iso(summary)},
            "job": jobs.job,
        })

    # ------------------------------------------------------------------ live signal
    @app.get("/api/signal")
    def signal(refresh: bool = False):
        with signal_lock:
            fresh = time.monotonic() - cache["signal_at"] < SIGNAL_TTL
            if not fresh or refresh:
                m = model()
                if m is None:
                    raise HTTPException(409, "No trained model yet. Update the candles and train the model first.")
                try:
                    feed = feed_holder.setdefault("feed", feed_factory())
                    cache["signal"] = services.live_snapshot(cfg, m, feed)
                    cache["signal_error"] = None
                except Exception as exc:
                    log.warning("Could not fetch live signal: %s", exc)
                    cache["signal_error"] = str(exc)
                cache["signal_at"] = time.monotonic()
            if cache["signal"] is None:
                raise HTTPException(503, cache["signal_error"] or "Signal unavailable")
            return {**cache["signal"], "stale_error": cache["signal_error"]}

    # ------------------------------------------------------------------ backtest report
    @app.get("/api/backtest")
    def backtest_report():
        path = services.summary_path(cfg)
        mtime = path.stat().st_mtime if path.exists() else None
        if cache["backtest_mtime"] != mtime or cache["backtest"] is None:
            cache["backtest"], cache["backtest_mtime"] = services.load_backtest(cfg), mtime
        return cache["backtest"]

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    def job_status():
        return services.clean(jobs.job or {})

    @app.post("/api/jobs/{kind}")
    def start_job(kind: str, options: dict = Body(default={})):
        if kind == "fetch":
            job = jobs.start("fetch", lambda progress: services.fetch(cfg))
        elif kind == "train":
            evaluate = bool(options.get("evaluate", True))
            job = jobs.start("train", lambda progress: services.train(cfg, evaluate=evaluate, on_progress=progress))
        elif kind == "backtest":
            ignore = bool(options.get("ignore_kill_switch", False))
            job = jobs.start("backtest", lambda progress: services.backtest(cfg, ignore_kill_switch=ignore, on_progress=progress)[1])
        else:
            raise HTTPException(404, f"Unknown job {kind!r}")
        return services.clean(job)

    # ------------------------------------------------------------------ paper trading
    @app.get("/api/paper")
    def paper_status():
        state = load_state(cfg, "paper")
        journal = Journal(cfg.data_dir / "journal.db", "paper", cfg.symbol)
        try:
            fills = journal.recent_fills(200)
            events = journal.recent_events(50)
            equity = journal.equity_history(2000)
        finally:
            journal.close()
        closed = fills[fills["side"] == "sell"]
        return services.clean({
            "running": paper.running,
            "started_at": paper.started_at,
            "error": paper.error,
            "starting_equity": cfg.risk.starting_equity,
            "state": _state_dict(state),
            "stats": {
                "closed_trades": int(len(closed)),
                "wins": int((closed["pnl"] > 0).sum()) if len(closed) else 0,
            },
            "fills": fills.to_dict("records"),
            "events": events.to_dict("records"),
            "equity": {"t": equity["ts"].tolist(), "equity": equity["equity"].tolist(),
                       "price": equity["price"].tolist(), "prob": equity["prob"].tolist()},
        })

    @app.post("/api/paper/start")
    def paper_start():
        try:
            paper.start()
        except FileNotFoundError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"running": paper.running}

    @app.post("/api/paper/stop")
    def paper_stop():
        paper.stop()
        return {"running": paper.running}

    @app.post("/api/paper/reset")
    def paper_reset():
        if paper.running:
            raise HTTPException(409, "Stop paper trading before resetting the account.")
        path = state_path(cfg, "paper")
        if path.exists():
            path.unlink()
        log.info("Paper account reset to %.2f", cfg.risk.starting_equity)
        return {"ok": True}

    @app.post("/api/paper/reset-halt")
    def paper_reset_halt():
        if paper.running:
            raise HTTPException(409, "Stop paper trading before clearing the kill switch.")
        state = load_state(cfg, "paper")
        if state and state.risk:
            state.risk.update({"halted": False, "halt_reason": "", "peak_equity": 0.0})
            state_path(cfg, "paper").write_text(state.to_json(), encoding="utf-8")
            log.info("Paper kill switch cleared")
        return {"ok": True}

    # ------------------------------------------------------------------ logs
    @app.get("/api/logs")
    def logs(after: int = 0):
        return {"lines": logbuf.since(after)}

    return app


def _mtime_iso(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def _state_dict(state: EngineState | None) -> dict[str, Any] | None:
    return None if state is None else asdict(state)
