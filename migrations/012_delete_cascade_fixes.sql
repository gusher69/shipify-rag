-- ============================================================
-- Migration 012: Guarantee ON DELETE CASCADE on every FK involved in
-- deleting a knowledge_files row's related data.
--
-- Context: application code (admin/routes.py, ingestion/embedder.py) now
-- explicitly hard-deletes excel_rows/excel_sheets/excel_workbooks and
-- knowledge_chunks by file_id, and soft-deletes knowledge_items/
-- knowledge_attachments — it does NOT rely on these constraints for
-- correctness anymore. This migration exists as defense-in-depth /
-- documentation of intent: if any of these constraints were missing or
-- configured as SET NULL / NO ACTION on the live database (which is the
-- root cause reported — orphaned excel_sheets/excel_rows/knowledge_items/
-- knowledge_attachments rows survived file deletion), this closes that gap
-- at the schema level too, in case anything ever hard-deletes a
-- knowledge_files row directly (bypassing the app's soft-delete policy).
--
-- Idempotent: safe to run multiple times. For each relationship, finds
-- whatever FK constraint currently exists on that (table, column) pair
-- (regardless of its name) and replaces it with one that has
-- ON DELETE CASCADE, unless it already does. Adds the constraint fresh if
-- none exists yet (this is the case for knowledge_chunks.file_id, which
-- was added via a bare ALTER TABLE ADD COLUMN with no FK at all).
-- ============================================================

DO $$
DECLARE
    rel RECORD;
    existing_conname TEXT;
    existing_rule TEXT;
BEGIN
    FOR rel IN
        SELECT * FROM (VALUES
            ('excel_sheets',           'workbook_id',       'excel_workbooks',   'id'),
            ('excel_rows',             'sheet_id',          'excel_sheets',      'id'),
            ('excel_workbooks',        'file_id',           'knowledge_files',   'id'),
            ('excel_sheets',           'file_id',           'knowledge_files',   'id'),
            ('excel_rows',             'file_id',           'knowledge_files',   'id'),
            ('knowledge_items',        'knowledge_file_id', 'knowledge_files',   'id'),
            ('knowledge_attachments',  'knowledge_file_id', 'knowledge_files',   'id'),
            ('knowledge_attachments',  'knowledge_item_id', 'knowledge_items',   'id'),
            ('knowledge_chunks',       'file_id',           'knowledge_files',   'id')
        ) AS t(child_table, child_column, parent_table, parent_column)
    LOOP
        -- Only proceed if both tables actually exist (some are optional
        -- depending on which earlier migrations were run).
        IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = rel.child_table)
           AND EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = rel.parent_table)
           AND EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = rel.child_table AND column_name = rel.child_column)
        THEN
            SELECT tc.constraint_name, rc.delete_rule
              INTO existing_conname, existing_rule
              FROM information_schema.table_constraints tc
              JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
              JOIN information_schema.referential_constraints rc
                ON tc.constraint_name = rc.constraint_name AND tc.table_schema = rc.constraint_schema
             WHERE tc.constraint_type = 'FOREIGN KEY'
               AND tc.table_name = rel.child_table
               AND kcu.column_name = rel.child_column
             LIMIT 1;

            IF existing_conname IS NOT NULL AND existing_rule = 'CASCADE' THEN
                RAISE NOTICE '% . % -> % already CASCADE, skipping', rel.child_table, rel.child_column, rel.parent_table;
            ELSE
                IF existing_conname IS NOT NULL THEN
                    EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', rel.child_table, existing_conname);
                END IF;
                EXECUTE format(
                    'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES %I(%I) ON DELETE CASCADE',
                    rel.child_table,
                    rel.child_table || '_' || rel.child_column || '_fkey_cascade',
                    rel.child_column, rel.parent_table, rel.parent_column
                );
                RAISE NOTICE 'Set % . % -> % to ON DELETE CASCADE', rel.child_table, rel.child_column, rel.parent_table;
            END IF;
        END IF;
    END LOOP;
END $$;

-- knowledge_sync_job_files.file_id is DELIBERATELY left as ON DELETE SET
-- NULL (set in migration 006), not CASCADE. Sync job history is meant to
-- survive file deletion (the Sync Activity page shows what a job did even
-- after the file it processed is gone) — the application layer explicitly
-- nulls file_id and marks the row 'cancelled' on delete
-- (_handle_sync_job_files_on_delete in admin/routes.py), which already
-- satisfies "no row references a deleted file_id" without destroying the
-- job's audit record. Changing this to CASCADE would delete that history
-- outright the moment a file is deleted, which is a regression, not a fix.

-- ── Verification query ──────────────────────────────────────
-- SELECT tc.table_name, kcu.column_name, rc.delete_rule
-- FROM information_schema.table_constraints tc
-- JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
-- JOIN information_schema.referential_constraints rc ON tc.constraint_name = rc.constraint_name
-- WHERE tc.constraint_type = 'FOREIGN KEY'
--   AND tc.table_name IN ('excel_sheets','excel_rows','excel_workbooks','knowledge_items','knowledge_attachments','knowledge_chunks')
-- ORDER BY tc.table_name;
