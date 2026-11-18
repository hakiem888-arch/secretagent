"""Standalone 24/7 background scanner worker. Does not depend on Streamlit browser tabs or Admin session tokens.

BUG-18, BUG-22, BUG-24, BUG-36, BUG-38 + P0-1..P0-4 + Hybrid Adaptive v1.1 (Section 9) + Final Remediation:
- Enables `FuturesProviderRouter` by default in production worker without relying on optional environment variables
  (Binance USDⓈ-M Futures primary -> Bybit USDT Linear Perpetual secondary -> Spot Reference strictly as reference).
- Enforces `futures_primary_only=True` across all production configurations.
- Aligns auto-scanner execution to exact UTC market candle close boundaries (`align_to_market_time=True`: e.g. M1 at `:00`,
  M5 at exact 5-minute market candle closes, M15 at exact 15-minute market candle closes).
- Propagates `DATABASE_URL` (`pg_url`) and `SupabaseStateSync` across `PaperLedger`,
  `SecurityManager`, `TelegramAlerts`, and `MarketScanner` so worker state and dashboard
  state remain synchronized across reboots.
- Explicitly defaults to `strategy_profile="BALANCED"` and `strategy_engine="HYBRID_ADAPTIVE_V1"`,
  `interval=60`, `candidates=20`, `shortlist_limit=50`, `max_groq_candidates=5`, `risk_pct=0.25`,
  and synchronizes `strategy_profile`, `relaxed_mode`, `shortlist_limit`, and all operator
  preferences from global `user_preferences` on every cycle without losing preferences on restart (`P0-3`).
- Emits verified worker heartbeats every 30 seconds (stale threshold > 90 seconds) with full runtime proof:
  `worker_id`, `last_heartbeat_ms`, `last_scan_ms`, `next_scan_ms`, `effective_strategy_profile`,
  `effective_strategy_engine`, and `database_name` (Section 9).
"""
import argparse
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from futures.db_backend import (
    SupabaseStateSync,
    configured_database_url,
    configured_supabase_rest,
    load_global_preferences,
    persistence_status,
)
from futures.groq_rotation import GroqRotation, load_keys, DEFAULT_MODEL
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.provider_router import FuturesProviderRouter
from futures.scanner import (
    ALLOWED_SCAN_INTERVALS,
    current_market_candle_boundary_ms,
    next_market_aligned_scan_ms,
)
from futures.security import SecurityManager
from futures.spot_market import resolve_db_path
from futures.telegram import TelegramAlerts
from futures.version import APP_VERSION

WORKER_HEARTBEAT_INTERVAL_S = 30
WORKER_STALE_THRESHOLD_S = 90


def sync_worker_config_from_preferences(engine_or_ledger, base_config):
    """BUG-38, P0-3 & v1.1 Section 9: Synchronize worker ScannerConfig with saved global `user_preferences`."""
    try:
        target_ledger = getattr(engine_or_ledger, "ledger", engine_or_ledger)
        prefs = load_global_preferences(
            target_ledger.db,
            pg_url=getattr(target_ledger, "pg_url", None),
            get_secret=getattr(target_ledger, "secret_getter", None),
        )
        if not prefs or not prefs.get("_exists"):
            return base_config
        interval_val = int(prefs.get("scan_interval", base_config.interval))
        if interval_val not in ALLOWED_SCAN_INTERVALS:
            interval_val = base_config.interval
        cand_val = max(1, min(50, int(prefs.get("candidates", base_config.candidates))))
        shortlist_val = max(5, min(100, int(prefs.get("shortlist_limit", getattr(base_config, "shortlist_limit", 50)))))
        relaxed_flag = bool(prefs.get("relaxed_mode", base_config.relaxed_mode))
        raw_profile = str(prefs.get("strategy_profile") or ("BALANCED" if relaxed_flag else getattr(base_config, "strategy_profile", "BALANCED"))).strip().upper()
        if raw_profile not in ("CONSERVATIVE", "BALANCED", "ACTIVE"):
            raw_profile = "BALANCED"
        if relaxed_flag and raw_profile == "CONSERVATIVE":
            raw_profile = "BALANCED"
        align_mkt = bool(prefs.get("align_to_market_time", getattr(base_config, "align_to_market_time", True)))
        return replace(
            base_config,
            interval=interval_val,
            candidates=cand_val,
            shortlist_limit=shortlist_val,
            futures_primary_only=True,
            align_to_market_time=align_mkt,
            relaxed_mode=relaxed_flag,
            strategy_profile=raw_profile,
            strategy_engine="HYBRID_ADAPTIVE_V1",
            allow_paper_on_ai_unavailable=True,
            advisor_only_ai=True,
            send_alerts=bool(prefs.get("send_alerts", base_config.send_alerts)),
            send_watchlist_alerts=bool(prefs.get("send_watchlist_alerts", base_config.send_watchlist_alerts)),
            send_scan_summary=bool(prefs.get("send_scan_summary", base_config.send_scan_summary)),
            min_volume=float(prefs.get("min_volume", base_config.min_volume)),
            leverage=int(prefs.get("leverage", base_config.leverage)),
            risk_pct=min(0.25, max(0.05, float(prefs.get("risk_pct", base_config.risk_pct)))),
            fee=float(prefs.get("fee_pct", base_config.fee * 100.0)) / 100.0,
            slippage_bps=float(prefs.get("slippage_bps", base_config.slippage_bps)),
            funding=float(prefs.get("funding_pct", base_config.funding * 100.0)) / 100.0,
        )
    except Exception:
        return base_config


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Single-flight market scanner worker (Hybrid Adaptive v1.1)")
    p.add_argument("--interval", type=int, default=int(os.getenv("SCAN_INTERVAL_SECONDS", "60")))
    p.add_argument("--heartbeat-interval", type=int, default=int(os.getenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "30")))
    p.add_argument("--candidates", type=int, default=int(os.getenv("SCAN_CANDIDATES", "20")))
    p.add_argument("--shortlist-limit", type=int, default=int(os.getenv("SCAN_SHORTLIST_LIMIT", "50")))
    p.add_argument("--max-groq-candidates", type=int, default=int(os.getenv("SCAN_MAX_GROQ_CANDIDATES", "5")))
    p.add_argument("--strategy-profile", default=os.getenv("STRATEGY_PROFILE", "BALANCED"))
    p.add_argument("--min-volume", type=float, default=float(os.getenv("SCAN_MIN_VOLUME", "5000000")))
    p.add_argument("--leverage", type=int, default=int(os.getenv("PAPER_LEVERAGE", "2")))
    p.add_argument("--risk-pct", type=float, default=float(os.getenv("PAPER_RISK_PCT", "0.25")))
    p.add_argument("--fee", type=float, default=float(os.getenv("PAPER_TAKER_FEE", "0.0005")))
    p.add_argument("--slippage-bps", type=float, default=float(os.getenv("PAPER_SLIPPAGE_BPS", "2")))
    p.add_argument("--funding", type=float, default=float(os.getenv("PAPER_FUNDING_RESERVE", "0.0003")))
    p.add_argument("--initial-equity", type=float, default=float(os.getenv("PAPER_INITIAL_EQUITY", "1000")))
    p.add_argument("--db", default="")
    p.add_argument("--model", default=os.getenv("GROQ_MODEL", DEFAULT_MODEL))
    p.add_argument("--no-alerts", "--no-telegram", dest="no_alerts", action="store_true")
    p.add_argument("--once", action="store_true")
    return p.parse_args(argv)


def create_worker_engine(args, root=None, secret_getter=None):
    """Build a standalone MarketScanner wired to the centralized persistence layer with FuturesProviderRouter and Strategy F (BALANCED) as default."""
    root = Path(root or Path(__file__).resolve().parent)
    getter = secret_getter or os.environ.get
    # P0 Futures-only enforcement: FuturesProviderRouter is active by default without needing optional env vars.
    market = FuturesProviderRouter()
    initial_kind = getattr(market, "instrument_kind", "USD_M_PERPETUAL")
    db_path = Path(args.db) if getattr(args, "db", "") else resolve_db_path(root, initial_kind)

    pg_url = configured_database_url(get_secret=getter)
    sb_url, sb_key = configured_supabase_rest(get_secret=getter)
    sb_sync = SupabaseStateSync(sb_url, sb_key)

    ledger = PaperLedger(
        db_path,
        args.initial_equity,
        expected_instrument_kind=initial_kind,
        pg_url=pg_url,
        secret_getter=getter,
        supabase_sync=sb_sync,
    )
    security = SecurityManager(db_path, pg_url=pg_url, secret_getter=getter)
    pool = GroqRotation(load_keys(getter), security=security)
    alerts = TelegramAlerts(
        getter("TELEGRAM_BOT_TOKEN"),
        getter("TELEGRAM_CHAT_ID"),
        db_path,
        security=security,
        pg_url=pg_url,
        secret_getter=getter,
    )
    scanner = MarketScanner(
        market,
        pool,
        NewsCalendar(),
        ledger,
        alerts,
        model=args.model,
        security=security,
        trusted_worker=True,
    )
    raw_prof = str(getattr(args, "strategy_profile", "BALANCED") or "BALANCED").strip().upper()
    if raw_prof not in ("CONSERVATIVE", "BALANCED", "ACTIVE"):
        raw_prof = "BALANCED"
    config = ScannerConfig(
        candidates=args.candidates,
        shortlist_limit=int(getattr(args, "shortlist_limit", 50)),
        max_groq_candidates=int(getattr(args, "max_groq_candidates", 5)),
        futures_primary_only=True,
        align_to_market_time=True,
        min_volume=args.min_volume,
        fee=args.fee,
        slippage_bps=args.slippage_bps,
        funding=args.funding,
        leverage=args.leverage,
        risk_pct=min(0.25, max(0.05, float(args.risk_pct))),
        send_alerts=not args.no_alerts,
        relaxed_mode=(raw_prof in ("BALANCED", "ACTIVE")),
        strategy_profile=raw_prof,
        strategy_engine="HYBRID_ADAPTIVE_V1",
        allow_paper_on_ai_unavailable=True,
        advisor_only_ai=True,
        interval=args.interval,
    )
    config = sync_worker_config_from_preferences(ledger, config)
    return scanner, config


def main(argv=None):
    args = parse_args(argv)
    if args.interval < 30 or args.interval > 3600:
        raise SystemExit(f"Interval harus salah satu dari {ALLOWED_SCAN_INTERVALS} detik")
    hb_interval_s = max(5, min(30, int(getattr(args, "heartbeat_interval", WORKER_HEARTBEAT_INTERVAL_S))))
    root = Path(__file__).resolve().parent
    engine, config = create_worker_engine(args, root=root)
    worker_id = f"worker-{os.getpid()}-{uuid4().hex[:6]}"

    health = engine.ledger.integrity_check()
    p_info = persistence_status(
        db_path=engine.ledger.db,
        get_secret=os.environ.get,
        pg_url=engine.ledger.pg_url,
        ledger=engine.ledger,
        supabase_sync=engine.ledger.supabase_sync,
    )
    print(
        f"[CoinAgentX Worker v{APP_VERSION}] worker_id={worker_id} "
        f"backend={p_info['backend']} status={p_info.get('status', p_info.get('backend', 'OK'))} "
        f"db_healthy={health.get('ok')} db={engine.ledger.db.name} "
        f"effective_strategy_profile={config.strategy_profile} strategy_engine={config.strategy_engine} "
        f"futures_primary_only={config.futures_primary_only} align_to_market_time={config.align_to_market_time} "
        f"heartbeat_interval={hb_interval_s}s scan_interval={config.interval}s",
        flush=True,
    )

    last_scan_ms = int(time.time() * 1000)
    next_scan_ms = (
        next_market_aligned_scan_ms(last_scan_ms, int(config.interval))
        if getattr(config, "align_to_market_time", True)
        else (last_scan_ms + int(config.interval) * 1000)
    )
    stop_hb_event = threading.Event()

    def _emit_heartbeat(now_override_ms=None):
        stamp = int(now_override_ms if now_override_ms is not None else int(time.time() * 1000))
        try:
            engine.ledger.record_worker_heartbeat(
                worker_id,
                stamp,
                config.interval,
                strategy_profile=config.strategy_profile,
                strategy_engine=config.strategy_engine,
                last_scan_ms=last_scan_ms,
                next_scan_ms=next_scan_ms,
                database_name=engine.ledger.db.name,
            )
        except Exception as hb_exc:
            print(f"[CoinAgentX Worker v{APP_VERSION}] WARN heartbeat write failed: {hb_exc}", flush=True)

    def _heartbeat_daemon():
        while not stop_hb_event.wait(hb_interval_s):
            _emit_heartbeat()

    if not args.once:
        hb_thread = threading.Thread(target=_heartbeat_daemon, name="worker-heartbeat-30s", daemon=True)
        hb_thread.start()

    try:
        while True:
            started = time.monotonic()
            now_ms = int(time.time() * 1000)
            config = sync_worker_config_from_preferences(engine.ledger, config)
            last_scan_ms = now_ms
            next_scan_ms = (
                next_market_aligned_scan_ms(now_ms, int(config.interval))
                if getattr(config, "align_to_market_time", True)
                else (now_ms + int(config.interval) * 1000)
            )
            _emit_heartbeat(now_ms)

            import inspect
            run_params = inspect.signature(engine.run).parameters
            if "worker_id" in run_params:
                report = engine.run(config, worker_id=worker_id)
            else:
                report = engine.run(config)
            if isinstance(report, dict) and (report.get("market_error") or {}).get("access_blocked"):
                http_st = (report.get("market_error") or {}).get("http_status", 451)
                print(
                    f"[CoinAgentX Worker v{APP_VERSION}] STOPPED_HTTP_{http_st} "
                    f"started_at_ms={report.get('started_at_ms', 0)} "
                    "Akses data pasar diblokir; menghentikan loop retry otomatis.",
                    flush=True,
                )
                return 2

            completed_ms = int(time.time() * 1000)
            ref_market_ms = int(report.get("now_ms") or completed_ms) if isinstance(report, dict) else completed_ms
            if getattr(config, "align_to_market_time", True):
                next_scan_ms = int(
                    (report.get("next_scan_at_ms") if isinstance(report, dict) else 0)
                    or next_market_aligned_scan_ms(ref_market_ms, int(config.interval))
                )
            else:
                next_scan_ms = int(
                    (report.get("next_scan_at_ms") if isinstance(report, dict) else 0)
                    or (last_scan_ms + int(config.interval) * 1000)
                )
            _emit_heartbeat(completed_ms)

            valid = sum(d.get("status") == "VALID" for d in report.get("analyses", []))
            mkt_boundary_ms = current_market_candle_boundary_ms(ref_market_ms, int(config.interval))
            print(
                f"[CoinAgentX Worker v{APP_VERSION}] scan={report.get('scan_id', '-')}"
                f" provider={report.get('market_provider', getattr(engine.market, 'market_label', 'Unknown'))}"
                f" kind={report.get('instrument_kind', getattr(engine.market, 'instrument_kind', 'USD_M_PERPETUAL'))}"
                f" profile={report.get('effective_strategy_profile', config.strategy_profile)}"
                f" engine={report.get('strategy_engine', 'HYBRID_ADAPTIVE_V1')}"
                f" db={Path(report.get('ledger_db', str(engine.ledger.db))).name}"
                f" market={report.get('universe_count', 0)}"
                f" shortlist={len(report.get('shortlist', []))}"
                f" deep={len(report.get('selected', []))}"
                f" analyzed={report.get('analyzed_count', len(report.get('analyses', [])))}"
                f" deferred={report.get('deferred_count', 0)}"
                f" valid={valid}"
                f" interval={config.interval}s"
                f" boundary_ms={mkt_boundary_ms}"
                f" next_scan_ms={next_scan_ms}"
                f" errors={len(report.get('errors', []))}",
                flush=True,
            )
            if args.once:
                return 0
            if getattr(config, "align_to_market_time", True):
                elapsed_s = max(0.0, time.monotonic() - started)
                current_est_ms = ref_market_ms + int(elapsed_s * 1000)
                target_boundary_ms = next_market_aligned_scan_ms(current_est_ms, int(config.interval))
                next_scan_ms = target_boundary_ms
                remaining_sleep = max(0.5, (target_boundary_ms - current_est_ms) / 1000.0)
            else:
                remaining_sleep = max(1.0, config.interval - (time.monotonic() - started))
            while remaining_sleep > 0:
                chunk = min(float(hb_interval_s), remaining_sleep)
                time.sleep(chunk)
                remaining_sleep -= chunk
                _emit_heartbeat()
    finally:
        stop_hb_event.set()


if __name__ == "__main__":
    sys.exit(main())
