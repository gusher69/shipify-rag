-- Integration Schema Studio — a completely separate, versioned schema
-- store layered ON TOP OF a Business Action (never a column added to
-- the frozen business_actions/business_action_parameters/
-- business_action_response_mapping/... tables). Purely additive, same
-- precedent as migrations/031_erp_test_cases.sql.
--
-- One logical "Integration Schema" per Business Action, with full
-- draft/published/archived versioning (Part 13). The Generic
-- Integration Runtime (services/erp_test_harness.py) reads ONLY the
-- currently published version (via
-- services/integration_schema_service.py::resolve_effective_schema);
-- it never reads a draft. The Integration Schema Studio UI reads/writes
-- drafts and can publish/rollback.
--
-- `schema` JSONB holds the full shape documented in
-- services/integration_schema_service.py::derive_default_schema's
-- docstring (general/input_fields/response_fields/localization/
-- conversation/ai_behaviour/visibility/security/prompt/examples all
-- live inside this one JSONB blob per version row).

CREATE TABLE IF NOT EXISTS integration_action_schemas (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id           UUID NOT NULL REFERENCES business_actions(id) ON DELETE CASCADE,
    version_number      INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'draft',   -- draft|published|archived
    parent_version_id   UUID REFERENCES integration_action_schemas(id),
    schema              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by          TEXT,
    updated_by          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_integration_action_schemas_action ON integration_action_schemas(action_id);
CREATE INDEX IF NOT EXISTS idx_integration_action_schemas_action_status ON integration_action_schemas(action_id, status);

ALTER TABLE integration_action_schemas ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS integration_action_schemas_service_role ON integration_action_schemas;
CREATE POLICY integration_action_schemas_service_role ON integration_action_schemas
    FOR ALL USING (true) WITH CHECK (true);
