from pathlib import Path
from streamlit.testing.v1 import AppTest
from futures.market_scanner import MarketScanner
from futures.market import MarketError
from futures.spot_market import SpotReferenceMarket
from futures.news import NewsCalendar
from tests.fixtures import NOW

APP = Path(__file__).resolve().parents[1] / "app.py"


def by_label(elements, label):
    return next(x for x in elements if x.label == label)


def admin_app(monkeypatch, tmp_path):
    """Create an AppTest instance authenticated as ADMIN so privileged scan controls can be tested."""
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(tmp_path / "paper.db"))
    monkeypatch.setenv("COINAGENT_ADMIN_TOKEN", "test-admin-secret-2026")
    at = AppTest.from_file(str(APP), default_timeout=20)
    at.session_state["admin_auto_authenticate_token"] = "test-admin-secret-2026"
    return at


def test_public_viewer_defaults_off_and_cannot_trigger_server_actions(monkeypatch, tmp_path):
    """BUG-15 & BUG-16: Unauthenticated public visitor is VIEWER; privileged toggles default to False and scans are blocked."""
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(tmp_path / "viewer.db"))
    monkeypatch.delenv("COINAGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    calls = []
    monkeypatch.setattr(MarketScanner, "run", lambda self, cfg, manual=False, **kw: calls.append(cfg) or {})
    at = AppTest.from_file(str(APP), default_timeout=20).run()
    assert not at.exception
    # Public viewer has all privileged toggles OFF by default
    assert by_label(at.toggle, "Auto-scan seluruh market").value is False
    assert by_label(at.toggle, "Alert Telegram PAPER BUY/SELL").value is False
    assert by_label(at.toggle, "Alert Telegram REVERSAL WATCHLIST").value is False
    assert by_label(at.toggle, "Kirim MARKET SCANNER REPORT setiap siklus").value is False
    assert len(calls) == 0
    # Clicking manual scan as unauthenticated viewer is denied on server side
    by_label(at.button, "Scan sekarang").click().run()
    assert len(calls) == 0
    assert any("UNAUTHORIZED" in x.value for x in at.warning)


def test_dashboard_needs_no_pair_or_api_inputs_and_can_stop(monkeypatch, tmp_path):
    calls = []
    def run(self, config, manual=False, **kwargs):
        calls.append(config)
        return {"now_ms": NOW, "universe_count": 300, "eligible_count": 50, "shortlist": [],
                "selected": [], "analyses": [], "errors": [], "duration_seconds": 1}
    monkeypatch.setattr(MarketScanner, "run", run)
    at = admin_app(monkeypatch, tmp_path).run()
    assert not at.exception
    assert not at.text_input
    assert not any(x.label == "Mode" for x in at.selectbox)
    assert by_label(at.toggle, "Auto-scan seluruh market").value
    assert by_label(at.selectbox, "Interval scan (detik)").options == ["1m (60 detik)", "5m (300 detik)", "15m (900 detik)"]
    assert [x.label for x in at.tabs] == ["Scanner market", "Paper trading", "Riwayat analisis", "Aturan"]
    assert len(calls) == 1
    at.run(); assert len(calls) == 1
    by_label(at.selectbox, "Interval scan (detik)").set_value(300).run()
    assert not at.exception and calls[-1].interval == 300
    by_label(at.toggle, "Auto-scan seluruh market").set_value(False).run()
    count = len(calls)
    at.session_state["market_schedule"].next_due = 0
    at.run(); assert len(calls) == count
    by_label(at.button, "Scan sekarang").click().run()
    assert not at.exception and len(calls) == count + 1


def test_dashboard_renders_error_and_journal_backup(monkeypatch, tmp_path):
    monkeypatch.setattr(MarketScanner, "run", lambda *_args, **_kwargs: {"now_ms": NOW, "errors": ["WAIT: HTTP 451"]})
    at = admin_app(monkeypatch, tmp_path).run()
    assert not at.exception
    assert any("451" in x.value for x in at.warning)
    assert any(x.label == "Unduh cadangan jurnal lengkap" for x in at.get("download_button"))


def test_real_scanner_451_records_button_clicks_and_pauses_automatic_requests(monkeypatch, tmp_path):
    calls = []
    def blocked(self, path, params=None):
        calls.append(path)
        raise MarketError("HTTP 451", http_status=451, endpoint=path)
    monkeypatch.setattr(SpotReferenceMarket, "get", blocked)
    at = admin_app(monkeypatch, tmp_path).run()
    assert not at.exception and calls == ["/api/v3/time"]
    assert any("SCANNER SPOT" in x.value for x in at.info)
    first = at.session_state["market_report"]
    assert first["scan_status"] == "FAILED" and first["started_at_ms"] > 0
    assert [metric.value for metric in at.metric[:4]] == ["N/A"] * 4
    assert any("Percobaan scan terakhir:" in x.value for x in at.caption)
    assert not any("Belum scan" in x.value for x in at.caption)
    assert not any("Snapshot hasil scan" in x.value for x in at.info)
    assert any("DIJEDA (HTTP 451)" in x.value for x in at.caption)
    at.session_state["market_schedule"].next_due = 0
    at.run()
    assert len(calls) == 1
    for expected in (2, 3):
        by_label(at.button, "Scan sekarang").click().run()
        assert not at.exception and len(calls) == expected
        report = at.session_state["market_report"]
        assert report["scan_id"] != first["scan_id"] and report["trigger"] == "MANUAL"
        first = report
    by_label(at.toggle, "Auto-scan seluruh market").set_value(False).run()
    by_label(at.button, "Scan sekarang").click().run()
    assert not at.exception and len(calls) == 4
    assert not at.session_state["market_schedule"].running


def test_manual_recovery_clears_access_error_and_renders_market(monkeypatch, tmp_path):
    from tests.test_screener import universe
    calls = []
    def recover(self, path, params=None):
        calls.append(path)
        if len(calls) == 1:
            raise MarketError("HTTP 451", http_status=451, endpoint=path)
        return {"serverTime": NOW}
    monkeypatch.setattr(SpotReferenceMarket, "get", recover)
    monkeypatch.setattr(SpotReferenceMarket, "universe", lambda self: universe(120))
    monkeypatch.setattr(NewsCalendar, "window", lambda self, now: {"ok": False, "reason": "Fixture calendar"})
    at = admin_app(monkeypatch, tmp_path).run()
    by_label(at.button, "Scan sekarang").click().run()
    assert not at.exception and len(calls) == 2
    assert at.session_state["market_report"]["market_error"] == {}
    assert at.metric[0].value == "120"
    assert not any("451" in x.value for x in at.error)
    assert any("auto-scan ON" in x.value for x in at.caption)


def test_failed_exchange_info_does_not_claim_a_market_snapshot_exists(monkeypatch, tmp_path):
    def fail_later(self, path, params=None):
        if path.endswith("/time"):
            return {"serverTime": NOW}
        raise MarketError("HTTP 451", http_status=451, endpoint=path)
    monkeypatch.setattr(SpotReferenceMarket, "get", fail_later)
    at = admin_app(monkeypatch, tmp_path).run()
    assert not at.exception
    assert at.session_state["market_report"]["now_ms"] == NOW
    assert at.metric[0].value == "N/A"
    assert any("Waktu bursa diterima" in x.value for x in at.info)
    assert not any("Snapshot hasil scan" in x.value for x in at.info)
