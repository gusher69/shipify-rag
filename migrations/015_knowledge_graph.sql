-- ============================================================
-- Migration 015: Knowledge Graph (metadata layer for future Graph RAG)
-- Shipify AI Agent — รันใน Supabase Dashboard > SQL Editor
--
-- Stores entities/concepts (nodes) and their relationships (edges)
-- extracted by services.knowledge_graph_service.KnowledgeGraphService.
-- This does NOT replace vector search — it's an additive metadata layer
-- the AI Playground reads for "Graph Context" explanations, and that a
-- future Graph RAG search (vector + keyword + graph traversal) would
-- read from. The original document/chunk always remains the source of
-- truth; every edge stores evidence_text quoted from the source so a
-- relationship can always be traced back to where it came from.
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_graph_nodes (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_file_id  UUID REFERENCES knowledge_files(id) ON DELETE CASCADE,
    knowledge_item_id  UUID REFERENCES knowledge_items(id) ON DELETE SET NULL,
    chunk_id           UUID,
    node_type          TEXT NOT NULL CHECK (node_type IN (
        'company','service','product','feature','process','step','policy',
        'requirement','department','platform','location','document',
        'topic','faq','system','attachment','other'
    )),
    name               TEXT NOT NULL,
    normalized_name    TEXT NOT NULL,
    description        TEXT,
    metadata           JSONB DEFAULT '{}'::jsonb,
    confidence         NUMERIC DEFAULT 0.5,
    created_at         TIMESTAMPTZ DEFAULT now(),
    deleted_at         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS knowledge_graph_edges (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_file_id  UUID REFERENCES knowledge_files(id) ON DELETE CASCADE,
    source_node_id     UUID NOT NULL REFERENCES knowledge_graph_nodes(id) ON DELETE CASCADE,
    target_node_id     UUID NOT NULL REFERENCES knowledge_graph_nodes(id) ON DELETE CASCADE,
    relation_type      TEXT NOT NULL CHECK (relation_type IN (
        'provides','supports','contains','requires','belongs_to',
        'handled_by','located_at','part_of','next_step','related_to',
        'answers','references','attached_to','depends_on','applies_to'
    )),
    relation_label     TEXT,
    evidence_text      TEXT NOT NULL,
    source_chunk_id    UUID,
    confidence         NUMERIC DEFAULT 0.5,
    metadata           JSONB DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ DEFAULT now(),
    deleted_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_kgn_file_id        ON knowledge_graph_nodes (knowledge_file_id);
CREATE INDEX IF NOT EXISTS idx_kgn_item_id        ON knowledge_graph_nodes (knowledge_item_id);
CREATE INDEX IF NOT EXISTS idx_kgn_chunk_id        ON knowledge_graph_nodes (chunk_id);
CREATE INDEX IF NOT EXISTS idx_kgn_normalized_name ON knowledge_graph_nodes (normalized_name);
CREATE INDEX IF NOT EXISTS idx_kgn_deleted_at      ON knowledge_graph_nodes (deleted_at);

CREATE INDEX IF NOT EXISTS idx_kge_file_id      ON knowledge_graph_edges (knowledge_file_id);
CREATE INDEX IF NOT EXISTS idx_kge_source_node  ON knowledge_graph_edges (source_node_id);
CREATE INDEX IF NOT EXISTS idx_kge_target_node  ON knowledge_graph_edges (target_node_id);
CREATE INDEX IF NOT EXISTS idx_kge_source_chunk ON knowledge_graph_edges (source_chunk_id);
CREATE INDEX IF NOT EXISTS idx_kge_deleted_at    ON knowledge_graph_edges (deleted_at);

ALTER TABLE knowledge_graph_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_graph_edges ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all_kgn" ON knowledge_graph_nodes;
DROP POLICY IF EXISTS "service_role_all_kge" ON knowledge_graph_edges;

CREATE POLICY "service_role_all_kgn" ON knowledge_graph_nodes
    FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "service_role_all_kge" ON knowledge_graph_edges
    FOR ALL USING (auth.role() = 'service_role');
