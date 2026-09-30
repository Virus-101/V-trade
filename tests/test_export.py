import json

from vtrade.copytrade import TopTraders
from vtrade.news import NewsCalendar
from vtrade.web import export


def test_export_builds_a_read_only_snapshot(cfg, tmp_path, monkeypatch):
    real_create_app = export.create_app
    monkeypatch.setattr(export, "create_app", lambda c: real_create_app(
        c,
        news=NewsCalendar(c, fetch=lambda url: []),
        traders=TopTraders(c, get=lambda url: {"leaderboardRows": []}, post=lambda url, payload: {}),
    ))
    out = tmp_path / "site"
    result = export.export_site(cfg, out)

    page = (out / "index.html").read_text(encoding="utf-8")
    assert "window.VTRADE_STATIC" in page and page.index("VTRADE_STATIC") < page.index("/static/app.js")
    assert (out / "static" / "app.js").exists() and not (out / "static" / "index.html").exists()
    overview = json.loads((out / "api" / "overview.json").read_text())
    assert overview["job"] is None and overview["market"]["symbol"] == cfg.symbol
    # No model in a fresh folder: the signal snapshot records why instead of failing the export.
    assert json.loads((out / "api" / "signal.json").read_text())["unavailable"] is True
    assert "signal" in result["failed"]
    assert "noindex" in (out / "_headers").read_text()
