-- Migration 004: knowledge_attachments
-- Stores image/file attachments linked to Excel Q&A rows

CREATE TABLE IF NOT EXISTS knowledge_attachments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_file_id   UUID NOT NULL REFERENCES knowledge_files(id) ON DELETE CASCADE,
    chunk_id            UUID,                         -- nullable; links to knowledge_chunks
    row_index           INT,                          -- 1-based row index in source sheet
    sheet_name          TEXT,                         -- source worksheet name
    filename            TEXT NOT NULL,                -- local filename (after download) or original
    original_url        TEXT,                         -- original URL if downloaded from web
    storage_provider    TEXT DEFAULT 'local',         -- 'gdrive' | 'local' | 'supabase'
    storage_file_id     TEXT,                         -- Drive file_id or equivalent
    storage_path        TEXT,                         -- relative local path or Drive path
    public_url          TEXT,                         -- publicly accessible URL for LINE OA
    mime_type           TEXT,
    file_size           BIGINT,
    attachment_type     TEXT DEFAULT 'image',         -- 'image' | 'document' | 'other'
    description         TEXT,
    status              TEXT DEFAULT 'linked'         -- 'linked' | 'missing' | 'failed' | 'downloaded'
                        CHECK (status IN ('linked','missing','failed','downloaded')),
    error_message       TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_katt_file_id   ON knowledge_attachments (knowledge_file_id);
CREATE INDEX IF NOT EXISTS idx_katt_chunk_id  ON knowledge_attachments (chunk_id);
CREATE INDEX IF NOT EXISTS idx_katt_status    ON knowledge_attachments (status);
CREATE INDEX IF NOT EXISTS idx_katt_filename  ON knowledge_attachments (filename);
CREATE INDEX IF NOT EXISTS idx_katt_sheet_row ON knowledge_attachments (knowledge_file_id, sheet_name, row_index);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_knowledge_attachments_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END; $$;

DROP TRIGGER IF EXISTS trg_katt_updated_at ON knowledge_attachments;
CREATE TRIGGER trg_katt_updated_at
    BEFORE UPDATE ON knowledge_attachments
    FOR EACH ROW EXECUTE FUNCTION update_knowledge_attachments_updated_at();
