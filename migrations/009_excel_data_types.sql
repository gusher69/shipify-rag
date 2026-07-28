-- ============================================================
-- Migration 009: Excel data-type preservation (currency/percentage/boolean)
-- Shipify AI Agent — รันใน Supabase Dashboard > SQL Editor
--
-- Numbers were already preserved as real numbers (never strings), but the
-- fact that a column was formatted as currency ("$#,##0.00") or percentage
-- ("0.00%") in the original workbook was discarded — the calculation
-- engine and any answer text couldn't tell "15" apart from "15%" or "$15".
-- These columns record that classification per sheet so it can be shown
-- in answers ("Preserve currency/unit if available").
-- ============================================================

ALTER TABLE excel_sheets
  ADD COLUMN IF NOT EXISTS currency_columns   JSONB DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS percentage_columns JSONB DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS boolean_columns    JSONB DEFAULT '[]';
