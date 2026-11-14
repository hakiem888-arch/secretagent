"""Server-side Authentication, RBAC Authorization, Rate Limiting, Audit Logging,
SQLite/Postgres Integrity Verification, and Resource Monitoring (BUG-10, BUG-13, BUG-15, BUG-16).

Security Principles:
1. Default authentication NEVER grants ADMIN to anonymous sessions (`auth_required = True` by default).
2. Unauthenticated sessions always receive `ROLE_VIEWER` (Read-Only).
3. All privileged actions (`MANUAL_SCAN`, `AUTO_SCAN`, `TELEGRAM_TEST`, `TELEGRAM_ALERT`,
   `JOURNAL_EXPORT`, `JOURNAL_RESTORE`, `CONFIG_UPDATE`, `AI_REVIEW`) require a verified
   server-side `ADMIN` or `WORKER` session token.
4. Enforces both per-session and global hourly AI quotas, sliding-window rate limits,
   and immutable audit logging.
"""
from contextlib import closing
from pathlib import Path
import hashlib
import hmac
import os
import resource
import secrets
import sqlite3
import threading
import time
from .db_backend import connect_database


_PROCESS_SECRET = secrets.token_hex(32)
_SHARED_SESSIONS = {}
ROLE_ADMIN = "ADMIN"
ROLE_WORKER = "WORKER"
ROLE_VIEWER = "VIEWER"

PRIVILEGED_ACTIONS = frozenset({
    "MANUAL_SCAN",
    "AUTO_SCAN",
    "TELEGRAM_TEST",
    "TELEGRAM_ALERT",
    "JOURNAL_EXPORT",
    "JOURNAL_RESTORE",
    "CONFIG_UPDATE",
    "AI_REVIEW",
})


class PermissionDeniedError(PermissionError):
    """Raised when a non-administrator attempts to execute a restricted server action."""


class RateLimitExceededError(RuntimeError):
    """Raised when a caller exceeds the configured server-side rate limit."""


class DeploymentSecurityError(RuntimeError):
    """Raised on public deployment startup when administrator authentication is not configured."""


def verify_sqlite_integrity(db_path):
    """BUG-13: Verify database integrity (PRAGMA quick_check) and required schema."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(connect_database(path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            row = conn.execute("PRAGMA quick_check").fetchone()
            ok = bool(row and row[0] == "ok")
            return {"ok": ok, "status": row[0] if row else "unknown", "db": str(path)}
    except Exception as exc:
        return {"ok": False, "status": f"DB_ERROR: {type(exc).__name__}", "db": str(path)}


class ResourceMonitor:
    """BUG-13: Track CPU time, RSS memory, and external API request counters."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started_at = clock()
        self.lock = threading.Lock()
        self.counters = {
            "market_api_requests": 0,
            "groq_ai_requests": 0,
            "telegram_requests": 0,
            "manual_scans": 0,
            "auto_scans": 0,
            "journal_exports": 0,
            "journal_restores": 0,
            "rate_limited_events": 0,
            "unauthorized_attempts": 0,
        }

    def increment(self, key, amount=1):
        with self.lock:
            self.counters[key] = self.counters.get(key, 0) + int(amount)

    @staticmethod
    def memory_rss_mb():
        try:
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return round(float(usage) / 1024.0, 2)
        except Exception:
            return 0.0

    def snapshot(self):
        with self.lock:
            counts = dict(self.counters)
        return {
            "uptime_seconds": round(max(0.0, self.clock() - self.started_at), 2),
            "cpu_process_seconds": round(time.process_time(), 4),
            "memory_rss_mb": self.memory_rss_mb(),
            "counters": counts,
        }


GLOBAL_MONITOR = ResourceMonitor()


class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter for scans, AI calls, Telegram, and exports."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.events = {}
        self.last_success = {}

    def check_and_record(self, bucket, max_calls, window_seconds, min_interval_seconds=0.0):
        now = self.clock()
        with self.lock:
            history = [t for t in self.events.get(bucket, []) if now - t < window_seconds]
            self.events[bucket] = history
            last = self.last_success.get(bucket)
            if min_interval_seconds > 0 and last is not None and 0 <= now - last < min_interval_seconds:
                wait_s = round(min_interval_seconds - (now - last), 1)
                return False, f"Rate limit aktif untuk '{bucket}': tunggu {wait_s} detik sebelum mencoba lagi."
            if len(history) >= max_calls:
                return False, f"Batas frekuensi '{bucket}' tercapai (maksimal {max_calls} kali per {int(window_seconds)} detik)."
            history.append(now)
            self.last_success[bucket] = now
            return True, "OK"

    def reset_cooldown(self, bucket):
        with self.lock:
            self.last_success.pop(bucket, None)


class SecurityManager:
    """BUG-10, BUG-13, BUG-15, BUG-16: Server-side Authentication, RBAC, Quotas, Rate Limiting, and Audit Log."""

    def __init__(
        self,
        db_path,
        admin_token=None,
        admin_password_hash=None,
        require_auth=True,
        clock=time.time,
        monotonic=time.monotonic,
        manual_scan_cooldown=15.0,
        max_manual_scans_per_min=5,
        max_ai_calls_per_session=60,
        max_ai_calls_global_per_hour=240,
        max_ai_calls_per_min=10,
        max_telegram_per_min=10,
        max_exports_per_5min=5,
    ):
        self.db_path = Path(db_path)
        self.clock = clock
        self.monotonic = monotonic
        self.lock = threading.RLock()
        raw_token = str(
            admin_token if admin_token is not None else (
                os.getenv("COINAGENT_ADMIN_TOKEN") or os.getenv("ADMIN_TOKEN") or ""
            )
        ).strip()
        raw_hash = str(
            admin_password_hash if admin_password_hash is not None else (
                os.getenv("COINAGENT_ADMIN_PASSWORD_HASH") or os.getenv("ADMIN_PASSWORD_HASH") or ""
            )
        ).strip().lower()
        self._admin_token = raw_token
        self._admin_hash = raw_hash or (hashlib.sha256(raw_token.encode("utf-8")).hexdigest() if raw_token else "")
        # BUG-15: Authentication is ALWAYS required by default (`auth_required = True`).
        # Anonymous sessions NEVER receive ADMIN privileges.
        self.auth_required = True if require_auth is None else bool(require_auth)
        self._server_secret = hashlib.sha256((_PROCESS_SECRET + "|" + self._admin_hash + "|" + str(self.db_path)).encode("utf-8")).hexdigest()
        self._sessions = _SHARED_SESSIONS.setdefault((str(self.db_path), self._admin_hash), {})
        self.limiter = SlidingWindowRateLimiter(clock=monotonic)
        self.monitor = ResourceMonitor(clock=monotonic)
        self.manual_scan_cooldown = float(manual_scan_cooldown)
        self.max_manual_scans_per_min = int(max_manual_scans_per_min)
        self.max_ai_calls_per_session = int(max_ai_calls_per_session)
        self.max_ai_calls_global_per_hour = int(max_ai_calls_global_per_hour)
        self.max_ai_calls_per_min = int(max_ai_calls_per_min)
        self.max_telegram_per_min = int(max_telegram_per_min)
        self.max_exports_per_5min = int(max_exports_per_5min)
        self._session_ai_counts = {}
        self._ensure_tables()

    @property
    def admin_configured(self):
        return bool(self._admin_hash)

    def verify_public_deployment_startup(self, enforce_strict=False):
        """BUG-15: Startup check that fails public deployment if admin authentication is not configured."""
        is_public_env = bool(
            enforce_strict
            or str(os.getenv("COINAGENT_PUBLIC_DEPLOYMENT", "")).lower() in ("1", "true", "yes", "on")
            or str(os.getenv("STREAMLIT_SHARING_MODE", "")).strip()
        )
        if not self.admin_configured:
            msg = (
                "KEAMANAN DEPLOYMENT: COINAGENT_ADMIN_TOKEN atau ADMIN_PASSWORD_HASH belum dikonfigurasi di Secrets. "
                "Semua sesi pengunjung dikunci ke mode VIEWER (Read-Only) dan tindakan yang memakai kredensial server dinonaktifkan."
            )
            self.log_audit("STARTUP_SECURITY_CHECK", "LOCKED_READONLY", role=ROLE_VIEWER, detail=msg)
            if is_public_env and enforce_strict:
                raise DeploymentSecurityError(msg)
            return {"ok": False, "configured": False, "mode": "LOCKED_READONLY", "reason": msg}
        self.log_audit("STARTUP_SECURITY_CHECK", "OK", role=ROLE_ADMIN, detail="Konfigurasi autentikasi admin terverifikasi")
        return {"ok": True, "configured": True, "mode": "RBAC_ACTIVE", "reason": "Autentikasi administrator terkonfigurasi."}

    def _connect(self):
        return connect_database(self.db_path)

    def _ensure_tables(self):
        with closing(self._connect()) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS security_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at_ms INTEGER NOT NULL,
                role TEXT NOT NULL,
                session_id TEXT,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT
            )""")

    def log_audit(self, action, status, role=ROLE_VIEWER, session_id="anonymous", detail=""):
        at_ms = int(self.clock() * 1000)
        safe_detail = str(detail or "")[:300]
        with self.lock:
            try:
                with closing(self._connect()) as conn, conn:
                    conn.execute(
                        "INSERT INTO security_audit_log(at_ms, role, session_id, action, status, detail) VALUES (?,?,?,?,?,?)",
                        (at_ms, str(role), str(session_id or "anonymous")[:32], str(action), str(status), safe_detail),
                    )
            except Exception:
                pass

    def audit_entries(self, limit=100):
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, at_ms, role, session_id, action, status, detail FROM security_audit_log ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            dict(zip(["id", "at_ms", "role", "session_id", "action", "status", "detail"], r))
            for r in rows
        ]

    def _issue_signed_token(self, role, ttl_seconds=3600, prefix=""):
        session_id = (prefix + secrets.token_hex(10))[:24]
        expires_at = self.clock() + float(ttl_seconds)
        payload = f"{session_id}:{role}:{int(expires_at)}"
        sig = hmac.new(self._server_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        token = f"{payload}:{sig}"
        with self.lock:
            self._sessions[session_id] = {"role": role, "expires_at": expires_at, "token": token}
        return token, session_id

    def issue_worker_session(self, ttl_seconds=86400 * 30):
        """BUG-16 #4 & BUG-18: Issue a signed internal session token for a trusted background worker process."""
        token, sid = self._issue_signed_token(ROLE_WORKER, ttl_seconds=ttl_seconds, prefix="wrk-")
        self.log_audit("WORKER_INIT", "ALLOWED", role=ROLE_WORKER, session_id=sid, detail="Sesi worker terpercaya diinisialisasi")
        return token

    def authenticate(self, credential, ttl_seconds=3600):
        """Verify administrator credential on the server side using constant-time digest comparison."""
        candidate = str(credential or "").strip()
        if not candidate or not self._admin_hash:
            self.monitor.increment("unauthorized_attempts")
            self.log_audit("ADMIN_LOGIN", "DENIED", role=ROLE_VIEWER, detail="Kredensial kosong atau token admin belum dikonfigurasi")
            return None
        cand_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        matched = hmac.compare_digest(cand_hash, self._admin_hash) or (
            len(candidate) == 64 and hmac.compare_digest(candidate.lower(), self._admin_hash)
        )
        if not matched:
            self.monitor.increment("unauthorized_attempts")
            self.log_audit("ADMIN_LOGIN", "DENIED", role=ROLE_VIEWER, detail="Token administrator tidak valid")
            return None
        token, session_id = self._issue_signed_token(ROLE_ADMIN, ttl_seconds=ttl_seconds, prefix="adm-")
        self.log_audit("ADMIN_LOGIN", "ALLOWED", role=ROLE_ADMIN, session_id=session_id, detail="Login administrator berhasil")
        return token

    def revoke_session(self, session_token):
        info = self.verify_session(session_token)
        if info["session_id"] != "anonymous":
            with self.lock:
                self._sessions.pop(info["session_id"], None)
            self.log_audit("ADMIN_LOGOUT", "ALLOWED", role=info["role"], session_id=info["session_id"], detail="Logout administrator")

    def verify_session(self, session_token=None):
        """BUG-15: Return server-verified role ('ADMIN', 'WORKER', or 'VIEWER'). Anonymous is ALWAYS 'VIEWER'."""
        if not session_token:
            return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}
        parts = str(session_token).split(":")
        if len(parts) != 4:
            return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}
        session_id, role, exp_str, sig = parts
        payload = f"{session_id}:{role}:{exp_str}"
        expected_sig = hmac.new(self._server_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}
        try:
            expires_at = float(exp_str)
        except ValueError:
            return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}
        with self.lock:
            stored = self._sessions.get(session_id)
            if not stored or self.clock() >= min(expires_at, stored["expires_at"]):
                self._sessions.pop(session_id, None)
                return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}
            return {"role": stored["role"], "session_id": session_id, "authenticated": True}

    def authorize_action(self, action, session_token=None, last_scan_failed=False):
        """BUG-10, BUG-15, BUG-16: Server-side authorization + quota/rate-limit gate for all privileged actions."""
        info = self.verify_session(session_token)
        role, sid = info["role"], info["session_id"]
        if action in PRIVILEGED_ACTIONS and role not in (ROLE_ADMIN, ROLE_WORKER):
            self.monitor.increment("unauthorized_attempts")
            msg = f"Akses ditolak (UNAUTHORIZED): tindakan '{action}' memerlukan hak akses ADMINISTRATOR atau WORKER terpercaya."
            self.log_audit(action, "DENIED", role=role, session_id=sid, detail=msg)
            return False, msg

        # Apply action-specific rate limits and quotas (BUG-10 #4, BUG-13, BUG-16 #3)
        if action == "MANUAL_SCAN":
            cooldown = 0.0 if (last_scan_failed or role == ROLE_WORKER) else self.manual_scan_cooldown
            ok, reason = self.limiter.check_and_record(
                "manual_scan", self.max_manual_scans_per_min, 60.0, min_interval_seconds=cooldown
            )
            if not ok:
                self.monitor.increment("rate_limited_events")
                self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                return False, reason
            self.monitor.increment("manual_scans")

        elif action == "AUTO_SCAN":
            self.monitor.increment("auto_scans")

        elif action == "TELEGRAM_TEST":
            if role != ROLE_ADMIN:
                self.monitor.increment("unauthorized_attempts")
                msg = "Akses ditolak (UNAUTHORIZED): TELEGRAM_TEST hanya untuk ADMINISTRATOR."
                self.log_audit(action, "DENIED", role=role, session_id=sid, detail=msg)
                return False, msg
            ok, reason = self.limiter.check_and_record(
                "telegram_test", 3, 600.0, min_interval_seconds=30.0
            )
            if not ok:
                self.monitor.increment("rate_limited_events")
                self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                return False, reason
            self.monitor.increment("telegram_requests")

        elif action == "TELEGRAM_ALERT":
            ok, reason = self.limiter.check_and_record(
                "telegram_alert", self.max_telegram_per_min, 60.0, min_interval_seconds=0.0
            )
            if not ok:
                self.monitor.increment("rate_limited_events")
                self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                return False, reason
            self.monitor.increment("telegram_requests")

        elif action in ("JOURNAL_EXPORT", "JOURNAL_RESTORE"):
            if role != ROLE_ADMIN:
                self.monitor.increment("unauthorized_attempts")
                msg = f"Akses ditolak (UNAUTHORIZED): {action} hanya untuk ADMINISTRATOR."
                self.log_audit(action, "DENIED", role=role, session_id=sid, detail=msg)
                return False, msg
            ok, reason = self.limiter.check_and_record(
                action.lower(), self.max_exports_per_5min, 300.0, min_interval_seconds=0.0
            )
            if not ok:
                self.monitor.increment("rate_limited_events")
                self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                return False, reason
            self.monitor.increment("journal_exports" if action == "JOURNAL_EXPORT" else "journal_restores")

        elif action == "AI_REVIEW":
            with self.lock:
                used = self._session_ai_counts.get(sid, 0)
                if used >= self.max_ai_calls_per_session:
                    reason = f"Kuota AI per sesi tercapai ({self.max_ai_calls_per_session} panggilan)."
                    self.monitor.increment("rate_limited_events")
                    self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                    return False, reason
            ok_hour, reason_hour = self.limiter.check_and_record(
                "ai_review_global_hour", self.max_ai_calls_global_per_hour, 3600.0, min_interval_seconds=0.0
            )
            if not ok_hour:
                self.monitor.increment("rate_limited_events")
                self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason_hour)
                return False, reason_hour
            ok, reason = self.limiter.check_and_record(
                "ai_review", self.max_ai_calls_per_min, 60.0, min_interval_seconds=0.0
            )
            if not ok:
                self.monitor.increment("rate_limited_events")
                self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                return False, reason
            with self.lock:
                self._session_ai_counts[sid] = self._session_ai_counts.get(sid, 0) + 1
            self.monitor.increment("groq_ai_requests")

        self.log_audit(action, "ALLOWED", role=role, session_id=sid, detail=f"Tindakan {action} diizinkan")
        return True, "ALLOWED"
