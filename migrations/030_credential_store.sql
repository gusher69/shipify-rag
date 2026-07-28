-- Secure multi-tenant Credential Store — eliminates the need for one
-- environment variable per customer API secret. Purely additive: existing
-- secret_configuration (environment variable) parameters keep working
-- unchanged (see services/business_action_registry.py's resolution
-- priority: credential_store -> secret_configuration -> error).

CREATE TABLE IF NOT EXISTS integration_credentials (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    integration_id   TEXT,                                   -- optional free-form grouping (e.g. "fasttrade"), nullable
    credential_key   TEXT NOT NULL,                           -- stable reference name Business Action parameters point to
    display_name     TEXT NOT NULL,
    credential_type  TEXT NOT NULL DEFAULT 'secret_code',     -- api_key|bearer_token|basic_auth|secret_code|username_password|client_credentials|custom_header|custom_body_secret
    encrypted_value  TEXT NOT NULL,                           -- Fernet ciphertext (base64) — never plaintext, never returned by list/get APIs
    masked_preview   TEXT NOT NULL DEFAULT '',                -- e.g. "********8151" — safe to display anywhere
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status           TEXT NOT NULL DEFAULT 'enabled',         -- enabled|disabled|revoked
    created_by       TEXT,
    updated_by       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_at       TIMESTAMPTZ,
    last_used_at     TIMESTAMPTZ,
    UNIQUE (tenant_id, credential_key)
);

CREATE INDEX IF NOT EXISTS idx_integration_credentials_tenant ON integration_credentials(tenant_id);

ALTER TABLE integration_credentials ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS integration_credentials_service_role ON integration_credentials;
CREATE POLICY integration_credentials_service_role ON integration_credentials
    FOR ALL USING (true) WITH CHECK (true);

-- Business Action Parameter: new input_source "credential_store" +
-- a credential_ref column distinct from the existing secret_ref (which
-- stays exactly as-is for the legacy environment-variable mode).
ALTER TABLE business_action_parameters
    ADD COLUMN IF NOT EXISTS credential_ref TEXT;

-- Safe audit trail — never stores raw/encrypted credential values, only
-- event metadata. Purely additive, new table.
CREATE TABLE IF NOT EXISTS credential_audit_log (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      TEXT NOT NULL,
    credential_key TEXT NOT NULL,
    event          TEXT NOT NULL,     -- credential_created|credential_rotated|credential_disabled|credential_revoked|credential_used|credential_tested
    actor          TEXT,
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE credential_audit_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS credential_audit_log_service_role ON credential_audit_log;
CREATE POLICY credential_audit_log_service_role ON credential_audit_log
    FOR ALL USING (true) WITH CHECK (true);
