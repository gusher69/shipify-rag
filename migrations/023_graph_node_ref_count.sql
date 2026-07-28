-- Migration 023: reference counting for knowledge_graph_nodes.
--
-- Today every node is created by exactly one file's graph extraction
-- (services/knowledge_graph_service.py's save_graph() always inserts a
-- fresh row per file — no cross-file dedup/sharing exists yet), so
-- ref_count starts at 1 and deleting that one owning file always brings
-- it to 0. This column is what lets a FUTURE feature that shares/reuses
-- a node across multiple files increment ref_count on each additional
-- reference, so the delete pipeline (admin/routes.py's file-delete path
-- via KnowledgeGraphService.delete_graph_for_file) only actually removes
-- a node once its ref_count reaches 0 — never on the first file's delete
-- alone, and never leaving it referenced-but-orphaned either.

ALTER TABLE knowledge_graph_nodes
    ADD COLUMN IF NOT EXISTS ref_count INT NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_kgn_ref_count ON knowledge_graph_nodes (ref_count);
