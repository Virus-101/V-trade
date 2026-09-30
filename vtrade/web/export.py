"""Export the dashboard as a static, read-only site (for Netlify or any static host).

Static hosts can't run the Python server, the model or the paper engine, so this calls the
dashboard's own API handlers once and saves each response as a JSON file next to a copy of the
page. The page detects the snapshot and reads those files instead of the live API; its trading
controls are hidden. Re-run it (`vtrade publish`) whenever you want fresh numbers online.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from vtrade import __version__
from vtrade.config import Config
from vtrade.web.server import STATIC, create_app

log = logging.getLogger(__name__)

# API path -> snapshot file name (the page maps /api/a/b to api/a-b.json)
SNAPSHOTS = {
    "/api/overview": "overview",
    "/api/signal": "signal",
    "/api/backtest": "backtest",
    "/api/news": "news",
    "/api/traders": "traders",
    "/api/paper": "paper",
    "/api/paper/settings": "paper-settings",
    "/api/jobs": "jobs",
}


def _call(app, path: str) -> Any:
    for route in app.routes:
        if getattr(route, "path", None) == path and "GET" in getattr(route, "methods", ()):
            return route.endpoint()
    raise KeyError(path)


def _scrub(overview: dict[str, Any]) -> dict[str, Any]:
    """Drop local file paths from what gets published."""
    data = overview.get("data") or {}
    if data.get("path"):
        data["path"] = Path(data["path"]).name
    overview["job"] = None
    return overview


def export_site(cfg: Config, out_dir: Path) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "api").mkdir(parents=True)
    shutil.copytree(STATIC, out_dir / "static", ignore=shutil.ignore_patterns("index.html"))

    app = create_app(cfg)
    written, failed = [], {}
    for path, name in SNAPSHOTS.items():
        try:
            payload = _call(app, path)
        except HTTPException as exc:  # e.g. no model yet: the page shows the same message as locally
            failed[name] = exc.detail
            payload = {"detail": exc.detail, "unavailable": True}
        except Exception as exc:
            log.warning("Snapshot of %s failed: %s", path, exc)
            failed[name] = str(exc)
            payload = {"detail": str(exc), "unavailable": True}
        if name == "overview" and isinstance(payload, dict):
            payload = _scrub(payload)
        (out_dir / "api" / f"{name}.json").write_text(json.dumps(payload, default=str), encoding="utf-8")
        written.append(name)
    (out_dir / "api" / "logs.json").write_text(json.dumps({"lines": []}), encoding="utf-8")

    snapshot = {"generated_at": generated_at, "version": __version__, "symbol": cfg.symbol, "timeframe": cfg.timeframe}
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    marker = '<script src="/static/app.js"></script>'
    page = page.replace(marker, f"<script>window.VTRADE_STATIC = {json.dumps(snapshot)};</script>\n{marker}")
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    # Always revalidate so a re-publish shows up immediately; keep search engines out.
    (out_dir / "_headers").write_text(
        "/*\n  Cache-Control: no-cache\n  X-Robots-Tag: noindex\n  Referrer-Policy: no-referrer\n", encoding="utf-8"
    )
    return {"out_dir": str(out_dir), "generated_at": generated_at, "files": written, "failed": failed}
