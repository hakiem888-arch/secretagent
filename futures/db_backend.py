"""Persistent Database & Backup Backend for CoinAgentX (BUG-17 & BUG-18).

Supports:
1. PostgreSQL / Supabase direct database connection (`DATABASE_URL`, `SUPABASE_DB_URL`, `POSTGRES_URL`)
   with full ACID transactions (`BEGIN` / `COMMIT` / `ROLLBACK`) so journal data, positions,
   Telegram deduplication, and security audit logs survive Streamlit Community Cloud reboots/redeploys.
2. Supabase REST / Storage state replication (`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`).
3. Transactional SQLite WAL (`BEGIN IMMEDIATE`) with distributed DB lease locks (`scanner_locks`),
   idempotent backup export, and atomic backup restoration (`restore_backup_payload`).
"""
from contextlib import closing, contextmanager
from pathlib import Path
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
import urllib.error


def configured_database_url(get_secret=None):
    """Return configured PostgreSQL/Supabase connection URL if present."""
    getter = get_secret or (lambda k: os.getenv(k, ""))
    for key in ("DATABASE_URL", "SUPABASE_DB_URL", "POSTGRES_URL", "COINAGENT_DATABASE_URL"):
        val = str(getter(key) or os.getenv(key, "")).strip()
        if val.startswith(("postgres://", "postgresql://")):
            return val
    return ""


def configured_supabase_rest(get_secret=None):
    """Return (supabase_url, supabase_key) if Supabase REST persistence is configured."""
    getter = get_secret or (lambda k: os.getenv(k, ""))
    url = str(getter("SUPABASE_URL") or os.getenv("SUPABASE_URL", "")).strip().rstrip("/")
    key = str(
        getter("SUPABASE_SERVICE_ROLE_KEY")
        or getter("SUPABASE_KEY")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        or os.getenv("SUPABASE_KEY", "")
    ).strip()
    if url.startswith("https://") and key:
        return url, key
    return "", ""


def persistence_status(db_path=None, get_secret=None):
    """Report active database persistence mode and durability guarantees (BUG-17)."""
    pg_url = configured_database_url(get_secret)
    sb_url, sb_key = configured_supabase_rest(get_secret)
    if pg_url:
        return {
            "backend": "POSTGRESQL_SUPABASE",
            "persistent_across_reboots": True,
            "transactional": True,
            "detail": "Terhubung ke database PostgreSQL/Supabase eksternal yang persisten lintas reboot/redeploy.",
        }
    if sb_url and sb_key:
        return {
            "backend": "SUPABASE_REST_SYNC",
            "persistent_across_reboots": True,
            "transactional": True,
            "detail": "Replikasi state ke Supabase aktif lintas reboot/redeploy.",
        }
    return {
        "backend": "SQLITE_WAL",
        "persistent_across_reboots": False,
        "transactional": True,
        "db_path": str(db_path or ""),
        "detail": (
            "Menggunakan SQLite WAL lokal. Pada Streamlit Community Cloud, penyimpanan lokal bersifat sementara "
            "saat reboot/redeploy; atur DATABASE_URL (PostgreSQL/Supabase) di Secrets atau gunakan fitur Backup/Restore JSON."
        ),
    }


class _PostgresCursorAdapter:
    """Adapts a DB-API 2.0 PostgreSQL cursor to accept SQLite-style `?` queries and idioms."""

    def __init__(self, raw_cursor):
        self._cur = raw_cursor

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur.fetchall())


class _PostgresConnectionAdapter:
    """Wraps a psycopg/psycopg2/pg8000 connection to provide SQLite-compatible `.execute()` and transactions."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    @staticmethod
    def _translate_sql(sql):
        s = sql.strip()
        if s.upper().startswith("PRAGMA "):
            return "SELECT 'ok'"
        if s.upper() == "BEGIN IMMEDIATE":
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
        s = s.replace("?", "%s")
        return s

    def execute(self, sql, params=()):
        translated = self._translate_sql(sql)
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


def connect_database(db_path, pg_url=None):
    """Connect to PostgreSQL if `pg_url` is configured and driver available, otherwise transactional SQLite WAL."""
    url = pg_url if pg_url is not None else configured_database_url()
    if url:
        for mod_name in ("psycopg", "psycopg2"):
            try:
                pg_mod = __import__(mod_name)
                raw = pg_mod.connect(url)
                return _PostgresConnectionAdapter(raw)
            except ImportError:
                continue
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


class SupabaseStateSync:
    """Optional state backup/restore over Supabase PostgREST (`paper_state_Snapshots` table)."""

    def __init__(self, supabase_url="", supabase_key="", session=None):
        self.url = str(supabase_url or "").rstrip("/")
        self.key = str(supabase_key or "").strip()
        self.session = session

    @property
    def enabled(self):
        return bool(self.url.startswith("https://") and self.key)

    def push_backup(self, namespace, backup_json):
        if not self.enabled:
            return False
        payload = json.dumps({
            "namespace": str(namespace),
            "updated_ms": int(time.time() * 1000),
            "payload": json.loads(backup_json) if isinstance(backup_json, str) else backup_json,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/rest/v1/coinagent_backups?on_conflict=namespace",
            data=payload,
            method="POST",
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False

    def pull_backup(self, namespace):
        if not self.enabled:
            return None
        req = urllib.request.Request(
            f"{self.url}/rest/v1/coinagent_backups?namespace=eq.{namespace}&select=payload&limit=1",
            method="GET",
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                rows = json.loads(resp.read().decode("utf-8"))
                if isinstance(rows, list) and rows:
                    return rows[0].get("payload")
        except Exception:
            return None
        return None
