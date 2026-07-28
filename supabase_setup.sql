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
    segment             TEXT DEFAULT 'cold',   -- cold | warm | hot
    order_count         INT  DEFAULT 0,
    total_spend         DECIMAL(12,2) DEFAULT 0,
    preferred_price_range TEXT,
    chat_style          TEXT DEFAULT 'short',  -- short | detailed
    last_active         TIMESTAMPTZ,
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
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
