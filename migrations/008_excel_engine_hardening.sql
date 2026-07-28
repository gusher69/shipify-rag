-- ============================================================
-- Migration 008: Excel Calculation Engine hardening
-- Shipify AI Agent — รันใน Supabase Dashboard > SQL Editor
--
-- Adds hidden-sheet tracking so the calculation engine (and admin UI) can
-- see sheets that are hidden in the original workbook, which are still
-- extracted and queryable but should be flagged as such.
-- ============================================================

ALTER TABLE excel_sheets
  ADD COLUMN IF NOT EXISTS is_hidden BOOLEAN DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_excel_sheets_is_hidden ON excel_sheets (is_hidden);
