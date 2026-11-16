"""Persistent Database & Backup Backend for CoinAgentX (BUG-17, BUG-18, BUG-22, BUG-23, BUG-24).

Guarantees:
1. BUG-22 (No PostgreSQL False Positives & Centralized Config):
   - `resolve_database_config` / `configured_database_url` resolves `DATABASE_URL` from centralized
     runtime configuration, Streamlit Secrets getter, or environment variables.
   - `connect_database` NEVER silently falls back to SQLite when PostgreSQL is requested; if the
     driver (`psycopg` / `psycopg2`) is missing or connection fails, it raises `DatabaseConfigurationError`.
   - `persistence_status` only reports `POSTGRESQL` (`persistent=True`) after opening a real
     PostgreSQL connection and completing a verified read/write health probe.
2. BUG-23 (Active Supabase REST Sync with Namespace, Schema Version, Checksum, Stale Guard & Retry):
   - `SupabaseStateSync` envelopes every backup with `namespace`, `schema_version`, `backup_at_ms`,
     `state_updated_ms`, and canonical `checksum_sha256`.
   - Handles transient network errors with bounded retries and never claims `SUPABASE_REST_SYNC`
     is active until synchronization is verified. Never equates REST snapshot replication with
     ACID PostgreSQL transactions.
3. BUG-24 (Authentic SQLite vs PostgreSQL Health & Integrity Verification):
   - `_PostgresConnectionAdapter` never translates `PRAGMA quick_check` into a fake `SELECT 'ok'`.
   - Provides separate `verify_sqlite_integrity`, `verify_postgres_health`, and unified
     `verify_database_health` distinguishing connection, database health, and backup availability.
"""
from contextlib import closing, contextmanager
from pathlib import Path
import hashlib
import inspect
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


BACKUP_SCHEMA_VERSION = 2
_RUNTIME_CONFIG_LOCK = threading.RLock()
_RUNTIME_SECRET_GETTER = None
_RUNTIME_DB_URL = None


class DatabaseConfigurationError(RuntimeError):
    """BUG-22: Raised when PostgreSQL is configured but the driver is missing, URL is invalid, or connection fails."""


def configure_database_runtime(secret_getter=None, db_url=None):
    """BUG-22 #1: Register centralized runtime secret getter and/or explicit DATABASE_URL for dashboard and worker."""
    global _RUNTIME_SECRET_GETTER, _RUNTIME_DB_URL
    with _RUNTIME_CONFIG_LOCK:
        if secret_getter is not None:
            _RUNTIME_SECRET_GETTER = secret_getter
        if db_url is not None:
            _RUNTIME_DB_URL = str(db_url).strip() or None


def get_runtime_secret_getter():
    with _RUNTIME_CONFIG_LOCK:
        return _RUNTIME_SECRET_GETTER


def _effective_getter(get_secret=None):
    if callable(get_secret):
        return get_secret
    runtime_getter = get_runtime_secret_getter()
    if callable(runtime_getter):
        return runtime_getter
    return lambda k: os.getenv(k, "")


def configured_database_url(get_secret=None, explicit_url=None):
    """BUG-22 #1: Return configured PostgreSQL/Supabase connection URL from explicit arg, Secrets getter, runtime config, or env."""
    if explicit_url is not None and str(explicit_url).strip():
        return str(explicit_url).strip()
    with _RUNTIME_CONFIG_LOCK:
        if _RUNTIME_DB_URL:
            return _RUNTIME_DB_URL
    getter = _effective_getter(get_secret)
    for key in ("DATABASE_URL", "SUPABASE_DB_URL", "POSTGRES_URL", "COINAGENT_DATABASE_URL"):
        try:
            val = str(getter(key) or os.getenv(key, "")).strip()
        except Exception:
            val = str(os.getenv(key, "")).strip()
        if val:
            return val
    return ""


def configured_supabase_rest(get_secret=None):
    """Return (supabase_url, supabase_key) if Supabase REST persistence is configured."""
    getter = _effective_getter(get_secret)
    try:
        url = str(getter("SUPABASE_URL") or os.getenv("SUPABASE_URL", "")).strip().rstrip("/")
    except Exception:
        url = str(os.getenv("SUPABASE_URL", "")).strip().rstrip("/")
    try:
        key = str(
            getter("SUPABASE_SERVICE_ROLE_KEY")
            or getter("SUPABASE_KEY")
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
            or os.getenv("SUPABASE_KEY", "")
        ).strip()
    except Exception:
        key = str(os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "")).strip()
    if url.startswith("https://") and key:
        return url, key
    return "", ""


def resolve_database_config(get_secret=None, explicit_url=None):
    """BUG-22 #1: Single centralized configuration resolver for PostgreSQL and Supabase REST."""
    pg_url = configured_database_url(get_secret=get_secret, explicit_url=explicit_url)
    sb_url, sb_key = configured_supabase_rest(get_secret=get_secret)
    return {
        "pg_url": pg_url,
        "supabase_url": sb_url,
        "supabase_key": sb_key,
    }


def _sanitize_namespace_prefix(db_path):
    if not db_path:
        return ""
    stem = re.sub(r"[^a-zA-Z0-9_]", "_", Path(db_path).stem.lower()).strip("_")
    if not stem or stem in ("postgres", "postgres_health", "default"):
        return ""
    return f"{stem}_"


class _PostgresCursorAdapter:
    """Adapts a DB-API 2.0 PostgreSQL cursor to accept SQLite-style queries and idioms."""

    def __init__(self, raw_cursor):
        self._cur = raw_cursor

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        rows = self._cur.fetchall()
        return iter(rows if rows is not None else [])


class _PostgresConnectionAdapter:
    """Wraps a psycopg/psycopg2 connection to provide SQLite-compatible `.execute()` and ACID transactions."""

    is_postgres = True
    backend_kind = "POSTGRESQL"

    def __init__(self, raw_conn, driver_name="psycopg", namespace_prefix=""):
        self._conn = raw_conn
        self.driver_name = driver_name
        self.namespace_prefix = str(namespace_prefix or "")

    @staticmethod
    def _translate_sql(sql, namespace_prefix=""):
        s = sql.strip()
        upper = s.upper()
        # BUG-24: NEVER fake PRAGMA quick_check or PRAGMA integrity_check on PostgreSQL!
        if upper.startswith("PRAGMA QUICK_CHECK") or upper.startswith("PRAGMA INTEGRITY_CHECK") or upper.startswith("PRAGMA FOREIGN_KEY_CHECK"):
            raise DatabaseConfigurationError(
                "PRAGMA quick_check/integrity_check hanya valid untuk SQLite; gunakan verify_postgres_health() untuk PostgreSQL."
            )
        if upper.startswith("PRAGMA JOURNAL_MODE"):
            return "SELECT 'wal'"
        if upper == "BEGIN IMMEDIATE":
            return "BEGIN"
        s = re.sub(
            r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
            "BIGSERIAL PRIMARY KEY",
            s,
            flags=re.I,
        )
        if re.match(r"^INSERT\s+OR\s+IGNORE\s+INTO\s+(\w+)", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + " ON CONFLICT DO NOTHING"
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+paper_meta", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+paper_trades", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + (
                " ON CONFLICT (signal) DO UPDATE SET symbol = EXCLUDED.symbol, mode = EXCLUDED.mode, "
                "status = EXCLUDED.status, realized = EXCLUDED.realized, updated_ms = EXCLUDED.updated_ms, "
                "payload = EXCLUDED.payload"
            )
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+telegram_alerts", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + (
                " ON CONFLICT (target, signal) DO UPDATE SET state = EXCLUDED.state, "
                "retry_at = EXCLUDED.retry_at, updated_at = EXCLUDED.updated_at"
            )
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+analyses", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + (
                " ON CONFLICT (id) DO UPDATE SET at_ms = EXCLUDED.at_ms, symbol = EXCLUDED.symbol, "
                "mode = EXCLUDED.mode, status = EXCLUDED.status, payload = EXCLUDED.payload"
            )
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+scanner_locks", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + (
                " ON CONFLICT (lock_name) DO UPDATE SET owner_id = EXCLUDED.owner_id, "
                "acquired_ms = EXCLUDED.acquired_ms, expires_ms = EXCLUDED.expires_ms"
            )
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+security_meta", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+security_sessions", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + (
                " ON CONFLICT (session_id) DO UPDATE SET role = EXCLUDED.role, "
                "expires_at = EXCLUDED.expires_at, auth_fp = EXCLUDED.auth_fp, token = EXCLUDED.token"
            )
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+coinagent_db_healthcheck", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + " ON CONFLICT (probe_key) DO UPDATE SET probe_ts = EXCLUDED.probe_ts"

        if namespace_prefix:
            for tbl in ("paper_meta", "paper_trades", "paper_events", "paper_status", "scanner_locks"):
                s = re.sub(rf"\b{tbl}\b", f"{namespace_prefix}{tbl}", s)

        s = s.replace("?", "%s")
        return s

    def execute(self, sql, params=()):
        translated = self._translate_sql(sql, namespace_prefix=self.namespace_prefix)
        cur = self._conn.cursor()
        cur.execute(translated, tuple(params) if params else ())
        return _PostgresCursorAdapter(cur)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        return False


def normalize_postgres_url(raw_url):
    """Sanitize common user paste mistakes in DATABASE_URL (e.g., [password] brackets, raw @/#/$/?/ in password, quotes, sslmode)."""
    s = str(raw_url or "").strip().strip('"').strip("'")
    if not s:
        return ""
    # Strip accidental key assignment prefix if user pasted `DATABASE_URL = "postgresql://..."`
    s = re.sub(r"^(?:DATABASE_URL|SUPABASE_DB_URL|POSTGRES_URL)\s*=\s*", "", s, flags=re.I).strip().strip('"').strip("'")
    if "://" not in s:
        return s
    scheme, rest = s.split("://", 1)
    if scheme.lower() == "postgres":
        scheme = "postgresql"
    if "@" not in rest:
        return f"{scheme}://{rest}"
    # Split on the LAST '@' so raw '@', '#', '?', or '/' inside the password do not corrupt the hostname
    userinfo, hostpart = rest.rsplit("@", 1)
    if ":" in userinfo:
        user, pwd = userinfo.split(":", 1)
        user = user.strip()
        pwd = pwd.strip()
        while pwd.startswith("[") and pwd.endswith("]") and len(pwd) >= 2:
            pwd = pwd[1:-1].strip()
        raw_pwd = urllib.parse.unquote(pwd)
        safe_pwd = urllib.parse.quote(raw_pwd, safe="")
        userinfo = f"{user}:{safe_pwd}"
    normalized = f"{scheme}://{userinfo}@{hostpart}"
    if ("supabase.com" in hostpart or "supabase.co" in hostpart) and "sslmode=" not in hostpart:
        sep = "&" if "?" in hostpart else "?"
        normalized = f"{normalized}{sep}sslmode=require"
    return normalized


def connect_database(db_path, pg_url=None, get_secret=None):
    """BUG-22: Connect to PostgreSQL when configured (failing closed on driver/connection error), else SQLite WAL."""
    raw_url = configured_database_url(get_secret=get_secret, explicit_url=pg_url)
    url = normalize_postgres_url(raw_url)
    if url:
        if not url.startswith(("postgres://", "postgresql://")):
            raise DatabaseConfigurationError(
                f"Skema DATABASE_URL tidak valid ({url.split(':', 1)[0]}). Gunakan postgres:// atau postgresql://."
            )
        last_import_err = None
        for mod_name in ("psycopg", "psycopg2"):
            try:
                pg_mod = __import__(mod_name)
            except ImportError as imp_exc:
                last_import_err = imp_exc
                continue
            try:
                sig_params = inspect.signature(pg_mod.connect).parameters if hasattr(pg_mod, "connect") else {}
                conn_kwargs = {}
                if "connect_timeout" in sig_params:
                    conn_kwargs["connect_timeout"] = 10
                if "prepare_threshold" in sig_params:
                    # Required for Supabase Transaction Pooler (Supavisor port 6543) compatibility
                    conn_kwargs["prepare_threshold"] = None
                raw = pg_mod.connect(url, **conn_kwargs) if conn_kwargs else pg_mod.connect(url)
                prefix = _sanitize_namespace_prefix(db_path)
                return _PostgresConnectionAdapter(raw, driver_name=mod_name, namespace_prefix=prefix)
            except Exception as conn_exc:
                hint = ""
                if re.search(r"@db\.[a-z0-9]+\.supabase\.co", url, flags=re.I):
                    hint = (
                        " [PETUNJUK SUPABASE: Host `db.xxxx.supabase.co` adalah Direct Connection IPv6 yang TIDAK "
                        "didukung oleh jaringan IPv4 Streamlit Cloud. Di Supabase klik tombol Connect -> ubah Method "
                        "menjadi 'Transaction pooler' (port 6543) atau 'Session pooler' (port 5432) dengan host "
                        "`aws-0-....pooler.supabase.com`]"
                    )
                raise DatabaseConfigurationError(
                    f"Gagal membuka koneksi PostgreSQL ({mod_name}): {type(conn_exc).__name__}: {conn_exc}{hint}"
                ) from conn_exc
        raise DatabaseConfigurationError(
            "DATABASE_URL PostgreSQL dikonfigurasi, tetapi driver PostgreSQL (psycopg / psycopg2) tidak terpasang. "
            "Pasang `psycopg[binary]` dari requirements.txt; sistem menolak fallback diam-diam ke SQLite."
        ) from last_import_err

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def atomic_transaction(conn):
    """Execute a block inside an explicit database transaction with automatic rollback on failure."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def verify_sqlite_integrity(db_path):
    """BUG-24: Dedicated SQLite-only integrity check via PRAGMA quick_check & foreign_key_check."""
    if isinstance(db_path, _PostgresConnectionAdapter):
        return {
            "ok": False,
            "backend": "POSTGRESQL",
            "connection_status": "INVALID_BACKEND_FOR_SQLITE_CHECK",
            "health_status": "INVALID_CHECK",
            "status": "INVALID_CALL_USE_VERIFY_POSTGRES_HEALTH",
            "db": "",
        }
    raw_str = str(db_path or "")
    if raw_str.startswith(("postgres://", "postgresql://")):
        return {
            "ok": False,
            "backend": "POSTGRESQL",
            "connection_status": "INVALID_BACKEND_FOR_SQLITE_CHECK",
            "health_status": "INVALID_CHECK",
            "status": "INVALID_CALL_USE_VERIFY_POSTGRES_HEALTH",
            "db": raw_str,
        }
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(path, timeout=10)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            row = conn.execute("PRAGMA quick_check").fetchone()
            fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            ok = bool(row and row[0] == "ok" and not fk_rows)
            return {
                "ok": ok,
                "backend": "SQLITE",
                "connection_status": "CONNECTED_SQLITE",
                "health_status": "HEALTHY_SQLITE_QUICK_CHECK" if ok else "CORRUPT_SQLITE",
                "status": row[0] if row else "unknown",
                "db": str(path),
            }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "SQLITE",
            "connection_status": "CONNECTION_FAILED",
            "health_status": "UNHEALTHY",
            "status": f"DB_ERROR: {type(exc).__name__}",
            "error": str(exc),
            "db": str(path),
        }


def verify_postgres_health(pg_url=None, get_secret=None, db_path=None):
    """BUG-22 & BUG-24: Authentic PostgreSQL health check performing real catalog inspection and read/write verification."""
    url = configured_database_url(get_secret=get_secret, explicit_url=pg_url)
    if not url:
        return {
            "ok": False,
            "backend": "POSTGRESQL",
            "connection_status": "NOT_CONFIGURED",
            "health_status": "UNCONFIGURED",
            "backup_status": "NONE",
            "verified_rw": False,
            "status": "NOT_CONFIGURED",
            "error": "DATABASE_URL belum dikonfigurasi.",
        }
    try:
        with closing(connect_database(db_path or "postgres.db", pg_url=url, get_secret=get_secret)) as conn:
            if not isinstance(conn, _PostgresConnectionAdapter) or isinstance(conn, sqlite3.Connection):
                raise DatabaseConfigurationError("Koneksi yang dikembalikan bukan _PostgresConnectionAdapter.")
            cat_row = conn.execute("SELECT current_database(), current_schema()").fetchone()
            db_name = str(cat_row[0]) if cat_row and len(cat_row) > 0 else "postgres"
            schema_name = str(cat_row[1]) if cat_row and len(cat_row) > 1 else "public"
            probe_key = _sanitize_namespace_prefix(db_path) or "global"
            probe_ts = int(time.time() * 1000)
            with atomic_transaction(conn):
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS coinagent_db_healthcheck ("
                    "probe_key TEXT PRIMARY KEY, probe_ts BIGINT NOT NULL)"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO coinagent_db_healthcheck VALUES (?, ?)",
                    (probe_key, probe_ts),
                )
                read_row = conn.execute(
                    "SELECT probe_ts FROM coinagent_db_healthcheck WHERE probe_key = ?",
                    (probe_key,),
                ).fetchone()
            verified_rw = bool(read_row and int(read_row[0]) == probe_ts)
            if not verified_rw:
                raise DatabaseConfigurationError("Verifikasi baca/tulis PostgreSQL gagal (nilai probe tidak cocok).")
            return {
                "ok": True,
                "backend": "POSTGRESQL",
                "driver": conn.driver_name,
                "database": db_name,
                "schema": schema_name,
                "connection_status": "CONNECTED_POSTGRESQL",
                "health_status": "HEALTHY_POSTGRESQL_RW",
                "backup_status": "ACID_PRIMARY_POSTGRESQL",
                "verified_rw": True,
                "status": "ok",
            }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "POSTGRESQL",
            "connection_status": "CONNECTION_FAILED",
            "health_status": "UNHEALTHY",
            "backup_status": "UNAVAILABLE",
            "verified_rw": False,
            "status": f"POSTGRES_ERROR: {type(exc).__name__}",
            "error": str(exc),
        }


def verify_database_health(db_path=None, pg_url=None, get_secret=None, supabase_sync=None):
    """BUG-24: Unified database health check distinguishing connection, backend health, and backup availability."""
    url = configured_database_url(get_secret=get_secret, explicit_url=pg_url)
    if url:
        return verify_postgres_health(pg_url=url, get_secret=get_secret, db_path=db_path)
    sq = verify_sqlite_integrity(db_path or "futures_desk.db")
    if supabase_sync is not None and getattr(supabase_sync, "enabled", False):
        if supabase_sync.is_verified_active():
            sq["backup_status"] = "REPLICA_SYNC_VERIFIED"
        else:
            sq["backup_status"] = "REPLICA_SYNC_UNVERIFIED"
    else:
        sq["backup_status"] = "LOCAL_EPHEMERAL_NO_REMOTE_BACKUP"
    return sq


VALID_INSTRUMENT_KINDS = frozenset({"USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL", "SPOT_REFERENCE"})


def compute_backup_checksum(payload_dict):
    """BUG-23 #4 & BUG-28: Compute deterministic SHA-256 checksum over ALL critical backup & security data."""
    if isinstance(payload_dict, (str, bytes)):
        payload_dict = json.loads(payload_dict)
    canonical_obj = {
        "schema": int(payload_dict.get("schema", BACKUP_SCHEMA_VERSION)),
        "type": str(payload_dict.get("type") or "PAPER_SIMULATION"),
        "instrument_kind": str(payload_dict.get("instrument_kind") or ""),
        "namespace": str(payload_dict.get("namespace") or ""),
        "meta": {
            str(k): str(v) for k, v in sorted((payload_dict.get("meta") or {}).items())
            if k not in ("worker_heartbeat", "scan_start", "state_updated_ms")
        },
        "trades": payload_dict.get("trades", []),
        "events": payload_dict.get("events", []),
        "telegram_alerts": payload_dict.get("telegram_alerts", []),
        "security_audit_log": payload_dict.get("security_audit_log", []),
        "security_quota_events": payload_dict.get("security_quota_events", []),
    }
    raw = json.dumps(canonical_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class SupabaseStateSync:
    """BUG-23: State snapshot backup/restore over Supabase REST with namespace, version, checksum, stale-guard, and retry."""

    SCHEMA_VERSION = BACKUP_SCHEMA_VERSION

    def __init__(self, supabase_url="", supabase_key="", transport=None, max_retries=3, retry_backoff_seconds=0.05):
        self.url = str(supabase_url or "").rstrip("/")
        self.key = str(supabase_key or "").strip()
        self.transport = transport
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.last_push_ok = False
        self.last_pull_ok = False
        self.last_sync_at_ms = 0
        self.last_error = ""
        self.last_checksum = ""
        self.push_count = 0
        self.pull_count = 0

    @property
    def enabled(self):
        return bool(self.url.startswith("https://") and self.key)

    def is_verified_active(self):
        """BUG-23 #7: Never claim Supabase sync is active until a push or pull operation has been verified."""
        return bool(self.enabled and (self.last_push_ok or self.last_pull_ok) and not self.last_error)

    @staticmethod
    def _extract_state_updated_ms(payload_dict, fallback_ms=0):
        candidates = [int(fallback_ms or 0)]
        meta = payload_dict.get("meta") or {}
        if meta.get("state_updated_ms"):
            try:
                candidates.append(int(meta["state_updated_ms"]))
            except (TypeError, ValueError):
                pass
        for t in payload_dict.get("trades", []):
            if isinstance(t, dict) and t.get("updated_ms") is not None:
                try:
                    candidates.append(int(t["updated_ms"]))
                except (TypeError, ValueError):
                    pass
        for ev in payload_dict.get("events", []):
            if isinstance(ev, dict) and ev.get("at_ms") is not None:
                try:
                    candidates.append(int(ev["at_ms"]))
                except (TypeError, ValueError):
                    pass
        return max(candidates)

    def build_envelope(self, namespace, backup_json, backup_at_ms=None):
        """BUG-23 #3 & #4: Wrap backup payload with isolated namespace, schema_version, timestamps, and SHA-256 checksum."""
        data = json.loads(backup_json) if isinstance(backup_json, (str, bytes)) else dict(backup_json)
        ns = str(namespace or data.get("namespace") or "DEFAULT")
        data["namespace"] = ns
        data["schema"] = int(data.get("schema", self.SCHEMA_VERSION))
        now_ms = int(backup_at_ms if backup_at_ms is not None else int(time.time() * 1000))
        state_ms = self._extract_state_updated_ms(data, fallback_ms=now_ms)
        checksum = compute_backup_checksum(data)
        data["checksum_sha256"] = checksum
        data["backup_at_ms"] = now_ms
        data["state_updated_ms"] = state_ms
        return {
            "namespace": ns,
            "schema_version": self.SCHEMA_VERSION,
            "backup_at_ms": now_ms,
            "state_updated_ms": state_ms,
            "checksum_sha256": checksum,
            "payload": data,
        }

    def verify_envelope(self, envelope, expected_namespace=None):
        """BUG-23 #3, #4: Verify backup envelope schema, namespace, and SHA-256 integrity checksum."""
        if not isinstance(envelope, dict):
            return False, "Envelope backup bukan objek dict yang valid.", None
        payload = envelope.get("payload") if "payload" in envelope and isinstance(envelope.get("payload"), dict) else envelope
        schema_ver = int(envelope.get("schema_version") or payload.get("schema") or 0)
        if schema_ver < self.SCHEMA_VERSION:
            return False, f"Versi schema backup ({schema_ver}) tidak didukung (minimal v{self.SCHEMA_VERSION}).", None
        ns = str(envelope.get("namespace") or payload.get("namespace") or "")
        if expected_namespace and ns and ns != str(expected_namespace):
            return False, f"Namespace backup ({ns}) tidak cocok dengan target ({expected_namespace}).", None
        declared_sum = str(envelope.get("checksum_sha256") or payload.get("checksum_sha256") or "").strip()
        if not declared_sum:
            return False, "Backup tidak memiliki checksum_sha256.", None
        computed_sum = compute_backup_checksum(payload)
        if declared_sum != computed_sum:
            return False, "Checksum SHA-256 backup tidak cocok (data korup atau dimodifikasi).", None
        return True, "OK", payload

    def _execute_request(self, req, timeout=8):
        if callable(self.transport):
            return self.transport(req, timeout)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()

    def push_backup(self, namespace, backup_json, backup_at_ms=None):
        """BUG-23 #1, #6: Push verified state snapshot to Supabase REST with automatic retries on transient failures."""
        if not self.enabled:
            return False
        envelope = self.build_envelope(namespace, backup_json, backup_at_ms=backup_at_ms)
        body = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode("utf-8")
        last_err = ""
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                f"{self.url}/rest/v1/coinagent_backups?on_conflict=namespace",
                data=body,
                method="POST",
                headers={
                    "apikey": self.key,
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
            )
            try:
                status, _ = self._execute_request(req, timeout=8)
                if 200 <= int(status) < 300:
                    self.last_push_ok = True
                    self.last_sync_at_ms = envelope["backup_at_ms"]
                    self.last_checksum = envelope["checksum_sha256"]
                    self.last_error = ""
                    self.push_count += 1
                    return True
                last_err = f"HTTP {status}"
                if int(status) < 500 and int(status) != 429:
                    break
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < self.max_retries and self.retry_backoff_seconds > 0:
                time.sleep(self.retry_backoff_seconds * (2 ** attempt))
        self.last_push_ok = False
        self.last_error = last_err
        return False

    def pull_backup(self, namespace):
        """BUG-23 #2, #4, #6: Pull and verify remote backup envelope for a specific ledger namespace with retries."""
        if not self.enabled:
            return None
        ns = str(namespace)
        encoded_ns = urllib.parse.quote(ns, safe="")
        last_err = ""
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                f"{self.url}/rest/v1/coinagent_backups?namespace=eq.{encoded_ns}"
                "&select=namespace,schema_version,backup_at_ms,state_updated_ms,checksum_sha256,payload&limit=1",
                method="GET",
                headers={
                    "apikey": self.key,
                    "Authorization": f"Bearer {self.key}",
                    "Accept": "application/json",
                },
            )
            try:
                status, raw_bytes = self._execute_request(req, timeout=8)
                if 200 <= int(status) < 300:
                    rows = json.loads(raw_bytes.decode("utf-8") if isinstance(raw_bytes, bytes) else str(raw_bytes))
                    if not isinstance(rows, list) or not rows:
                        return None
                    row = rows[0]
                    ok, reason, _ = self.verify_envelope(row, expected_namespace=ns)
                    if not ok:
                        self.last_pull_ok = False
                        self.last_error = reason
                        return None
                    self.last_pull_ok = True
                    self.last_sync_at_ms = int(row.get("backup_at_ms") or int(time.time() * 1000))
                    self.last_checksum = str(row.get("checksum_sha256") or "")
                    self.last_error = ""
                    self.pull_count += 1
                    return row
                last_err = f"HTTP {status}"
                if int(status) < 500 and int(status) != 429:
                    break
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < self.max_retries and self.retry_backoff_seconds > 0:
                time.sleep(self.retry_backoff_seconds * (2 ** attempt))
        self.last_pull_ok = False
        self.last_error = last_err
        return None


def persistence_status(db_path=None, get_secret=None, pg_url=None, ledger=None, supabase_sync=None):
    """BUG-22 & BUG-23: Report verified active database backend; never claim PERSISTENT or POSTGRESQL without real verification."""
    effective_pg_url = configured_database_url(
        get_secret=get_secret,
        explicit_url=pg_url or (getattr(ledger, "pg_url", None) if ledger is not None else None),
    )
    target_db = db_path or (getattr(ledger, "db", None) if ledger is not None else None)

    if effective_pg_url:
        health = verify_postgres_health(pg_url=effective_pg_url, get_secret=get_secret, db_path=target_db)
        if health.get("ok") and health.get("verified_rw"):
            return {
                "backend": "POSTGRESQL",
                "mode": "POSTGRESQL_SUPABASE",
                "persistent": True,
                "persistent_across_reboots": True,
                "transactional": True,
                "acid_transactional": True,
                "connected": True,
                "verified_rw": True,
                "driver": health.get("driver", "psycopg"),
                "database": health.get("database", ""),
                "detail": "Terhubung & terverifikasi baca/tulis pada database PostgreSQL eksternal (ACID persisten lintas reboot/redeploy).",
            }
        return {
            "backend": "POSTGRESQL_ERROR",
            "mode": "POSTGRESQL_ERROR",
            "persistent": False,
            "persistent_across_reboots": False,
            "transactional": False,
            "acid_transactional": False,
            "connected": False,
            "verified_rw": False,
            "error": health.get("error", "Koneksi atau verifikasi baca/tulis PostgreSQL gagal."),
            "detail": (
                f"KONFIGURASI POSTGRESQL GAGAL: {health.get('error', 'Koneksi gagal')}. "
                "Sistem tidak melakukan fallback diam-diam ke SQLite dan tidak mengklaim status PERSISTENT."
            ),
        }

    sync_obj = supabase_sync or (getattr(ledger, "supabase_sync", None) if ledger is not None else None)
    sb_url, sb_key = configured_supabase_rest(get_secret=get_secret)
    if (sync_obj is not None and sync_obj.enabled) or (sb_url and sb_key):
        verified = bool(sync_obj is not None and sync_obj.is_verified_active())
        if verified:
            return {
                "backend": "SUPABASE_REST_SYNC",
                "mode": "SQLITE_WITH_SUPABASE_REST_REPLICA",
                "persistent": True,
                "persistent_across_reboots": True,
                "transactional": True,
                "acid_transactional": False,
                "sync_active": True,
                "verified_rw": True,
                "last_sync_at_ms": getattr(sync_obj, "last_sync_at_ms", 0),
                "last_checksum": getattr(sync_obj, "last_checksum", ""),
                "detail": (
                    "SQLite WAL lokal dengan replikasi snapshot REST ke Supabase yang telah terverifikasi "
                    "(catatan: replikasi REST adalah snapshot ber-checksum, bukan transaksi ACID PostgreSQL)."
                ),
            }
        return {
            "backend": "SQLITE_WAL_UNVERIFIED_SYNC",
            "mode": "SQLITE_WAL_UNVERIFIED_SYNC",
            "persistent": False,
            "persistent_across_reboots": False,
            "transactional": True,
            "acid_transactional": False,
            "sync_active": False,
            "verified_rw": False,
            "error": getattr(sync_obj, "last_error", "") if sync_obj is not None else "",
            "detail": (
                "Kredensial Supabase REST terdeteksi, tetapi sinkronisasi belum terverifikasi berhasil; "
                "status belum diklaim PERSISTENT."
            ),
        }

    return {
        "backend": "SQLITE_WAL",
        "mode": "SQLITE_LOCAL_EPHEMERAL",
        "persistent": False,
        "persistent_across_reboots": False,
        "transactional": True,
        "acid_transactional": True,
        "connected": True,
        "verified_rw": False,
        "db_path": str(target_db or ""),
        "detail": (
            "Menggunakan SQLite WAL lokal (NON-PERSISTENT pada Streamlit Community Cloud saat reboot/redeploy). "
            "Konfigurasikan DATABASE_URL (PostgreSQL) di Secrets untuk persistensi ACID penuh."
        ),
    }
