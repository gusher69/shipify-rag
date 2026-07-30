# Database

## Engine

**Supabase-hosted Postgres** with the **`pgvector`** extension enabled. There is no local/dockerized database — development and production both point at a Supabase project (a separate free-tier project is recommended for local development). There is **no ORM**; schema changes are plain, hand-written, numbered SQL files applied in order.

## Schema Location

- `supabase_setup.sql` (repo root) — the original base schema (predates the numbered migrations).
- `migrations/001_*.sql` through `migrations/030_credential_store.sql` — every schema change since, applied strictly in numeric order. `migrations/verify_metadata.sql` is a verification/sanity-check script, not a migration.

There is no migration runner/CLI — apply files via the Supabase SQL Editor or `psql`. See `docs/SETUP.md` §4 for the exact commands.

## Key Tables (grouped by area)

### RAG / Knowledge Base
| Table | Purpose |
|---|---|
| `knowledge_files` | One row per ingested source document (PDF/Word/Excel/etc.) |
| `knowledge_items` | Normalized content items extracted from a file |
| `knowledge_chunks` | Chunked, embedded text — `embedding VECTOR(3072)` (OpenAI `text-embedding-3-large`); this is what `rag/searcher.py` queries |
| `knowledge_chunk_embeddings`, `embedding_versions` | Embedding version tracking (supports re-embedding after a model change) |
| `knowledge_attachments` | Non-text attachments belonging to a knowledge file |
| `knowledge_sync_jobs`, `knowledge_sync_job_files` | Google Drive sync job tracking |
| `knowledge_graph_nodes`, `knowledge_graph_edges` | Entity/relationship graph extracted from knowledge content |
| `rag_query_synonyms`, `rag_retrieval_settings` | Query expansion and retrieval tuning, editable via Admin UI |

### Excel Engine
| Table | Purpose |
|---|---|
| `excel_workbooks`, `excel_sheets`, `excel_rows` | Structured ingestion of Excel files as queryable rows (not just chunked text) |

### Business Action Center
| Table | Purpose |
|---|---|
| `business_actions` | One row per configured ERP/API integration (the Business Action itself: name, type, category, endpoint, AI behaviour text, routing metadata) |
| `business_action_parameters` | Per-action parameter definitions (technical name, business label, required/validation rules, `input_source`, secret/credential reference) |
| `business_action_tags`, `business_action_examples` | Tags and example trigger questions for a Business Action |
| `business_action_execution` | Resolved execution config (HTTP method, headers, auth type) used at call time |
| `business_action_response_mapping` | Maps API response fields (JSON path) to a human-readable label |
| `business_action_validation` | Parameter/response validation rules |
| `business_action_embeddings` | Embeddings used for semantic similarity/duplicate detection between Business Actions |
| `integration_credentials` | (Legacy/earlier) per-integration credential storage — see also `credentials`/Credential Store below |
| `credential_audit_log` | Audit trail for credential create/rotate/use events |
| `erp_test_cases` (migration 031) | Saved ERP Conversation Tester test cases (intent_param/simulation/live) per Business Action |
| `integration_action_schemas` (migration 033) | Integration Schema Studio's draft/published/archived versioned business/conversation-layer schema per action — completely separate from the technical `business_action_*` tables above; never modifies them |
| `business_action_audit_log` (migration 034) | Immutable audit record for **permanent** Business Action deletion (2026-07-29 hard-delete rework) — mirrors `credential_audit_log`'s shape/RLS convention; never stores secrets/credentials/request-response payloads; a write failure never blocks the delete itself, only surfaces `audit_log_warning` in the API response |
| migration 032 (`field_metadata`) | Additive per-field display metadata columns consumed by the Conversation Form Generator |

**Note (2026-07-29 architecture change):** `business_actions` deletion is **no longer soft-delete**. The old `deleted_at`/restore convention was replaced with a genuine `DELETE` relying on `ON DELETE CASCADE` FKs across 9 dependent tables (verified against live `information_schema`/`pg_constraint`, not just code assumptions) — see `docs/adr/0001-business-action-hard-delete.md` and `services/business_action_registry.py::hard_delete_action()`.

### Credential Store
| Table | Purpose |
|---|---|
| `credentials` (created by `migrations/030_credential_store.sql`) | Fernet-encrypted secret values, multi-tenant, referenced by `business_action_parameters.credential_ref` |

### AI Playground / Prompt Studio / Policies
| Table | Purpose |
|---|---|
| `ai_sessions`, `ai_session_messages`, `ai_session_events`, `ai_session_traces` | Playground conversation sessions and their step-by-step traces |
| `ai_prompt_templates`, `ai_prompt_assignments` | Prompt Studio's editable prompt templates and where they're assigned |
| `ai_policy_sets` | AI Policies configuration |

### Evaluation
| Table | Purpose |
|---|---|
| `rag_benchmark_datasets`, `rag_benchmark_cases`, `rag_benchmark_runs`, `rag_benchmark_results` | AI Evaluation / Benchmark feature's test cases and run history |

### Customer Profiles
| Table | Purpose |
|---|---|
| `user_profiles` | LINE user segmentation (`cold`/`warm`/`hot`), chat style, order stats — written by `profiles/manager.py` |

## Relationships (high level)

```
business_actions 1──* business_action_parameters ──0..1── credentials
business_actions 1──* business_action_examples
business_actions 1──* business_action_response_mapping
business_actions 1──1 business_action_execution
knowledge_files   1──* knowledge_items 1──* knowledge_chunks (vector search target)
knowledge_files   1──* knowledge_attachments
knowledge_sync_jobs 1──* knowledge_sync_job_files
excel_workbooks   1──* excel_sheets 1──* excel_rows
ai_sessions       1──* ai_session_messages / ai_session_events / ai_session_traces
```

`user_profiles`, credential/audit tables, and evaluation tables are largely independent of the above, keyed by their own identifiers (LINE user ID, credential key, benchmark dataset ID respectively).

## Migrations

- Always add the **next sequential number** — never edit an already-applied migration file (write a new one that alters/corrects instead).
- Migrations are idempotent-ish in style (`if not exists` used where reasonable) but are not guaranteed re-runnable — track what's been applied per environment.
- If you change the embedding model or its output dimension, `migrations/019_openai_embedding_dimension.sql` shows the pattern for altering `knowledge_chunks.embedding`'s `VECTOR(n)` width — you must re-embed existing content afterward.

## Seed Data

Located in `tools/`, run manually (not part of any automated setup):

```bash
python tools/seed_benchmark_dataset.py                    # AI Evaluation sample dataset
python tools/seed_business_action_getdatacustomer.py       # Example Business Action (customer lookup)
python tools/seed_business_action_providers.py             # Example provider configuration
python tools/backfill_suggested_questions.py               # Backfill script for existing rows
python tools/reset_knowledge_base.py                        # ⚠ destructive — clears knowledge tables
```

None of these seed real customer data — they create example/demo configuration only. **Do not run `reset_knowledge_base.py` against a production database** without explicit confirmation from the project owner.

## Backup & Restore

There is no automated backup tooling in this repo. A one-off manual export exists at `backups/20260710T144200Z/*.json` (table-by-table JSON dumps) from a prior session — this directory is **gitignored** (may contain real ingested/customer data) and should not be treated as a maintained backup mechanism.

For real backup/restore, use Supabase's own project-level backup features (Dashboard → Database → Backups), or `pg_dump`/`pg_restore` against `SUPABASE_DB_URL` for ad-hoc snapshots:

```bash
pg_dump "$SUPABASE_DB_URL" -F c -f backup.dump
pg_restore -d "$SUPABASE_DB_URL" backup.dump
```

Treat this as a gap to formalize — see `docs/NEXT_STEPS.md`.
