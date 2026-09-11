import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the directory this file lives in (works regardless of cwd)
load_dotenv(Path(__file__).parent / ".env")

# OpenAI
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")
OPENAI_CHAT_MODEL    = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o")
# NOTE: there is no top-level OPENAI_EMBED_MODEL constant here on purpose.
# The legacy OPENAI_EMBED_MODEL env var is still honored, but ONLY as a
# fallback inside OPENAI_EMBEDDING_MODEL's own precedence expression below
# — see the Embedding section. A prior version of this file defined a
# second, disconnected OPENAI_EMBED_MODEL constant here that defaulted to
# text-embedding-3-small; nothing imported it, but its mere existence was
# a landmine (2026-08-01 .env.example audit) — removed rather than left
# as a trap for a future accidental import.

# Supabase
SUPABASE_URL         = os.getenv("SUPABASE_URL")
SUPABASE_KEY         = os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_DB_URL      = os.getenv("SUPABASE_DB_URL")

# LINE OA
LINE_CHANNEL_SECRET  = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_TOKEN   = os.getenv("LINE_CHANNEL_TOKEN")
LINE_NOTIFY_TOKEN    = os.getenv("LINE_NOTIFY_TOKEN")
# P2.1A — SHADOW-OBSERVABILITY SAMPLE SEPARATION ONLY (not auth, not a
# feature flag, not read by routing). Comma-separated LINE user ids
# (the webhook's own HMAC-verified event.source.user_id) that
# line_bot/webhook.py classifies as OWNER_TEST traffic so it never
# counts toward the >= 200 REAL_LINE shadow-parity cutover sample
# (services/conversation_intelligence_telemetry.py). The id itself is
# never persisted — only the resulting "OWNER_TEST" label is. Absent or
# empty (the default) -> every LINE sender is REAL_LINE, unchanged.
OWNER_TEST_LINE_USER_IDS = frozenset(
    x.strip() for x in os.getenv("OWNER_TEST_LINE_USER_IDS", "").split(",") if x.strip())

# Google Drive
# NOTE: GOOGLE_DRIVE_ATTACHMENTS_FOLDER_ID was removed here (2026-08-01 final
# config cleanup) — confirmed zero importers anywhere in the codebase. See
# LEGACY_ENV.md.
GOOGLE_DRIVE_FOLDER_ID            = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
GOOGLE_SERVICE_ACCOUNT_JSON       = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials.json")

# Attachments
# Base URL used for building public URLs when attachments are stored locally.
# Should point to the admin server, e.g. https://admin.yourdomain.com
ATTACHMENT_BASE_URL = os.getenv("ATTACHMENT_BASE_URL", "http://localhost:8001/admin/attachments")

# ── Storage abstraction (see storage/ package) ─────────────────
# Only this and the matching provider's credentials should change when
# migrating storage backends — no application code should need to change.
STORAGE_PROVIDER    = os.getenv("STORAGE_PROVIDER", "local")  # local | supabase | google_drive | s3 | minio
STORAGE_DELETE_MODE = os.getenv("STORAGE_DELETE_MODE", "delete")  # delete | archive

# Local provider
# Defaults to the existing "knowledge/" directory this deployment already
# uses (knowledge files directly under it, attachments under
# knowledge/attachments/ via the "attachments" folder param) — do NOT
# change this default without also migrating existing files on disk, or
# already-uploaded files become unservable.
LOCAL_STORAGE_ROOT = os.getenv("LOCAL_STORAGE_ROOT", "knowledge")

# Supabase Storage provider
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "knowledge-files")

# Google Drive provider (distinct from the legacy attachment-only vars
# above, which remain for backward compatibility with existing rows)
GOOGLE_DRIVE_ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", GOOGLE_DRIVE_FOLDER_ID)
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", GOOGLE_SERVICE_ACCOUNT_JSON)

# S3 / MinIO provider (MinIO is S3-compatible — set S3_ENDPOINT to the
# MinIO endpoint URL and leave everything else the same)
S3_BUCKET            = os.getenv("S3_BUCKET")
S3_REGION            = os.getenv("S3_REGION", "us-east-1")
S3_ENDPOINT          = os.getenv("S3_ENDPOINT")  # None => real AWS S3
S3_ACCESS_KEY_ID     = os.getenv("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY")

# App
# NOTE: AUTO_MODE was removed here (2026-08-01 final config cleanup) — was
# imported by line_bot/webhook.py but never referenced again anywhere; a
# "Full Auto Mode" flag from the platform's original design docs that was
# never implemented. See LEGACY_ENV.md.
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.75"))

# Production Integration Sprint (2026-08-02), Phase 1 Step D — temporary
# rollout flag for line_bot/webhook.py. Defaulted to true (2026-08-02,
# Local Production Pipeline Verification phase) — we are no longer
# validating the architecture, we are validating the production pipeline
# itself, so the Decision Engine path is now the default for local/
# Cloudflare Tunnel work. The legacy adapter (line_bot/intent.py
# classify() + hardcoded dispatch) remains available ONLY as a temporary
# fallback (set this env var to "false" explicitly to use it) — after
# LINE OA UAT passes, the legacy adapter and this flag are both removed
# entirely in a separate cleanup step, per the sprint's explicit plan.
DECISION_ENGINE_LIVE_ROUTING = os.getenv("DECISION_ENGINE_LIVE_ROUTING", "true").strip().lower() == "true"

# LINE Confirmation Flow (2026-08-10) — how long a pending confirmation
# (services/pending_confirmation_service.py) stays valid before a reply
# like "ยืนยัน" is treated as expired and must be re-requested. Applies to
# ANY COMMAND-type Business Action gated by
# services/decision_engine.py::_requires_confirmation, not just SendLineNotiCS.
PENDING_CONFIRMATION_TIMEOUT_SECONDS = int(os.getenv("PENDING_CONFIRMATION_TIMEOUT_SECONDS", "300"))

# Full Auto Import — no Import Preview / manual confirmation step. Every
# upload always runs the full AI Knowledge Analyzer + Knowledge Graph
# pipeline; these are documentation-level constants (not env-configurable)
# since the platform no longer offers a way to choose otherwise. See
# admin/routes.py's upload/_run_sync_list for where this is enforced.
DEFAULT_AI_PROFILE          = "advanced"
AI_ANALYSIS_ENABLED         = True
KNOWLEDGE_GRAPH_ENABLED     = True
FULL_AUTO_IMPORT            = True
REQUIRE_IMPORT_PREVIEW      = False
REQUIRE_MANUAL_CONFIRMATION = False

# Admin UI
ADMIN_USERNAME       = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD       = os.getenv("ADMIN_PASSWORD", "changeme")
SESSION_SECRET       = os.getenv("SESSION_SECRET", "shipify-secret-change-me")

# Embedding
# OpenAI text-embedding-3-large is now the ACTIVE default provider/model
# (post clean-reset migration — see migrations/019_openai_embedding_dimension.sql
# and tools/reset_knowledge_base.py). The local sentence-transformers
# provider in services/embedding_service.py remains in source as an
# explicit, non-default fallback (selected only by setting
# EMBEDDING_PROVIDER=local) — it is never chosen silently. Query and
# document embeddings always go through the SAME
# services.embedding_service.get_embedding_provider() singleton, so they
# can never diverge in provider/model.
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"  # only used if EMBEDDING_PROVIDER=local
EMBEDDING_DIM   = 768  # only meaningful for the local provider

# "openai" (default, active) | "local" (explicit opt-in fallback only —
# must be set deliberately via env var, never chosen automatically).
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "openai")
# Single source of truth for the OpenAI embedding model. If
# OPENAI_EMBEDDING_MODEL is explicitly set, it ALWAYS wins — the legacy
# OPENAI_EMBED_MODEL alias is only ever consulted as a fallback when the
# canonical variable is absent, and must never silently override an
# explicitly-configured canonical value. (2026-07-29 incident: an .env
# with only the legacy alias set to text-embedding-3-small silently
# resolved here to 1536 dimensions while knowledge_chunks.embedding is
# VECTOR(3072) — see services/embedding_service.py::
# validate_embedding_configuration() for the startup/pre-ingestion check
# that now catches this class of mismatch before any chunk is embedded.)
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-large"))
# Optional — the OpenAI embeddings API supports a `dimensions` param that
# truncates the model's native output (e.g. text-embedding-3-large
# defaults to 3072, but can be requested at 1024/768/etc). Leave unset to
# use the model's native/default dimension. Never silently force 768 just
# to avoid a migration — see migrations/018_embedding_versions.sql, which
# doesn't require a fixed dimension.
OPENAI_EMBEDDING_DIMENSIONS = int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS")) if os.getenv("OPENAI_EMBEDDING_DIMENSIONS") else None
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "64"))

CHUNK_SIZE      = 400
CHUNK_OVERLAP   = 50
TOP_K           = 3
# Split from TOP_K per the Phase 2 spec: how many raw candidates to pull
# from vector search BEFORE hybrid reranking (large — so a chunk with
# weak vector similarity but strong lexical/heading evidence still gets a
# chance to be scored) vs. how many make it into the final LLM prompt.
RAG_RAW_CANDIDATE_COUNT = int(os.getenv("RAG_RAW_CANDIDATE_COUNT", "30"))

# Widened final context size for broad "summarize/overview" questions
# (rag/searcher.py::is_broad_summary_query) — lets the LLM merge several
# FAQ/company chunks into one summary instead of only the top-3 default.
RAG_SUMMARY_TOP_K       = int(os.getenv("RAG_SUMMARY_TOP_K", "12"))

# NOTE: RAG_VECTOR_WEIGHT / RAG_KEYWORD_WEIGHT / RAG_HEADING_WEIGHT (hybrid
# retrieval scoring weights) and RAG_FINAL_TOP_K were removed here (2026-08-01
# final config cleanup) — confirmed never imported by any module. Superseded
# by services/retrieval_settings.py's RAG_SEMANTIC_WEIGHT / RAG_KEYWORD_WEIGHT_V2
# / RAG_HEADING_WEIGHT_V2 / RAG_FINAL_CONTEXT_TOP_K (DB-backed, live-reload —
# see the Admin UI's Settings -> Retrieval Settings card). See LEGACY_ENV.md.

# ── Multilingual query expansion (rag/query_expansion.py) ───────────
# Level 2 (LLM query rewrite) is OFF by default — the pipeline must work
# fully on Level 1 (deterministic glossary) alone; enabling this costs a
# real LLM call per NEW (uncached) query.
RAG_QUERY_REWRITE_ENABLED = os.getenv("RAG_QUERY_REWRITE_ENABLED", "false").strip().lower() == "true"

# strict: forbid the LLM from adding unsupported general/external
# knowledge (invented version numbers, "best practice" recommendations,
# etc.) — see services/prompt_builder.py's STRICT_GROUNDING_RULES.
RAG_GROUNDING_MODE = os.getenv("RAG_GROUNDING_MODE", "strict")

# Vision + OCR ingestion pipeline (see services/pdf_page_pipeline.py) —
# per-PDF-page hybrid extraction. "disabled" = native text only (never
# spends anything, never renders a page image). "basic" = native + local
# OCR fallback (free — Tesseract if installed, else pages needing OCR are
# just marked with a warning). "advanced" = native + OCR + Vision
# (OpenAI, real per-image cost) for pages that genuinely need it —
# corrupted/sparse text, image-only pages, tables, infographics. Default
# is 'basic' so upgrading this code never silently starts spending money;
# an admin must explicitly opt into 'advanced'.
VISION_OCR_ANALYSIS_PROFILE = os.getenv("VISION_OCR_ANALYSIS_PROFILE", "basic")

# ── Credential Store — platform-level master encryption key ──────────────
# Encrypts every row in `integration_credentials` (services/
# credential_store.py). Loaded once at startup; NEVER stored in the DB,
# never exposed in the Admin UI, never logged. If unset, encrypted
# credential storage is simply unavailable (create/rotate/resolve all
# fail with a structured error) — existing env-var
# (secret_configuration) secrets keep working regardless, since they
# don't depend on this key at all.
CREDENTIAL_ENCRYPTION_KEY = os.getenv("CREDENTIAL_ENCRYPTION_KEY")

# Single-tenant deployments never need to set this — every credential
# is scoped under this tenant_id by default.
DEFAULT_TENANT_ID = os.getenv("DEFAULT_TENANT_ID", "default")

# NOTE: SHOW_AI_EVALUATION / SHOW_PRODUCTION_VALIDATION /
# SHOW_BUSINESS_ACTION_CENTER precomputed constants were removed here
# (2026-08-01 final config cleanup) — confirmed nothing ever imported them.
# The env var NAMES themselves are still fully live and functional (this is
# NOT a behavior change): services/developer_mode.py's _env_hard_disabled()
# reads each one directly via os.getenv(env_override, "true") at call time,
# completely independent of this file. See LEGACY_ENV.md.
