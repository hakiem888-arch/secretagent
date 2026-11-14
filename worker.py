"""Optional persistent-host worker: python worker.py --interval 300 --medium-liquidity-fallback.

Reads local .streamlit/secrets.toml or environment variables. Community Cloud
Secrets cannot be read remotely by this process on another host.
"""
import argparse
import json
import os
import signal
import threading
import time
import tomllib
from pathlib import Path
from futures.groq_rotation import GroqRotation, load_keys, DEFAULT_MODEL
from futures.provider_router import FuturesProviderRouter
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.spot_market import futures_db_path, resolve_db_path
from futures.security import SecurityManager
from futures.telegram import TelegramAlerts
from futures.scanner import ALLOWED_SCAN_INTERVALS


def create_worker_engine(root=None, settings=None, interval=300, candidates=2, no_telegram=False, medium_fallback=False):
    """Factory shared by worker CLI and parity tests so worker and dashboard instantiate identical components."""
    root = Path(root or Path(__file__).resolve().parent)
    if settings is None:
        path = root / ".streamlit" / "secrets.toml"
        settings = tomllib.loads(path.read_text()) if path.exists() else {}
    get = lambda name: settings.get(name, os.getenv(name, ""))
    base_db = Path(get("FUTURES_HISTORY_DB") or futures_db_path(root))
    market = FuturesProviderRouter()
    initial_kind = getattr(market, "instrument_kind", "USD_M_PERPETUAL")
    resolved_db = resolve_db_path(root, initial_kind, base_db=base_db)
    initial_equity = float(get("PAPER_INITIAL_EQUITY") or 1000)
    ledger = PaperLedger(resolved_db, initial_equity, expected_instrument_kind=initial_kind)
    pool = GroqRotation(load_keys(get))
    security = SecurityManager(resolved_db)
    engine = MarketScanner(market, pool, NewsCalendar(), ledger,
                           TelegramAlerts(get("TELEGRAM_BOT_TOKEN"), get("TELEGRAM_CHAT_ID"), resolved_db),
                           str(get("GROQ_MODEL") or DEFAULT_MODEL),
                           security=security)
    for kind in ("USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL", "SPOT_REFERENCE"):
        engine.ledger_for(kind)
    relaxed_env = str(get("MEDIUM_LIQUIDITY_FALLBACK")).lower() in ("1", "true", "yes", "on")
    config = ScannerConfig(candidates=candidates, interval=interval,
                           send_alerts=not no_telegram,
                           send_watchlist_alerts=not no_telegram,
                           send_scan_summary=not no_telegram,
                           relaxed_mode=bool(medium_fallback or relaxed_env))
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
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        started = time.monotonic()
        try:
            report = engine.run(config)
            print(json.dumps({"scope": "PAPER", "provider": engine.market.market_label,
                "instrument_kind": report.get("instrument_kind"), "ledger_db": report.get("ledger_db"),
                "scan_id": report.get("scan_id"),
                "started_at_ms": report.get("started_at_ms"), "scan_status": report.get("scan_status"),
                "data_status": report.get("data_status", "OK"),
                "market_error": report.get("market_error", {}),
                "at_ms": report.get("now_ms"), "universe": report.get("universe_count"),
                "shortlist": len(report.get("shortlist", [])), "candidates": len(report.get("selected", [])),
                "analyses": len(report.get("analyses", [])), "errors": report.get("errors", [])}, ensure_ascii=False), flush=True)
            if report.get("market_error", {}).get("access_blocked"):
                print(json.dumps({"scope": "PAPER", "status": "STOPPED_HTTP_451",
                    "message": "Kedua provider gagal. Periksa akses jaringan dan pembatasan layanan; worker dihentikan."}), flush=True)
                return 2
        except Exception as exc:
            # Never print provider responses, tokens, or credential-bearing URLs.
            print(json.dumps({"scope": "PAPER", "status": "WAIT", "error_type": type(exc).__name__}), flush=True)
        if args.once:
            break
        elapsed = time.monotonic() - started
        # No catch-up loop if a cycle took longer than the configured interval.
        stop.wait(max(1, args.interval - elapsed) if elapsed < args.interval else args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
