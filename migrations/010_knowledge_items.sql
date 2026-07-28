-- ============================================================
-- Migration 010: knowledge_items + attachment linking by item
-- Shipify AI Agent — รันใน Supabase Dashboard > SQL Editor
--
-- Each Q&A-style Excel row becomes one knowledge_items row (question,
-- answer, and the attachments that belong to THAT row only — never
-- matched by filename, always by this row's own UUID). Data-table Excel
-- sheets (financial/numeric) do NOT get knowledge_items rows — those stay
-- on the summary-embedding + structured-calculation path introduced for
-- the Excel Calculation Engine; this table is additive, not a replacement.
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_items (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_file_id UUID        REFERENCES knowledge_files(id) ON DELETE CASCADE,
    chunk_id          UUID,       -- the embedding chunk that represents this item, if one exists
    title             TEXT,
    question          TEXT,
    answer            TEXT,
    scope             TEXT,
    channel           TEXT,
    sheet_name        TEXT,
    row_index         INT,
    metadata          JSONB       DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ DEFAULT now(),
    deleted_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_ki_file_id    ON knowledge_items (knowledge_file_id);
CREATE INDEX IF NOT EXISTS idx_ki_chunk_id   ON knowledge_items (chunk_id);
CREATE INDEX IF NOT EXISTS idx_ki_deleted_at ON knowledge_items (deleted_at);
CREATE INDEX IF NOT EXISTS idx_ki_sheet_row  ON knowledge_items (knowledge_file_id, sheet_name, row_index);

ALTER TABLE knowledge_attachments
  ADD COLUMN IF NOT EXISTS knowledge_item_id UUID,
  ADD COLUMN IF NOT EXISTS metadata          JSONB DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_katt_knowledge_item_id ON knowledge_attachments (knowledge_item_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_katt_knowledge_item_id'
    ) THEN
        ALTER TABLE knowledge_attachments
            ADD CONSTRAINT fk_katt_knowledge_item_id FOREIGN KEY (knowledge_item_id)
            REFERENCES knowledge_items(id) ON DELETE SET NULL;
    END IF;
END $$;

ALTER TABLE knowledge_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_ki" ON knowledge_items;
CREATE POLICY "service_role_all_ki" ON knowledge_items
    FOR ALL USING (auth.role() = 'service_role');
