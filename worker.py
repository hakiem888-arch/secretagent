"""Persistent 24/7 headless worker: python worker.py --interval 300 --medium-liquidity-fallback.

BUG-18, BUG-21, BUG-22, BUG-25, BUG-36, BUG-40, BUG-41:
- Runs independently of any browser/Streamlit session or ADMIN login token expiry (`BUG-36`).
- Resolves centralized database config (`resolve_database_config`) and propagates `pg_url`
  and `secret_getter` to `PaperLedger`, `SecurityManager`, and `TelegramAlerts`.
- Dynamically synchronizes `ScannerConfig` (including `scan_interval`) from the global
  `user_preferences` table on every cycle without requiring a worker restart (`BUG-40`).
- Enforces single active scanner per database via ProcessLock + DB lease lock.
- Records worker heartbeats and scan reports into global `scanner_runs` (`BUG-41`).
"""
from dataclasses import replace
import argparse
import inspect
import json
import os
import signal
import threading
import time
import tomllib
from uuid import uuid4
from pathlib import Path
from futures.db_backend import configure_database_runtime, load_global_preferences, resolve_database_config
from futures.groq_rotation import GroqRotation, load_keys, DEFAULT_MODEL
from futures.provider_router import FuturesProviderRouter
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.spot_market import futures_db_path, resolve_db_path
from futures.security import SecurityManager
from futures.telegram import TelegramAlerts
from futures.scanner import ALLOWED_SCAN_INTERVALS
from futures.version import APP_VERSION


def sync_worker_config_from_preferences(engine, base_config, user_id="global_operator"):
    """BUG-40: Dynamically synchronize worker ScannerConfig (including interval) from global `user_preferences`."""
    try:
        if hasattr(engine, "ledger") and hasattr(engine.ledger, "load_ui_preferences"):
            prefs = engine.ledger.load_ui_preferences(user_id=user_id)
        else:
            db_path = getattr(getattr(engine, "ledger", None), "db", None)
            pg_url = getattr(getattr(engine, "ledger", None), "pg_url", None)
            secret_getter = getattr(getattr(engine, "ledger", None), "secret_getter", None)
            prefs = load_global_preferences(db_path, user_id=user_id, pg_url=pg_url, get_secret=secret_getter)
    except Exception:
        return base_config
    if not isinstance(prefs, dict) or not prefs.get("_exists"):
        return base_config
    interval_val = int(prefs.get("scan_interval", prefs.get("interval", base_config.interval)))
    if interval_val not in ALLOWED_SCAN_INTERVALS:
        interval_val = base_config.interval
    candidates_val = int(prefs.get("candidates", base_config.candidates))
    if candidates_val not in (1, 2):
        candidates_val = base_config.candidates
    return replace(
        base_config,
        interval=interval_val,
        candidates=candidates_val,
        min_volume=float(prefs.get("min_volume", prefs.get("volume", base_config.min_volume))),
        fee=float(prefs.get("fee_pct", prefs.get("fee", base_config.fee * 100.0))) / 100.0,
        slippage_bps=float(prefs.get("slippage_bps", prefs.get("slip", base_config.slippage_bps))),
        funding=float(prefs.get("funding_pct", prefs.get("funding", base_config.funding * 100.0))) / 100.0,
        leverage=int(prefs.get("leverage", base_config.leverage)),
        risk_pct=float(prefs.get("risk_pct", prefs.get("risk", base_config.risk_pct))),
        send_alerts=bool(prefs.get("send_alerts", prefs.get("alert", base_config.send_alerts))),
        send_watchlist_alerts=bool(prefs.get("send_watchlist_alerts", prefs.get("watchlist_alert", base_config.send_watchlist_alerts))),
        send_scan_summary=bool(prefs.get("send_scan_summary", prefs.get("summary_alert", base_config.send_scan_summary))),
        relaxed_mode=bool(prefs.get("relaxed_mode", prefs.get("relaxed", base_config.relaxed_mode))),
    )


def create_worker_engine(root=None, settings=None, interval=300, candidates=2, no_telegram=False, medium_fallback=False):
    """Factory shared by worker CLI and parity tests so worker and dashboard instantiate identical components."""
    root = Path(root or Path(__file__).resolve().parent)
    if settings is None:
        path = root / ".streamlit" / "secrets.toml"
        settings = tomllib.loads(path.read_text()) if path.exists() else {}
    get = lambda name: settings.get(name, os.getenv(name, ""))
    configure_database_runtime(secret_getter=get)
    db_cfg = resolve_database_config(get_secret=get)
    pg_url = db_cfg["pg_url"]
    base_db = Path(get("FUTURES_HISTORY_DB") or futures_db_path(root))
    market = FuturesProviderRouter()
    initial_kind = getattr(market, "instrument_kind", "USD_M_PERPETUAL")
    resolved_db = resolve_db_path(root, initial_kind, base_db=base_db)
    initial_equity = float(get("PAPER_INITIAL_EQUITY") or 1000)
    ledger = PaperLedger(
        resolved_db,
        initial_equity,
        expected_instrument_kind=initial_kind,
        pg_url=pg_url,
        secret_getter=get,
    )
    security = SecurityManager(
        resolved_db,
        admin_token=get("COINAGENT_ADMIN_TOKEN") or get("ADMIN_TOKEN"),
        admin_password_hash=get("COINAGENT_ADMIN_PASSWORD_HASH") or get("ADMIN_PASSWORD_HASH"),
        pg_url=pg_url,
        secret_getter=get,
    )
    pool = GroqRotation(load_keys(get), security=security)
    alerts = TelegramAlerts(
        get("TELEGRAM_BOT_TOKEN"),
        get("TELEGRAM_CHAT_ID"),
        resolved_db,
        security=security,
        pg_url=pg_url,
        secret_getter=get,
    )
    engine = MarketScanner(
        market,
        pool,
        NewsCalendar(),
        ledger,
        alerts,
        str(get("GROQ_MODEL") or DEFAULT_MODEL),
        security=security,
        trusted_worker=True,
    )
    for kind in ("USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL", "SPOT_REFERENCE"):
        engine.ledger_for(kind)
    relaxed_env = str(get("MEDIUM_LIQUIDITY_FALLBACK")).lower() in ("1", "true", "yes", "on")
    base_config = ScannerConfig(
        candidates=candidates,
        interval=interval,
        send_alerts=not no_telegram,
        send_watchlist_alerts=not no_telegram,
        send_scan_summary=not no_telegram,
        relaxed_mode=bool(medium_fallback or relaxed_env),
    )
    config = sync_worker_config_from_preferences(engine, base_config)
    return engine, config


def main():
    parser = argparse.ArgumentParser(description="Automatic USDT perpetual PAPER worker (Binance primary, Bybit fallback); no live exchange orders")
    parser.add_argument("--interval", type=int, choices=list(ALLOWED_SCAN_INTERVALS), default=300)
    parser.add_argument("--candidates", type=int, choices=[1, 2], default=2)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--medium-liquidity-fallback", "--relaxed", dest="medium_fallback", action="store_true", help="Allow 5M USDT liquidity fallback for watchlist only; entry gates remain strict")
    args = parser.parse_args()
    engine, config = create_worker_engine(
        interval=args.interval,
        candidates=args.candidates,
        no_telegram=args.no_telegram,
        medium_fallback=args.medium_fallback,
    )
    worker_id = f"worker-{uuid4().hex[:8]}"
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        started = time.monotonic()
        try:
            config = sync_worker_config_from_preferences(engine, config)
            if hasattr(engine.ledger, "record_worker_heartbeat"):
                engine.ledger.record_worker_heartbeat(worker_id, int(time.time() * 1000), config.interval)
            run_params = inspect.signature(engine.run).parameters
            if "worker_id" in run_params:
                report = engine.run(config, worker_id=worker_id)
            else:
                report = engine.run(config)
            print(json.dumps({"scope": "PAPER", "version": APP_VERSION, "worker_id": worker_id,
                "provider": engine.market.market_label,
                "instrument_kind": report.get("instrument_kind"), "ledger_db": report.get("ledger_db"),
                "scan_id": report.get("scan_id"),
                "started_at_ms": report.get("started_at_ms"), "scan_status": report.get("scan_status"),
                "data_status": report.get("data_status", "OK"),
                "configured_interval": report.get("configured_interval", config.interval),
                "effective_interval": report.get("effective_interval", config.interval),
                "next_scan_at_ms": report.get("next_scan_at_ms", 0),
                "market_error": report.get("market_error", {}),
                "at_ms": report.get("now_ms"), "universe": report.get("universe_count"),
                "shortlist": len(report.get("shortlist", [])), "candidates": len(report.get("selected", [])),
                "analyses": len(report.get("analyses", [])), "errors": report.get("errors", [])}, ensure_ascii=False), flush=True)
            if report.get("market_error", {}).get("access_blocked"):
                print(json.dumps({"scope": "PAPER", "status": "STOPPED_HTTP_451",
                    "message": "Kedua provider gagal. Periksa akses jaringan dan pembatasan layanan; worker dihentikan."}), flush=True)
                return 2
        except Exception as exc:
            print(json.dumps({"scope": "PAPER", "status": "WAIT", "error_type": type(exc).__name__}), flush=True)
        if args.once:
            break
        elapsed = time.monotonic() - started
        wait_s = max(1, config.interval - elapsed) if elapsed < config.interval else config.interval
        stop.wait(wait_s)


if __name__ == "__main__":
    raise SystemExit(main())
