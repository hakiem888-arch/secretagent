-- =============================================================================
-- CoinAgentX v6.0.2 — PostgreSQL & Supabase Setup Migration (OPS-01, BUG-17, BUG-22, BUG-23)
-- =============================================================================
-- Run this migration in the Supabase SQL Editor (or via `psql $DATABASE_URL -f supabase/migrations/001_coinagent_schema.sql`)
-- to provision both:
--   1. The `coinagent_backups` table for Supabase PostgREST state snapshot replication (`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`).
--   2. The primary ACID tables when connecting directly via PostgreSQL (`DATABASE_URL` / `SUPABASE_DB_URL`).
-- =============================================================================

-- 1. Supabase REST Snapshot Backup Table (BUG-23 & BUG-28)
CREATE TABLE IF NOT EXISTS public.coinagent_backups (
    namespace TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 2,
    backup_at_ms BIGINT NOT NULL,
    state_updated_ms BIGINT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT coinagent_backups_namespace_unique UNIQUE (namespace)
);

CREATE INDEX IF NOT EXISTS idx_coinagent_backups_state_updated_ms
    ON public.coinagent_backups (state_updated_ms DESC);

ALTER TABLE public.coinagent_backups ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'coinagent_backups'
          AND policyname = 'service_role_full_access_coinagent_backups'
    ) THEN
        CREATE POLICY service_role_full_access_coinagent_backups
            ON public.coinagent_backups
            FOR ALL
            TO service_role
            USING (true)
            WITH CHECK (true);
    END IF;
END
$$;

-- 2. PostgreSQL Health Check Probe Table (BUG-22 & BUG-24)
CREATE TABLE IF NOT EXISTS public.coinagent_db_healthcheck (
    probe_key TEXT PRIMARY KEY,
    probe_ts BIGINT NOT NULL
);

-- 3. Shared Security, RBAC Sessions, & Cross-Process AI Quota Tables (BUG-10, BUG-25, BUG-26)
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

CREATE TABLE IF NOT EXISTS public.security_sessions (
    session_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    auth_fp TEXT NOT NULL,
    token TEXT NOT NULL
);

-- 4. Telegram Deduplication & Analysis History Tables
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
