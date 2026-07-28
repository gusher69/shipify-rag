# Setup Guide (New Machine)

Step-by-step setup for a fresh clone on a new computer.

## 1. Prerequisites

| Software | Version | Notes |
|---|---|---|
| Python | 3.13 | Match the version used in development; earlier 3.x may work but is untested. |
| pip | bundled with Python | |
| Git | any recent version | |
| A Supabase project | — | Postgres + `pgvector` extension + Storage bucket. Free tier is sufficient for development. |
| (Optional) LINE Developers account | — | Only needed to actually receive/send LINE messages. |
| (Optional) Google Cloud service account | — | Only needed for the Google Drive knowledge-sync job. |
| (Optional) Docker | any recent version | Only needed if deploying via the provided `Dockerfile`/`docker-compose.yml`. |

No Node.js/npm is required — there is no frontend build step.

## 2. Clone & Install

```bash
git clone <repo-url>
cd shipify-rag

python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS/Linux

pip install -r requirements.txt
```

## 3. Configure Environment Variables

```bash
copy .env.example .env            # Windows
# cp .env.example .env            # macOS/Linux
```

Open `.env` and fill in real values. See inline comments in `.env.example` for what each variable is and how to generate it. Minimum required for the Admin UI to start:

- `OPENAI_API_KEY`
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_DB_URL`
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`
- `SESSION_SECRET` — generate with `python -c "import secrets; print(secrets.token_hex(32))"`
- `CREDENTIAL_ENCRYPTION_KEY` — generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

Additionally required only if you're running that specific feature:

- LINE webhook: `LINE_CHANNEL_SECRET`, `LINE_CHANNEL_TOKEN`, `LINE_NOTIFY_TOKEN`
- Google Drive sync: `GOOGLE_DRIVE_FOLDER_ID` (+ related IDs), `GOOGLE_SERVICE_ACCOUNT_JSON` pointing at a real service-account JSON key file (**never commit that file**)
- S3 storage backend (instead of the default Supabase Storage): `STORAGE_PROVIDER=s3` + `S3_*` variables

## 4. Set Up the Database (Supabase)

1. Create a project at https://supabase.com.
2. In the SQL Editor, enable pgvector: `create extension if not exists vector;`
3. Run `supabase_setup.sql` (repo root) — the base schema.
4. Run every file in `migrations/` **in numeric order**, 001 through the highest-numbered file (currently `030_credential_store.sql`), plus `verify_metadata.sql` if you want to sanity-check afterward. Either paste each file into the SQL Editor, or:
   ```bash
   for f in migrations/0*.sql; do
     psql "$SUPABASE_DB_URL" -f "$f"
   done
   ```
   (On Windows PowerShell, loop with `Get-ChildItem migrations\0*.sql | Sort-Object Name | ForEach-Object { psql $env:SUPABASE_DB_URL -f $_.FullName }`.)
5. Create a Storage bucket named `knowledge-files` (or match whatever `SUPABASE_STORAGE_BUCKET` is set to) if you're using the default Supabase Storage backend.
6. See `docs/DATABASE.md` for the full table list and optional seed-data scripts.

## 5. Run the Admin Web UI

```bash
python -m uvicorn admin.routes:app --reload --port 8001
```

Open http://localhost:8001/admin/documents and log in with `ADMIN_USERNAME`/`ADMIN_PASSWORD`.

## 6. (Optional) Run the LINE Webhook

Only needed if you're testing actual LINE message delivery (requires a public HTTPS URL registered with LINE, e.g. via `ngrok` during development):

```bash
python -m uvicorn line_bot.webhook:app --reload --port 8000
```

## 7. Ingest Knowledge Base Content

Place documents in `knowledge/` (this folder is gitignored — its contents are local/customer data, not source code), then:

```bash
python -m ingestion.embedder
```

Or use the Admin UI's Knowledge Base upload screen, which triggers ingestion automatically.

## 8. Run Tests

```bash
python -m unittest discover -s tests -q
```

Expect **1 known pre-existing failure** unrelated to environment setup — see `docs/CURRENT_STATUS.md` → Known Issues (`test_executor_blocks_safely_when_secret_missing`, an Action Executor test that depends on live network behavior).

## 9. Claude Code / AI Agent Setup

If continuing development with Claude Code on this new machine:

1. Read `CLAUDE.md` first — it has the architecture map, conventions, and things not to change without necessity.
2. A dev-server launch config already exists at `.claude/launch.json` (`admin.preview_server:app`, port 8001) for use with Claude Code's browser-preview tooling.
3. Read `docs/CURRENT_STATUS.md` and `docs/NEXT_STEPS.md` before starting new work.

## Troubleshooting

- **`ModuleNotFoundError`** — confirm the virtualenv is activated and `pip install -r requirements.txt` completed without errors.
- **Supabase connection errors** — double-check `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` (not the anon key — the service key is required for server-side writes) and that `SUPABASE_DB_URL` uses the *transaction pooler* port (6543), not the direct connection port.
- **`CREDENTIAL_ENCRYPTION_KEY` missing** — Business Actions that use the newer Credential Store (rather than a plain env-var secret) will fail to save/resolve credentials until this is set; legacy `secret_configuration` env-var secrets work without it.
- **pgvector dimension mismatch** — if you change `OPENAI_EMBEDDING_MODEL`/`OPENAI_EMBEDDING_DIMENSIONS`, you must re-apply `migrations/019_openai_embedding_dimension.sql` with the matching `VECTOR(n)` width, and re-embed existing content.
