-- ============================================================
-- Migration 002: Persistent Sync Job Tables
-- Shipify AI Agent — รันใน Supabase Dashboard > SQL Editor
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_sync_jobs (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    status            TEXT        NOT NULL DEFAULT 'pending',
    total_files       INT         DEFAULT 0,
    completed_files   INT         DEFAULT 0,
    failed_files      INT         DEFAULT 0,
    progress_percent  INT         DEFAULT 0,
    current_file_name TEXT,
    current_step      TEXT,
    error_message     TEXT,
    created_by        TEXT        DEFAULT 'admin',
    metadata          JSONB       DEFAULT '{}'::jsonb,
    started_at        TIMESTAMPTZ DEFAULT now(),
    completed_at      TIMESTAMPTZ,
    cancelled_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_sync_job_files (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id           UUID        NOT NULL REFERENCES knowledge_sync_jobs(id) ON DELETE CASCADE,
    file_id          UUID,
    file_name        TEXT,
    status           TEXT        DEFAULT 'pending',
    progress_percent INT         DEFAULT 0,
    current_step     TEXT,
    chunk_count      INT         DEFAULT 0,
    error_message    TEXT,
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ksj_status    ON knowledge_sync_jobs (status);
CREATE INDEX IF NOT EXISTS idx_ksj_started   ON knowledge_sync_jobs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ksjf_job_id   ON knowledge_sync_job_files (job_id);
CREATE INDEX IF NOT EXISTS idx_ksjf_status   ON knowledge_sync_job_files (status);

-- Reuse set_updated_at from migration 001
DROP TRIGGER IF EXISTS trg_ksj_updated  ON knowledge_sync_jobs;
CREATE TRIGGER trg_ksj_updated
    BEFORE UPDATE ON knowledge_sync_jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_ksjf_updated ON knowledge_sync_job_files;
CREATE TRIGGER trg_ksjf_updated
    BEFORE UPDATE ON knowledge_sync_job_files
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE knowledge_sync_jobs      ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_sync_job_files ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all_ksj"  ON knowledge_sync_jobs;
CREATE POLICY "service_role_all_ksj"  ON knowledge_sync_jobs
    FOR ALL USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "service_role_all_ksjf" ON knowledge_sync_job_files;
CREATE POLICY "service_role_all_ksjf" ON knowledge_sync_job_files
    FOR ALL USING (auth.role() = 'service_role');
