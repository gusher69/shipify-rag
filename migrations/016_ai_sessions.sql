-- AI Session Management (AI Trace Console) — every AI Playground turn
-- becomes part of a Session, with a full reproducible execution history:
-- messages (conversation), events (pipeline stages), and one raw trace
-- per assistant message (chunks/prompt/policy/services_used/metadata).
--
-- Idempotent — safe to run multiple times.

CREATE TABLE IF NOT EXISTS ai_sessions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT NOT NULL DEFAULT 'New Session',
    status            TEXT NOT NULL DEFAULT 'completed', -- running | completed | failed | cancelled
    model             TEXT,
    embedding_model   TEXT,
    prompt_template_id   TEXT,
    prompt_version    TEXT,
    message_count     INT NOT NULL DEFAULT 0,
    total_input_tokens  INT NOT NULL DEFAULT 0,
    total_output_tokens INT NOT NULL DEFAULT 0,
    total_cost_usd    NUMERIC(12,6) NOT NULL DEFAULT 0,
    avg_latency_ms    NUMERIC(12,2),
    last_confidence   NUMERIC(5,4),
    last_question     TEXT,
    last_answer       TEXT,
    search_text       TEXT, -- denormalized lower(question+answer+filenames+prompt+policy) for cheap ILIKE search
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_message_at   TIMESTAMPTZ,
    deleted_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ai_session_messages (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        UUID NOT NULL REFERENCES ai_sessions(id) ON DELETE CASCADE,
    turn_index        INT NOT NULL,
    role              TEXT NOT NULL, -- user | assistant
    content           TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'completed', -- running | completed | failed | cancelled
    latency_ms        NUMERIC(12,2),
    input_tokens      INT,
    output_tokens     INT,
    estimated_cost_usd NUMERIC(12,6),
    confidence        NUMERIC(5,4),
    confidence_label  TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_session_events (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        UUID NOT NULL REFERENCES ai_sessions(id) ON DELETE CASCADE,
    message_id        UUID NOT NULL REFERENCES ai_session_messages(id) ON DELETE CASCADE,
    stage_index       INT NOT NULL,
    stage_name        TEXT NOT NULL,
    status            TEXT NOT NULL, -- success | failed | skipped | triggered
    duration_ms       NUMERIC(12,2) NOT NULL DEFAULT 0,
    detail            TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_session_traces (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        UUID NOT NULL REFERENCES ai_sessions(id) ON DELETE CASCADE,
    message_id        UUID NOT NULL REFERENCES ai_session_messages(id) ON DELETE CASCADE UNIQUE,
    chunks            JSONB NOT NULL DEFAULT '[]',
    prompt            JSONB NOT NULL DEFAULT '{}',
    policy            JSONB NOT NULL DEFAULT '{}',
    services_used     JSONB NOT NULL DEFAULT '[]',
    metadata          JSONB NOT NULL DEFAULT '{}',
    raw_response      JSONB NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_sessions_created_at ON ai_sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_sessions_deleted_at ON ai_sessions(deleted_at);
CREATE INDEX IF NOT EXISTS idx_ai_session_messages_session_id ON ai_session_messages(session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_ai_session_events_message_id ON ai_session_events(message_id, stage_index);
CREATE INDEX IF NOT EXISTS idx_ai_session_traces_message_id ON ai_session_traces(message_id);

ALTER TABLE ai_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_session_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_session_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_session_traces ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY service_role_all_ai_sessions ON ai_sessions FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE POLICY service_role_all_ai_session_messages ON ai_session_messages FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE POLICY service_role_all_ai_session_events ON ai_session_events FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE POLICY service_role_all_ai_session_traces ON ai_session_traces FOR ALL TO service_role USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
