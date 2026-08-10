# Setup: Brand-New Machine

_Written 2026-08-01 as part of the final configuration cleanup. Assumes **no prior knowledge**
of this project — follow every step in order. If you get stuck, the error messages from
`scripts/preflight.py` (step 8) are designed to tell you exactly what's wrong and how to fix it._

For the deeper "why" behind each configuration decision, see
[CONFIGURATION_ARCHITECTURE.md](CONFIGURATION_ARCHITECTURE.md) once you're up and running.

---

## 1. Install Git

| OS | How |
|---|---|
| Windows | https://git-scm.com/download/win — run the installer, accept defaults |
| macOS | `xcode-select --install`, or https://git-scm.com/download/mac |
| Linux | `sudo apt install git` (Debian/Ubuntu) or your distro's package manager |

Verify:
```bash
git --version
```

## 2. Install Python 3.13

Download from https://www.python.org/downloads/ — this project requires **Python 3.13**
specifically (earlier 3.x versions are untested).

Verify:
```bash
python --version
```
(On some systems this is `python3 --version` — if so, use `python3`/`pip3` for every command
below instead of `python`/`pip`.)

## 3. Clone the Repository

```bash
git clone <repo-url>
cd shipify-rag
```

## 4. Create a Virtual Environment (recommended)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS/Linux
```

## 5. Install Dependencies

```bash
pip install -r requirements.txt
```

This installs FastAPI, Supabase's Python client, OpenAI's SDK, `psycopg2-binary` (for the
PostgreSQL/pgvector health check), and everything else the project needs. It will take a
few minutes — some packages (`sentence-transformers`) are large.

## 6. Get Your Credentials Ready

Before continuing, get these from whoever owns the project's accounts (never invent a
value, never reuse one from an old `.env` you find lying around, and never copy one out of
git history):

- An **OpenAI API key** (https://platform.openai.com/api-keys)
- A **Supabase project** — URL, service-role key, and DB connection string
  (https://supabase.com/dashboard — Project Settings → API, and Project Settings →
  Database → Connection string, **transaction pooler**, port 6543)
- (Optional, only if you'll work on the LINE integration) LINE channel secret + access
  token from the LINE Developers Console

## 7. Create Your `.env` File

```bash
copy .env.setup.example .env      # Windows
# cp .env.setup.example .env      # macOS/Linux
```

Open `.env` in a text editor and fill in every blank:

```
OPENAI_API_KEY=
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
SUPABASE_DB_URL=
ADMIN_PASSWORD=
SESSION_SECRET=
CREDENTIAL_ENCRYPTION_KEY=
```

For the two generated secrets, run these commands and paste the output in:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
# -> paste into SESSION_SECRET

python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# -> paste into CREDENTIAL_ENCRYPTION_KEY
```

**Never commit `.env`.** It's already in `.gitignore`.

That's the minimum. Everything else the app might use has a safe default — see
`.env.example` (the full, documented reference file) if you need something beyond the
basics (Google Drive sync, LINE, S3 storage, alternate storage backends, RAG tuning). Do
not copy `.env.example` itself as your `.env` — it documents many optional things you
almost certainly don't need on day one; `.env.setup.example` is the intentionally-minimal
starting point.

## 8. Set Up the Database

1. In the Supabase dashboard's SQL Editor, enable pgvector:
   ```sql
   create extension if not exists vector;
   ```
2. Run `supabase_setup.sql` (repo root) — the base schema.
3. Run every file in `migrations/` **in numeric order**, `001_...sql` through the
   highest-numbered file. Either paste each into the SQL Editor one at a time, or:
   ```bash
   for f in migrations/0*.sql; do psql "$SUPABASE_DB_URL" -f "$f"; done
   ```
4. Create a Storage bucket named `knowledge-files` (Storage → New bucket) — this matches
   the default `SUPABASE_STORAGE_BUCKET` value.

## 9. Run Preflight

This is **the one command** that tells you whether everything above actually worked:

```bash
python scripts/preflight.py
```

It checks, in order: Python version → dependencies installed → `.env` exists → every
required variable (empty/placeholder/invalid) → live connectivity to OpenAI, Supabase's
REST API, PostgreSQL, pgvector, your Storage bucket, the Credential Store, and (if
configured) LINE. It prints a clear `PASS`/`FAIL` report and tells you exactly which line
to fix next. **Do not proceed to step 10 until this says `PREFLIGHT — PASS`** (a few
`WARN`/`SKIP` lines are fine — only `FAIL` blocks you).

If something fails and the message isn't enough, see
[CONFIGURATION_ARCHITECTURE.md](CONFIGURATION_ARCHITECTURE.md) for what each variable
actually does and who consumes it, or `LEGACY_ENV.md` if you're wondering about a variable
name you found somewhere that doesn't seem to do anything.

## 10. Start the Admin App

```bash
python -m uvicorn admin.routes:app --reload --port 8001
```

## 11. Open the Admin Dashboard

Go to **http://localhost:8001/admin/login** in your browser (note: there is no page at
bare `/admin` — you must go to `/admin/login` specifically).

Log in with the `ADMIN_USERNAME` (defaults to `admin`) and the `ADMIN_PASSWORD` you set in
step 7. You should land on the Dashboard.

**You're done.** From here:
- Read [CLAUDE.md](CLAUDE.md) for the architecture, conventions, and platform-first
  engineering principles this repo follows.
- Read [docs/CURRENT_STATUS.md](docs/CURRENT_STATUS.md) and
  [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md) before starting new work.
- Run the test suite to confirm your local checkout matches the documented baseline:
  ```bash
  python -m pytest -q
  ```
  Expect **1592 passed, 2 failed** — both known, pre-existing, unrelated to setup (one
  needs a fix to `services/action_executor.py`'s pre-flight secret validation; the other
  needs a real Supabase-backed synonym/tag-vocabulary dataset). See `docs/CURRENT_STATUS.md`
  → Known Issues.

---

## Optional: Running the LINE Webhook

Only needed if you're testing actual LINE message delivery (requires a public HTTPS URL,
e.g. via `ngrok` during development):
```bash
python -m uvicorn line_bot.webhook:app --reload --port 8000
```

## Troubleshooting

- **`ModuleNotFoundError`** — the virtualenv isn't activated, or step 5 didn't complete.
  Re-run `pip install -r requirements.txt`.
- **`scripts/preflight.py` says a variable "looks like a placeholder"** — you copied
  `.env.setup.example`'s blank correctly, but the value you pasted matches a known example
  value (e.g. literally `sk-xxx`) — get a real credential per step 6.
- **Supabase connection errors** — double check you used the **service-role** key (not the
  anon key), and that `SUPABASE_DB_URL` uses the **transaction pooler** port (6543), not
  the direct-connection port.
- **pgvector dimension mismatch** — only relevant if you changed `OPENAI_EMBEDDING_MODEL`
  from the default; re-apply `migrations/019_openai_embedding_dimension.sql` with the
  matching `VECTOR(n)` width.
