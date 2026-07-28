-- ============================================================
-- Migration 027: Business Action Center — Parameter Groups + Secret
-- References + Parameter Visibility. Purely additive (ADD COLUMN IF
-- NOT EXISTS) on the EXISTING business_actions/business_action_
-- parameters/business_action_execution tables from migration 026 — no
-- new tables, no renamed/removed columns, safe on a DB that already
-- has registered Business Actions.
--
-- parameter_groups (on business_actions) shape:
--   [{"name": "customer_search_identifier", "rule": "AT_LEAST_ONE",
--     "members": ["CustCode","CustEmail","CustName","CustPhone"]}, ...]
-- rule ∈ ALL | AT_LEAST_ONE | EXACTLY_ONE | OPTIONAL — reusable by ANY
-- future action (login API email-OR-phone, order API order-OR-tracking
-- number, claim API claim-OR-policy-number, ...), never specific to
-- customer lookup.
--
-- Idempotent — safe to run multiple times.
-- ============================================================

ALTER TABLE business_actions
    ADD COLUMN IF NOT EXISTS parameter_groups JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE business_action_parameters
    ADD COLUMN IF NOT EXISTS input_source                TEXT NOT NULL DEFAULT 'customer_message',
    -- customer_message | customer_profile | conversation_context |
    -- fixed_configuration | secret_configuration | system_generated
    ADD COLUMN IF NOT EXISTS secret_ref                   TEXT,   -- env var NAME only — never a value
    ADD COLUMN IF NOT EXISTS send_as                      TEXT NOT NULL DEFAULT 'form',  -- form | json | query
    ADD COLUMN IF NOT EXISTS visible_to_customer          BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS visible_in_developer_mode    BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS loggable                     BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE business_action_execution
    ADD COLUMN IF NOT EXISTS base_url        TEXT,
    ADD COLUMN IF NOT EXISTS endpoint_path   TEXT,
    ADD COLUMN IF NOT EXISTS content_type    TEXT NOT NULL DEFAULT 'application/json';
    -- application/json | application/x-www-form-urlencoded
    -- `endpoint` (migration 026) still holds the FULL resolved URL for
    -- backward compatibility with existing execution code — base_url/
    -- endpoint_path are additive fields the Simple Mode UI edits
    -- separately, recombined into `endpoint` on save.
