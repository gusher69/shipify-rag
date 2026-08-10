# Deployment Checklist

## 2026-08-10 — Deployment Freeze (customer UAT deployment target)

**Database architecture, re-verified this pass** (code/config inspection only, no secrets printed): managed Supabase Postgres + pgvector — confirmed via `services/business_action_registry.py`/`services/credential_store.py`/`rag/searcher.py` all talking to Supabase tables through `supabase-py`, and `scripts/validate_env.py`'s live connectivity checks (OpenAI, Supabase REST, direct Postgres, pgvector extension, `knowledge_chunks` column width, Credential Store table) all PASS. `STORAGE_PROVIDER` is currently `local` (not `supabase`) on this dev machine — the one piece of state that would NOT automatically be available on a fresh customer server; either switch it to `supabase` before deploy (recommended, zero code change) or plan to copy the local `knowledge/` directory's files across. Nothing else is local-only: Business Actions, Credential Store, all knowledge chunks/embeddings, and the new `pending_confirmations` table (migration 036) all live in the same managed Supabase project a customer-server deployment would reuse as-is.

**8-endpoint ERP status** (see `PROJECT_STATE.md` Current Sprint for full detail): 7 of 8 enabled and production-ready; `SearchDataTracking` disabled (`CUSTOMER_API_DEFECT`, external API bug, not a Shipify defect, not a deployment blocker); `SendLineNotiCS` enabled with a generic pre-execution confirmation gate — no real notification has ever been sent.

**Knowledge Base**: cleanup applied and verified (2026-08-10) — 15 of 17 active files (unrelated vendor/demo content) disabled (`is_active=false`, not hard-deleted); only the 2 genuine Shipify FAQ files remain active. See `PROJECT_STATE.md`/`docs/NEXT_STEPS.md` item H.

**UAT test accounts** (customer-provided, real ERP data): `FT1004`, `SP1008`, `FT3182`, `SP1014`.

**Regression**: 1726/1726 passing; `scripts/preflight.py` full PASS.

**Local ports this pass** (`.claude/launch.json`): Admin (`admin.routes:app`, the real app, not the preview variant) on `:8010`; LINE webhook (`line_bot.webhook:app`) on `:8000`.

**Not committed, not pushed, not deployed** — this pass is local verification + a cleanup proposal only.

---

## 2026-08-07 — Customer Server Deployment (tomorrow) — historical, see 2026-08-10 above for current state

### Architecture decision: Dockerized app vs. self-hosted Supabase

The customer mentioned installing Docker/Supabase on their server. Before planning anything, here's what the app actually depends on today:

- **Postgres + pgvector** — every table, plus `knowledge_chunks.embedding VECTOR(3072)` and the `match_knowledge_chunks` RPC function (cosine similarity search).
- **PostgREST** — every `services/*.py` module talks to the DB through `supabase-py`'s `.table(...)` calls, i.e. the REST layer, not raw SQL (one exception: today's migration was applied via a direct `psycopg2`/`SUPABASE_DB_URL` connection, since `psql` isn't installed on this dev machine).
- **Supabase Storage** — knowledge file/attachment uploads.
- **NOT used**: Supabase Auth (admin login is custom, cookie-based — `admin/routes.py::auth()`), Realtime, Edge Functions. Confirmed via a full-codebase grep — zero references.

**Option A — Dockerize the application, keep the existing managed Supabase project.**
`Dockerfile` + `docker-compose.yml` already exist and are current (`admin` service on 8001, `line-webhook` service on 8000, both reading `.env`). Zero data migration. Zero new infrastructure to debug. The app already works against this exact managed Supabase project — every ERP/RAG/Decision Engine test run today, including the two new ERP endpoints, ran against it live.

**Option B — Fully self-host Supabase on the customer server too.**
Requires standing up Supabase's own multi-container stack (Postgres+pgvector, PostgREST, Storage API, and normally GoTrue/Realtime even though this app doesn't use them — Supabase's official self-host compose bundles them together), migrating every table + the real knowledge base + real customer profile data across, re-pointing `SUPABASE_URL`/`SUPABASE_DB_URL` at the new instance, and re-verifying the `match_knowledge_chunks` RPC + pgvector extension behave identically on whatever Postgres version the customer's Docker host ends up running. This is a real, multi-day infrastructure migration, not a same-day task.

**Recommendation: Option A, for tomorrow.** Self-hosting Supabase is a legitimate future move (data residency, cost, control) but is a separate, deliberate infrastructure project — not something to fold into a same-day deploy under deadline pressure. Flagging as a Future Enhancement (`docs/NEXT_STEPS.md`) rather than doing it now.

### Exact commands for tomorrow (Option A)

```bash
# On the customer server, with Docker installed:
git clone <repo-url> shipify-rag && cd shipify-rag
cp .env.example .env
# edit .env: fill in OPENAI_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY,
# SUPABASE_DB_URL, ADMIN_PASSWORD, SESSION_SECRET, CREDENTIAL_ENCRYPTION_KEY,
# LINE_CHANNEL_SECRET, LINE_CHANNEL_TOKEN (see SETUP_NEW_MACHINE.md for how
# to generate SESSION_SECRET/CREDENTIAL_ENCRYPTION_KEY)

# Apply migrations (psql, or the same psycopg2 approach used today if
# psql isn't available):
psql "$SUPABASE_DB_URL" -f migrations/001_....sql   # ... through the latest numbered file, in order
# (every migration is idempotent — safe to re-run if unsure what's applied)

docker compose up -d admin                  # Admin Web UI, port 8001
docker compose --profile line up -d line-webhook   # LINE webhook, port 8000 (only if going live on LINE immediately)

# Verify:
curl -I http://localhost:8001/admin/login   # expect 200
curl -I http://localhost:8000/               # expect 404 (no handler on GET /, expected — only POST /webhook)
```

Then expose the webhook publicly (customer's own reverse proxy/HTTPS, or a Cloudflare Tunnel as used in local dev) and update the Webhook URL in the LINE Developers Console to the new public URL.

### What's NOT ready for this deploy (as of 2026-08-10 — see the Deployment Freeze section at the top for the current, authoritative state)

- `SearchDataTracking` remains disabled (`CUSTOMER_API_DEFECT`) — external API bug, not fixable from our side, not a deployment blocker.
- `STORAGE_PROVIDER=local` on this dev machine only — `.env.example`'s template already defaults to `STORAGE_PROVIDER=supabase`, so a fresh customer-server `.env` created from the template gets the correct value automatically; nothing to fix in the repo itself.
- Everything else (8-endpoint ERP status, Knowledge Base cleanup, LINE confirmation flow, regression, preflight) is applied and verified — see the Deployment Freeze section at the top.

---

# Deployment Checklist — AI Engine v1.0.1 (original, generic — superseded above for the 2026-08-07 deploy)

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
