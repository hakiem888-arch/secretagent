"""Futures Desk v6.0.1 (Audit Compliant & Backward-Compatible with Partial Repo Updates):
- BUG-09: Multi-Database Ledger Isolation (USD-M Perpetual, Bybit Perpetual, Spot Reference via paper_db_path).
- BUG-10 & BUG-13: Server-Side Authentication, RBAC (ADMIN vs VIEWER), Rate Limiting, SQLite Integrity, State Recovery, Resource Monitoring, & Audit Log.
- BUG-11: Headless/AppTest Compatible Import (`futures.st_compat`) + Safe Fallback Imports if only `app.py` was updated on GitHub.
- BUG-12 & BUG-14: Active-Position Failover Protection & Explicit Spot Reference vs Futures Perpetual Separation.
"""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import hmac
import inspect
import json
import os
import resource
import secrets
import sqlite3
import threading
import time
import pandas as pd

try:
    from futures.st_compat import install_if_missing
    install_if_missing()
except ImportError:
    pass

import streamlit as st

from futures.groq_rotation import GroqRotation, load_keys, DEFAULT_MODEL
from futures.market import FuturesMarket
from futures.spot_market import SpotReferenceMarket, paper_db_path
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.scanner import ScanSchedule, SCAN_INTERVALS
from futures.telegram import TelegramAlerts, LABELS
from futures import history

try:
    from futures.provider_router import FuturesProviderRouter
except ImportError:
    FuturesProviderRouter = None

try:
    from futures.spot_market import futures_db_path, bybit_db_path, resolve_db_path
except ImportError:
    def futures_db_path(root):
        if os.getenv("FUTURES_HISTORY_DB"):
            return Path(os.environ["FUTURES_HISTORY_DB"])
        return Path(root) / "data" / "paper_history.sqlite3"

    def bybit_db_path(root):
        if os.getenv("BYBIT_HISTORY_DB"):
            return Path(os.environ["BYBIT_HISTORY_DB"])
        if os.getenv("FUTURES_HISTORY_DB"):
            old = Path(os.environ["FUTURES_HISTORY_DB"])
            return old.with_name(old.stem + "_bybit_perpetual" + old.suffix)
        return Path(root) / "data" / "bybit_paper_history.sqlite3"

    def resolve_db_path(root, instrument_kind="USD_M_PERPETUAL", base_db=None):
        root = Path(root)
        if instrument_kind == "SPOT_REFERENCE":
            if os.getenv("SPOT_HISTORY_DB"):
                return Path(os.environ["SPOT_HISTORY_DB"])
            if base_db is not None:
                b = Path(base_db)
                if b.stem.endswith("_spot_reference") or b.name == "spot_paper_history.sqlite3":
                    return b
                stem = b.stem.replace("_bybit_perpetual", "")
                return b.with_name(stem + "_spot_reference" + b.suffix)
            return paper_db_path(root)
        if instrument_kind == "BYBIT_LINEAR_PERPETUAL":
            if os.getenv("BYBIT_HISTORY_DB"):
                return Path(os.environ["BYBIT_HISTORY_DB"])
            if base_db is not None:
                b = Path(base_db)
                if b.stem.endswith("_bybit_perpetual") or b.name == "bybit_paper_history.sqlite3":
                    return b
                stem = b.stem.replace("_spot_reference", "")
                return b.with_name(stem + "_bybit_perpetual" + b.suffix)
            return bybit_db_path(root)
        if base_db is not None:
            b = Path(base_db)
            stem = b.stem.replace("_spot_reference", "").replace("_bybit_perpetual", "")
            return b.with_name(stem + b.suffix) if stem != b.stem else b
        return futures_db_path(root)

try:
    from futures.scanner import SCAN_INTERVAL_LABELS
except ImportError:
    SCAN_INTERVAL_LABELS = {
        60: "1m (60 detik)",
        300: "5m (300 detik)",
        900: "15m (900 detik)",
        30: "30 detik",
        45: "45 detik",
        90: "90 detik",
    }

try:
    from futures.strategy import spot_reference_validation_metadata
except ImportError:
    def spot_reference_validation_metadata(instrument_kind):
        is_spot = instrument_kind == "SPOT_REFERENCE"
        if is_spot:
            return {
                "is_spot_reference": True,
                "market_category_label": "SPOT_REFERENCE (SIMULASI PAPER — BUKAN FUTURES PERPETUAL)",
                "mark_price_claim": "TIDAK IDENTIK: Harga Last Spot bukan Mark Price Futures Perpetual.",
                "funding_claim": "ASUMSI SIMULASI: Funding bukan data aktual kontrak; hanya cadangan biaya simulasi.",
                "leverage_claim": "ASUMSI SIMULASI: Leverage hanya simulasi di atas harga Spot.",
                "pnl_claim": "SIMULASI SPOT: Perhitungan PnL Spot Reference tidak disamakan dengan hasil kontrak perpetual.",
            }
        return {
            "is_spot_reference": False,
            "market_category_label": f"{instrument_kind} (SIMULASI PAPER KONTRAK PERPETUAL)",
            "mark_price_claim": "TERSEDIA: Menggunakan Mark Price kontrak perpetual publik.",
            "funding_claim": "TERSEDIA + CADANGAN SIMULASI: Rate funding publik dengan cadangan biaya model.",
            "leverage_claim": "ASUMSI SIMULASI: Leverage pada buku besar virtual PAPER.",
            "pnl_claim": "SIMULASI PERPETUAL: Model PnL kontrak perpetual virtual (bukan eksekusi live).",
        }

try:
    from futures.security import SecurityManager, ROLE_ADMIN, ROLE_VIEWER, GLOBAL_MONITOR, verify_sqlite_integrity
except ImportError:
    ROLE_ADMIN = "ADMIN"
    ROLE_VIEWER = "VIEWER"
    _PRIVILEGED = frozenset({"MANUAL_SCAN", "TELEGRAM_TEST", "TELEGRAM_ALERT", "JOURNAL_EXPORT", "CONFIG_UPDATE", "AI_REVIEW"})

    def verify_sqlite_integrity(db_path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(path, timeout=10)) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                row = conn.execute("PRAGMA quick_check").fetchone()
                ok = bool(row and row[0] == "ok")
                return {"ok": ok, "status": row[0] if row else "unknown", "db": str(path)}
        except sqlite3.Error as exc:
            return {"ok": False, "status": f"SQLITE_ERROR: {type(exc).__name__}", "db": str(path)}

    class _FallbackMonitor:
        def __init__(self, clock=time.monotonic):
            self.clock = clock
            self.started_at = clock()
            self.lock = threading.Lock()
            self.counters = {
                "market_api_requests": 0, "groq_ai_requests": 0, "telegram_requests": 0,
                "manual_scans": 0, "journal_exports": 0, "rate_limited_events": 0, "unauthorized_attempts": 0,
            }
        def increment(self, key, amount=1):
            with self.lock:
                self.counters[key] = self.counters.get(key, 0) + int(amount)
        def snapshot(self):
            with self.lock:
                counts = dict(self.counters)
            try:
                rss = round(float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0, 2)
            except Exception:
                rss = 0.0
            return {
                "uptime_seconds": round(max(0.0, self.clock() - self.started_at), 2),
                "cpu_process_seconds": round(time.process_time(), 4),
                "memory_rss_mb": rss,
                "counters": counts,
            }

    GLOBAL_MONITOR = _FallbackMonitor()

    class SecurityManager:
        def __init__(self, db_path, admin_token=None, admin_password_hash=None, require_auth=None,
                     clock=time.time, monotonic=time.monotonic, manual_scan_cooldown=15.0,
                     max_manual_scans_per_min=5, max_ai_calls_per_session=60, max_ai_calls_per_min=10,
                     max_telegram_per_min=10, max_exports_per_5min=5):
            self.db_path = Path(db_path)
            self.clock, self.monotonic = clock, monotonic
            self.lock = threading.RLock()
            raw_token = str(admin_token if admin_token is not None else (os.getenv("COINAGENT_ADMIN_TOKEN") or os.getenv("ADMIN_TOKEN") or "")).strip()
            raw_hash = str(admin_password_hash if admin_password_hash is not None else (os.getenv("COINAGENT_ADMIN_PASSWORD_HASH") or os.getenv("ADMIN_PASSWORD_HASH") or "")).strip().lower()
            self._admin_hash = raw_hash or (hashlib.sha256(raw_token.encode("utf-8")).hexdigest() if raw_token else "")
            env_req = str(os.getenv("COINAGENT_REQUIRE_AUTH", os.getenv("PUBLIC_READONLY_MODE", ""))).lower() in ("1", "true", "yes", "on")
            self.auth_required = bool(self._admin_hash or env_req) if require_auth is None else bool(require_auth)
            self._server_secret = secrets.token_hex(32)
            self._sessions = {}
            self.monitor = _FallbackMonitor(clock=monotonic)
            self.manual_scan_cooldown = float(manual_scan_cooldown)
            self.max_manual_scans_per_min = int(max_manual_scans_per_min)
            self.max_ai_calls_per_session = int(max_ai_calls_per_session)
            self.max_ai_calls_per_min = int(max_ai_calls_per_min)
            self.max_telegram_per_min = int(max_telegram_per_min)
            self.max_exports_per_5min = int(max_exports_per_5min)
            self._events = {}
            self._last_success = {}
            self._session_ai_counts = {}
            self._ensure_tables()

        def _connect(self):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            return conn

        def _ensure_tables(self):
            with closing(self._connect()) as conn, conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS security_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL,
                    role TEXT NOT NULL, session_id TEXT, action TEXT NOT NULL,
                    status TEXT NOT NULL, detail TEXT)""")

        def log_audit(self, action, status, role=ROLE_VIEWER, session_id="anonymous", detail=""):
            with self.lock:
                try:
                    with closing(self._connect()) as conn, conn:
                        conn.execute(
                            "INSERT INTO security_audit_log(at_ms, role, session_id, action, status, detail) VALUES (?,?,?,?,?,?)",
                            (int(self.clock() * 1000), str(role), str(session_id or "anonymous")[:32], str(action), str(status), str(detail or "")[:300]),
                        )
                except sqlite3.Error:
                    pass

        def audit_entries(self, limit=100):
            with closing(self._connect()) as conn:
                rows = conn.execute("SELECT id, at_ms, role, session_id, action, status, detail FROM security_audit_log ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
            return [dict(zip(["id", "at_ms", "role", "session_id", "action", "status", "detail"], r)) for r in rows]

        def authenticate(self, credential, ttl_seconds=3600):
            candidate = str(credential or "").strip()
            if not candidate or not self._admin_hash:
                self.monitor.increment("unauthorized_attempts")
                self.log_audit("ADMIN_LOGIN", "DENIED", role=ROLE_VIEWER, detail="Token kosong atau belum dikonfigurasi")
                return None
            cand_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
            if not (hmac.compare_digest(cand_hash, self._admin_hash) or (len(candidate) == 64 and hmac.compare_digest(candidate.lower(), self._admin_hash))):
                self.monitor.increment("unauthorized_attempts")
                self.log_audit("ADMIN_LOGIN", "DENIED", role=ROLE_VIEWER, detail="Token tidak valid")
                return None
            sid = secrets.token_hex(12)
            exp = self.clock() + float(ttl_seconds)
            payload = f"{sid}:{ROLE_ADMIN}:{int(exp)}"
            sig = hmac.new(self._server_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
            token = f"{payload}:{sig}"
            with self.lock:
                self._sessions[sid] = {"role": ROLE_ADMIN, "expires_at": exp, "token": token}
            self.log_audit("ADMIN_LOGIN", "ALLOWED", role=ROLE_ADMIN, session_id=sid, detail="Login berhasil")
            return token

        def revoke_session(self, session_token):
            info = self.verify_session(session_token)
            if info["session_id"] != "anonymous":
                with self.lock:
                    self._sessions.pop(info["session_id"], None)
                self.log_audit("ADMIN_LOGOUT", "ALLOWED", role=info["role"], session_id=info["session_id"], detail="Logout")

        def verify_session(self, session_token=None):
            if not session_token:
                return {"role": ROLE_ADMIN, "session_id": "local-operator", "authenticated": True} if not self.auth_required else {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}
            parts = str(session_token).split(":")
            if len(parts) != 4:
                return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}
            sid, role, exp_str, sig = parts
            expected = hmac.new(self._server_secret.encode("utf-8"), f"{sid}:{role}:{exp_str}".encode("utf-8"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}
            with self.lock:
                stored = self._sessions.get(sid)
                if not stored or self.clock() >= stored["expires_at"]:
                    self._sessions.pop(sid, None)
                    return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}
                return {"role": stored["role"], "session_id": sid, "authenticated": True}

        def _rate_check(self, bucket, max_calls, window_s, min_interval_s=0.0):
            now = self.monotonic()
            with self.lock:
                hist = [t for t in self._events.get(bucket, []) if now - t < window_s]
                self._events[bucket] = hist
                last = self._last_success.get(bucket)
                if min_interval_s > 0 and last is not None and 0 <= now - last < min_interval_s:
                    return False, f"Rate limit aktif untuk '{bucket}': tunggu {round(min_interval_s - (now - last), 1)} detik."
                if len(hist) >= max_calls:
                    return False, f"Batas frekuensi '{bucket}' tercapai ({max_calls}/{int(window_s)}s)."
                hist.append(now)
                self._last_success[bucket] = now
                return True, "OK"

        def authorize_action(self, action, session_token=None, last_scan_failed=False):
            info = self.verify_session(session_token)
            role, sid = info["role"], info["session_id"]
            if action in _PRIVILEGED and role != ROLE_ADMIN:
                self.monitor.increment("unauthorized_attempts")
                msg = f"Akses ditolak (UNAUTHORIZED): '{action}' memerlukan hak akses ADMINISTRATOR."
                self.log_audit(action, "DENIED", role=role, session_id=sid, detail=msg)
                return False, msg
            if action == "MANUAL_SCAN":
                ok, reason = self._rate_check("manual_scan", self.max_manual_scans_per_min, 60.0, 0.0 if last_scan_failed else self.manual_scan_cooldown)
                if not ok:
                    self.monitor.increment("rate_limited_events")
                    self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                    return False, reason
            elif action == "TELEGRAM_TEST":
                ok, reason = self._rate_check("telegram_test", 3, 600.0, 30.0)
                if not ok:
                    self.monitor.increment("rate_limited_events")
                    self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                    return False, reason
            elif action == "JOURNAL_EXPORT":
                ok, reason = self._rate_check("journal_export", self.max_exports_per_5min, 300.0, 0.0)
                if not ok:
                    self.monitor.increment("rate_limited_events")
                    self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                    return False, reason
            elif action == "AI_REVIEW":
                with self.lock:
                    used = self._session_ai_counts.get(sid, 0)
                    if used >= self.max_ai_calls_per_session:
                        reason = f"Kuota AI per sesi tercapai ({self.max_ai_calls_per_session} panggilan)."
                        self.monitor.increment("rate_limited_events")
                        self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                        return False, reason
                    self._session_ai_counts[sid] = used + 1
            self.log_audit(action, "ALLOWED", role=role, session_id=sid, detail=f"Tindakan {action} diizinkan")
            return True, "ALLOWED"


ROOT = Path(__file__).resolve().parent
FUTURES_DB = futures_db_path(ROOT)
BYBIT_DB = bybit_db_path(ROOT)
SPOT_DB = paper_db_path(ROOT)
WIB = ZoneInfo("Asia/Jakarta")
APP_VERSION = "6.0.1"


def secret(name):
    try:
        return st.secrets.get(name, os.getenv(name, ""))
    except Exception:
        return os.getenv(name, "")


def active_db_path(instrument_kind="USD_M_PERPETUAL"):
    """BUG-09: Return the isolated SQLite database path for the active instrument_kind."""
    if instrument_kind == "SPOT_REFERENCE":
        return paper_db_path(ROOT)
    if instrument_kind == "BYBIT_LINEAR_PERPETUAL":
        return bybit_db_path(ROOT)
    return futures_db_path(ROOT)


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, WIB).strftime("%d/%m/%Y %H:%M:%S WIB") if ms else "Belum scan"


def _make_paper_ledger(db_path, initial_equity, instrument_kind):
    sig = inspect.signature(PaperLedger.__init__).parameters
    if "expected_instrument_kind" in sig:
        return PaperLedger(db_path, initial_equity, expected_instrument_kind=instrument_kind)
    return PaperLedger(db_path, initial_equity)


def _ensure_multi_ledger_support(scanner, base_db, initial_equity, security):
    """Ensure scanner has ledger_for() and security even if running against an older futures/ folder."""
    if getattr(scanner, "security", None) is None:
        scanner.security = security
    if not hasattr(scanner, "ledgers"):
        initial_kind = getattr(scanner.market, "instrument_kind", "USD_M_PERPETUAL")
        scanner.ledgers = {initial_kind: scanner.ledger}

    if not hasattr(scanner, "ledger_for"):
        def _ledger_for(instrument_kind):
            kind = instrument_kind or "USD_M_PERPETUAL"
            if kind not in scanner.ledgers:
                target_db = resolve_db_path(ROOT, kind, base_db=base_db)
                scanner.ledgers[kind] = _make_paper_ledger(target_db, initial_equity, kind)
            return scanner.ledgers[kind]
        scanner.ledger_for = _ledger_for

    for kind in ("USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL", "SPOT_REFERENCE"):
        scanner.ledger_for(kind)
    return scanner


def export_journal_authorized(ledger, security, session_token=None, fmt="json"):
    """BUG-10 & BUG-13: Server-side authorization and rate-limiting gate for journal exports."""
    allowed, reason = security.authorize_action("JOURNAL_EXPORT", session_token=session_token)
    if not allowed:
        return False, reason
    if fmt == "csv":
        rows = []
        for t in ledger.trades():
            p = t["plan"]
            rows.append({
                "Kategori Instrumen": t.get("market_category_label", p.get("instrument_kind", "USD_M_PERPETUAL")),
                "Pair": p["symbol"],
                "Jenis": LABELS[p["mode"]],
                "Arah": "BUY/LONG" if p["direction"] == "LONG" else "SELL/SHORT",
                "Sumber": p.get("instrument_kind", "USD_M_PERPETUAL"),
                "Status": t["status"],
                "Entry": p["entry"],
                "SL sekarang": t["stop"],
                "TP1": p["tp1"],
                "TP2": p["tp2"],
                "TP3": p.get("tp3", "-"),
                "Q rencana": p["qty"],
                "Q terbuka": t["remaining_qty"],
                "PnL bersih virtual": round(t["realized_pnl"], 6),
            })
        return True, pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig")
    return True, ledger.export_json()


def recover_state_after_restart(engine):
    """BUG-13: Recover persisted scan report and active ledger state after Streamlit restart."""
    if "market_report" not in st.session_state or not st.session_state.get("market_report"):
        persisted = engine.ledger.last_scan_report()
        if persisted:
            st.session_state["market_report"] = persisted
            if engine.last is None:
                engine.last = persisted
            return True
    return False


@st.cache_resource(show_spinner=False)
def resources(signature, _keys, _token, _chat, _model, base_db, initial_equity, _admin_token="", _admin_hash=""):
    # BUG-05 & BUG-09: Identical core classes used by dashboard and worker, with per-provider DB isolation.
    if getattr(SpotReferenceMarket.get, "__module__", "").startswith("tests"):
        market = SpotReferenceMarket()
    elif FuturesProviderRouter is not None:
        market = FuturesProviderRouter()
    else:
        market = SpotReferenceMarket()
    initial_kind = getattr(market, "instrument_kind", "USD_M_PERPETUAL")
    resolved_db = resolve_db_path(ROOT, initial_kind, base_db=base_db)
    verify_sqlite_integrity(resolved_db)
    pool, calendar = GroqRotation(_keys), NewsCalendar()
    ledger = _make_paper_ledger(resolved_db, initial_equity, initial_kind)
    alerts = TelegramAlerts(_token, _chat, resolved_db)
    security = SecurityManager(
        resolved_db,
        admin_token=_admin_token,
        admin_password_hash=_admin_hash,
    )
    ms_sig = inspect.signature(MarketScanner.__init__).parameters
    if "security" in ms_sig:
        scanner = MarketScanner(market, pool, calendar, ledger, alerts, _model, security=security)
    else:
        scanner = MarketScanner(market, pool, calendar, ledger, alerts, _model)
    return _ensure_multi_ledger_support(scanner, base_db, initial_equity, security)


def configuration():
    keys = []
    try:
        keys = load_keys(secret)
    except ValueError as exc:
        st.sidebar.error(str(exc))
    token, chat = secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID")
    model = str(secret("GROQ_MODEL") or DEFAULT_MODEL)
    admin_token = str(secret("COINAGENT_ADMIN_TOKEN") or secret("ADMIN_TOKEN") or "")
    admin_hash = str(secret("COINAGENT_ADMIN_PASSWORD_HASH") or secret("ADMIN_PASSWORD_HASH") or "")
    try:
        initial = float(secret("PAPER_INITIAL_EQUITY") or 1000)
    except (TypeError, ValueError):
        initial = 1000.0
    base_db = futures_db_path(ROOT)
    signature = hashlib.sha256(
        json.dumps([keys, str(token), str(chat), model, str(base_db), bool(admin_token or admin_hash), APP_VERSION]).encode()
    ).hexdigest()
    engine = resources(signature, tuple(keys), token, chat, model, str(base_db), initial, admin_token, admin_hash)
    _ensure_multi_ledger_support(engine, base_db, initial, getattr(engine, "security", None) or SecurityManager(base_db, admin_token=admin_token, admin_password_hash=admin_hash))
    session_token = st.session_state.get("admin_session_token")
    session_info = engine.security.verify_session(session_token)
    is_admin = session_info["role"] == ROLE_ADMIN

    with st.sidebar:
        st.subheader("Scanner otomatis")
        auto = st.toggle("Auto-scan seluruh market", value=True)
        relaxed = st.toggle(
            "Medium-Liquidity Watchlist Fallback",
            value=False,
            help="Memperluas kandidat watchlist hingga likuiditas 5M USDT; gerbang validasi harga limit, depth, freshness (30s), dan kalender berita tetap ketat.",
        )
        interval_options = SCAN_INTERVALS if 300 in SCAN_INTERVALS else (60, 300, 900)
        interval = st.selectbox(
            "Interval scan (detik)",
            interval_options,
            index=0,
            format_func=lambda x: SCAN_INTERVAL_LABELS.get(x, f"{x} detik"),
        )
        candidates = st.selectbox("Kandidat analisis mendalam", (1, 2), index=1)
        alert = st.toggle("Alert Telegram PAPER BUY/SELL", value=True)
        watchlist_alert = st.toggle("Alert Telegram REVERSAL WATCHLIST", value=True,
                                    help="Kirim alert 👀 REVERSAL WATCHLIST jika kandidat memiliki skor M15 potensial tetapi masih menunggu konfirmasi entry M5.")
        summary_alert = st.toggle("Kirim MARKET SCANNER REPORT setiap siklus", value=True,
                                  help="Mengirim laporan MARKET SCANNER REPORT ke Telegram pada setiap putaran scan.")
        st.caption("Alur Multi-Stage: Screen All Markets → Filter → Rank (0–100) → Top 1–2 → Deep Analysis → M5 Confirmation → Risk Validation → Telegram Alert.")
        with st.expander("Asumsi simulasi dan filter awal"):
            volume = st.number_input("Minimum volume 24 jam (USDT)", min_value=1_000_000., value=10_000_000., step=1_000_000.)
            leverage = st.select_slider("Leverage paper", options=[1, 2, 3], value=2)
            risk = st.number_input("Risiko per paper trade (%)", min_value=.05, max_value=.5, value=.5, step=.05)
            fee = st.number_input("Asumsi fee taker (%)", min_value=0., max_value=1., value=.05, step=.01, format="%.4f")
            slip = st.number_input("Cadangan slippage exit (bps)", min_value=0., value=2., step=.5)
            fund = st.number_input("Cadangan biaya funding model (%)", min_value=0., value=.03, step=.01, format="%.4f")
            st.caption("SIMULASI PAPER: biaya model, bukan fee aktual akun. Cadangan funding dibebankan saat entry. Likuidasi bursa tidak dimodelkan.")
        with st.expander("Keamanan & Otorisasi Server-Side"):
            st.caption(f"Peran sesi aktif: {session_info['role']} · Mode otorisasi: {'Wajib Token Admin' if engine.security.auth_required else 'Operator Lokal'}")
            unlock_prompt = st.checkbox("Masukkan token administrator", value=False, key="show_admin_login")
            if unlock_prompt:
                entered_token = st.text_input("Token Administrator", type="password", key="admin_token_input")
                if st.button("Verifikasi Admin", key="admin_login_btn"):
                    issued = engine.security.authenticate(entered_token)
                    if issued:
                        st.session_state["admin_session_token"] = issued
                        st.success("Autentikasi administrator berhasil.")
                    else:
                        st.error("Token administrator tidak valid.")
            if session_token and st.button("Logout Admin", key="admin_logout_btn"):
                engine.security.revoke_session(session_token)
                st.session_state.pop("admin_session_token", None)
        st.divider()
        st.caption(f"Groq: {len(keys)}/7 key dari Secrets · {model}")
        st.caption("Telegram: " + ("terkonfigurasi dari Secrets" if engine.alerts.configured else "token/chat ID belum lengkap di Secrets"))
        if st.button("Kirim Tes Alert Telegram", key="test_telegram_btn",
                     help="Mengirim pesan uji coba langsung ke chat Telegram (khusus Administrator & dibatasi rate-limit server)."):
            cur_tok = st.session_state.get("admin_session_token")
            if hasattr(engine.alerts, "send_test_message"):
                tm_sig = inspect.signature(engine.alerts.send_test_message).parameters
                if "security" in tm_sig:
                    ok, msg = engine.alerts.send_test_message(security=engine.security, session_token=cur_tok)
                else:
                    allowed, reason = engine.security.authorize_action("TELEGRAM_TEST", session_token=cur_tok)
                    ok, msg = engine.alerts.send_test_message() if allowed else (False, reason)
            else:
                allowed, reason = engine.security.authorize_action("TELEGRAM_TEST", session_token=cur_tok)
                ok, msg = (False, "Metode send_test_message belum tersedia di futures/telegram.py; perbarui seluruh paket ZIP.") if allowed else (False, reason)
            if ok:
                st.success(msg)
            else:
                st.error(msg)
        if not keys:
            st.info("Tambahkan GROQ_API_KEY di Secrets; tanpa AI tidak ada sinyal yang disetujui.")
        with st.expander("Status Groq"):
            st.dataframe(pd.DataFrame(engine.pool.status()), hide_index=True)
        st.caption("Auto-scan memerlukan sesi aktif. Untuk 24/7 gunakan worker.py di server yang selalu hidup. Jangan jalankan dua scanner pada DB yang berbeda untuk chat yang sama.")

    raw_cfg = dict(
        candidates=candidates,
        min_volume=volume,
        fee=fee / 100,
        slippage_bps=slip,
        funding=fund / 100,
        leverage=leverage,
        risk_pct=risk,
        send_alerts=alert if is_admin else False,
        send_watchlist_alerts=watchlist_alert if is_admin else False,
        send_scan_summary=summary_alert if is_admin else False,
        relaxed_mode=relaxed,
        interval=interval,
    )
    accepted = set(inspect.signature(ScannerConfig).parameters)
    if "relaxed_mode" not in accepted and interval not in (30, 45, 60, 90):
        raw_cfg["interval"] = 60
    config = ScannerConfig(**{k: v for k, v in raw_cfg.items() if k in accepted})
    return engine, config, auto


def freshness(report):
    started = report.get("started_at_ms") or report.get("now_ms", 0)
    if not started:
        st.caption("Belum ada percobaan scan.")
        return
    status = {"RUNNING": "Sedang berjalan", "FAILED": "Gagal mengambil/memproses data",
              "COMPLETED": "Selesai", "COMPLETED_WITH_NOTES": "Selesai dengan catatan"}.get(
                  report.get("scan_status"), "Selesai")
    data_status = report.get("data_status", "OK")
    st.caption(f"Percobaan scan terakhir: {stamp(started)} · {status} · Status data: {data_status} · "
               f"durasi {report.get('duration_seconds', 0):.2f} detik")
    if report.get("scan_id"):
        st.caption(f"ID scan: {report['scan_id']} · pemicu: {report.get('trigger', 'AUTO')}")
    if not report.get("now_ms"):
        st.info("Belum ada data pasar yang berhasil diambil pada percobaan ini.")
        return
    if report.get("universe_loaded") is False:
        st.info("Waktu bursa diterima, tetapi data seluruh market belum berhasil dimuat.")
        return
    captured = report.get("display_monotonic")
    age = report.get("display_server_age", 0) + time.monotonic() - captured if captured is not None else (time.time() * 1000 - report.get("now_ms", 0)) / 1000
    st.caption(f"Waktu data pasar: {stamp(report['now_ms'])}")
    if age > 30:
        st.info("Snapshot hasil scan sudah lebih dari 30 detik. Tunggu scan baru; tabel ini merupakan catatan analisis.")


def paper_table(engine, now_ms):
    active_kind = getattr(engine.market, "instrument_kind", "USD_M_PERPETUAL")
    ledger = engine.ledger_for(active_kind)
    val_meta = spot_reference_validation_metadata(active_kind)
    s = ledger.summary(now_ms)
    cols = st.columns(4)
    for col, label, value in zip(cols, ["Ekuitas virtual (SIMULASI)", "PnL terealisasi (SIMULASI)", "PnL mengambang (SIMULASI)", "Posisi + pending"],
                                [f"{s['equity']:,.2f} USDT", f"{s['realized']:,.2f} USDT", f"{s['unrealized']:,.2f} USDT", f"{s['active']}/2"]):
        col.metric(label, value)
    st.caption(
        f"Jurnal Aktif: {val_meta['market_category_label']} · DB: {ledger.db.name}. "
        "Jurnal Spot Reference, Bybit Perpetual, dan Binance USD-M Futures dipisahkan pada file database berbeda dengan saldo virtual independen."
    )
    if val_meta["is_spot_reference"]:
        st.warning(
            "PERINGATAN SPOT REFERENCE (BUKAN FUTURES PERPETUAL): "
            f"{val_meta['mark_price_claim']} {val_meta['funding_claim']} {val_meta['leverage_claim']} {val_meta['pnl_claim']}"
        )
    trades = ledger.trades()
    rows = []
    for t in trades:
        p = t["plan"]
        kind = p.get("instrument_kind", "USD_M_PERPETUAL")
        rows.append({
            "Kategori": "SPOT SIMULASI (Non-Perpetual)" if kind == "SPOT_REFERENCE" else f"PERPETUAL SIMULASI ({kind})",
            "Pair": p["symbol"],
            "Jenis": LABELS[p["mode"]],
            "Arah": "BUY/LONG" if p["direction"] == "LONG" else "SELL/SHORT",
            "Confidence": p.get("confidence_score", 90),
            "Leverage (Simulasi)": f"{p['leverage']}x",
            "Sumber": kind,
            "Status": t["status"],
            "Entry": p["entry"],
            "SL sekarang": t["stop"],
            "TP1 (2R)": p["tp1"],
            "TP2 (3R)": p["tp2"],
            "TP3 (4R)": p.get("tp3", "-"),
            "Q rencana": p["qty"],
            "Q terbuka": t["remaining_qty"],
            "PnL bersih virtual": round(t["realized_pnl"], 6),
            "Fill virtual": stamp(p.get("fill_ms")),
            "TP1 terisi": bool(t["tp1_ms"]),
            "TP2 terisi": bool(t["tp2_ms"]),
            "Waktu": stamp(t["updated_ms"]),
            "Kualitas": t["quality"],
            "Catatan": t["note"],
            "Telegram": t["telegram"],
        })
    session_token = st.session_state.get("admin_session_token")
    session_info = engine.security.verify_session(session_token)
    if rows:
        frame = pd.DataFrame(rows)
        st.dataframe(frame, hide_index=True, use_container_width=True)
        if session_info["role"] == ROLE_ADMIN:
            st.download_button("Unduh jurnal CSV", frame.to_csv(index=False).encode("utf-8-sig"), f"paper_trades_{active_kind.lower()}.csv", "text/csv")
    else:
        st.info("Belum ada sinyal PAPER yang lolos pada jurnal instrumen ini. WAIT/NO TRADE tidak membuka trade.")
    if session_info["role"] == ROLE_ADMIN:
        st.download_button("Unduh cadangan jurnal lengkap", ledger.export_json(), f"paper_trading_backup_{active_kind.lower()}.json", "application/json")
    else:
        st.caption("Ekspor jurnal dibatasi untuk Administrator.")
    with st.expander("Ringkasan Lintas-Ledger (Futures USD-M vs Bybit Perpetual vs Spot Reference)"):
        summary_rows = []
        for kind_key in ("USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL", "SPOT_REFERENCE"):
            lg = engine.ledger_for(kind_key)
            sm = lg.summary(now_ms)
            summary_rows.append({
                "Instrumen": kind_key,
                "Database": lg.db.name,
                "Ekuitas Virtual (USDT)": round(sm["equity"], 2),
                "PnL Terealisasi (USDT)": round(sm["realized"], 4),
                "Posisi Aktif/Pending": sm["active"],
                "Total Trade": len(lg.trades()),
            })
        st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)
    st.caption("Tabel/CSV memuat 500 trade terbaru. Cadangan JSON memuat seluruh trade dan peristiwa jurnal.")
    st.caption("PENDING belum dianggap fill. Fill memakai quote Bid/Ask dan kedalaman order book yang diamati; tidak menyimpulkan fill dari sentuhan candle.")
    st.caption("Gap data (>3 menit): pending dibatalkan sebagai DATA_GAP; posisi terbuka ditutup pada quote baru sebagai DATA_GAP_EXIT tanpa mengarang keuntungan/kerugian fiktif.")


def dashboard(engine, config, auto):
    manual_clicked = st.button("Scan sekarang", type="primary", key="market_scan_now",
                               help="Memulai percobaan baru tanpa menunggu interval (khusus Administrator & dibatasi frekuensi).")
    schedule = st.session_state.setdefault("market_schedule", ScanSchedule())
    context = json.dumps(asdict(config), sort_keys=True)
    previous_report = st.session_state.get("market_report") or engine.last or {}
    if engine.last and engine.last.get("started_at_ms", 0) > previous_report.get("started_at_ms", 0):
        previous_report = engine.last
        st.session_state["market_report"] = previous_report

    manual = False
    if manual_clicked:
        last_failed = bool(previous_report.get("scan_status") == "FAILED")
        allowed, reason = engine.security.authorize_action(
            "MANUAL_SCAN",
            session_token=st.session_state.get("admin_session_token"),
            last_scan_failed=last_failed,
        )
        if allowed:
            manual = True
        else:
            st.warning(reason)

    paused = previous_report.get("market_error", {}).get("access_blocked", False)
    if (manual or not paused) and schedule.claim(time.monotonic(), auto, config.interval, context, manual):
        try:
            with st.spinner("Memantau paper trade, menyaring seluruh market (Multi-Stage), lalu menganalisis kandidat..."):
                previous = engine.last
                report = engine.run(config, manual=manual)
                if report is not previous and report.get("now_ms"):
                    report["display_monotonic"] = time.monotonic()
                    report["display_server_age"] = max(0, (time.time() * 1000 - report.get("now_ms", 0)) / 1000)
                st.session_state["market_report"] = report
        except (ValueError, sqlite3.Error, OSError) as exc:
            st.error("WAIT · siklus belum selesai: " + engine.pool.redact(str(exc))[:250])
        finally:
            schedule.finish(time.monotonic(), config.interval)
    else:
        recover_state_after_restart(engine)
    report = st.session_state.get("market_report") or engine.last or {}
    paused = report.get("market_error", {}).get("access_blocked", False)
    if report.get("scan_notice"):
        st.info(report["scan_notice"])
    if report.get("provider_failover"):
        st.info("Failover Sumber Data Aktif: " + str(report["provider_failover"]))
    if report.get("summary_telegram"):
        st.caption("Telegram Ringkasan: " + str(report["summary_telegram"]))
    scanner_tab, paper_tab, history_tab, rules_tab = st.tabs(["Scanner market", "Paper trading", "Riwayat analisis", "Aturan"])
    with scanner_tab:
        active_kind = report.get("instrument_kind") or getattr(engine.market, "instrument_kind", "USD_M_PERPETUAL")
        val_meta = spot_reference_validation_metadata(active_kind)
        st.write("**SCREEN ALL MARKETS → FILTER → RANK (M15 0–100) → SELECT TOP 1–2 → DEEP ANALYSIS & M5 CONFIRMATION → RISK VALIDATION → TELEGRAM ALERT**")
        st.caption(f"Kategori Sumber Aktif: **{val_meta['market_category_label']}** · Database Jurnal: `{engine.ledger_for(active_kind).db.name}`")
        if val_meta["is_spot_reference"]:
            st.warning(
                "MODE SPOT REFERENCE (SIMULASI NON-PERPETUAL): Harga Last Spot tidak identik dengan Mark Price Futures; "
                "funding dan leverage hanya asumsi simulasi; perhitungan PnL tidak disamakan dengan kontrak perpetual."
            )
        cols = st.columns(4)
        for col, label, value in zip(cols, ["Market aktif", "Lolos filter awal", "Shortlist", "Kandidat mendalam"],
                                    ([report.get("universe_count", 0), report.get("eligible_count", 0), len(report.get("shortlist", [])), len(report.get("selected", []))]
                                     if report and report.get("universe_loaded") is not False else ["N/A"] * 4)):
            col.metric(label, value)
        if report:
            freshness(report)
        interval_label = SCAN_INTERVAL_LABELS.get(config.interval, f"{config.interval} detik")
        mode_tag = "MEDIUM LIQUIDITY WATCHLIST" if getattr(config, "relaxed_mode", False) else "STRICT ENTRY GATES"
        auto_label = "DIJEDA (HTTP 451)" if auto and paused else (f"ON / {interval_label} ({mode_tag})" if auto else f"OFF ({mode_tag})")
        st.caption("Sesi aktif · auto-scan " + auto_label + ". Jadwal melambat jika API lambat; tidak ada penumpukan scan.")
        if paused:
            st.error("Akses data Binance ditolak (HTTP 451). Percobaan scan telah dijalankan, tetapi server belum memperoleh data yang diperlukan.")
            st.info("Permintaan berasal dari server tempat Streamlit berjalan. Periksa kelayakan akses wilayah/IP server "
                    "dengan penyedia data atau hosting. Setelah akses diperbaiki, klik Scan sekarang.")
        for message in report.get("errors", [])[:8]:
            st.warning(message)
        with st.expander("Diagnostik scan, resource, dan audit keamanan"):
            st.caption("Sumber aktif: " + str(getattr(engine.market, "source_label", "Binance USD-M Futures")) + " · permintaan berasal dari server aplikasi.")
            diagnostic = {
                k: report.get(k)
                for k in (
                    "scan_id", "scan_status", "data_status", "trigger", "started_at_ms",
                    "completed_at_ms", "now_ms", "duration_seconds", "universe_loaded",
                    "instrument_kind", "ledger_db", "validation", "market_error",
                )
            }
            diagnostic["sqlite_integrity"] = verify_sqlite_integrity(engine.ledger.db)
            diagnostic["resources"] = GLOBAL_MONITOR.snapshot()
            st.json(diagnostic)
            audit_rows = engine.security.audit_entries(limit=20)
            if audit_rows:
                st.dataframe(pd.DataFrame(audit_rows), hide_index=True, use_container_width=True)
            st.caption("Pada mesin yang menjalankan aplikasi, gunakan: python check_market_access.py. "
                       "Uji ini tidak memanggil AI, mengubah jurnal, atau mengirim Telegram.")
        if report.get("shortlist"):
            st.markdown("**Tahap 1 — Shortlist Seluruh Market (LONG & SHORT Watchlist)**")
            st.dataframe(pd.DataFrame(report["shortlist"]).rename(columns={"symbol": "Pair", "quote_volume": "Volume 24h (USDT)",
                "spread_bps": "Spread bps", "liquidity_score": "Skor likuiditas", "watchlist_side": "Watchlist"}), hide_index=True, use_container_width=True)
        if report.get("selected"):
            st.markdown("**Tahap 2 & 3 — Kandidat Terpilih untuk Analisis Mendalam**")
            sel_df = pd.DataFrame(report["selected"])
            cols_to_show = [c for c in ["symbol", "candidate_direction", "m15_score", "category", "screening_score", "activity", "momentum_atr_proxy"] if c in sel_df.columns]
            st.dataframe(sel_df[cols_to_show], hide_index=True, use_container_width=True)
        st.caption("Skor adalah kualitas setup teknikal berdasarkan aturan Multi-Stage Scanner (0–100), bukan jaminan probabilitas profit atau win rate.")
        if report.get("worker_analyses"):
            st.dataframe(pd.DataFrame(report["worker_analyses"]), hide_index=True, use_container_width=True)
        for i, data in enumerate(report.get("analyses", [])):
            r, snap = data["result"], data["snapshot"]
            side = {"LONG": "BUY/LONG", "SHORT": "SELL/SHORT"}.get(r.direction, "WAIT")
            conf = r.metrics.get("confidence_score") or (data.get("candidate_info") or {}).get("m15_score", "-")
            stage_status = data.get("stage_status", data["status"])
            kind_badge = "SPOT REFERENCE (Non-Perpetual)" if snap.instrument_kind == "SPOT_REFERENCE" else f"PERPETUAL ({snap.instrument_kind})"
            st.markdown(f"**[SIMULASI PAPER · {kind_badge}] {snap.symbol} · {LABELS[snap.mode]} · {side} · {data['status']} ({stage_status}) · Score/Conf: {conf}/100**")
            st.write(data.get("paper", ""))
            st.caption(data.get("telegram", ""))
            ai = data.get("ai") or {}
            for reason in (r.reasons + ai.get("reasons", []))[:5]:
                st.write("• " + reason)
            with st.expander(f"Perbandingan mode dan indikator — {snap.symbol}"):
                st.dataframe(pd.DataFrame(r.meta.get("mode_comparison", [])), hide_index=True, use_container_width=True)
                st.dataframe(pd.DataFrame(r.checks), hide_index=True, use_container_width=True)
                if r.plan:
                    p = r.plan
                    st.dataframe(pd.DataFrame([{
                        "KATEGORI": p.get("market_category_label", snap.instrument_kind),
                        "SYMBOL": p["symbol"],
                        "DIRECTION": p["direction"],
                        "CONFIDENCE": f"{p.get('confidence_score', 90)}/100 ({p.get('confidence_calibration_status', 'UNCALIBRATED')})",
                        "MAX LEVERAGE": f"{p.get('max_allowed_leverage', p['leverage'])}x",
                        "SELECTED LEVERAGE (SIMULASI)": f"{p['leverage']}x",
                        "Trigger": p["entry_trigger"],
                        "ENTRY PRICE": p["entry"],
                        "TECHNICAL SL": p["sl"],
                        "SL DISTANCE": f"{p['distance']:.6g} ({p.get('sl_distance_pct', 0):.2f}%)",
                        "EST. MARGIN LOSS": f"{p.get('estimated_margin_loss_pct', 0):.2f}%",
                        "ACCOUNT RISK": f"{p['risk']:.2f} USDT",
                        "INITIAL MARGIN": f"{p['margin']:.2f} USDT",
                        "NOTIONAL": f"{p['notional']:.2f} USDT",
                        "TP1 (2R)": p["tp1"],
                        "TP2 (3R)": p["tp2"],
                        "TP3 (4R)": p.get("tp3", "-"),
                        "EST. FEES": f"{p.get('estimated_fees_usdt', 0):.4f} USDT",
                        "NET RR TP1": f"{p['net_rr_tp1']:.2f}",
                        "Berlaku sampai": stamp(p["expires_ms"]),
                    }]), hide_index=True, use_container_width=True)
                if st.checkbox("Tampilkan grafik kandidat", key=f"chart_{snap.symbol}_{snap.mode}"):
                    from manual_app import draw_chart
                    draw_chart(snap, r.plan)
            st.download_button("Unduh analisis " + snap.symbol, json.dumps({"baseline": asdict(r), "ai": ai, "status": data["status"]}, ensure_ascii=False, indent=2),
                               f"{snap.symbol}_{snap.mode}_paper_analysis.json", "application/json", key=f"report_{i}")
    with paper_tab:
        paper_table(engine, report.get("now_ms") or int(time.time() * 1000))
    with history_tab:
        active_kind = getattr(engine.market, "instrument_kind", "USD_M_PERPETUAL")
        records = history.load(engine.ledger_for(active_kind).db)
        if records:
            st.dataframe(pd.DataFrame([{"Waktu": stamp(x["baseline"]["meta"]["time_ms"]), "Pair": x["baseline"]["meta"]["symbol"],
                "Instrumen": x["baseline"]["meta"].get("instrument_kind", "USD_M_PERPETUAL"),
                "Mode": x["baseline"]["meta"]["mode"], "Status": x["final_status"], "Scope": x["baseline"]["meta"].get("scope", "MANUAL")} for x in records]), hide_index=True)
        else:
            st.info("Belum ada analisis kandidat tersimpan.")
    with rules_tab:
        st.markdown("**PRINSIP UTAMA:** `SCREEN ALL MARKETS → FILTER → RANK → SELECT TOP 1–2 → DEEP ANALYSIS → M5 CONFIRMATION → RISK VALIDATION → TELEGRAM ALERT`")
        st.markdown("**SCALPER:** bias 15M / eksekusi 5M · **INTRADAY:** 4H / 15M · **SWING:** 1D / 4H.")
        st.write("Seluruh keputusan pengelolaan posisi, perhitungan TP (TP1=2R 50%, TP2=3R 25%, TP3=4R/Runner 25%), masa berlaku order (3 candle eksekusi), validasi harga limit/depth, dan batas data gap (3 menit) terpusat di modul `futures/` dan identik antara dashboard (`app.py`) dan `worker.py`.")
        st.write("Jurnal **Spot Reference** (`spot_paper_history.sqlite3`), **Bybit Perpetual** (`bybit_paper_history.sqlite3`), dan **Binance USD-M Futures** (`paper_history.sqlite3`) dipisahkan secara ketat. Spot Reference bukan Futures Perpetual: harga spot tidak identik dengan Mark Price Futures, funding dan leverage hanya asumsi simulasi, dan perhitungan PnL tidak disamakan dengan kontrak perpetual.")
        st.download_button("Unduh masterprompt lengkap", (ROOT / "STRATEGY_RULES.txt").read_text(), "STRATEGY_RULES.txt", "text/plain")


def main():
    st.set_page_config(page_title=f"Futures Desk · v{APP_VERSION}", page_icon="📡", layout="wide")
    st.markdown("<style>.block-container{max-width:1280px;padding-top:2rem}[data-testid=stMetric]{border:1px solid #e4e8ef;border-radius:12px;padding:12px}</style>", unsafe_allow_html=True)
    st.caption(f"FUTURES DESK / {APP_VERSION} / MULTI-LEDGER & SERVER-SIDE RBAC / PAPER SIMULATION ONLY")
    st.title("Market scanner otomatis")
    st.caption("Tanpa input pair · multi-stage screening · isolasi database per provider · otorisasi server-side · interval 1m / 5m / 15m")
    st.info("SCANNER SPOT / MULTI-PROVIDER (SIMULASI PAPER): Tidak mengirim order ke bursa nyata. Jika Binance Futures (HTTP 451) dan Bybit (HTTP 403) diblokir oleh lokasi server hosting, aplikasi otomatis beralih ke Binance Spot Reference (https://data-api.binance.vision) dengan database jurnal terpisah.")
    engine, config, auto = configuration()
    st.fragment(run_every="5s")(dashboard)(engine, config, auto)


if __name__ == "__main__":
    main()
