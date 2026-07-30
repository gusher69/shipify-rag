-- Migration 032: field_metadata (Product-Agnostic Architecture sprint, Part 3/4/8)
--
-- Additive, nullable JSONB column on business_action_parameters and
-- business_action_response_mapping so a specific field's aliases and
-- display_labels can be admin-configured instead of falling back to
-- services/erp_test_harness.py's generic last-resort heuristics.
-- Shape (all keys optional):
--   {"aliases": ["phrase1", "phrase2", ...], "display_labels": {"th": "...", "en": "..."}}
--
-- NOT applied against the live Supabase DB by this sprint — this repo
-- has no migration runner; migrations are applied by hand (see
-- migrations/031_erp_test_cases.sql for the established precedent).
-- services/erp_test_harness.py already reads this column defensively
-- via `.get("field_metadata") or {}` (see _field_metadata() in that
-- module), so the application works identically whether or not this
-- migration has been applied yet — every existing row simply has no
-- field_metadata configured and falls back to the generic heuristics.
-- Apply manually (e.g. via a short psycopg2 script, as was done for
-- migration 031) only when ready.

ALTER TABLE business_action_parameters
    ADD COLUMN IF NOT EXISTS field_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE business_action_response_mapping
    ADD COLUMN IF NOT EXISTS field_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
