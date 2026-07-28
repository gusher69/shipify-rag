# Shipify AI Platform

An AI customer-service platform that answers customers automatically over **LINE Official Account**, combining a **RAG knowledge base** (policies, FAQs, product docs) with **live ERP data** (orders, stock, customer records) through a configuration-driven **Business Action** integration layer — plus a full **Admin Web UI** for managing knowledge, ERP integrations, AI behaviour, and reviewing conversations.

Built as a reusable **platform**, not a one-off customer build: a second customer in a different industry should be onboarded through configuration (Business Actions, Knowledge Base content, AI Policies), not custom code. See [CLAUDE.md](CLAUDE.md) for the platform-first engineering principles this repo follows.

## Main Features

- **RAG Knowledge Base** — ingest PDF/Word/Excel/Markdown documents (manual upload or nightly Google Drive sync), chunk + embed (OpenAI `text-embedding-3-large`, 3072-dim pgvector), and answer policy/FAQ questions with citation-grounded responses.
- **Business Action Center** — a config-driven registry for connecting external/ERP APIs (REST, form or JSON body, header/query/body-form secrets) without writing integration code per customer. Includes an **AI Auto Setup / Smart Capability Setup** wizard that reads a pasted cURL/Postman/API doc and generates the full configuration (parameters, auth, routing, AI behaviour) automatically, with a Review & Edit UI, live explainability ("Why?" panels), and a semantic business-layer (canonical field names, detected intents/entities, KB recommendations) layered on top.
- **Decision Engine** — routes each customer message to the right source (ERP Business Action vs. Knowledge Base vs. both) based on intent, confidence, and configured routing rules.
- **Credential Store** — encrypted (Fernet), multi-tenant storage for API secrets used by Business Actions, separate from environment-variable-based legacy secrets.
- **LINE OA Webhook** — intent + sentiment + language detection, tone-adjusted replies, and Smart Handoff to a human agent (via LINE Notify) when confidence is low or the customer is upset.
- **User Profiles** — customer segmentation (cold/warm/hot) based on order history, stored in Supabase Postgres.
- **Admin Web UI** — Knowledge Base management, ERP Integration (Business Actions), AI Playground, Prompt Studio, AI Policies, AI Evaluation/Benchmark, Production Validation, Sync Activity, and more.

## Tech Stack

| Layer | Technology |
|---|---|
| Language / runtime | Python 3.13 |
| Web framework | FastAPI + Uvicorn, Jinja2 templates |
| LLM / Embeddings | OpenAI (`gpt-4o`, `text-embedding-3-large`); optional Azure OpenAI, Groq, OpenRouter |
| Vector DB | Supabase Postgres + pgvector (`knowledge_chunks`, `VECTOR(3072)`) |
| Relational DB | Supabase Postgres (Business Actions, credentials, user profiles, sync jobs, etc.) |
| File storage | Supabase Storage (default) or S3-compatible / local disk |
| Messaging channel | LINE Messaging API v3, LINE Notify |
| File ingestion | pdfplumber, PyMuPDF, python-docx, pandas/openpyxl, markitdown |
| Scheduled sync | APScheduler (Google Drive → Knowledge Base, nightly) |
| Secrets encryption | `cryptography` (Fernet) |
| Testing | `unittest` (standard library), ~85 test modules |

No frontend build tooling — the Admin UI is server-rendered Jinja2 + vanilla JS (no `package.json`/npm project).

## Folder Structure

```
admin/            FastAPI admin web app — routes, Jinja2 templates, static assets, sidebar config
line_bot/         LINE Messaging API webhook, intent/tone/sentiment, message adapter
rag/              RAG pipeline — retrieval, hybrid scoring, query understanding, confidence, conversation state
ingestion/        Document ingestion — PDF/Word/Excel readers, chunker, embedder, Google Drive sync
services/         Business logic layer — Business Action registry/executor, AI Auto Setup, Credential Store,
                  Decision Engine, LLM/embedding service abstractions, prompt builder, etc.
erp/              Read-only ERP bridge (PHP/MySQL wrapper)
profiles/         Customer profile manager (segmentation)
storage/          Storage backend abstraction (Supabase / S3 / local / Google Drive)
migrations/       Numbered, hand-written SQL migrations (run in order, no ORM/Alembic)
tools/            One-off operational scripts — seeding, backfill, knowledge-base reset
tests/            unittest suite (~85 files) + manual QA fixtures (uat_questions.csv, smoke_test_cases.md)
data/             Small static JSON reference data (synonym groups, spell-correction fallback)
knowledge/        Local working copy of ingested documents (gitignored — real content lives in Supabase Storage)
docs/             Project documentation (see below)
config.py         Central settings module — every environment variable is read here
```

Documentation:
- [CLAUDE.md](CLAUDE.md) — guidance for Claude Code / any AI coding agent working in this repo
- [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md) — scope, objectives, user flows
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components and data flow
- [docs/SETUP.md](docs/SETUP.md) — detailed new-machine setup steps
- [docs/DATABASE.md](docs/DATABASE.md) — schema, migrations, backup/restore
- [docs/API.md](docs/API.md) — endpoints, auth, external integrations
- [docs/CURRENT_STATUS.md](docs/CURRENT_STATUS.md) — what's done / in progress / known issues
- [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md) — prioritized backlog

Several legacy feature-specific docs remain at the repo root (`AI_AUTO_SETUP.md`, `BUSINESS_ACTION_CENTER.md`, `CREDENTIAL_STORE.md`, `DECISION_ENGINE.md`, `SMART_CAPABILITY_SETUP.md`, release notes, etc.) — see `docs/CURRENT_STATUS.md` for how these relate to the current implementation.

## Installation

Requires **Python 3.13** and a Supabase project (Postgres + pgvector + Storage).

```bash
git clone <repo-url>
cd shipify-rag
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

## Environment Setup

```bash
copy .env.example .env        # Windows
# cp .env.example .env        # macOS/Linux
```

Fill in real values in `.env`. At minimum for local development you need:
`OPENAI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_DB_URL`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `SESSION_SECRET`, `CREDENTIAL_ENCRYPTION_KEY`.

LINE and Google Drive variables are only required if you're running the LINE webhook or the Drive sync job — see comments in `.env.example` for what each variable is for and how to generate it.

**Never commit `.env`.** See `.gitignore` — it's already excluded.

## Database Setup

The database is **Supabase-hosted Postgres with the `pgvector` extension** — there is no local Postgres/Docker DB to run.

1. Create a Supabase project, enable the `pgvector` extension (SQL Editor → `create extension if not exists vector;`).
2. Run `supabase_setup.sql` first (base schema).
3. Run every file in `migrations/` **in numeric order** (`001_...sql` through `030_credential_store.sql`) via the Supabase SQL Editor, or `psql "$SUPABASE_DB_URL" -f migrations/001_...sql` etc.
4. See [docs/DATABASE.md](docs/DATABASE.md) for the full table list, relationships, and seed-data scripts.

## Running in Development

```bash
# Admin Web UI (Knowledge Base, Business Actions, AI Playground, etc.) — port 8001
python -m uvicorn admin.routes:app --reload --port 8001
# Admin UI: http://localhost:8001/admin/documents  (login: ADMIN_USERNAME / ADMIN_PASSWORD)

# LINE OA webhook — port 8000 (only needed to actually receive LINE messages)
python -m uvicorn line_bot.webhook:app --reload --port 8000
```

A `.claude/launch.json` dev-server config is also provided (`admin.preview_server:app`, port 8001) for use with Claude Code's browser preview tooling.

## Running in Production

```bash
python -m uvicorn admin.routes:app --host 0.0.0.0 --port 8001 --workers 2
python -m uvicorn line_bot.webhook:app --host 0.0.0.0 --port 8000 --workers 2
```

Put both processes behind a reverse proxy (TLS termination) and expose the LINE webhook URL to LINE's platform. See [docs/SETUP.md](docs/SETUP.md) for a full checklist and `DEPLOYMENT_CHECKLIST.md` for the pre/during/post-deploy checklist. A `Dockerfile` and `docker-compose.yml` are provided for containerized deployment (see below).

## Testing

```bash
# Full suite
python -m unittest discover -s tests -q

# Single file
python -m unittest tests.test_business_action_registry -v
```

There is no CI pipeline configured yet (no `.github/workflows`) — tests are run manually before every merge.

## Deployment

```bash
docker build -t shipify-admin .
docker run --env-file .env -p 8001:8001 shipify-admin
```

Or with docker-compose (runs the Admin app; the LINE webhook can be added as a second service using the same image with a different command):

```bash
docker compose up -d
```

The target hosting platform referenced in prior docs is **Railway.app** (Supabase remains a separate hosted service either way). See [docs/SETUP.md](docs/SETUP.md) and `DEPLOYMENT_CHECKLIST.md`.
