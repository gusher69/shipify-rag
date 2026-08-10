-- ============================================================
-- Shipify AI Agent — Supabase Setup
-- รันใน Supabase Dashboard > SQL Editor
-- ============================================================

-- 1. เปิด pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. ตาราง knowledge_chunks (แทน Qdrant)
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content    TEXT    NOT NULL,
    source     TEXT,
    intent     TEXT,                          -- สต็อก | ออเดอร์ | นโยบาย | ทั่วไป
    embedding  VECTOR(1536),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- index สำหรับ vector search (cosine)
CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_idx
    ON knowledge_chunks
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- 3. ตาราง user_profiles
CREATE TABLE IF NOT EXISTS user_profiles (
    line_user_id        TEXT PRIMARY KEY,
    display_name        TEXT,
    segment             TEXT DEFAULT 'cold',   -- cold | warm | hot (purchase-based, order_count/total_spend)
    order_count         INT  DEFAULT 0,
    total_spend         DECIMAL(12,2) DEFAULT 0,
    preferred_price_range TEXT,
    chat_style          TEXT DEFAULT 'short',  -- short | detailed
    last_active         TIMESTAMPTZ,
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT now(),
    -- Phase 3 (Conversation Intelligence, 2026-08-05) — see
    -- migrations/035_phase3_conversation_intelligence.sql for the
    -- idempotent ALTER TABLE version of these same columns applied to an
    -- already-deployed database.
    first_seen           TIMESTAMPTZ NOT NULL DEFAULT now(),
    message_count        INT NOT NULL DEFAULT 0,
    conversation_count   INT NOT NULL DEFAULT 0,
    avg_confidence        NUMERIC(5,4),
    avg_response_time_ms  NUMERIC(12,2),
    interested_topics     JSONB NOT NULL DEFAULT '[]',
    products_mentioned    JSONB NOT NULL DEFAULT '[]',
    erp_requests_count    INT NOT NULL DEFAULT 0,
    complaint_count       INT NOT NULL DEFAULT 0,
    positive_count        INT NOT NULL DEFAULT 0,
    negative_count        INT NOT NULL DEFAULT 0,
    escalation_count      INT NOT NULL DEFAULT 0,
    tags                  JSONB NOT NULL DEFAULT '[]',
    -- `conversation_tier` is a SEPARATE axis from `segment` above —
    -- conversation-behavior-based (sentiment/frequency/ERP usage), not
    -- purchase-based. Drives Prompt Studio's Customer Tier Prompt.
    conversation_tier     TEXT NOT NULL DEFAULT 'cold'
                          CHECK (conversation_tier IN ('cold', 'warm', 'hot', 'negative')),
    tier_score            NUMERIC(6,2) NOT NULL DEFAULT 0
);

-- 4. Function สำหรับ vector search (ใช้ใน rag/searcher.py)
CREATE OR REPLACE FUNCTION match_knowledge_chunks(
    query_embedding VECTOR(1536),
    match_count     INT DEFAULT 3
)
RETURNS TABLE (
    id         UUID,
    content    TEXT,
    source     TEXT,
    intent     TEXT,
    similarity FLOAT
)
LANGUAGE sql STABLE
AS $$
    SELECT
        id,
        content,
        source,
        intent,
        1 - (embedding <=> query_embedding) AS similarity
    FROM knowledge_chunks
    ORDER BY embedding <=> query_embedding
    LIMIT match_count;
$$;

-- 5. Row Level Security (เปิด แต่ allow service_role ผ่านทั้งหมด)
ALTER TABLE knowledge_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_profiles    ENABLE ROW LEVEL SECURITY;

-- service_role (ใช้ใน server) เข้าถึงได้ทั้งหมด
CREATE POLICY "service_role_all_knowledge" ON knowledge_chunks
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "service_role_all_profiles" ON user_profiles
    FOR ALL USING (auth.role() = 'service_role');
