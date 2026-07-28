-- ============================================================
-- Migration 003: Excel structured storage for analytics
-- Run in Supabase SQL Editor (service_role required)
-- ============================================================

-- ── excel_workbooks ──────────────────────────────────────────
-- One record per uploaded Excel file (links to knowledge_files)
CREATE TABLE IF NOT EXISTS excel_workbooks (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  file_id      UUID REFERENCES knowledge_files(id) ON DELETE CASCADE,
  filename     TEXT NOT NULL,
  sheet_count  INT  DEFAULT 0,
  sheet_names  JSONB DEFAULT '[]',
  created_at   TIMESTAMPTZ DEFAULT now(),
  updated_at   TIMESTAMPTZ DEFAULT now()
);

-- ── excel_sheets ─────────────────────────────────────────────
-- One record per sheet inside a workbook
CREATE TABLE IF NOT EXISTS excel_sheets (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workbook_id      UUID REFERENCES excel_workbooks(id) ON DELETE CASCADE,
  file_id          UUID REFERENCES knowledge_files(id) ON DELETE CASCADE,
  sheet_name       TEXT NOT NULL,
  sheet_index      INT  DEFAULT 0,
  row_count        INT  DEFAULT 0,
  column_count     INT  DEFAULT 0,
  headers          JSONB DEFAULT '[]',    -- ordered list of column names
  numeric_columns  JSONB DEFAULT '[]',    -- column names that contain numbers
  date_columns     JSONB DEFAULT '[]',    -- column names that contain dates
  has_formula      BOOLEAN DEFAULT false,
  formula_cells    JSONB DEFAULT '{}',    -- {cell_ref: formula_string}
  cell_range       TEXT,                  -- e.g. "A1:Z100"
  markdown_preview TEXT,                  -- Markdown table for vector search
  created_at       TIMESTAMPTZ DEFAULT now(),
  updated_at       TIMESTAMPTZ DEFAULT now()
);

-- ── excel_rows ────────────────────────────────────────────────
-- One record per data row; row_data is a {column_name: value} JSONB object.
-- This enables SQL aggregations like:
--   SELECT row_data->>'Month', SUM((row_data->>'Sales')::numeric)
--   FROM excel_rows WHERE sheet_id = '...' GROUP BY row_data->>'Month'
CREATE TABLE IF NOT EXISTS excel_rows (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sheet_id    UUID REFERENCES excel_sheets(id) ON DELETE CASCADE,
  file_id     UUID REFERENCES knowledge_files(id) ON DELETE CASCADE,
  row_index   INT  NOT NULL,   -- 1-based (header row = 0)
  row_data    JSONB DEFAULT '{}',
  created_at  TIMESTAMPTZ DEFAULT now()
);

-- ── Indexes ──────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_excel_workbooks_file_id ON excel_workbooks(file_id);
CREATE INDEX IF NOT EXISTS idx_excel_sheets_workbook_id ON excel_sheets(workbook_id);
CREATE INDEX IF NOT EXISTS idx_excel_sheets_file_id ON excel_sheets(file_id);
CREATE INDEX IF NOT EXISTS idx_excel_rows_sheet_id ON excel_rows(sheet_id);
CREATE INDEX IF NOT EXISTS idx_excel_rows_file_id ON excel_rows(file_id);
CREATE INDEX IF NOT EXISTS idx_excel_rows_data_gin ON excel_rows USING gin(row_data);

-- ── Auto-update triggers ─────────────────────────────────────
-- (reuse the function created in migration 001 if it exists)
CREATE OR REPLACE FUNCTION _update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

DROP TRIGGER IF EXISTS trg_excel_workbooks_updated_at ON excel_workbooks;
CREATE TRIGGER trg_excel_workbooks_updated_at
  BEFORE UPDATE ON excel_workbooks
  FOR EACH ROW EXECUTE FUNCTION _update_updated_at();

DROP TRIGGER IF EXISTS trg_excel_sheets_updated_at ON excel_sheets;
CREATE TRIGGER trg_excel_sheets_updated_at
  BEFORE UPDATE ON excel_sheets
  FOR EACH ROW EXECUTE FUNCTION _update_updated_at();

-- ── Row Level Security ────────────────────────────────────────
ALTER TABLE excel_workbooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE excel_sheets     ENABLE ROW LEVEL SECURITY;
ALTER TABLE excel_rows       ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all" ON excel_workbooks;
DROP POLICY IF EXISTS "service_role_all" ON excel_sheets;
DROP POLICY IF EXISTS "service_role_all" ON excel_rows;

CREATE POLICY "service_role_all" ON excel_workbooks FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON excel_sheets     FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON excel_rows       FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ── Verification query ────────────────────────────────────────
-- Run after migration to confirm:
-- SELECT table_name FROM information_schema.tables
-- WHERE table_schema = 'public' AND table_name LIKE 'excel_%';
