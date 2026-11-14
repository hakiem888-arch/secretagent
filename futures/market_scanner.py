"""Universe -> 20 liquid coins -> 1/2 candidates -> SCALPING M15/M5 -> AI -> journal/alert.

BUG-09, BUG-12, BUG-15, BUG-16, BUG-18:
- Maintains isolated PaperLedger databases per instrument_kind.
- Enforces server-side RBAC on every manual scan, auto-scan, AI review, and Telegram alert.
- Enforces a single active scanner per database via both OS ProcessLock and DB lease lock.
"""
import inspect
import threading
import time
from uuid import uuid4
from dataclasses import dataclass, asdict
from .strategy import evaluate, spot_reference_validation_metadata
from .screener import shortlist, select_candidates
from .groq_rotation import combine, DEFAULT_MODEL
from .market import MarketError
from .paper import PaperLedger, DEFAULT_MAX_GAP_MS
from .spot_market import resolve_db_path
from .security import SecurityManager, GLOBAL_MONITOR
from .process_lock import ProcessLock
from .scanner import ScanBusy, ALLOWED_SCAN_INTERVALS
from . import history


@dataclass(frozen=True)
class ScannerConfig:
    candidates: int = 2
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
    interval: int = 60
    max_gap_ms: int = DEFAULT_MAX_GAP_MS

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
        self.instance_id = f"scanner-{uuid4().hex[:8]}"
        self._base_db = ledger.db
        self._initial_equity = ledger.initial_equity
        initial_kind = getattr(market, "instrument_kind", "USD_M_PERPETUAL")
        self.ledgers = {initial_kind: ledger}
        self.ledger = ledger
        self.security = security or SecurityManager(self._base_db)
        self.trusted_worker = bool(trusted_worker)
        self.worker_session_token = self.security.issue_worker_session() if self.trusted_worker else None
        # Bind security manager to AI pool and Telegram alerts so all calls are authorized
        if hasattr(self.pool, "security"):
            self.pool.security = self.security
        if hasattr(self.alerts, "security"):
            self.alerts.security = self.security

    def _effective_token(self, session_token=None):
        if session_token is not None:
            return session_token
        return self.worker_session_token if self.trusted_worker else None

    def _call_alert(self, method_name, *args, session_token=None):
        fn = getattr(self.alerts, method_name, None)
        if fn is None:
            return "Metode Telegram tidak tersedia"
        eff_token = self._effective_token(session_token)
        if hasattr(self.alerts, "session_token"):
            self.alerts.session_token = eff_token
        params = inspect.signature(fn).parameters
        kwargs = {}
        if "security" in params:
            kwargs["security"] = self.security
        if "session_token" in params:
            kwargs["session_token"] = eff_token
        return fn(*args, **kwargs)

    def _call_ai_review(self, chosen, session_token=None):
        eff_token = self._effective_token(session_token)
        if hasattr(self.pool, "session_token"):
            self.pool.session_token = eff_token
        params = inspect.signature(self.pool.review).parameters
        kwargs = {}
        if "security" in params:
            kwargs["security"] = self.security
        if "session_token" in params:
            kwargs["session_token"] = eff_token
        return self.pool.review(chosen, self.model, **kwargs)

    def ledger_for(self, instrument_kind):
        """BUG-09: Return the isolated PaperLedger for the given instrument_kind."""
        kind = instrument_kind or "USD_M_PERPETUAL"
        if kind not in self.ledgers:
            root = self._base_db.parent.parent if self._base_db.parent.name == "data" else self._base_db.parent
            target_db = resolve_db_path(root, kind, base_db=self._base_db)
            self.ledgers[kind] = PaperLedger(target_db, self._initial_equity, expected_instrument_kind=kind)
        return self.ledgers[kind]

    def _sync_active_ledger(self):
        """Ensure self.ledger points to the database matching the currently active market provider."""
        current_kind = getattr(self.market, "instrument_kind", "USD_M_PERPETUAL")
        target = self.ledger_for(current_kind)
        if self.ledger is not target:
            if hasattr(self.alerts, "ledger") and self.alerts.ledger is self.ledger:
                self.alerts.ledger = target
            if hasattr(self.alerts, "db"):
                self.alerts.db = target.db
            self.ledger = target
        return self.ledger

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
                news = self.calendar.window(snap.now_ms)
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
                    )
                current = next((t for t in self.ledger.active() if t["signal"] == trade["signal"]), None)
                if current and current["status"] == "PENDING" and config.send_alerts and any(
                    word in current["telegram"] for word in ("Belum dicoba", "rate limit", "cooldown", "belum lengkap", "menolak", "dinonaktifkan")):
                    account = self.ledger.account(snap.now_ms, news, fee=p["fee"],
                        slippage_bps=p["slippage_per_unit"] / p["entry"] * 10000,
                        funding=p["funding_per_unit"] / p["entry"], leverage=p["leverage"],
                        risk_pct=p["risk_budget"] / p["equity"] * 100, exclude_signal=current["signal"])
                    result = evaluate(snap, account, paper=True, relaxed=config.relaxed_mode)
                    if result.plan and result.plan["id"] == p["id"]:
                        data = {"result": result, "snapshot": snap, "ai": current["ai"],
                                "status": combine(result, current["ai"]), "monotonic": self.clock()}
                        self.ledger.telegram_status(
                            current["signal"],
                            self._call_alert("send", data, session_token=session_token),
                        )
            except (MarketError, ValueError, KeyError, TypeError) as exc:
                if isinstance(exc, MarketError):
                    report["data_status"] = "CONNECTION_ERROR"
                    if exc.access_blocked:
                        raise
                elif "STALE_DATA" in str(exc) or "kedaluwarsa" in str(exc):
                    report["data_status"] = "STALE_DATA"
                ok = False
                report["errors"].append(p["symbol"] + " monitoring WAIT: " + self.pool.redact(str(exc))[:200])
        return ok

    def run(self, config, manual=False, session_token=None):
        if config.candidates not in (1, 2) or config.interval not in ALLOWED_SCAN_INTERVALS:
            raise ValueError("Konfigurasi scanner tidak valid")
        eff_token = self._effective_token(session_token)
        # BUG-15 & BUG-16 #4: Every scan (manual OR auto-scan) requires server-side authorization
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
        process_lock, started = ProcessLock(self.ledger.db), False
        db_lock_acquired = False
        try:
            try:
                process_lock.acquire()
            except ScanBusy:
                return dict(self.ledger.last_scan_report() or {},
                            scan_notice="Scanner/worker lain sedang berjalan; percobaan baru belum dimulai.")
            if hasattr(self.ledger, "acquire_db_scanner_lock"):
                if not self.ledger.acquire_db_scanner_lock(self.instance_id, int(time.time() * 1000)):
                    return dict(self.ledger.last_scan_report() or {},
                                scan_notice="Scanner/worker lain sedang memegang lock database; percobaan baru belum dimulai.")
                db_lock_acquired = True
            if self.last and self.last.get("market_error", {}).get("access_blocked") and not manual:
                return self.last
            if self.last and not manual and self.clock() - self.last_start < config.interval - .5:
                return self.last
            if not self.ledger.claim_scan_tick(time.time(), config.interval, manual):
                return self.ledger.last_scan_report() or {"errors": ["Menunggu interval scan bersama berikutnya"]}
            self.last_start = self.clock()
            started = True
            current_kind = getattr(self.market, "instrument_kind", "USD_M_PERPETUAL")
            report = {"scan_id": uuid4().hex[:12], "started_at_ms": int(time.time() * 1000),
                      "instrument_kind": current_kind,
                      "ledger_db": str(self.ledger.db),
                      "validation": spot_reference_validation_metadata(current_kind),
                      "scan_status": "RUNNING", "data_status": "OK", "trigger": "MANUAL" if manual else "AUTO",
                      "now_ms": 0, "universe_loaded": False, "market_error": {},
                      "universe_count": 0, "eligible_count": 0, "shortlist": [],
                      "long_watchlist": [], "short_watchlist": [],
                      "long_watchlist_count": 0, "short_watchlist_count": 0,
                      "selected": [], "ranked": [], "analyses": [], "errors": [], "monitored": 0,
                      "news": {"ok": False, "reason": "Belum diperiksa"}, "config": asdict(config)}
            self.last = report
            try:
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
                            )
                monitor_ok = self.monitor(report, config, session_token=eff_token)
                GLOBAL_MONITOR.increment("market_api_requests")
                universe = self.market.universe()
                self._sync_active_ledger()
                report["instrument_kind"] = getattr(self.market, "instrument_kind", report["instrument_kind"])
                report["ledger_db"] = str(self.ledger.db)
                report["validation"] = spot_reference_validation_metadata(report["instrument_kind"])
                report["market_provider"] = getattr(self.market, "market_label", "Unknown")
                if getattr(self.market, "failover_reason", None):
                    report["provider_failover"] = self.market.failover_reason
                now = universe["now_ms"]
                report["now_ms"] = max(now, report["now_ms"])
                screened = shortlist(universe, config.min_volume, limit=20, allow_medium_liquidity_fallback=config.relaxed_mode)
                report.update(
                    universe_loaded=True,
                    universe_count=screened["universe_count"],
                    eligible_count=screened["eligible_count"],
                    shortlist=screened["rows"],
                    long_watchlist=screened.get("long_watchlist", []),
                    short_watchlist=screened.get("short_watchlist", []),
                    long_watchlist_count=len(screened.get("long_watchlist", [])),
                    short_watchlist_count=len(screened.get("short_watchlist", [])),
                )
                report["news"] = self.calendar.window(now)
                if not monitor_ok:
                    report["errors"].append("Sinyal baru ditahan karena posisi paper belum mendapat data segar")
                    return report
                if not report["news"]["ok"]:
                    report["errors"].append(report["news"]["reason"])
                    return report
                active = self.ledger.active()
                if len(active) >= 2:
                    report["errors"].append("Dua posisi/pending paper aktif; tetap dipantau, sinyal baru ditahan")
                    return report
                busy = {t["plan"]["symbol"] for t in active}
                selected, ranked, errors = select_candidates([r for r in screened["rows"] if r["symbol"] not in busy],
                    self.market, now, min(config.candidates, 2 - len(active)))
                report.update(selected=selected, ranked=ranked,
                    long_watchlist=[x for x in ranked if x.get("candidate_direction") == "LONG"],
                    short_watchlist=[x for x in ranked if x.get("candidate_direction") == "SHORT"],
                    long_watchlist_count=sum(x.get("candidate_direction") == "LONG" for x in ranked),
                    short_watchlist_count=sum(x.get("candidate_direction") == "SHORT" for x in ranked))
                report["errors"].extend(errors)
                processing_order = sorted(selected, key=lambda r: r["symbol"] != self.next_symbol)
                for row in processing_order:
                    if self.clock() - self.last_start > 60:
                        self.next_symbol = row["symbol"]
                        report["errors"].append("Anggaran waktu siklus tercapai; kandidat tersisa menunggu siklus berikutnya")
                        break
                    try:
                        if self.next_symbol == row["symbol"]:
                            self.next_symbol = None
                        self._analyse(row["symbol"], config, report, candidate_info=row, session_token=eff_token)
                    except (MarketError, ValueError, KeyError, TypeError) as exc:
                        if isinstance(exc, MarketError) and exc.access_blocked:
                            raise
                        report["errors"].append(row["symbol"] + " WAIT: " + self.pool.redact(str(exc))[:300])
                if config.send_scan_summary and hasattr(self.alerts, "send_summary"):
                    report["summary_telegram"] = self._call_alert("send_summary", report, session_token=eff_token)
            except (MarketError, ValueError, KeyError, TypeError) as exc:
                report["scan_status"] = "FAILED"
                if isinstance(exc, MarketError):
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
                if self.last is not None and started:
                    self.last["completed_at_ms"] = int(time.time() * 1000)
                    self.last["duration_seconds"] = round(self.clock() - self.last_start, 2)
                    self.last["resources"] = GLOBAL_MONITOR.snapshot()
                    if self.last["scan_status"] == "RUNNING":
                        self.last["scan_status"] = "COMPLETED_WITH_NOTES" if self.last["errors"] else "COMPLETED"
                    self.ledger.save_scan_report(self.last)
            finally:
                if db_lock_acquired and hasattr(self.ledger, "release_db_scanner_lock"):
                    try:
                        self.ledger.release_db_scanner_lock(self.instance_id)
                    except Exception:
                        pass
                process_lock.release()
                self.lock.release()

    def _analyse(self, symbol, config, report, candidate_info=None, session_token=None):
        GLOBAL_MONITOR.increment("market_api_requests")
        snapshots = self.market.load_modes(symbol, ("SCALPING",))
        self._sync_active_ledger()
        results = []
        for mode in ("SCALPING",):
            snap = snapshots[mode]
            news_win = self.calendar.window(snap.now_ms)
            account = self.ledger.account(snap.now_ms, news_win, **config.account_args())
            results.append(evaluate(snap, account, paper=True, relaxed=config.relaxed_mode))
        chosen = max(results, key=mode_score)
        if chosen.meta.get("stale_data"):
            report["data_status"] = "STALE_DATA"
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
        GLOBAL_MONITOR.increment("groq_ai_requests")
        ai = self._call_ai_review(chosen, session_token=session_token)
        if combine(chosen, ai) == "VALID":
            reviewed_id = chosen.plan["id"]
            if not self.monitor(report, config, session_token=session_token):
                ai = {"ok": False, "decision": "WAIT", "reasons": ["Portofolio belum terverifikasi ulang"], "cautions": []}
            GLOBAL_MONITOR.increment("market_api_requests")
            snap = self.market.load(symbol, mode)
            self._sync_active_ledger()
            acquired = self.clock()
            news_win = self.calendar.window(snap.now_ms)
            account = self.ledger.account(snap.now_ms, news_win, **config.account_args())
            chosen = evaluate(snap, account, paper=True, relaxed=config.relaxed_mode)
            chosen.meta["mode_comparison"] = comparison
            if not chosen.plan or chosen.plan["id"] != reviewed_id:
                ai = {"ok": False, "decision": "WAIT", "reasons": ["Harga, setup, atau anggaran berubah setelah AI"], "cautions": []}
        final_status = combine(chosen, ai)
        stage_status = "ENTRY VALID" if final_status == "VALID" else (
            "WAITING FOR CONFIRMATION" if (candidate_info and candidate_info.get("m15_score", 0) >= 70) else (
                "WATCHLIST" if final_status == "WAIT" else "REJECTED"
            )
        )
        data = {"snapshot": snap, "result": chosen, "ai": ai, "status": final_status,
                "stage_status": stage_status, "candidate_info": candidate_info or {},
                "monotonic": acquired, "paper": "Tidak dicatat: WAIT/NO TRADE", "telegram": "Tidak dikirim"}
        report["now_ms"] = max(report["now_ms"], snap.now_ms)
        if data["status"] == "VALID":
            queued, reason = self.ledger.queue(data, self.clock)
            if queued:
                data["paper"] = "PENDING · " + reason
                data["telegram"] = self._call_alert("send", data, session_token=session_token) if config.send_alerts else "Alert Telegram dinonaktifkan."
                self.ledger.telegram_status(reason, data["telegram"])
            else:
                data["paper"] = reason
                data["status"] = "WAIT"
                data["stage_status"] = "WAITING FOR CONFIRMATION"
        elif (config.send_watchlist_alerts and hasattr(self.alerts, "send_watchlist")
              and candidate_info and candidate_info.get("m15_score", 0) >= 70):
            data["telegram"] = self._call_alert("send_watchlist", data, candidate_info, session_token=session_token)
        history.save(self.ledger.db, chosen, data["status"], ai)
        report["analyses"].append(data)
