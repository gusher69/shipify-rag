import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the directory this file lives in (works regardless of cwd)
load_dotenv(Path(__file__).parent / ".env")

# OpenAI
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")
OPENAI_CHAT_MODEL    = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o")
OPENAI_EMBED_MODEL   = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# Supabase
SUPABASE_URL         = os.getenv("SUPABASE_URL")
SUPABASE_KEY         = os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_DB_URL      = os.getenv("SUPABASE_DB_URL")

# LINE OA
LINE_CHANNEL_SECRET  = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_TOKEN   = os.getenv("LINE_CHANNEL_TOKEN")
LINE_NOTIFY_TOKEN    = os.getenv("LINE_NOTIFY_TOKEN")

# Google Drive
GOOGLE_DRIVE_FOLDER_ID            = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
GOOGLE_DRIVE_ATTACHMENTS_FOLDER_ID = os.getenv("GOOGLE_DRIVE_ATTACHMENTS_FOLDER_ID", os.getenv("GOOGLE_DRIVE_FOLDER_ID"))
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
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.75"))
AUTO_MODE            = os.getenv("AUTO_MODE", "false").lower() == "true"

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
RAG_FINAL_TOP_K         = int(os.getenv("RAG_FINAL_TOP_K", str(TOP_K)))

# Widened final context size for broad "summarize/overview" questions
# (rag/searcher.py::is_broad_summary_query) — lets the LLM merge several
# FAQ/company chunks into one summary instead of only the top-3 default.
RAG_SUMMARY_TOP_K       = int(os.getenv("RAG_SUMMARY_TOP_K", "12"))

# ── Hybrid retrieval (rag/hybrid_scoring.py) ────────────────────────
# Generic, entity/domain-agnostic scoring weights — never hardcode a
# specific document/topic here. See rag/hybrid_scoring.py for the actual
# formula: hybrid_score = vector*W_VECTOR + keyword*W_KEYWORD + heading*W_HEADING.
# NOT re-validated against a new embedding model yet — re-run
# rag/evaluation.py after any embedding-model change before trusting
# these defaults.
RAG_VECTOR_WEIGHT  = float(os.getenv("RAG_VECTOR_WEIGHT", "0.5"))
RAG_KEYWORD_WEIGHT = float(os.getenv("RAG_KEYWORD_WEIGHT", "0.3"))
RAG_HEADING_WEIGHT = float(os.getenv("RAG_HEADING_WEIGHT", "0.2"))

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

# ── Admin sidebar navigation feature flags ────────────────────────────
# Temporarily hides these menu items during the RAG customer demo phase
# (the demo focuses only on Knowledge Base upload + AI Playground chat
# testing) — routes, templates, services, and permissions are all
# untouched, only sidebar visibility is affected (see
# admin/sidebar_config.py). Direct URLs keep working either way. Flip
# any of these back to "true" (or unset — all default to visible) to
# restore the menu item once the demo phase ends.
SHOW_AI_EVALUATION        = os.getenv("SHOW_AI_EVALUATION", "true").strip().lower() == "true"
SHOW_PRODUCTION_VALIDATION = os.getenv("SHOW_PRODUCTION_VALIDATION", "true").strip().lower() == "true"
SHOW_BUSINESS_ACTION_CENTER = os.getenv("SHOW_BUSINESS_ACTION_CENTER", "true").strip().lower() == "true"
