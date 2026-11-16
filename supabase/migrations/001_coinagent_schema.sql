-- =============================================================================
-- CoinAgentX v6.0.2 — PostgreSQL & Supabase Setup Migration
-- (OPS-01, SUPABASE-01, SUPABASE-03, SEC-03)
-- =============================================================================
-- Run this migration in the Supabase SQL Editor (Role: postgres / Run without RLS)
-- or via `psql "$DATABASE_URL" -f supabase/migrations/001_coinagent_schema.sql`.
--
-- Security Architecture (SUPABASE-01 & SEC-03):
-- 1. Grants explicit schema USAGE and table DML privileges ONLY to `service_role`.
-- 2. Explicitly REVOKES all privileges on all internal tables and sequences from
--    `PUBLIC`, `anon`, and `authenticated` so internal trading journals, session
--    token hashes, quota counters, and audit logs are never exposed via public Data API.
-- 3. Enables and FORCES Row Level Security (RLS) across all public tables with
--    strict `service_role`-only policies.
-- 4. Stores only SHA-256 session token hashes (`token_hash`), never raw bearer tokens.
-- =============================================================================

-- 0. Explicit Schema Usage & Default Privilege Hardening (SUPABASE-01 & SEC-03)
GRANT USAGE ON SCHEMA public TO service_role;
REVOKE CREATE ON SCHEMA public FROM PUBLIC, anon, authenticated;

-- 1. Supabase REST Snapshot Backup Table (SUPABASE-01, SUPABASE-02, SUPABASE-03)
CREATE TABLE IF NOT EXISTS public.coinagent_backups (
    namespace TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 2,
    backup_at_ms BIGINT NOT NULL,
    state_updated_ms BIGINT NOT NULL,
    state_seq BIGINT NOT NULL DEFAULT 1,
    writer_id TEXT NOT NULL DEFAULT '',
    checksum_sha256 TEXT NOT NULL,
    hmac_sha256 TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT coinagent_backups_namespace_unique UNIQUE (namespace)
);

CREATE INDEX IF NOT EXISTS idx_coinagent_backups_state_updated_ms
    ON public.coinagent_backups (state_updated_ms DESC);

-- 2. PostgreSQL Health Check Probe Table (BUG-22 & BUG-24)
CREATE TABLE IF NOT EXISTS public.coinagent_db_healthcheck (
    probe_key TEXT PRIMARY KEY,
    probe_ts BIGINT NOT NULL
);

-- 3. Shared Security, RBAC Session Token Hashes, & Cross-Process AI Quota Tables (SEC-03, BUG-25, BUG-26)
CREATE TABLE IF NOT EXISTS public.security_audit_log (
    id BIGSERIAL PRIMARY KEY,
    at_ms BIGINT NOT NULL,
    role TEXT NOT NULL,
    session_id TEXT,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS public.security_quota_events (
    id BIGSERIAL PRIMARY KEY,
    bucket TEXT NOT NULL,
    session_id TEXT NOT NULL,
    event_ts DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_security_quota_bucket_ts
    ON public.security_quota_events (bucket, event_ts);

CREATE TABLE IF NOT EXISTS public.security_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- SEC-03: Store `token_hash` (SHA-256 digest), NEVER raw session bearer tokens!
CREATE TABLE IF NOT EXISTS public.security_sessions (
    session_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    auth_fp TEXT NOT NULL,
    token_hash TEXT NOT NULL
);

-- 4. Primary ACID Trading Journal, Scanner Locks, Telegram Dedup & Analysis History Tables
CREATE TABLE IF NOT EXISTS public.paper_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS public.paper_trades (
    signal TEXT PRIMARY KEY,
    symbol TEXT,
    mode TEXT,
    status TEXT,
    realized DOUBLE PRECISION,
    updated_ms BIGINT,
    payload TEXT
);

CREATE INDEX IF NOT EXISTS paper_status ON public.paper_trades (status);

CREATE TABLE IF NOT EXISTS public.paper_events (
    id BIGSERIAL PRIMARY KEY,
    signal TEXT,
    at_ms BIGINT,
    event TEXT,
    qty DOUBLE PRECISION,
    price DOUBLE PRECISION,
    pnl DOUBLE PRECISION,
    note TEXT
);

CREATE TABLE IF NOT EXISTS public.scanner_locks (
    lock_name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    acquired_ms BIGINT NOT NULL,
    expires_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS public.telegram_alerts (
    target TEXT NOT NULL,
    signal TEXT NOT NULL,
    state TEXT,
    retry_at DOUBLE PRECISION,
    updated_at DOUBLE PRECISION,
    PRIMARY KEY (target, signal)
);

CREATE TABLE IF NOT EXISTS public.analyses (
    id TEXT PRIMARY KEY,
    at_ms BIGINT,
    symbol TEXT,
    mode TEXT,
    status TEXT,
    payload TEXT
);

-- =============================================================================
-- 5. SUPABASE-01 & SEC-03: REVOKE ANON/AUTHENTICATED & GRANT SERVICE_ROLE ONLY
-- =============================================================================

REVOKE ALL ON TABLE
    public.coinagent_backups,
    public.coinagent_db_healthcheck,
    public.security_audit_log,
    public.security_quota_events,
    public.security_meta,
    public.security_sessions,
    public.paper_meta,
    public.paper_trades,
    public.paper_events,
    public.scanner_locks,
    public.telegram_alerts,
    public.analyses
FROM PUBLIC, anon, authenticated;

REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, anon, authenticated;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    public.coinagent_backups,
    public.coinagent_db_healthcheck,
    public.security_audit_log,
    public.security_quota_events,
    public.security_meta,
    public.security_sessions,
    public.paper_meta,
    public.paper_trades,
    public.paper_events,
    public.scanner_locks,
    public.telegram_alerts,
    public.analyses
TO service_role;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;

-- =============================================================================
-- 6. SEC-03: ENABLE & FORCE ROW LEVEL SECURITY ON ALL INTERNAL TABLES
-- =============================================================================

DO $$
DECLARE
    tbl TEXT;
    pol_name TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'coinagent_backups',
        'coinagent_db_healthcheck',
        'security_audit_log',
        'security_quota_events',
        'security_meta',
        'security_sessions',
        'paper_meta',
        'paper_trades',
        'paper_events',
        'scanner_locks',
        'telegram_alerts',
        'analyses'
    ]
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', tbl);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY;', tbl);
        pol_name := 'service_role_only_' || tbl;
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename = tbl
              AND policyname = pol_name
        ) THEN
            EXECUTE format(
                'CREATE POLICY %I ON public.%I FOR ALL TO service_role USING (true) WITH CHECK (true);',
                pol_name, tbl
            );
        END IF;
    END LOOP;
END
$$;
