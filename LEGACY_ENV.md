# Legacy / Removed Environment Variables

_Written 2026-08-01 during the final configuration cleanup pass. This file exists so that
someone who remembers an old variable name (from an earlier `.env`, an old doc, or search
history) can find out what happened to it, without it cluttering the live `.env.example`._

Every entry below was verified against the actual codebase before being listed — not
guessed. "Confirmed dead" means a repo-wide search found zero importers/consumers outside
the variable's own definition line.

---

## Removed from runtime code (2026-08-01)

These variables' `os.getenv()` calls were deleted from the codebase in this pass. Setting
them in `.env` today has **no effect whatsoever** — nothing reads them anymore.

| Variable | Used to live in | Why it was dead |
|---|---|---|
| `GOOGLE_DRIVE_ATTACHMENTS_FOLDER_ID` | `config.py` | Defined, defaulted to `GOOGLE_DRIVE_FOLDER_ID`, never imported by any other module. |
| `AUTO_MODE` | `config.py`, imported (unused) by `line_bot/webhook.py` | A "Full Auto Mode" flag from the platform's original design docs (see `CONTEXT.md`); imported but never referenced again anywhere. Never implemented. |
| `RAG_VECTOR_WEIGHT` | `config.py` | Hybrid-retrieval vector-score weight. Never imported by any module — superseded by `RAG_SEMANTIC_WEIGHT` (below). |
| `RAG_KEYWORD_WEIGHT` | `config.py` | Same story — superseded by `RAG_KEYWORD_WEIGHT_V2`. |
| `RAG_HEADING_WEIGHT` | `config.py` | Same story — superseded by `RAG_HEADING_WEIGHT_V2`. |
| `RAG_FINAL_TOP_K` | `config.py` | Never imported by any module — superseded by `RAG_FINAL_CONTEXT_TOP_K`. |
| `RAG_WEAK_SEMANTIC_RELATIVE_THRESHOLD` | `rag/hybrid_scoring.py` | Defined via its own direct `os.getenv()` call, never referenced again — even within its own module. |
| `SHOW_AI_EVALUATION` (constant) | `config.py` | **The env var name itself is still fully live** — `services/developer_mode.py::_env_hard_disabled()` reads it directly via `os.getenv(env_override, "true")` at call time. Only `config.py`'s redundant precomputed boolean was dead; removing it changed nothing observable. |
| `SHOW_PRODUCTION_VALIDATION` (constant) | `config.py` | Same as above. |
| `SHOW_BUSINESS_ACTION_CENTER` (constant) | `config.py` | Same as above. |

**The superseding system** for the four `RAG_*` weight/top-k variables above is
`services/retrieval_settings.py` — DB-backed (`rag_retrieval_settings`, migration 021),
live-reload, edited from Admin → Settings → **Retrieval Settings**. Its own env-var
fallbacks (`RAG_SEMANTIC_WEIGHT`, `RAG_KEYWORD_WEIGHT_V2`, `RAG_HEADING_WEIGHT_V2`,
`RAG_FINAL_CONTEXT_TOP_K`, and 12 others) are genuinely alive and documented in
`.env.example` — they are consulted only before any DB override row exists.

---

## Kept as deliberate backward compatibility

| Variable | Status | Why it stays |
|---|---|---|
| `OPENAI_EMBED_MODEL` | Legacy alias, still functional | `config.py`'s `OPENAI_EMBEDDING_MODEL` precedence expression still consults this as a fallback when the canonical variable is unset. A real 2026-07-29 production incident (a stale small-model value here silently producing 1536-dim vectors into a `VECTOR(3072)` column) makes this one worth keeping as a safety-net rather than a hard break — "absolutely necessary" per the backward-compatibility bar. Covered by `tests/test_embedding_service.py`. |

---

## Never implemented (documented, but no code ever consumed them)

Not "dead" in the sense of code being removed — these describe a feature that was never
built. Kept in `.env.example` as commented-out, clearly-labeled placeholders for when (if)
the feature is added, rather than silently deleted.

| Variable(s) | Intended feature | Verified status |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_CHAT_DEPLOYMENT`, `AZURE_OPENAI_EMBED_DEPLOYMENT` | Multi-provider LLM support (Azure OpenAI) | `services/llm_service.py` has only an `OpenAIProvider` class; `get_llm_service()` never branches on any of these. Zero consumers. |
| `GROQ_API_KEY` | Multi-provider LLM support (Groq) | Same — zero consumers. |
| `OPENROUTER_API_KEY` | Multi-provider LLM support (OpenRouter) | Same — zero consumers. |

---

## Settings UI fields with no runtime consumer (not removed from the UI this pass)

Flagged during the 2026-08-01 Settings-key-consistency audit. The **admin form field**
still exists (writing it to `.env` has no functional effect) — only the underlying env var
is "dead." Left as-is pending a decision on whether to remove the UI field itself, which is
a Settings-template change and was judged out of scope for a config-file cleanup pass.

| Variable | Status |
|---|---|
| `SUPABASE_ANON_KEY` | `config.py` has no `SUPABASE_ANON_KEY` constant at all; no module reads it. The Settings → Supabase card's "Anon Key" field currently saves a value that nothing consumes. |

`LINE_CHANNEL_ACCESS_TOKEN` (the previous wrong name for `LINE_CHANNEL_TOKEN`) is **not**
listed here — it was already renamed to the correct, functional variable name in a prior
session's Settings-key-consistency fix, not merely flagged.

---

## Full audit trail

See the repo's own commit history / session notes around 2026-08-01 ("configuration
hardening", "final configuration cleanup") for the complete verification methodology —
every variable in this file and in `.env.example` was checked against a real
`grep`-confirmed consumer, never assumed.
