"""Persistent virtual portfolio; observed-quote fills, never real exchange orders.

BUG-17, BUG-18, BUG-22, BUG-23, BUG-24, BUG-25:
- Propagates centralized `DATABASE_URL` (`pg_url` / `secret_getter`) to all `PaperLedger` connections.
- Automatically pushes verified, checksummed, namespaced state backups to `SupabaseStateSync`
  after every state-changing transaction and automatically restores when local SQLite is empty
  on startup after reboot, while rejecting stale/older backups (`allow_if_newer_only=True`).
- Delegates health/integrity checks to `verify_database_health` (distinguishing SQLite vs PostgreSQL).
"""
import json
import math
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from .db_backend import (
    BACKUP_SCHEMA_VERSION,
    VALID_INSTRUMENT_KINDS,
    SupabaseStateSync,
    atomic_transaction,
    compute_backup_checksum,
    compute_backup_hmac,
    configured_database_url,
    configured_supabase_rest,
    connect_database,
    persistence_status,
    verify_database_health,
)
from .models import Account, TIMEFRAMES
from .indicators import indicators
from .strategy import break_even, quantize, frame_problem, spot_reference_validation_metadata
from .lifecycle import after_fill, manage_position, evaluate_intrabar_ohlc
from .telegram import eligible, candle_key
from .version import APP_VERSION

ACTIVE = ("PENDING", "OPEN")
DEFAULT_MAX_GAP_MS = 180000


def classify_monitoring_state(snap=None, last_quote_ms=None, max_gap_ms=DEFAULT_MAX_GAP_MS, error=None):
    """BUG-04: Explicitly distinguish CONNECTION_ERROR, STALE_DATA, DATA_GAP, and OK."""
    if error is not None:
        return "CONNECTION_ERROR"
    if snap is None:
        return "CONNECTION_ERROR"
    if not all(0 <= snap.now_ms - snap.quote_times.get(k, 0) <= 30000 for k in snap.quote_keys):
        return "STALE_DATA"
    if last_quote_ms is not None and snap.now_ms - last_quote_ms > max_gap_ms:
        return "DATA_GAP"
    return "OK"


class PaperLedger:
    def __init__(
        self,
        db,
        initial_equity=1000.0,
        expected_instrument_kind=None,
        pg_url=None,
        secret_getter=None,
        supabase_sync=None,
    ):
        if not math.isfinite(initial_equity) or initial_equity <= 0:
            raise ValueError("Saldo awal paper harus positif")
        self.db = Path(db)
        self.initial_equity = float(initial_equity)
        self.expected_instrument_kind = expected_instrument_kind
        self.secret_getter = secret_getter
        self.pg_url = configured_database_url(get_secret=secret_getter, explicit_url=pg_url)
        ns_kind = self.expected_instrument_kind or "DEFAULT"
        self.namespace = f"{ns_kind}:{self.db.name}"
        if supabase_sync is not None:
            self.supabase_sync = supabase_sync
        else:
            sb_url, sb_key = configured_supabase_rest(get_secret=secret_getter)
            self.supabase_sync = SupabaseStateSync(sb_url, sb_key)
        if not self.pg_url and self.supabase_sync.enabled:
            self._restore_from_supabase_if_empty()

    def integrity_check(self):
        """BUG-13 & BUG-24: Check authentic database health according to the active backend (SQLite vs PostgreSQL)."""
        return verify_database_health(
            self.db,
            pg_url=self.pg_url,
            get_secret=self.secret_getter,
            supabase_sync=self.supabase_sync,
        )

    def persistence_info(self):
        """BUG-22 & BUG-23: Return verified persistence status for this ledger's actual backend."""
        return persistence_status(
            db_path=self.db,
            get_secret=self.secret_getter,
            pg_url=self.pg_url,
            ledger=self,
            supabase_sync=self.supabase_sync,
        )

    def connect(self):
        conn = connect_database(self.db, pg_url=self.pg_url, get_secret=self.secret_getter)
        conn.execute("CREATE TABLE IF NOT EXISTS paper_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
            signal TEXT PRIMARY KEY, symbol TEXT, mode TEXT, status TEXT,
            realized REAL, updated_ms INTEGER, payload TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS paper_status ON paper_trades(status)")
        conn.execute("""CREATE TABLE IF NOT EXISTS paper_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, signal TEXT, at_ms INTEGER,
            event TEXT, qty REAL, price REAL, pnl REAL, note TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS telegram_alerts (
            target TEXT, signal TEXT, state TEXT, retry_at REAL, updated_at REAL,
            PRIMARY KEY(target, signal))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS security_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL,
            role TEXT NOT NULL, session_id TEXT, action TEXT NOT NULL,
            status TEXT NOT NULL, detail TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS security_quota_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bucket TEXT NOT NULL,
            session_id TEXT NOT NULL, event_ts REAL NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS scanner_locks (
            lock_name TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
            acquired_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL)""")
        conn.execute("INSERT OR IGNORE INTO paper_meta VALUES ('initial_equity',?)", (str(self.initial_equity),))
        if self.expected_instrument_kind:
            conn.execute("INSERT OR IGNORE INTO paper_meta VALUES ('instrument_kind',?)", (str(self.expected_instrument_kind),))
        conn.execute("INSERT OR IGNORE INTO paper_meta VALUES ('namespace',?)", (str(self.namespace),))
        conn.commit()
        return conn

    @staticmethod
    def _current_state_updated_ms(conn):
        candidates = [0]
        row_meta = conn.execute("SELECT value FROM paper_meta WHERE key='state_updated_ms'").fetchone()
        if row_meta and row_meta[0]:
            try:
                candidates.append(int(row_meta[0]))
            except (TypeError, ValueError):
                pass
        row_trade = conn.execute("SELECT COALESCE(MAX(updated_ms), 0) FROM paper_trades").fetchone()
        if row_trade and row_trade[0]:
            try:
                candidates.append(int(row_trade[0]))
            except (TypeError, ValueError):
                pass
        row_ev = conn.execute("SELECT COALESCE(MAX(at_ms), 0) FROM paper_events").fetchone()
        if row_ev and row_ev[0]:
            try:
                candidates.append(int(row_ev[0]))
            except (TypeError, ValueError):
                pass
        return max(candidates)

    @classmethod
    def _mark_state_updated(cls, conn, now_ms=None):
        stamp = int(now_ms if now_ms is not None else int(time.time() * 1000))
        current = cls._current_state_updated_ms(conn)
        effective = max(stamp, current)
        conn.execute("INSERT OR REPLACE INTO paper_meta VALUES ('state_updated_ms',?)", (str(effective),))
        return effective

    def sync_to_supabase(self, backup_at_ms=None):
        """BUG-23 #1: Push current ledger state snapshot to Supabase REST if enabled."""
        if not self.supabase_sync or not self.supabase_sync.enabled:
            return False
        try:
            payload_json = self.export_json(backup_at_ms=backup_at_ms)
            return self.supabase_sync.push_backup(self.namespace, payload_json, backup_at_ms=backup_at_ms)
        except Exception as exc:
            self.supabase_sync.last_push_ok = False
            self.supabase_sync.last_error = str(exc)
            return False

    def _restore_from_supabase_if_empty(self):
        """BUG-23 #2: Automatically restore state from Supabase REST if local database is empty after reboot."""
        if not self.supabase_sync or not self.supabase_sync.enabled:
            return False
        try:
            with closing(self.connect()) as conn:
                trade_cnt = int(conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0])
                local_ms = self._current_state_updated_ms(conn)
            if trade_cnt > 0 or local_ms > 0:
                return False
            envelope = self.supabase_sync.pull_backup(self.namespace)
            if not envelope:
                return False
            res = self.import_backup_json(envelope, allow_if_newer_only=True, _from_sync=True)
            return bool(res.get("ok"))
        except Exception as exc:
            self.supabase_sync.last_pull_ok = False
            self.supabase_sync.last_error = str(exc)
            return False

    def bound_instrument_kind(self):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT value FROM paper_meta WHERE key='instrument_kind'").fetchone()
            return row[0] if row else self.expected_instrument_kind

    def acquire_db_scanner_lock(self, owner_id, now_ms, ttl_ms=120000):
        """BUG-18: Enforce at most one active scanner per database via transactional lease lock."""
        now_ms = int(now_ms)
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                row = conn.execute(
                    "SELECT owner_id, expires_ms FROM scanner_locks WHERE lock_name='active_scanner'"
                ).fetchone()
                if row and row[0] != owner_id and int(row[1]) > now_ms:
                    return False
                conn.execute(
                    "INSERT OR REPLACE INTO scanner_locks VALUES ('active_scanner',?,?,?)",
                    (str(owner_id), now_ms, now_ms + int(ttl_ms)),
                )
                return True

    def release_db_scanner_lock(self, owner_id):
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                conn.execute(
                    "DELETE FROM scanner_locks WHERE lock_name='active_scanner' AND owner_id=?",
                    (str(owner_id),),
                )

    def record_worker_heartbeat(self, worker_id, now_ms, interval_seconds):
        """BUG-18: Record background worker heartbeat in persistent database for read-only dashboard."""
        payload = json.dumps({
            "worker_id": str(worker_id),
            "heartbeat_ms": int(now_ms),
            "interval_seconds": int(interval_seconds),
            "version": APP_VERSION,
        })
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                conn.execute("INSERT OR REPLACE INTO paper_meta VALUES ('worker_heartbeat',?)", (payload,))

    def worker_heartbeat(self):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT value FROM paper_meta WHERE key='worker_heartbeat'").fetchone()
            return json.loads(row[0]) if row else None

    def worker_health_status(self, now_ms=None, max_stale_multiplier=2.5):
        """OPS-03: Monitor 24/7 worker heartbeat and detect dead/stale worker or lost database connection."""
        try:
            hb = self.worker_heartbeat()
        except Exception as exc:
            return {
                "status": "DB_CONNECTION_LOST",
                "alive": False,
                "worker_id": None,
                "heartbeat_ms": None,
                "stale_ms": None,
                "error": f"{type(exc).__name__}: {exc}",
                "detail": f"Gagal memeriksa heartbeat worker karena koneksi database terputus: {type(exc).__name__}",
            }
        if not hb:
            return {
                "status": "NO_WORKER",
                "alive": False,
                "worker_id": None,
                "heartbeat_ms": None,
                "stale_ms": None,
                "detail": "Worker 24/7 belum terdaftar pada database ini (mode on-demand dashboard).",
            }
        now_val = int(now_ms if now_ms is not None else int(time.time() * 1000))
        hb_ms = int(hb.get("heartbeat_ms") or 0)
        interval_s = max(30, int(hb.get("interval_seconds") or 300))
        max_allowed_stale_ms = max(120_000, int(interval_s * 1000 * float(max_stale_multiplier)))
        age_ms = max(0, now_val - hb_ms)
        if age_ms > max_allowed_stale_ms:
            return {
                "status": "WORKER_DEAD_OR_DISCONNECTED",
                "alive": False,
                "worker_id": hb.get("worker_id"),
                "heartbeat_ms": hb_ms,
                "interval_seconds": interval_s,
                "stale_ms": age_ms,
                "max_allowed_stale_ms": max_allowed_stale_ms,
                "detail": (
                    f"PERINGATAN: Worker 24/7 ({hb.get('worker_id')}) tidak mengirim heartbeat selama "
                    f"{age_ms // 1000} detik (batas {max_allowed_stale_ms // 1000} detik); "
                    "worker kemungkinan berhenti atau kehilangan koneksi."
                ),
            }
        return {
            "status": "WORKER_ALIVE",
            "alive": True,
            "worker_id": hb.get("worker_id"),
            "heartbeat_ms": hb_ms,
            "interval_seconds": interval_s,
            "stale_ms": age_ms,
            "max_allowed_stale_ms": max_allowed_stale_ms,
            "detail": f"Worker 24/7 ({hb.get('worker_id')}) aktif dan sehat.",
        }

    @staticmethod
    def _rows(conn, active=False):
        where = " WHERE status IN ('PENDING','OPEN')" if active else ""
        return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM paper_trades" + where + " ORDER BY updated_ms DESC")]

    @staticmethod
    def _store(conn, t):
        conn.execute("INSERT OR REPLACE INTO paper_trades VALUES (?,?,?,?,?,?,?)", (
            t["signal"], t["plan"]["symbol"], t["plan"]["mode"], t["status"], t["realized_pnl"], t["updated_ms"],
            json.dumps(t, allow_nan=False)))

    @staticmethod
    def _event(conn, t, kind, qty=0, price=None, pnl=0, note=""):
        conn.execute("INSERT INTO paper_events(signal,at_ms,event,qty,price,pnl,note) VALUES (?,?,?,?,?,?,?)",
                     (t["signal"], t["updated_ms"], kind, qty, price, pnl, note))

    @staticmethod
    def _risk(t):
        fraction = 1 if t["status"] == "PENDING" else t["remaining_qty"] / t["plan"]["qty"]
        return t["plan"]["risk"] * fraction

    @staticmethod
    def _reserved(t):
        p = t["plan"]
        q = p["qty"] if t["status"] == "PENDING" else t["remaining_qty"]
        return q * (p["entry"] / p["leverage"] + p["cost_sl_per_unit"])

    def _account(self, conn, now_ms, exclude_signal=None):
        active = self._rows(conn, True)
        active = [t for t in active if not (t["signal"] == exclude_signal and t["status"] == "PENDING")]
        initial = float(conn.execute("SELECT value FROM paper_meta WHERE key='initial_equity'").fetchone()[0])
        realized = float(conn.execute("SELECT COALESCE(SUM(realized),0) FROM paper_trades").fetchone()[0])
        unrealized = 0.0
        for t in active:
            if t["status"] == "OPEN":
                p = t["plan"]
                sign = 1 if p["direction"] == "LONG" else -1
                unrealized += t["remaining_qty"] * (sign * (t["mark"] - p["entry"]) - t["mark"] * p["fee"] - p["slippage_per_unit"])
        equity = initial + realized + unrealized
        day = datetime.fromtimestamp(now_ms / 1000, timezone.utc).date().isoformat()
        stored = dict(conn.execute("SELECT key,value FROM paper_meta"))
        day = max(day, stored.get("day", day))
        if stored.get("day") != day:
            conn.execute("INSERT OR REPLACE INTO paper_meta VALUES ('day',?)", (day,))
            conn.execute("INSERT OR REPLACE INTO paper_meta VALUES ('day_start',?)", (str(equity),))
            start = equity
        else:
            start = float(stored["day_start"])
        midnight = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)
        closed = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM paper_trades WHERE status='CLOSED' AND updated_ms>=? ORDER BY updated_ms DESC", (midnight,))]
        losses = 0
        for t in closed:
            if t["realized_pnl"] >= 0:
                break
            losses += 1
        return {"initial_equity": initial, "wallet": initial + realized, "equity": equity,
                "realized": realized, "unrealized": unrealized, "day_start": start, "day": day,
                "free_margin": max(0, equity - sum(self._reserved(t) for t in active)),
                "active": len(active), "total_risk": sum(self._risk(t) for t in active),
                "alt_risk": sum(self._risk(t) for t in active if t["plan"]["symbol"] != "BTCUSDT"), "losses": losses,
                "instrument_kind": stored.get("instrument_kind", self.expected_instrument_kind)}

    def summary(self, now_ms):
        with closing(self.connect()) as conn, conn:
            return self._account(conn, now_ms)

    def account(self, now_ms, news, fee=.0005, slippage_bps=2.0, funding=.0003, leverage=2, risk_pct=.5, exclude_signal=None):
        with closing(self.connect()) as conn, conn:
            s = self._account(conn, now_ms, exclude_signal)
        a = Account(equity=s["equity"], free_margin=s["free_margin"], day_start_equity=s["day_start"],
                    account_day_utc=s["day"], account_verified=True, active_pending_count=s["active"],
                    total_existing_risk=s["total_risk"], correlated_alt_risk=s["alt_risk"], consecutive_losses=s["losses"],
                    taker_fee=fee, exit_slippage_bps=slippage_bps, funding_reserve_rate=funding, costs_verified=True,
                    leverage=leverage, risk_pct=risk_pct, isolated=True, order_semantics_verified=True)
        if news.get("ok"):
            a.news_source, a.news_verified_at_ms = news["source"], news["checked_ms"]
            a.news_clear_from_ms, a.news_clear_until_ms = news["from_ms"], news["until_ms"]
        return a

    def active(self):
        with closing(self.connect()) as conn:
            return self._rows(conn, True)

    def expire_pending(self, now_ms):
        changed = False
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                for t in self._rows(conn, True):
                    if t["status"] == "PENDING" and now_ms >= t["plan"]["expires_ms"]:
                        t["updated_ms"] = now_ms
                        self._cancel(conn, t, "EXPIRED")
                        self._store(conn, t)
                        changed = True
                if changed:
                    self._mark_state_updated(conn, now_ms)
        if changed:
            self.sync_to_supabase(backup_at_ms=now_ms)

    def trades(self, limit=500):
        with closing(self.connect()) as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM paper_trades ORDER BY updated_ms DESC LIMIT ?", (limit,))]

    def claim_scan_tick(self, now, interval, manual=False):
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                row = conn.execute("SELECT value FROM paper_meta WHERE key='scan_start'").fetchone()
                if row and not manual and 0 <= now - float(row[0]) < interval - .5:
                    return False
                conn.execute("INSERT OR REPLACE INTO paper_meta VALUES ('scan_start',?)", (str(now),))
                return True

    def save_scan_report(self, report):
        summary = {k: v for k, v in report.items() if k not in ("analyses", "display_monotonic", "display_server_age")}
        summary["analyses"] = []
        summary["worker_analyses"] = [{"Pair": d["snapshot"].symbol, "Mode": d["snapshot"].mode,
            "Arah": d["result"].direction, "Status": d["status"], "Paper": d["paper"], "Telegram": d["telegram"]}
            for d in report.get("analyses", [])]
        stamp_ms = int(report.get("now_ms") or report.get("completed_at_ms") or int(time.time() * 1000))
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                conn.execute("INSERT OR REPLACE INTO paper_meta VALUES ('scan_report',?)", (json.dumps(summary, allow_nan=False),))
                self._mark_state_updated(conn, stamp_ms)
        self.sync_to_supabase(backup_at_ms=stamp_ms)

    def last_scan_report(self):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT value FROM paper_meta WHERE key='scan_report'").fetchone()
            return json.loads(row[0]) if row else None

    def save_ui_preferences(self, prefs):
        """Persist administrator sidebar preferences in DB so restarts/reconnects never reset them."""
        payload = json.dumps(dict(prefs or {}), sort_keys=True, allow_nan=False)
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                existing = conn.execute("SELECT value FROM paper_meta WHERE key='ui_preferences'").fetchone()
                if existing and existing[0] == payload:
                    return False
                conn.execute("INSERT OR REPLACE INTO paper_meta VALUES ('ui_preferences',?)", (payload,))
                return True

    def load_ui_preferences(self):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT value FROM paper_meta WHERE key='ui_preferences'").fetchone()
            if not row or not row[0]:
                return {}
            try:
                data = json.loads(row[0])
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

    def queue(self, data, monotonic):
        if data["result"].meta.get("scope") != "PAPER" or not eligible(data, monotonic):
            return False, "Tidak ada sinyal PAPER yang valid dan segar"
        p, now_ms = data["result"].plan, data["snapshot"].now_ms
        kind = p.get("instrument_kind", "USD_M_PERPETUAL")
        signal = candle_key(p)
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                bound = conn.execute("SELECT value FROM paper_meta WHERE key='instrument_kind'").fetchone()
                if bound and bound[0] != kind:
                    raise ValueError("Sumber jurnal berbeda; data spot dan perpetual tidak boleh dicampur dalam satu database")
                conn.execute("INSERT OR IGNORE INTO paper_meta VALUES ('instrument_kind',?)", (kind,))
                if conn.execute("SELECT 1 FROM paper_trades WHERE signal=?", (signal,)).fetchone():
                    return False, "Sinyal candle ini sudah tercatat"
                if conn.execute("SELECT 1 FROM paper_trades WHERE symbol=? AND status IN ('PENDING','OPEN')", (p["symbol"],)).fetchone():
                    return False, "Pair sudah memiliki posisi/pending paper"
                s = self._account(conn, now_ms)
                if (s["active"] >= 2 or s["equity"] <= 0 or p["risk"] > s["equity"] * .005 + 1e-8 or
                    s["total_risk"] + p["risk"] > s["equity"] * .01 + 1e-8 or
                    (p["symbol"] != "BTCUSDT" and s["alt_risk"] + p["risk"] > s["equity"] * .005 + 1e-8) or
                    p["margin"] > s["equity"] * .2 + 1e-8 or p["margin"] + p["qty"] * p["cost_sl_per_unit"] > s["free_margin"] + 1e-8 or
                    s["losses"] >= 3 or s["equity"] <= s["day_start"] * .98):
                    return False, "Batas portofolio berubah; paper trade tidak dibuka"
                val_meta = spot_reference_validation_metadata(kind)
                t = {"signal": signal, "plan": p, "status": "PENDING", "created_ms": now_ms, "updated_ms": now_ms,
                     "last_quote_ms": now_ms, "remaining_qty": 0.0, "realized_pnl": 0.0, "mark": data["snapshot"].reference_price,
                     "triggered": False, "stop": p["sl"], "tp1_ms": None, "tp2_ms": None, "tp3_ms": None, "seen_one_r": False,
                     "quality": "POLLING_SIMULATION", "note": "Menunggu trigger; belum dianggap fill", "telegram": "Belum dicoba",
                     "ai": data.get("ai"), "scope": "PAPER_SIMULATION", "instrument_kind": kind, **val_meta}
                self._store(conn, t)
                self._event(conn, t, "SIGNAL", note=t["note"])
                self._mark_state_updated(conn, now_ms)
        self.sync_to_supabase(backup_at_ms=now_ms)
        return True, signal

    def telegram_status(self, signal, text):
        updated_ms = None
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                row = conn.execute("SELECT payload FROM paper_trades WHERE signal=?", (signal,)).fetchone()
                if row:
                    t = json.loads(row[0])
                    t["telegram"] = text
                    self._store(conn, t)
                    updated_ms = int(t.get("updated_ms") or int(time.time() * 1000))
                    self._mark_state_updated(conn, updated_ms)
        if updated_ms is not None:
            self.sync_to_supabase(backup_at_ms=updated_ms)

    def _cancel(self, conn, t, reason):
        t.update(status="CANCELLED", note=reason)
        self._event(conn, t, "CANCELLED", note=reason)

    def _exit(self, conn, t, qty, price, reason):
        p = t["plan"]
        sign = 1 if p["direction"] == "LONG" else -1
        qty = min(qty, t["remaining_qty"])
        pnl = qty * (sign * (price - p["entry"]) - price * p["fee"])
        t["realized_pnl"] += pnl
        t["remaining_qty"] = max(0.0, round(t["remaining_qty"] - qty, 12))
        t["note"] = reason
        if t["remaining_qty"] < p["quantity_step"] * .01:
            t.update(status="CLOSED", closed_ms=t["updated_ms"])
        self._event(conn, t, reason, qty, price, pnl, "Fill virtual; fee taker dan slippage model")

    def advance(self, snap, news_ok=True, max_gap_ms=DEFAULT_MAX_GAP_MS, allow_partial=False):
        reference = snap.reference_price
        spot = snap.instrument_kind == "SPOT_REFERENCE"
        # BUG-01: Strict quote freshness validation; never rewrite timestamps or process expired quotes.
        if not all(0 <= snap.now_ms - snap.quote_times.get(k, 0) <= 30000 for k in snap.quote_keys):
            raise ValueError("STALE_DATA: Quote pemantauan paper kedaluwarsa")
        if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in (snap.last, reference, snap.bid, snap.ask)) or snap.ask < snap.bid:
            raise ValueError("Harga paper tidak valid")
        if not snap.bids or not snap.asks or not all(math.isfinite(float(p)) and math.isfinite(float(q)) and float(p) > 0 and float(q) >= 0
                                                    for p, q in snap.bids + snap.asks):
            raise ValueError("Depth paper tidak valid")
        if frame_problem(snap.execution, TIMEFRAMES[snap.mode][1], snap.now_ms) or frame_problem(snap.bias, TIMEFRAMES[snap.mode][0], snap.now_ms):
            raise ValueError("Candle pemantauan paper belum lengkap")
        changed = False
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                bound = conn.execute("SELECT value FROM paper_meta WHERE key='instrument_kind'").fetchone()
                if bound and bound[0] != snap.instrument_kind:
                    raise ValueError("Sumber jurnal berbeda; data spot dan perpetual tidak boleh dicampur")
                for t in self._rows(conn, True):
                    p = t["plan"]
                    if p.get("instrument_kind", "USD_M_PERPETUAL") != snap.instrument_kind:
                        raise ValueError("Sumber jurnal berbeda; data spot dan perpetual tidak boleh dicampur")
                    if p["symbol"] != snap.symbol or p["mode"] != snap.mode or snap.now_ms <= t["last_quote_ms"]:
                        continue
                    changed = True
                    gap = snap.now_ms - t["last_quote_ms"] > max_gap_ms
                    t.update(updated_ms=snap.now_ms, last_quote_ms=snap.now_ms, mark=reference)
                    sign = 1 if p["direction"] == "LONG" else -1
                    market_exit = (snap.bid if sign == 1 else snap.ask) - sign * p["slippage_per_unit"]
                    if market_exit <= 0:
                        raise ValueError("Harga exit virtual tidak valid")
                    if gap:
                        t["quality"] = "DATA_GAP"
                        if t["status"] == "PENDING":
                            self._cancel(conn, t, "DATA_GAP: pending dibatalkan; tidak mengarang fill saat offline")
                        else:
                            self._exit(conn, t, t["remaining_qty"], market_exit, "DATA_GAP_EXIT")
                        self._store(conn, t)
                        continue
                    if t["status"] == "PENDING":
                        is_relaxed = bool(p.get("relaxed_mode"))
                        b = indicators(snap.bias)
                        current_atr = float(indicators(snap.execution).atr.iloc[-1])
                        entry_book, exit_book = (snap.asks, snap.bids) if sign == 1 else (snap.bids, snap.asks)
                        entry_quote, exit_quote = (snap.ask, snap.bid) if sign == 1 else (snap.bid, snap.ask)
                        liquid = (sum(float(q) for price, q in entry_book if abs(float(price) - entry_quote) <= .10 * current_atr) >= p["qty"] and
                                  sum(float(q) for price, q in exit_book if abs(float(price) - exit_quote) <= p["slippage_per_unit"]) >= p["qty"])
                        last_b, old_b = b.iloc[-1], b.iloc[-6]
                        if is_relaxed:
                            bias_ok = ((last_b.close > last_b.ema50 if sign == 1 else last_b.close < last_b.ema50) and last_b.adx >= 15.0)
                        else:
                            bias_ok = (last_b.close > last_b.ema50 > last_b.ema200 and last_b.ema50 > old_b.ema50 if sign == 1 else
                                       last_b.close < last_b.ema50 < last_b.ema200 and last_b.ema50 < old_b.ema50) and last_b.adx >= 20
                        max_spread = 0.05 * current_atr
                        if snap.now_ms >= p["expires_ms"]:
                            self._cancel(conn, t, "EXPIRED")
                        elif not news_ok:
                            self._cancel(conn, t, "NEWS_WAIT")
                        elif sign * ((min(snap.last, reference) if sign == 1 else max(snap.last, reference)) - p["sl"]) <= 0:
                            self._cancel(conn, t, "SL_BEFORE_FILL")
                        elif (not bias_ok or not liquid or not math.isfinite(current_atr) or current_atr <= 0 or
                              snap.ask - snap.bid > max_spread or
                              (not spot and (abs(reference - snap.last) > .25 * current_atr or
                               snap.funding_rate is None or p["funding_per_unit"] / p["entry"] < max(0, sign * snap.funding_rate)))):
                            self._cancel(conn, t, "FILTER_CHANGED")
                        else:
                            if sign * (snap.last - p["entry_trigger"]) >= 0:
                                t["triggered"] = True
                            quote = snap.ask if sign == 1 else snap.bid
                            book = snap.asks if sign == 1 else snap.bids
                            depth = sum(float(q) for price, q in book if sign * (float(price) - p["entry"]) <= 0 and float(q) >= 0)
                            limit_ok = sign * (quote - p["entry"]) <= 0
                            if t["triggered"] and limit_ok and depth >= p["qty"]:
                                p = after_fill(p, p["entry"], p["qty"], snap.now_ms)
                                charge = p["qty"] * (p["entry"] * p["fee"] + p["funding_per_unit"])
                                t.update(plan=p, status="OPEN", remaining_qty=p["qty"], realized_pnl=-charge,
                                         stop=p["sl"], note="Fill PAPER pada limit rencana; fee + cadangan funding dibebankan")
                                self._event(conn, t, "ENTRY", p["qty"], p["entry"], -charge, t["note"])
                            elif allow_partial and t["triggered"] and limit_ok and 0 < depth < p["qty"]:
                                partial_qty = quantize(depth, p["quantity_step"])
                                if partial_qty >= p["min_qty"] and partial_qty * p["entry"] >= p["min_notional"]:
                                    p = after_fill(p, p["entry"], partial_qty, snap.now_ms)
                                    charge = p["qty"] * (p["entry"] * p["fee"] + p["funding_per_unit"])
                                    t.update(plan=p, status="OPEN", remaining_qty=p["qty"], realized_pnl=-charge,
                                             stop=p["sl"], note="Partial fill PAPER pada limit rencana; proteksi SL disesuaikan kuantitas terisi")
                                    self._event(conn, t, "PARTIAL_ENTRY", p["qty"], p["entry"], -charge, t["note"])
                    if t["status"] == "OPEN":
                        p = t["plan"]
                        intrabar_check = evaluate_intrabar_ohlc(
                            p, snap.last, snap.open_bar_high, snap.open_bar_low, snap.last, current_stop=p["sl"]
                        )
                        both_sl_and_tp = intrabar_check["sl_touched"] and intrabar_check["tp1_touched"]
                        if sign * (reference - t["stop"]) <= 0 or both_sl_and_tp:
                            stop_fill = min(market_exit, t["stop"] - p["slippage_per_unit"]) if sign == 1 else max(market_exit, t["stop"] + p["slippage_per_unit"])
                            self._exit(conn, t, t["remaining_qty"], stop_fill, "STOP_LOSS")
                        else:
                            t["seen_one_r"] |= sign * (snap.last - p["entry"]) >= p["distance"]
                            quote = snap.bid if sign == 1 else snap.ask
                            for target, qty_key, stamp_key in (
                                ("tp1", "tp1_qty", "tp1_ms"),
                                ("tp2", "tp2_qty", "tp2_ms"),
                                ("tp3", "runner_qty", "tp3_ms"),
                            ):
                                if t["status"] != "OPEN" or t.get(stamp_key) is not None or p.get(target) is None or sign * (quote - p[target]) < 0:
                                    continue
                                if target == "tp2" and t.get("tp1_ms") is None:
                                    continue
                                if target == "tp3" and (t.get("tp1_ms") is None or t.get("tp2_ms") is None):
                                    continue
                                book = snap.bids if sign == 1 else snap.asks
                                depth = sum(float(q) for price, q in book if sign * (float(price) - p[target]) >= 0 and float(q) >= 0)
                                req_qty = t["remaining_qty"] if target == "tp3" else p.get(qty_key, t["remaining_qty"])
                                if depth < req_qty:
                                    continue
                                self._exit(conn, t, req_qty, p[target] - sign * p["slippage_per_unit"], target.upper())
                                t[stamp_key] = snap.now_ms
                                t["seen_one_r"] = True
                                if target == "tp1":
                                    be = quantize(break_even(p["entry"], p["direction"], p["fee"], p["funding_per_unit"], p["slippage_per_unit"]), p["tick"], up=sign == 1)
                                    t["stop"] = max(t["stop"], be) if sign == 1 else min(t["stop"], be)
                                    if sign * (reference - t["stop"]) <= 0:
                                        self._exit(conn, t, t["remaining_qty"], market_exit, "BREAK_EVEN_EXIT")
                            if t["status"] == "OPEN":
                                advice = manage_position(p, snap.execution, snap.now_ms, reference, t["stop"],
                                    t["tp1_ms"], t["tp2_ms"], t["seen_one_r"], p["funding_per_unit"], p["entry"] * p["fee"])
                                t["stop"], t["seen_one_r"] = advice["stop"], advice["reached_one_r"]
                                if advice["action"] in ("EXIT", "REDUCE_OR_EXIT"):
                                    self._exit(conn, t, t["remaining_qty"], market_exit, "RULE_EXIT: " + advice["reason"])
                    self._store(conn, t)
                if changed:
                    self._mark_state_updated(conn, snap.now_ms)
        if changed:
            self.sync_to_supabase(backup_at_ms=snap.now_ms)

    def export_json(self, backup_at_ms=None):
        with closing(self.connect()) as conn:
            meta = dict(conn.execute("SELECT key,value FROM paper_meta"))
            kind = meta.get("instrument_kind", self.expected_instrument_kind or "USD_M_PERPETUAL")
            val_meta = spot_reference_validation_metadata(kind)
            trades = self._rows(conn)
            events = [
                dict(zip(["id", "signal", "at_ms", "event", "qty", "price", "pnl", "note"], r))
                for r in conn.execute("SELECT id, signal, at_ms, event, qty, price, pnl, note FROM paper_events ORDER BY id")
            ]
            telegram_rows = [
                dict(zip(["target", "signal", "state", "retry_at", "updated_at"], r))
                for r in conn.execute("SELECT target, signal, state, retry_at, updated_at FROM telegram_alerts")
            ]
            audit_rows = [
                dict(zip(["id", "at_ms", "role", "session_id", "action", "status", "detail"], r))
                for r in conn.execute("SELECT id, at_ms, role, session_id, action, status, detail FROM security_audit_log ORDER BY id")
            ]
            quota_rows = [
                dict(zip(["id", "bucket", "session_id", "event_ts"], r))
                for r in conn.execute("SELECT id, bucket, session_id, event_ts FROM security_quota_events ORDER BY id")
            ]
            state_ms = self._current_state_updated_ms(conn)
            now_ms = int(backup_at_ms if backup_at_ms is not None else state_ms)
            state_seq = int(meta.get("state_seq") or 1)
            meta["state_updated_ms"] = str(state_ms)
            meta["state_seq"] = str(state_seq)
            payload_obj = {
                "schema": BACKUP_SCHEMA_VERSION,
                "version": APP_VERSION,
                "type": "PAPER_SIMULATION",
                "instrument_kind": kind,
                "namespace": self.namespace,
                "backup_at_ms": now_ms,
                "state_updated_ms": state_ms,
                "state_seq": state_seq,
                "validation": val_meta,
                "meta": meta,
                "trades": trades,
                "events": events,
                "telegram_alerts": telegram_rows,
                "security_audit_log": audit_rows,
                "security_quota_events": quota_rows,
            }
            payload_obj["checksum_sha256"] = compute_backup_checksum(payload_obj)
            payload_obj["hmac_sha256"] = compute_backup_hmac(payload_obj)
            return json.dumps(payload_obj, ensure_ascii=False, indent=2, allow_nan=False)

    def import_backup_json(self, payload, allow_if_newer_only=True, _from_sync=False):
        """BUG-17, BUG-23, BUG-28 & SUPABASE-02: Atomically restore journal state with mandatory SHA-256 checksum covering timestamps, envelope-payload timestamp verification, and full audit/quota recovery."""
        raw_obj = json.loads(payload) if isinstance(payload, (str, bytes)) else dict(payload)
        has_nested = "payload" in raw_obj and isinstance(raw_obj.get("payload"), dict)
        if has_nested:
            envelope = raw_obj
            data = dict(raw_obj["payload"])
            # SUPABASE-02: Compare envelope timestamps with payload timestamps and reject any discrepancy
            env_b_ms = int(envelope.get("backup_at_ms") or 0)
            pay_b_ms = int(data.get("backup_at_ms") or 0)
            env_s_ms = int(envelope.get("state_updated_ms") or 0)
            pay_s_ms = int(data.get("state_updated_ms") or 0)
            if env_b_ms != pay_b_ms or env_s_ms != pay_s_ms:
                raise ValueError(
                    "Timestamp backup_at_ms / state_updated_ms antara envelope dan payload tidak cocok (manipulasi timestamp ditolak)."
                )
            if envelope.get("namespace") and data.get("namespace") and str(envelope["namespace"]) != str(data["namespace"]):
                raise ValueError("Namespace antara envelope dan payload tidak cocok.")
        else:
            envelope = raw_obj
            data = raw_obj

        if data.get("type") != "PAPER_SIMULATION" or not isinstance(data.get("trades"), list):
            raise ValueError("Format backup JSON tidak valid.")

        schema_ver = int(envelope.get("schema_version") or data.get("schema") or 0)
        if schema_ver < BACKUP_SCHEMA_VERSION:
            raise ValueError(f"Versi schema backup ({schema_ver}) tidak didukung.")

        meta_s_ms = (data.get("meta") or {}).get("state_updated_ms")
        if meta_s_ms is not None and data.get("state_updated_ms") is not None:
            if int(meta_s_ms) != int(data.get("state_updated_ms")):
                raise ValueError("Timestamp state_updated_ms tidak konsisten dengan meta.state_updated_ms.")

        # BUG-28 #3 & SUPABASE-02: Mandatory checksum_sha256 covering data AND timestamps
        declared_checksum = str(envelope.get("checksum_sha256") or data.get("checksum_sha256") or "").strip()
        if not declared_checksum:
            raise ValueError("Backup schema v2 wajib menyertakan checksum_sha256 yang valid.")
        computed_checksum = compute_backup_checksum(data)
        if declared_checksum != computed_checksum:
            raise ValueError("Checksum SHA-256 backup tidak cocok; data atau timestamp backup telah dimanipulasi.")

        restored_kind = str(data.get("instrument_kind") or (data.get("meta") or {}).get("instrument_kind") or "").strip()
        if restored_kind not in VALID_INSTRUMENT_KINDS:
            raise ValueError(f"instrument_kind backup tidak valid atau kosong: {restored_kind!r}")
        backup_ns = str(envelope.get("namespace") or data.get("namespace") or "").strip()
        if not backup_ns or ":" not in backup_ns:
            raise ValueError("Backup wajib menyertakan namespace yang valid (<INSTRUMENT_KIND>:<db_name>).")
        ns_kind = backup_ns.split(":", 1)[0]
        if ns_kind not in ("DEFAULT", restored_kind):
            raise ValueError(f"Namespace backup ({backup_ns}) tidak konsisten dengan instrument_kind ({restored_kind}).")
        if self.expected_instrument_kind and restored_kind != self.expected_instrument_kind:
            raise ValueError("Backup berasal dari instrument_kind berbeda; backup lintas instrumen ditolak.")
        if self.expected_instrument_kind and ns_kind not in ("DEFAULT", self.expected_instrument_kind):
            raise ValueError(f"Namespace backup ({backup_ns}) tidak sesuai dengan jurnal ({self.namespace}).")
        for tr_item in data.get("trades", []):
            tr_kind = (tr_item.get("plan") or {}).get("instrument_kind") or tr_item.get("instrument_kind")
            if tr_kind and tr_kind != restored_kind:
                raise ValueError("Data trade di dalam backup memiliki instrument_kind yang tidak konsisten.")

        incoming_ms = SupabaseStateSync._extract_state_updated_ms(
            data,
            fallback_ms=int(envelope.get("state_updated_ms") or data.get("state_updated_ms") or envelope.get("backup_at_ms") or 0),
        )

        audit_restored = 0
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                bound = conn.execute("SELECT value FROM paper_meta WHERE key='instrument_kind'").fetchone()
                if bound and bound[0] and bound[0] != restored_kind:
                    raise ValueError("Backup berasal dari instrument_kind berbeda; tidak boleh digabung.")
                local_ms = self._current_state_updated_ms(conn)
                if allow_if_newer_only and local_ms > 0 and incoming_ms > 0 and incoming_ms < local_ms:
                    raise ValueError(
                        f"Backup kedaluwarsa ditolak: timestamp backup ({incoming_ms}) lebih lama dari state lokal ({local_ms})."
                    )
                for k, v in (data.get("meta") or {}).items():
                    conn.execute("INSERT OR REPLACE INTO paper_meta VALUES (?,?)", (str(k), str(v)))
                conn.execute("INSERT OR REPLACE INTO paper_meta VALUES ('instrument_kind',?)", (restored_kind,))
                for t in data.get("trades", []):
                    self._store(conn, t)
                existing_events = {
                    (r[0], int(r[1]), r[2], float(r[3] or 0))
                    for r in conn.execute("SELECT signal, at_ms, event, qty FROM paper_events")
                }
                for ev in data.get("events", []):
                    sig_key = (ev["signal"], int(ev["at_ms"]), ev["event"], float(ev.get("qty") or 0))
                    if sig_key not in existing_events:
                        conn.execute(
                            "INSERT INTO paper_events(signal,at_ms,event,qty,price,pnl,note) VALUES (?,?,?,?,?,?,?)",
                            (ev["signal"], int(ev["at_ms"]), ev["event"], float(ev.get("qty") or 0),
                             ev.get("price"), float(ev.get("pnl") or 0), str(ev.get("note") or "")),
                        )
                        existing_events.add(sig_key)
                for tg in data.get("telegram_alerts", []):
                    conn.execute(
                        "INSERT OR REPLACE INTO telegram_alerts VALUES (?,?,?,?,?)",
                        (str(tg["target"]), str(tg["signal"]), str(tg["state"]),
                         float(tg.get("retry_at") or 0), float(tg.get("updated_at") or 0)),
                    )
                existing_audit = {
                    (int(r[0]), str(r[1]), str(r[2] or ""), str(r[3]), str(r[4]), str(r[5] or ""))
                    for r in conn.execute("SELECT at_ms, role, session_id, action, status, detail FROM security_audit_log")
                }
                for au in data.get("security_audit_log", []):
                    au_key = (
                        int(au["at_ms"]),
                        str(au["role"]),
                        str(au.get("session_id") or "anonymous")[:32],
                        str(au["action"]),
                        str(au["status"]),
                        str(au.get("detail") or "")[:300],
                    )
                    if au_key not in existing_audit:
                        conn.execute(
                            "INSERT INTO security_audit_log(at_ms, role, session_id, action, status, detail) VALUES (?,?,?,?,?,?)",
                            au_key,
                        )
                        existing_audit.add(au_key)
                        audit_restored += 1
                existing_quotas = {
                    (r[0], r[1], float(r[2]))
                    for r in conn.execute("SELECT bucket, session_id, event_ts FROM security_quota_events")
                }
                for q_ev in data.get("security_quota_events", []):
                    q_key = (str(q_ev["bucket"]), str(q_ev["session_id"]), float(q_ev["event_ts"]))
                    if q_key not in existing_quotas:
                        conn.execute(
                            "INSERT INTO security_quota_events(bucket,session_id,event_ts) VALUES (?,?,?)",
                            q_key,
                        )
                        existing_quotas.add(q_key)
                if incoming_ms > 0:
                    self._mark_state_updated(conn, incoming_ms)
        if not _from_sync:
            self.sync_to_supabase(backup_at_ms=incoming_ms or None)
        return {
            "ok": True,
            "trades_restored": len(data.get("trades", [])),
            "audit_entries_restored": audit_restored,
            "instrument_kind": restored_kind,
            "namespace": backup_ns,
            "state_updated_ms": incoming_ms,
        }
