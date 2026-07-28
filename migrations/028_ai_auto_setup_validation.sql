-- AI-Assisted API Auto Setup — adds the missing per-parameter fields the
-- Decision Engine's Contextual Slot Binding needs to distinguish multiple
-- same-shaped required parameters (e.g. C00001 vs PO202601001) instead of
-- relying only on parameter order. Purely additive, matches migration 027's
-- own convention (ADD COLUMN IF NOT EXISTS on the existing table).
ALTER TABLE business_action_parameters
    ADD COLUMN IF NOT EXISTS validation_pattern TEXT,
    ADD COLUMN IF NOT EXISTS min_length         INTEGER,
    ADD COLUMN IF NOT EXISTS max_length         INTEGER,
    ADD COLUMN IF NOT EXISTS follow_up_options  JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS validation_confidence TEXT NOT NULL DEFAULT 'high';

-- Tracks whether an action came from AI Auto Setup and its draft/enabled
-- lifecycle state — additive only, no existing column touched.
ALTER TABLE business_actions
    ADD COLUMN IF NOT EXISTS setup_source TEXT NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS is_draft     BOOLEAN NOT NULL DEFAULT false;
