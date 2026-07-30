-- Business Action Audit Log (2026-07-29 hard-delete production-readiness
-- audit, Part 4) — an immutable record of lifecycle events (starting
-- with business_action.deleted), mirroring the EXACT existing
-- credential_audit_log precedent (migrations/030_credential_store.sql,
-- services/credential_store.py::_audit()) rather than inventing a new
-- audit pattern. Purely additive, new table — no FK to business_actions
-- (deliberately: the whole point is that this row must survive after
-- the action itself is permanently gone).
--
-- Never stores secrets/credentials/auth headers/sensitive payloads —
-- `detail` is a small, explicitly-built JSONB blob (see
-- services/business_action_registry.py::hard_delete_action()), never a
-- raw request/response dump.

CREATE TABLE IF NOT EXISTS business_action_audit_log (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id        UUID NOT NULL,
    action_key       TEXT,
    event            TEXT NOT NULL,     -- business_action.deleted (future: .created/.published/.updated)
    actor            TEXT,
    detail           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_business_action_audit_log_action_id ON business_action_audit_log(action_id);
CREATE INDEX IF NOT EXISTS idx_business_action_audit_log_event ON business_action_audit_log(event);

ALTER TABLE business_action_audit_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS business_action_audit_log_service_role ON business_action_audit_log;
CREATE POLICY business_action_audit_log_service_role ON business_action_audit_log
    FOR ALL USING (true) WITH CHECK (true);
