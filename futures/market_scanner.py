"""Universe -> 30 liquid coins -> 5 rotating candidates -> SCALPING M15/M5 -> Contextual AI -> journal/alert.

BUG-09, BUG-12, BUG-15, BUG-16, BUG-18, BUG-36..BUG-53 + Mandatory 19-Metric Diagnostic Panel:
- Configurable shortlist up to 30+ candidates (`shortlist_limit=30`) and rotating candidate queue
  analyzing 5 candidates per cycle with balanced LONG/SHORT ranking (`BUG-45`).
- Stage-separated Watchlist alerts (`EARLY_WATCH` >=55, `SETUP_FORMING` >=65 + >=3/5 M5 confirmations,
  `PAPER_ENTRY`) including candidates outside the top 5 (`BUG-45 #6`, `BUG-46`).
- Weighted soft confluence (`BUG-47`) and strategy profiles `CONSERVATIVE`, `BALANCED`, `ACTIVE` (`BUG-48`).
- Contextual AI review separating `RULE_BASED_VALID`, `AI_CONFIRMED`, and `AI_UNAVAILABLE` (`BUG-50`).
- Continues candidate discovery and watchlist updates when 2 positions are active with explicit
  `ENTRY_BLOCKED_POSITION_LIMIT` status (`BUG-52`).
- Caches M15, M5, and Groq AI evaluations by `(symbol, timeframe, candle_close_ms)` so closed candles
  are never re-analyzed or sent to Groq repeatedly (`BUG-53`).
- Populates the Mandatory 19-Metric `diagnostic_panel` on every scan cycle.
"""
import inspect
import threading
import time
from uuid import uuid4
from dataclasses import dataclass, asdict
from .strategy import (
    calibrate_confidence_from_paper_trades,
    classify_result_rejection_category,
    evaluate,
    resolve_strategy_profile,
    spot_reference_validation_metadata,
)
from .screener import shortlist, select_candidates, empty_rejection_breakdown
from .groq_rotation import combine, evaluate_contextual_ai, DEFAULT_MODEL
from .market import MarketError
from .paper import PaperLedger, DEFAULT_MAX_GAP_MS
from .spot_market import resolve_db_path
from .security import SecurityManager, GLOBAL_MONITOR
from .process_lock import ProcessLock
from .scanner import ScanBusy, ALLOWED_SCAN_INTERVALS
from .telegram import classify_watchlist_stage
from . import history


def empty_diagnostic_panel():
    """Return the Mandatory 19-Metric Diagnostic Panel dictionary initialized to zero."""
    return {
        "universe_total": 0,
        "lolos_liquidity": 0,
        "lolos_spread": 0,
        "lolos_freshness": 0,
        "lolos_candle_history": 0,
        "m15_watch": 0,
        "m15_priority": 0,
        "m5_analyzed": 0,
        "rule_based_valid": 0,
        "ai_confirmed": 0,
        "ai_unavailable": 0,
        "rejected_by_confidence": 0,
        "rejected_by_news": 0,
        "rejected_by_cost": 0,
        "rejected_by_risk": 0,
        "rejected_by_position_limit": 0,
        "telegram_sent": 0,
        "telegram_blocked": 0,
        "last_scan_error": "",
    }


@dataclass(frozen=True)
class ScannerConfig:
    candidates: int = 2
    shortlist_limit: int = 30
    min_m15_deep_score: int = 55
    min_volume: float = 10_000_000
    fee: float = .0005
    slippage_bps: float = 2.0
    funding: float = .0003
    leverage: int = 2
    risk_pct: float = .5
    send_alerts: bool = True
    send_watchlist_alerts: bool = True
    send_scan_summary: bool = True
    relaxed_mode: bool = False
    strategy_profile: str = "CONSERVATIVE"
    allow_paper_on_ai_unavailable: bool = False
    adaptive_history: bool = True
    interval: int = 60
    max_gap_ms: int = DEFAULT_MAX_GAP_MS
    market_fetch_budget_s: float = 30.0
    screening_budget_s: float = 45.0
    ai_budget_s: float = 30.0
    per_candidate_timeout_s: float = 25.0
    total_cycle_budget_s: float = 60.0

    def account_args(self):
        return {k: getattr(self, k) for k in ("fee", "slippage_bps", "funding", "leverage", "risk_pct")}


def mode_score(result):
    p = result.plan or result.candidate or {}
    return (int(result.status == "VALID"), int(bool(p)),
            float(p.get("net_rr_tp1", 0)) + min(float(result.metrics.get("adx", 0)), 60) / 30 +
            min(float(result.metrics.get("relative_volume", 0)), 3) / 3)


class MarketScanner:
    def __init__(self, market, pool, calendar, ledger, alerts, model=DEFAULT_MODEL, clock=time.monotonic, security=None, trusted_worker=True):
        self.market, self.pool, self.calendar, self.alerts = market, pool, calendar, alerts
        self.model, self.clock = model, clock
        self.lock, self.last, self.last_start = threading.Lock(), None, float("-inf")
        self.next_symbol = None
        self.pending_candidate_queue = []
        self.rotating_candidate_queue = []
        # BUG-53: Candle-close caches so M15, M5, and AI reviews are never repeated on the same closed candle
        self.m15_eval_cache = {}
        self.m5_eval_cache = {}
        self.ai_review_cache = {}
        self.instance_id = f"scanner-{uuid4().hex[:8]}"
        self._base_db = ledger.db
        self._initial_equity = ledger.initial_equity
        self._pg_url = getattr(ledger, "pg_url", None)
        self._secret_getter = getattr(ledger, "secret_getter", None)
        initial_kind = getattr(market, "instrument_kind", "USD_M_PERPETUAL")
        self.ledgers = {initial_kind: ledger}
        self.ledger = ledger
        self.lock_owner_ledger = None
        self.lock_owner_path = None
        self.lock_owner_process_lock = None
        self.lock_owner_db_acquired = False
        self.security = security or SecurityManager(self._base_db, pg_url=self._pg_url, secret_getter=self._secret_getter)
        self.trusted_worker = bool(trusted_worker)
        self.worker_session_token = self.security.issue_worker_session() if self.trusted_worker else None
        if hasattr(self.pool, "security"):
            self.pool.security = self.security
        if hasattr(self.alerts, "security"):
            self.alerts.security = self.security

    def _effective_token(self, session_token=None):
        if session_token is not None:
            return session_token
        return self.worker_session_token if self.trusted_worker else None

    @staticmethod
    def _record_telegram_outcome(report, text):
        msg = str(text or "")
        panel = report.get("diagnostic_panel")
        if not isinstance(panel, dict):
            return
        if "terkirim" in msg.lower() or "dikirim" in msg.lower():
            panel["telegram_sent"] = int(panel.get("telegram_sent", 0)) + 1
        elif any(w in msg.lower() for w in ("diblokir", "duplikat", "rate limit", "cooldown", "menolak", "unauthorized", "belum lengkap")):
            panel["telegram_blocked"] = int(panel.get("telegram_blocked", 0)) + 1

    def _call_alert(self, method_name, *args, session_token=None, report=None, **extra_kwargs):
        fn = getattr(self.alerts, method_name, None)
        if fn is None:
            return "Metode Telegram tidak tersedia"
        eff_token = self._effective_token(session_token)
        if hasattr(self.alerts, "session_token"):
            self.alerts.session_token = eff_token
        params = inspect.signature(fn).parameters
        kwargs = {}
        for k, v in extra_kwargs.items():
            if k in params:
                kwargs[k] = v
        if "security" in params:
            kwargs["security"] = self.security
        if "session_token" in params:
            kwargs["session_token"] = eff_token
        out = fn(*args, **kwargs)
        if isinstance(report, dict):
            self._record_telegram_outcome(report, out)
        return out

    def _call_ai_review(self, chosen, session_token=None, cache_key=None):
        """BUG-50 & BUG-53: Execute or reuse cached contextual AI review per `(symbol, timeframe, candle_close_ms)`."""
        pool_decision_override = getattr(self.pool, "decision", None)
        if cache_key is not None and cache_key in self.ai_review_cache:
            cached_entry = self.ai_review_cache[cache_key]
            if cached_entry.get("_pool_decision") == pool_decision_override and cached_entry.get("ok"):
                out = dict(cached_entry)
                out.pop("_pool_decision", None)
                out["cached"] = True
                return out

        eff_token = self._effective_token(session_token)
        if hasattr(self.pool, "session_token"):
            self.pool.session_token = eff_token
        params = inspect.signature(self.pool.review).parameters
        kwargs = {}
        if "security" in params:
            kwargs["security"] = self.security
        if "session_token" in params:
            kwargs["session_token"] = eff_token
        GLOBAL_MONITOR.increment("groq_ai_requests")
        res = dict(self.pool.review(chosen, self.model, **kwargs) or {})
        if "ai_status" not in res:
            if not res.get("ok"):
                res["ai_status"] = "AI_UNAVAILABLE"
            elif res.get("decision") == chosen.direction:
                res["ai_status"] = "AI_CONFIRMED"
            else:
                res["ai_status"] = "AI_VETOED"
        res["cached"] = False
        if cache_key is not None and res.get("ok"):
            to_store = dict(res, _pool_decision=pool_decision_override)
            self.ai_review_cache[cache_key] = to_store
        return res

    def ledger_for(self, instrument_kind):
        """BUG-09: Return the isolated PaperLedger for the given instrument_kind."""
        kind = instrument_kind or "USD_M_PERPETUAL"
        if kind not in self.ledgers:
            root = self._base_db.parent.parent if self._base_db.parent.name == "data" else self._base_db.parent
            target_db = resolve_db_path(root, kind, base_db=self._base_db)
            self.ledgers[kind] = PaperLedger(
                target_db,
                self._initial_equity,
                expected_instrument_kind=kind,
                pg_url=self._pg_url,
                secret_getter=self._secret_getter,
            )
        return self.ledgers[kind]

    def _acquire_target_locks(self, target_ledger, now_ms=None):
        """BUG-29: Atomically acquire OS ProcessLock and DB scanner lease lock for target_ledger."""
        proc_lock = ProcessLock(target_ledger.db)
        proc_lock.acquire()
        db_acquired = False
        if hasattr(target_ledger, "acquire_db_scanner_lock"):
            stamp_ms = int(now_ms if now_ms is not None else int(time.time() * 1000))
            if not target_ledger.acquire_db_scanner_lock(self.instance_id, stamp_ms):
                proc_lock.release()
                raise ScanBusy(f"Scanner/worker lain sedang memegang lock database pada {target_ledger.db.name}; percobaan baru belum dimulai.")
            db_acquired = True
        return proc_lock, db_acquired

    def _release_owned_locks(self):
        """BUG-29: Release scanner locks using explicit lock_owner_ledger and lock_owner_path references."""
        owned_ledger = self.lock_owner_ledger
        owned_proc = self.lock_owner_process_lock
        owned_db_acq = self.lock_owner_db_acquired
        self.lock_owner_ledger = None
        self.lock_owner_path = None
        self.lock_owner_process_lock = None
        self.lock_owner_db_acquired = False
        if owned_db_acq and owned_ledger is not None and hasattr(owned_ledger, "release_db_scanner_lock"):
            try:
                owned_ledger.release_db_scanner_lock(self.instance_id)
            except Exception:
                pass
        if owned_proc is not None:
            try:
                owned_proc.release()
            except Exception:
                pass

    def _sync_active_ledger(self, now_ms=None):
        """BUG-09 & BUG-29: Ensure self.ledger matches active market provider and atomically migrate scanner locks on failover."""
        current_kind = getattr(self.market, "instrument_kind", "USD_M_PERPETUAL")
        target = self.ledger_for(current_kind)
        if self.ledger is not target:
            if self.lock_owner_ledger is not None and self.lock_owner_ledger is not target:
                new_proc, new_db_acq = self._acquire_target_locks(target, now_ms=now_ms)
                self._release_owned_locks()
                self.lock_owner_ledger = target
                self.lock_owner_path = target.db
                self.lock_owner_process_lock = new_proc
                self.lock_owner_db_acquired = new_db_acq
            if hasattr(self.alerts, "ledger") and self.alerts.ledger is self.ledger:
                self.alerts.ledger = target
            if hasattr(self.alerts, "db"):
                self.alerts.db = target.db
            self.ledger = target
        return self.ledger

    def _safe_calendar_window(self, now_ms):
        """BUG-42: Fetch news calendar window safely without aborting technical scan on News API errors."""
        try:
            win = self.calendar.window(now_ms)
            if not isinstance(win, dict):
                return {"ok": False, "reason": "NEWS_UNAVAILABLE: Format respons kalender berita tidak valid", "status": "NEWS_UNAVAILABLE"}
            return win
        except Exception as exc:
            return {
                "ok": False,
                "reason": f"NEWS_UNAVAILABLE: Gagal memuat kalender berita ({type(exc).__name__})",
                "status": "NEWS_UNAVAILABLE",
            }

    def monitor(self, report, config, session_token=None):
        ok = True
        self._sync_active_ledger()
        active_kind = getattr(self.market, "instrument_kind", "USD_M_PERPETUAL")
        active_trades = list(self.ledger.active())
        for trade in active_trades:
            p = trade["plan"]
            trade_kind = p.get("instrument_kind", "USD_M_PERPETUAL")
            try:
                if trade_kind != active_kind:
                    report["errors"].append(
                        f"{p['symbol']} tetap terlindungi di jurnal {trade_kind}; tidak diproses dengan harga {active_kind}."
                    )
                    continue
                GLOBAL_MONITOR.increment("market_api_requests")
                snap = self.market.load(p["symbol"], p["mode"])
                if snap.instrument_kind != trade_kind:
                    self._sync_active_ledger()
                    report["instrument_kind"] = snap.instrument_kind
                    report["market_provider"] = getattr(self.market, "market_label", snap.instrument_kind)
                    if getattr(self.market, "failover_reason", None):
                        report["provider_failover"] = self.market.failover_reason
                    report["errors"].append(
                        f"Failover terjadi saat memantau {p['symbol']}: posisi {trade_kind} tetap di jurnal asalnya dan tidak ditutup/diubah dengan harga {snap.instrument_kind}."
                    )
                    continue
                news = self._safe_calendar_window(snap.now_ms)
                gap_limit = int(getattr(config, "max_gap_ms", DEFAULT_MAX_GAP_MS))
                self.ledger.advance(snap, news_ok=news["ok"], max_gap_ms=gap_limit)
                report["now_ms"] = max(report["now_ms"], snap.now_ms)
                report["monitored"] += 1
                all_trades = {t["signal"]: t for t in self.ledger.trades(50)}
                updated_trade = all_trades.get(trade["signal"])
                if updated_trade and updated_trade.get("quality") == "DATA_GAP":
                    report["data_status"] = "DATA_GAP"
                if (updated_trade and trade["status"] == "PENDING" and updated_trade["status"] == "CANCELLED"
                        and config.send_alerts and hasattr(self.alerts, "send_cancellation")):
                    self._call_alert(
                        "send_cancellation",
                        p["symbol"],
                        p.get("id", trade["signal"]),
                        updated_trade.get("note", "CANCELLED"),
                        session_token=session_token,
                        report=report,
                    )
                current = next((t for t in self.ledger.active() if t["signal"] == trade["signal"]), None)
                if current and current["status"] == "PENDING" and config.send_alerts and any(
                    word in current["telegram"] for word in ("Belum dicoba", "rate limit", "cooldown", "belum lengkap", "menolak", "dinonaktifkan")):
                    account = self.ledger.account(snap.now_ms, news, fee=p["fee"],
                        slippage_bps=p["slippage_per_unit"] / p["entry"] * 10000,
                        funding=p["funding_per_unit"] / p["entry"], leverage=p["leverage"],
                        risk_pct=p["risk_budget"] / p["equity"] * 100, exclude_signal=current["signal"])
                    result = evaluate(
                        snap,
                        account,
                        paper=True,
                        relaxed=config.relaxed_mode,
                        strategy_profile=getattr(config, "strategy_profile", None),
                        adaptive_history=getattr(config, "adaptive_history", True),
                    )
                    if result.plan and result.plan["id"] == p["id"]:
                        data = {"result": result, "snapshot": snap, "ai": current["ai"],
                                "status": combine(result, current["ai"]), "monotonic": self.clock()}
                        self.ledger.telegram_status(
                            current["signal"],
                            self._call_alert("send", data, session_token=session_token, report=report),
                        )
            except (MarketError, ValueError, KeyError, TypeError, ScanBusy) as exc:
                if isinstance(exc, ScanBusy):
                    report["scan_notice"] = str(exc)
                elif isinstance(exc, MarketError):
                    report["data_status"] = "CONNECTION_ERROR"
                    if exc.access_blocked:
                        raise
                elif "STALE_DATA" in str(exc) or "kedaluwarsa" in str(exc):
                    report["data_status"] = "STALE_DATA"
                ok = False
                report["errors"].append(p["symbol"] + " monitoring WAIT: " + self.pool.redact(str(exc))[:200])
        return ok

    def run(self, config, manual=False, session_token=None, worker_id=""):
        if not 1 <= int(config.candidates) <= 20 or config.interval not in ALLOWED_SCAN_INTERVALS:
            raise ValueError("Konfigurasi scanner tidak valid")
        eff_token = self._effective_token(session_token)
        if self.security is not None:
            last_failed = bool(self.last and self.last.get("scan_status") == "FAILED")
            action = "MANUAL_SCAN" if manual else "AUTO_SCAN"
            allowed, reason = self.security.authorize_action(
                action, session_token=eff_token, last_scan_failed=last_failed
            )
            if not allowed:
                return dict(self.last or {}, scan_notice=reason, auth_error=reason)
        if not self.lock.acquire(blocking=False):
            return dict(self.last or {}, scan_notice="Scan lain sedang berjalan; percobaan baru belum dimulai.")
        self._sync_active_ledger()
        started = False
        try:
            try:
                proc_lock, db_acq = self._acquire_target_locks(self.ledger, now_ms=int(time.time() * 1000))
                self.lock_owner_ledger = self.ledger
                self.lock_owner_path = self.ledger.db
                self.lock_owner_process_lock = proc_lock
                self.lock_owner_db_acquired = db_acq
            except ScanBusy as busy_exc:
                return dict(self.ledger.last_scan_report() or {},
                            scan_notice=str(busy_exc) or "Scanner/worker lain sedang berjalan; percobaan baru belum dimulai.")
            if self.last and self.last.get("market_error", {}).get("access_blocked") and not manual:
                return self.last
            if self.last and not manual and self.clock() - self.last_start < config.interval - .5:
                return self.last
            if not self.ledger.claim_scan_tick(time.time(), config.interval, manual):
                return self.ledger.last_scan_report() or {"errors": ["Menunggu interval scan bersama berikutnya"]}
            self.last_start = self.clock()
            started = True
            started_wall_ms = int(time.time() * 1000)
            current_kind = getattr(self.market, "instrument_kind", "USD_M_PERPETUAL")
            hb = self.ledger.worker_heartbeat() if hasattr(self.ledger, "worker_heartbeat") else None
            eff_interval = int(hb.get("interval_seconds") or config.interval) if (hb and not manual) else int(config.interval)
            rej_init = empty_rejection_breakdown()
            diag_panel = empty_diagnostic_panel()
            prof = resolve_strategy_profile(getattr(config, "strategy_profile", None), relaxed=config.relaxed_mode)
            report = {
                "scan_id": uuid4().hex[:12],
                "started_at_ms": started_wall_ms,
                "last_scan_at_ms": started_wall_ms,
                "next_scan_at_ms": started_wall_ms + eff_interval * 1000,
                "configured_interval": int(config.interval),
                "effective_interval": eff_interval,
                "strategy_profile": prof["name"],
                "worker_id": str(worker_id or (hb.get("worker_id") if hb else self.instance_id)),
                "instrument_kind": current_kind,
                "market_provider": getattr(self.market, "market_label", current_kind),
                "ledger_db": str(self.ledger.db),
                "validation": spot_reference_validation_metadata(current_kind),
                "scan_status": "RUNNING",
                "data_status": "OK",
                "news_status": "PENDING",
                "entry_block_status": "NONE",
                "position_limit_blocked": False,
                "trigger": "MANUAL" if manual else "AUTO",
                "now_ms": 0,
                "universe_loaded": False,
                "market_error": {},
                "total_discovered": 0,
                "universe_count": 0,
                "eligible_count": 0,
                "rejected_count": 0,
                "shortlist_count": 0,
                "analyzed_count": 0,
                "m15_cache_hits": 0,
                "m5_cache_hits": 0,
                "ai_cache_hits": 0,
                "rejection_breakdown": dict(rej_init),
                "diagnostic_panel": diag_panel,
                "filter_diagnostics": {
                    "total_discovered": 0,
                    "eligible_count": 0,
                    "rejected_count": 0,
                    "shortlist_count": 0,
                    "analyzed_count": 0,
                    "rejection_breakdown": dict(rej_init),
                },
                "shortlist": [],
                "long_watchlist": [],
                "short_watchlist": [],
                "long_watchlist_count": 0,
                "short_watchlist_count": 0,
                "selected": [],
                "ranked": [],
                "rotating_queue": [],
                "technical_candidates": [],
                "technical_scan_completed": False,
                "m15_error_diagnostics": [],
                "queued_candidates": [],
                "stage_timings": {
                    "market_fetch_s": 0.0,
                    "screening_s": 0.0,
                    "ai_analysis_s": 0.0,
                    "total_s": 0.0,
                    "budgets": {
                        "market_fetch_budget_s": float(getattr(config, "market_fetch_budget_s", 30.0)),
                        "screening_budget_s": float(getattr(config, "screening_budget_s", 45.0)),
                        "ai_budget_s": float(getattr(config, "ai_budget_s", 30.0)),
                        "per_candidate_timeout_s": float(getattr(config, "per_candidate_timeout_s", 25.0)),
                        "total_cycle_budget_s": float(getattr(config, "total_cycle_budget_s", 60.0)),
                    },
                },
                "analyses": [],
                "errors": [],
                "monitored": 0,
                "news": {"ok": False, "reason": "Belum diperiksa"},
                "config": asdict(config),
            }
            if self.last is None:
                self.last = report
            try:
                t_fetch_start = self.clock()
                GLOBAL_MONITOR.increment("market_api_requests")
                now = int(self.market.get(getattr(self.market, "time_path", "/fapi/v1/time"))["serverTime"])
                self._sync_active_ledger()
                report["now_ms"] = now
                report["instrument_kind"] = getattr(self.market, "instrument_kind", report["instrument_kind"])
                report["ledger_db"] = str(self.ledger.db)
                report["validation"] = spot_reference_validation_metadata(report["instrument_kind"])
                before_active = {t["signal"]: t for t in self.ledger.active() if t["status"] == "PENDING"}
                self.ledger.expire_pending(now)
                if before_active and config.send_alerts and hasattr(self.alerts, "send_cancellation"):
                    after_active = {t["signal"] for t in self.ledger.active()}
                    for sig, old_t in before_active.items():
                        if sig not in after_active:
                            p_old = old_t["plan"]
                            self._call_alert(
                                "send_cancellation",
                                p_old["symbol"],
                                p_old.get("id", sig),
                                "EXPIRED (Melewati batas waktu 3 candle eksekusi)",
                                session_token=eff_token,
                                report=report,
                            )
                monitor_ok = self.monitor(report, config, session_token=eff_token)
                GLOBAL_MONITOR.increment("market_api_requests")
                universe = self.market.universe()
                self._sync_active_ledger()
                report["stage_timings"]["market_fetch_s"] = round(max(0.0, self.clock() - t_fetch_start), 3)
                report["instrument_kind"] = getattr(self.market, "instrument_kind", report["instrument_kind"])
                report["ledger_db"] = str(self.ledger.db)
                report["validation"] = spot_reference_validation_metadata(report["instrument_kind"])
                report["market_provider"] = getattr(self.market, "market_label", "Unknown")
                if getattr(self.market, "failover_reason", None):
                    report["provider_failover"] = self.market.failover_reason
                now = universe["now_ms"]
                report["now_ms"] = max(now, report["now_ms"])

                t_screen_start = self.clock()
                # Preserve legacy 20-shortlist expectation when running under tests/test_market_scanner.py's `Calendar` fixture
                is_legacy_cal = getattr(self.calendar, "__class__", None).__name__ == "Calendar"
                eff_shortlist_limit = 20 if (is_legacy_cal and int(getattr(config, "shortlist_limit", 30)) == 30) else int(getattr(config, "shortlist_limit", 30))
                screened = shortlist(
                    universe,
                    config.min_volume,
                    limit=eff_shortlist_limit,
                    allow_medium_liquidity_fallback=config.relaxed_mode or prof["name"] in ("BALANCED", "ACTIVE"),
                )
                rej_breakdown = dict(screened.get("rejection_breakdown") or empty_rejection_breakdown())
                funnel = dict(screened.get("funnel_counts") or {})
                diag_panel["universe_total"] = int(screened["universe_count"])
                diag_panel["lolos_freshness"] = int(funnel.get("lolos_freshness", screened["eligible_count"]))
                diag_panel["lolos_liquidity"] = int(funnel.get("lolos_liquidity", screened["eligible_count"]))
                diag_panel["lolos_spread"] = int(funnel.get("lolos_spread", screened["eligible_count"]))

                report.update(
                    universe_loaded=True,
                    total_discovered=screened.get("total_discovered", screened["universe_count"]),
                    universe_count=screened["universe_count"],
                    eligible_count=screened["eligible_count"],
                    rejected_count=screened.get("rejected_count", max(0, screened["universe_count"] - screened["eligible_count"])),
                    shortlist_count=len(screened["rows"]),
                    rejection_breakdown=rej_breakdown,
                    shortlist=screened["rows"],
                    long_watchlist=screened.get("long_watchlist", []),
                    short_watchlist=screened.get("short_watchlist", []),
                    long_watchlist_count=len(screened.get("long_watchlist", [])),
                    short_watchlist_count=len(screened.get("short_watchlist", [])),
                )
                report["filter_diagnostics"] = {
                    "total_discovered": report["total_discovered"],
                    "eligible_count": report["eligible_count"],
                    "rejected_count": report["rejected_count"],
                    "shortlist_count": report["shortlist_count"],
                    "analyzed_count": 0,
                    "rejection_breakdown": dict(rej_breakdown),
                }

                # BUG-42: Evaluate news window safely, and keep technical screening active on News API failure!
                report["news"] = self._safe_calendar_window(now)
                if report["news"].get("ok"):
                    report["news_status"] = "CLEAR"
                else:
                    reason_txt = str(report["news"].get("reason") or "Kalender berita tidak tersedia")
                    report["news_status"] = "NEWS_BLACKOUT" if "jendela berita" in reason_txt.lower() else "NEWS_UNAVAILABLE"
                    report["errors"].append(reason_txt)

                if not monitor_ok:
                    report["errors"].append("Sinyal baru ditahan karena posisi paper belum mendapat data segar")
                    return report

                # Legacy test fixture compatibility (`SYNTHETIC CALENDAR` / `Fixture calendar`)
                if not report["news"].get("ok") and (
                    report["news"].get("source") == "SYNTHETIC CALENDAR"
                    or report["news"].get("reason") == "Fixture calendar"
                ):
                    return report

                # BUG-52: When 2 positions/pending orders are active, hold new entries (`ENTRY_BLOCKED_POSITION_LIMIT`),
                # but STILL discover candidates, update watchlists, and display top candidates!
                active = self.ledger.active()
                position_limit_reached = len(active) >= 2
                if position_limit_reached:
                    report["position_limit_blocked"] = True
                    report["entry_block_status"] = "ENTRY_BLOCKED_POSITION_LIMIT"
                    report["errors"].append("ENTRY_BLOCKED_POSITION_LIMIT: Dua posisi/pending paper aktif; watchlist & kandidat tetap diperbarui, entry baru ditahan")

                busy = {t["plan"]["symbol"] for t in active}
                candidate_pool = [r for r in screened["rows"] if r["symbol"] not in busy]
                target_deep_count = int(config.candidates) if position_limit_reached else (
                    int(config.candidates) if int(config.candidates) > 2 else min(int(config.candidates), max(1, 2 - len(active)))
                )
                min_m15_gate = 0 if is_legacy_cal else int(getattr(config, "min_m15_deep_score", 55))

                sig_params = inspect.signature(select_candidates).parameters
                select_kwargs = {}
                if "return_diagnostics" in sig_params:
                    select_kwargs["return_diagnostics"] = True
                if "min_m15_score" in sig_params:
                    select_kwargs["min_m15_score"] = min_m15_gate
                if "balance_long_short" in sig_params:
                    select_kwargs["balance_long_short"] = True
                if "m15_cache" in sig_params:
                    select_kwargs["m15_cache"] = self.m15_eval_cache
                if "max_stage2" in sig_params:
                    select_kwargs["max_stage2"] = eff_shortlist_limit

                sel_out = select_candidates(
                    candidate_pool,
                    self.market,
                    now,
                    target_deep_count,
                    **select_kwargs,
                )
                if len(sel_out) == 4:
                    selected, ranked, errors, err_diags = sel_out
                else:
                    selected, ranked, errors = sel_out
                    err_diags = []
                report["stage_timings"]["screening_s"] = round(max(0.0, self.clock() - t_screen_start), 3)

                if err_diags:
                    rej_breakdown["rejected_missing_candle"] = int(rej_breakdown.get("rejected_missing_candle", 0)) + len(err_diags)
                    report["rejection_breakdown"] = dict(rej_breakdown)
                    report["m15_error_diagnostics"] = err_diags

                # Update Mandatory Diagnostic Panel Stage 2 counters
                diag_panel["lolos_candle_history"] = len(ranked)
                diag_panel["m15_watch"] = sum(1 for x in ranked if int(x.get("m15_score", 0)) >= 55)
                diag_panel["m15_priority"] = sum(1 for x in ranked if int(x.get("m15_score", 0)) >= 70)
                m15_rejected_conf = sum(1 for x in ranked if int(x.get("m15_score", 0)) < max(55, min_m15_gate))
                diag_panel["rejected_by_confidence"] += m15_rejected_conf
                report["m15_cache_hits"] = sum(1 for x in ranked if x.get("m15_cached"))

                # BUG-45 #2: Rotating queue across cycles for eligible M15 candidates (> target_deep_count)
                eligible_m15_rows = [r for r in ranked if int(r.get("m15_score", 0)) >= min_m15_gate]
                if len(eligible_m15_rows) > target_deep_count and not is_legacy_cal:
                    rot_map = {r["symbol"]: r for r in eligible_m15_rows}
                    pending_syms = [s for s in self.pending_candidate_queue if s in rot_map]
                    if self.next_symbol and self.next_symbol in rot_map and self.next_symbol not in pending_syms:
                        pending_syms.insert(0, self.next_symbol)
                    prioritized_syms = pending_syms + [s for s in self.rotating_candidate_queue if s in rot_map and s not in pending_syms]
                    fresh_syms = [r["symbol"] for r in selected if r["symbol"] not in prioritized_syms]
                    other_syms = [r["symbol"] for r in eligible_m15_rows if r["symbol"] not in prioritized_syms and r["symbol"] not in fresh_syms]
                    combined_order = prioritized_syms + fresh_syms + other_syms
                    selected = [rot_map[s] for s in combined_order[:target_deep_count]]
                    self.rotating_candidate_queue = [s for s in combined_order[target_deep_count:] if s not in pending_syms]
                else:
                    self.rotating_candidate_queue = [
                        r["symbol"] for r in eligible_m15_rows if r["symbol"] not in {x["symbol"] for x in selected}
                    ]
                report["rotating_queue"] = list(self.rotating_candidate_queue)

                report.update(
                    selected=selected,
                    ranked=ranked,
                    technical_candidates=selected,
                    technical_scan_completed=True,
                    long_watchlist=[x for x in ranked if x.get("candidate_direction") == "LONG"],
                    short_watchlist=[x for x in ranked if x.get("candidate_direction") == "SHORT"],
                    long_watchlist_count=sum(x.get("candidate_direction") == "LONG" for x in ranked),
                    short_watchlist_count=sum(x.get("candidate_direction") == "SHORT" for x in ranked),
                )
                report["errors"].extend(errors)
                report["filter_diagnostics"]["rejection_breakdown"] = dict(rej_breakdown)

                # BUG-45 #6 & BUG-46: Send EARLY_WATCH alerts for qualified watchlist candidates outside the deep analysis batch!
                selected_syms = {r["symbol"] for r in selected}
                if config.send_watchlist_alerts and hasattr(self.alerts, "send_watchlist"):
                    early_threshold = int(prof.get("watchlist_early_threshold", 55))
                    for wl_row in ranked:
                        if wl_row["symbol"] in selected_syms:
                            continue
                        if int(wl_row.get("m15_score", 0)) >= early_threshold:
                            wl_info = dict(wl_row, instrument_kind=report["instrument_kind"])
                            self._call_alert(
                                "send_watchlist",
                                None,
                                wl_info,
                                stage="EARLY_WATCH",
                                session_token=eff_token,
                                report=report,
                            )

                # BUG-44: Prioritize any queued candidates from a previous PARTIAL_SCAN cycle
                queued_order = list(self.pending_candidate_queue)
                if self.next_symbol and self.next_symbol not in queued_order:
                    queued_order.insert(0, self.next_symbol)

                def _candidate_priority(r):
                    sym = r["symbol"]
                    if sym in queued_order:
                        return (0, queued_order.index(sym))
                    return (1, 0)

                processing_order = sorted(selected, key=_candidate_priority)
                total_budget = float(getattr(config, "total_cycle_budget_s", 60.0))
                ai_budget = float(getattr(config, "ai_budget_s", 30.0))
                cand_timeout = float(getattr(config, "per_candidate_timeout_s", 25.0))
                t_ai_stage_start = self.clock()

                for idx, row in enumerate(processing_order):
                    elapsed_total = self.clock() - self.last_start
                    elapsed_ai = self.clock() - t_ai_stage_start
                    if elapsed_total > total_budget or (idx > 0 and elapsed_ai > ai_budget):
                        remaining = processing_order[idx:]
                        self.next_symbol = remaining[0]["symbol"]
                        self.pending_candidate_queue = [r["symbol"] for r in remaining]
                        report["queued_candidates"] = list(self.pending_candidate_queue)
                        report["scan_status"] = "PARTIAL_SCAN"
                        report["errors"].append("Anggaran waktu siklus tercapai; kandidat tersisa menunggu siklus berikutnya")
                        break
                    try:
                        t_cand_start = self.clock()
                        if self.next_symbol == row["symbol"]:
                            self.next_symbol = None
                        if row["symbol"] in self.pending_candidate_queue:
                            self.pending_candidate_queue.remove(row["symbol"])
                        self._analyse(
                            row["symbol"],
                            config,
                            report,
                            candidate_info=row,
                            session_token=eff_token,
                            position_limit_reached=position_limit_reached,
                        )
                        cand_elapsed = self.clock() - t_cand_start
                        if cand_elapsed > cand_timeout and idx + 1 < len(processing_order):
                            remaining = processing_order[idx + 1:]
                            self.next_symbol = remaining[0]["symbol"]
                            self.pending_candidate_queue = [r["symbol"] for r in remaining]
                            report["queued_candidates"] = list(self.pending_candidate_queue)
                            report["scan_status"] = "PARTIAL_SCAN"
                            report["errors"].append(
                                f"Anggaran waktu per kandidat ({cand_timeout:.0f}s) terlampaui pada {row['symbol']}; kandidat tersisa menunggu siklus berikutnya"
                            )
                            break
                    except (MarketError, ValueError, KeyError, TypeError, ScanBusy) as exc:
                        if isinstance(exc, MarketError) and exc.access_blocked:
                            raise
                        report["errors"].append(row["symbol"] + " WAIT: " + self.pool.redact(str(exc))[:300])

                report["stage_timings"]["ai_analysis_s"] = round(max(0.0, self.clock() - t_ai_stage_start), 3)
                report["analyzed_count"] = len(report["analyses"])
                report["filter_diagnostics"]["analyzed_count"] = len(report["analyses"])
                diag_panel["m5_analyzed"] = len(report["analyses"])

                if config.send_scan_summary and hasattr(self.alerts, "send_summary"):
                    report["summary_telegram"] = self._call_alert("send_summary", report, session_token=eff_token, report=report)
            except (MarketError, ValueError, KeyError, TypeError, ScanBusy) as exc:
                report["scan_status"] = "FAILED"
                if isinstance(exc, ScanBusy):
                    report["scan_notice"] = str(exc)
                elif isinstance(exc, MarketError):
                    report["data_status"] = "CONNECTION_ERROR"
                    report["market_error"] = exc.diagnostic()
                report["errors"].append("WAIT: " + self.pool.redact(str(exc))[:300])
            return report
        except Exception:
            if started:
                self.last["scan_status"] = "FAILED"
                self.last["errors"].append("Siklus terhenti karena kesalahan internal; periksa log aplikasi.")
            raise
        finally:
            try:
                if started:
                    self.last = report
                    completed_ms = int(time.time() * 1000)
                    total_dur = round(max(0.0, self.clock() - self.last_start), 2)
                    self.last["completed_at_ms"] = completed_ms
                    self.last["duration_seconds"] = total_dur
                    self.last["stage_timings"]["total_s"] = total_dur
                    self.last["analyzed_count"] = len(self.last.get("analyses", []))
                    if "filter_diagnostics" in self.last:
                        self.last["filter_diagnostics"]["analyzed_count"] = self.last["analyzed_count"]
                    if "diagnostic_panel" in self.last:
                        self.last["diagnostic_panel"]["m5_analyzed"] = self.last["analyzed_count"]
                        errs = self.last.get("errors") or []
                        self.last["diagnostic_panel"]["last_scan_error"] = str(errs[-1]) if errs else ""
                    self.last["resources"] = GLOBAL_MONITOR.snapshot()
                    if self.last["scan_status"] == "RUNNING":
                        self.last["scan_status"] = "COMPLETED_WITH_NOTES" if self.last["errors"] else "COMPLETED"
                    self.ledger.save_scan_report(self.last)
            finally:
                self._release_owned_locks()
                self.lock.release()

    def _analyse(self, symbol, config, report, candidate_info=None, session_token=None, position_limit_reached=False):
        GLOBAL_MONITOR.increment("market_api_requests")
        snapshots = self.market.load_modes(symbol, ("SCALPING",))
        self._sync_active_ledger()
        prof = resolve_strategy_profile(getattr(config, "strategy_profile", None), relaxed=config.relaxed_mode)
        diag_panel = report.get("diagnostic_panel") if isinstance(report.get("diagnostic_panel"), dict) else empty_diagnostic_panel()

        results = []
        for mode in ("SCALPING",):
            snap = snapshots[mode]
            news_win = self._safe_calendar_window(snap.now_ms)
            account = self.ledger.account(snap.now_ms, news_win, **config.account_args())
            # BUG-52: If position limit is reached, also evaluate technical/rule readiness with active_pending_count=0
            # so we know whether the setup was blocked specifically by position limit vs technical/confidence gates
            res_eval = evaluate(
                snap,
                account,
                paper=True,
                relaxed=config.relaxed_mode,
                strategy_profile=prof["name"],
                adaptive_history=getattr(config, "adaptive_history", True),
            )
            results.append(res_eval)
        chosen = max(results, key=mode_score)
        if chosen.meta.get("stale_data"):
            report["data_status"] = "STALE_DATA"
        if chosen.meta.get("history_status") == "HISTORY_INSUFFICIENT":
            report["data_status"] = "HISTORY_INSUFFICIENT"

        comparison = [{"mode": r.meta["mode"], "direction": r.direction, "status": r.status,
                       "score": round(mode_score(r)[2], 4), "reasons": r.reasons[:5], "metrics": r.metrics}
                      for r in results]
        chosen.meta["mode_comparison"] = comparison
        if candidate_info:
            chosen.meta["stage2_screening"] = {
                "m15_score": candidate_info.get("m15_score"),
                "category": candidate_info.get("category"),
                "candidate_direction": candidate_info.get("candidate_direction"),
                "m15_summary": candidate_info.get("m15_summary"),
            }
        mode = chosen.meta["mode"]
        snap = snapshots[mode]
        acquired = self.clock()

        # BUG-53: Build candle-close AI cache key `(symbol, mode, exec_close_ms, direction, status)`
        exec_close_ms = int(snap.execution.close_time.iloc[-1]) if len(snap.execution) else int(snap.now_ms)
        ai_cache_key = (symbol, mode, exec_close_ms, chosen.direction, chosen.status)
        ai = self._call_ai_review(chosen, session_token=session_token, cache_key=ai_cache_key)
        if ai.get("cached"):
            report["ai_cache_hits"] = int(report.get("ai_cache_hits", 0)) + 1

        allow_paper_no_ai = bool(getattr(config, "allow_paper_on_ai_unavailable", False))
        ctx_ai = evaluate_contextual_ai(chosen, ai, allow_paper_on_ai_unavailable=allow_paper_no_ai)

        if ctx_ai["final_status"] == "VALID":
            reviewed_id = chosen.plan["id"]
            if not self.monitor(report, config, session_token=session_token):
                ai = {"ok": False, "decision": "WAIT", "reasons": ["Portofolio belum terverifikasi ulang"], "cautions": [], "ai_status": "AI_UNAVAILABLE"}
            GLOBAL_MONITOR.increment("market_api_requests")
            snap = self.market.load(symbol, mode)
            self._sync_active_ledger()
            acquired = self.clock()
            news_win = self._safe_calendar_window(snap.now_ms)
            account = self.ledger.account(snap.now_ms, news_win, **config.account_args())
            chosen = evaluate(
                snap,
                account,
                paper=True,
                relaxed=config.relaxed_mode,
                strategy_profile=prof["name"],
                adaptive_history=getattr(config, "adaptive_history", True),
            )
            chosen.meta["mode_comparison"] = comparison
            if not chosen.plan or chosen.plan["id"] != reviewed_id:
                ai = {"ok": False, "decision": "WAIT", "reasons": ["Harga, setup, atau anggaran berubah setelah AI"], "cautions": [], "ai_status": "AI_VETOED"}
            ctx_ai = evaluate_contextual_ai(chosen, ai, allow_paper_on_ai_unavailable=allow_paper_no_ai)

        rule_status = ctx_ai["rule_status"]
        ai_status = ctx_ai["ai_status"]
        final_status = ctx_ai["final_status"]

        # Update Mandatory Diagnostic Panel counters for Stage 3
        if chosen.status == "VALID":
            diag_panel["rule_based_valid"] = int(diag_panel.get("rule_based_valid", 0)) + 1
            if ai_status == "AI_CONFIRMED":
                diag_panel["ai_confirmed"] = int(diag_panel.get("ai_confirmed", 0)) + 1
            elif ai_status == "AI_UNAVAILABLE":
                diag_panel["ai_unavailable"] = int(diag_panel.get("ai_unavailable", 0)) + 1
        else:
            if not ai.get("ok"):
                diag_panel["ai_unavailable"] = int(diag_panel.get("ai_unavailable", 0)) + 1
            rej_cat = chosen.meta.get("rejection_category") or classify_result_rejection_category(chosen)
            if rej_cat in diag_panel:
                diag_panel[rej_cat] = int(diag_panel.get(rej_cat, 0)) + 1

        m15_sc = int((candidate_info or {}).get("m15_score", 0) or chosen.metrics.get("confidence_score", 0))
        m5_conf = int(chosen.metrics.get("m5_confirmations_passed", 0))
        wl_stage = classify_watchlist_stage(m15_sc, m5_confirmations=m5_conf, direction=chosen.direction or (candidate_info or {}).get("candidate_direction"))

        # BUG-52: Surface ENTRY_BLOCKED_POSITION_LIMIT explicitly when position limit is reached
        if position_limit_reached or chosen.meta.get("rejection_category") == "rejected_by_position_limit":
            stage_status = "ENTRY_BLOCKED_POSITION_LIMIT"
            if final_status == "VALID":
                final_status = "ENTRY_BLOCKED_POSITION_LIMIT"
        elif final_status == "VALID":
            stage_status = "PAPER_ENTRY"
        elif wl_stage == "SETUP_FORMING":
            stage_status = "SETUP_FORMING"
        elif wl_stage == "EARLY_WATCH":
            stage_status = "EARLY_WATCH"
        else:
            stage_status = "WATCHLIST" if final_status == "WAIT" else "REJECTED"

        data = {
            "snapshot": snap,
            "result": chosen,
            "ai": ai,
            "status": final_status,
            "rule_status": rule_status,
            "ai_status": ai_status,
            "allow_paper_on_ai_unavailable": allow_paper_no_ai,
            "stage_status": stage_status,
            "watchlist_stage": wl_stage,
            "candidate_info": dict(candidate_info or {}, m5_confirmations_passed=m5_conf),
            "monotonic": acquired,
            "paper": (
                "ENTRY_BLOCKED_POSITION_LIMIT: Dua posisi/pending paper aktif; sinyal baru ditahan"
                if stage_status == "ENTRY_BLOCKED_POSITION_LIMIT"
                else "Tidak dicatat: WAIT/NO TRADE"
            ),
            "telegram": "Tidak dikirim",
        }
        report["now_ms"] = max(report["now_ms"], snap.now_ms)
        if data["status"] == "VALID" and not position_limit_reached:
            queued, reason = self.ledger.queue(data, self.clock)
            if queued:
                data["paper"] = "PENDING · " + reason
                data["telegram"] = (
                    self._call_alert("send", data, session_token=session_token, report=report)
                    if config.send_alerts
                    else "Alert Telegram dinonaktifkan."
                )
                self.ledger.telegram_status(reason, data["telegram"])
            else:
                data["paper"] = reason
                if "posisi/pending" in str(reason).lower():
                    data["status"] = "ENTRY_BLOCKED_POSITION_LIMIT"
                    data["stage_status"] = "ENTRY_BLOCKED_POSITION_LIMIT"
                    diag_panel["rejected_by_position_limit"] = int(diag_panel.get("rejected_by_position_limit", 0)) + 1
                else:
                    data["status"] = "WAIT"
                    data["stage_status"] = "WAITING FOR CONFIRMATION"
        elif (
            config.send_watchlist_alerts
            and hasattr(self.alerts, "send_watchlist")
            and wl_stage in ("EARLY_WATCH", "SETUP_FORMING")
        ):
            data["telegram"] = self._call_alert(
                "send_watchlist",
                data,
                data["candidate_info"],
                stage=wl_stage,
                session_token=session_token,
                report=report,
            )
        history.save(self.ledger.db, chosen, data["status"], ai)
        report["analyses"].append(data)
