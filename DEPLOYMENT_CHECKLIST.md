# Deployment Checklist — AI Engine v1.0.1

Documentation only. Use this checklist before, during, and after every production deployment.

## Pre-Deployment

- [ ] **Environment Variables** — confirm `.env` (or hosting platform secrets) contains all required keys; compare against `.env.example`.
- [ ] **Database** — confirm Supabase Postgres is reachable; confirm `SUPABASE_URL` / `SUPABASE_KEY` / `SUPABASE_DB_URL` are current and valid.
- [ ] **Vector DB** — confirm `knowledge_chunks` table's embedding column dimension matches the active embedding provider (`text-embedding-3-large`, 3072 dimensions — see `baseline_v1.0.1.json`).
- [ ] **OpenAI Key** — confirm `OPENAI_API_KEY` is valid and has sufficient quota for the expected chat model (`gpt-4o`) and embedding model usage.
- [ ] **LINE Channel Secret** — confirm the LINE Developers Console channel secret is provisioned (required for LINE OA Integration, the next phase).
- [ ] **LINE Access Token** — confirm the long-lived channel access token is issued and stored securely (never in source control).
- [ ] **Storage** — confirm the attachment/file storage backend (Supabase Storage or configured provider) is reachable and has adequate quota.
- [ ] **Logging** — confirm application logs (server stdout, `[SessionService]`, `[synonym_service]`, etc.) are captured to a persistent location, not just console output.
- [ ] **Monitoring** — confirm uptime/error monitoring is in place for the `/admin` FastAPI process and any future LINE webhook endpoint.

## Deployment

- [ ] **Migration** — confirm all migrations up to `migrations/025_rag_benchmark_phase2.sql` are applied (verify via `information_schema.columns`/`information_schema.tables`, as done during the AI Engine freeze audit). No new migration is introduced by v1.0.1.
- [ ] **Validation** — run `POST /admin/api/validation/run` (Production Validation Center) against the target environment and confirm scores match or exceed Production Baseline v1.0.1 (`baseline_v1.0.1.json`).
- [ ] **Smoke Test** — manually walk through `tests/smoke_test_cases.md` (all ~20 scenarios) against the deployed environment.
- [ ] **Rollback Plan** — confirm the previous known-good deployment (or container image/commit) is available to redeploy immediately if smoke tests fail; confirm database changes in this release are additive-only (no destructive migration) so rollback never requires a data restore.

## Post-Deployment

- [ ] **Verify AI Response** — send a real question through the live channel and confirm a coherent, grounded answer is returned.
- [ ] **Verify Citation** — confirm the response (or its Playground/Explainability equivalent) shows a citation matching real retrieved evidence (see SM-15 in `tests/smoke_test_cases.md`).
- [ ] **Verify Conversation** — run a short multi-turn conversation (see SM-10/SM-11) and confirm topic/entity state persists correctly across turns.
- [ ] **Verify Attachments** — confirm an attachment-eligible question returns the correct file, and a non-eligible question returns none.
- [ ] **Verify ERP** — if ERP integration is active in this environment, confirm the bridge (`erp/bridge.py`) responds correctly for a test lookup.
- [ ] **Verify Logging** — confirm the deployment's logs show no unexpected errors/exceptions in the first monitoring window after go-live.

---

**Scope note**: This checklist governs release process only — it does not modify AI behavior, retrieval, prompts, or policies. Any checklist item that fails should block deployment and trigger the rollback plan above, not an ad-hoc code change under deployment pressure.
