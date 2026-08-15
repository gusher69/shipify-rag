# Configuration Architecture

_Canonical reference, written 2026-08-01 at the end of a multi-pass configuration
hardening/cleanup effort. This document explains how every piece of configuration
tooling in this repo fits together — read this before touching `config.py`, `.env`,
the Settings UI, or any of the `scripts/` tooling described below._

---

## The Pieces, and What Each One Is For

```
.env.setup.example  ──copy──►  .env  ◄──edited by──  Admin UI (Settings page)
       │                         │
       │                         ├──loaded by──► config.py ──imported by──► every module
       │                         │
.env.example (full reference)    └──checked by──► scripts/validate_env.py
                                                          ▲
                                                          │composed by
                                                  scripts/preflight.py
                                                  (the one command new machines run)
```

### `.env.setup.example`
The **smallest possible** set of variables a developer must fill in before the Admin app
will start and behave correctly (7 lines: OpenAI key, 3 Supabase values, admin password,
2 generated secrets). Nothing optional lives here — if it has a safe default, it's not in
this file. This is what `SETUP_NEW_MACHINE.md` tells a brand-new developer to copy to
`.env` first.

### `.env.example`
The **complete, documented reference** for every environment variable the codebase
actually reads — organized into 7 numbered sections (Application, Database,
Authentication, LINE, Google Drive, Storage, AI/RAG Runtime Tuning), each line verified
against a real consumer in the code (never guessed). Every variable is tagged
`[REQUIRED]`, `[OPTIONAL]`, `[GENERATED]`, or `[SETTINGS]` inline. This is the file you
consult when you need something beyond the setup minimum — a Google Drive sync, a
different storage backend, LINE Notify, retrieval tuning.

Anything that was ever real but no longer has a live consumer is **not** in this file — it
moved to `LEGACY_ENV.md` (see below) with the reason it moved, so nothing silently
vanishes without explanation.

### `LEGACY_ENV.md`
The graveyard, with obituaries. Every variable that was removed from runtime code, kept
only for backward compatibility, or never actually implemented — each with the exact
reason and verification method (never "probably unused," always "confirmed zero
importers via repo-wide search"). If you inherit an old `.env` with a variable you don't
recognize, or find a stale reference in an old doc, check here before assuming it matters.

### `scripts/validate_env.py`
A standalone, importable Python module that runs a **complete environment health check**:
required-variable presence/placeholder/shape validation, plus live connectivity checks
against OpenAI, Supabase's REST API, PostgreSQL (direct `psycopg2` connection), pgvector
(extension + `knowledge_chunks.embedding`'s actual column width), the configured Storage
bucket, the Credential Store's master key + database table, and (if configured) the LINE
Messaging API. Every check degrades gracefully: a check whose prerequisite value is
missing or placeholder-shaped is `SKIP`ped (never attempts a network call with a
known-fake credential); only a genuine, confirmed problem is `FAIL`. Never prints a real
secret value — only whether one looks configured. Run directly (`python
scripts/validate_env.py`) or imported (`run_all_checks()`, `format_report()`).

**Deliberately not wired into either FastAPI app's startup** — running it never changes
what the real app does when it boots (see "Restart Required vs. Live" below for the one
exception that already existed before this cleanup: the embedding-dimension check, which
`validate_env.py` reuses rather than duplicates).

### `scripts/preflight.py`
The **single command** `SETUP_NEW_MACHINE.md` tells a new developer to run before starting
work: `python scripts/preflight.py`. It checks Python version and key dependencies are
importable, that `.env` exists at all, then delegates to `validate_env.py`'s full check —
composing it, not reimplementing it. Ends with one unambiguous verdict
(`PREFLIGHT — PASS` or `PREFLIGHT — FAIL`) and, on success, the exact command to start the
Admin app.

### Settings UI (`/admin/settings`)
A subset of `.env`-backed configuration can also be edited from the Admin UI after first
boot — OpenAI key/chat model/embedding model, Supabase URL/keys, LINE credentials, admin
username/password, and Developer Mode. Each field carries an inline badge (see "Runtime
Config" below) showing its own category. The Settings page writes directly to `.env`
(`admin/routes.py`'s `_read_env()`/`_write_env()`) — there is no second, separate storage
layer for these values.

### Retrieval Settings (a different, DB-backed system)
Not `.env`-backed at all: search strategy, top-k, ranking weights, and reranker choice
live in `services/retrieval_settings.py`, persisted in the `rag_retrieval_settings` table
(migration 021), edited from the Settings page's separate **Retrieval Settings** card.
This is genuinely live — no restart, ever — because nothing here is cached in a Python
module constant. The handful of `RAG_*`-v2 environment variables in `.env.example`'s
section 7 are this subsystem's file-level *default*, consulted only before any database
row exists.

---

## Runtime Config: Restart Required vs. Live

This is the single most important distinction for anyone editing configuration on a
running system, and it comes down to **how** a value is read, not where it's stored:

| Pattern | Behavior |
|---|---|
| `config.py` computes a module-level constant via `os.getenv(...)` at import time | **Restart required.** The constant is frozen the moment `config.py` is first imported into the running process. Editing `.env` (by hand or via Settings) changes the file; the already-running process keeps using the old value until it restarts. |
| A value is further cached in a singleton (`admin/routes.py::get_sb()`, `services/embedding_service.py`'s provider singleton, `services/llm_service.py::get_llm_service()`) | **Restart required, doubly so** — even if you could somehow force `config.py` to re-read the env var, the singleton built from the old value would still be cached. |
| A module reads `os.getenv(...)` directly, at **call time**, bypassing `config.py` entirely | **Live — no restart.** `services/developer_mode.py` is the one example: `is_developer_mode_enabled()` calls `os.getenv("DEVELOPER_MODE", ...)` fresh on every single check. This is why toggling Developer Mode in Settings takes effect immediately while every other `.env`-backed Settings field does not. |
| A value lives in a database table, read through a small in-process cache that's explicitly invalidated on save | **Live — no restart.** Retrieval Settings works this way: `get_active_settings()` checks the DB first, and `save_settings()` updates the cache in the same call. |

**Practical rule of thumb:** if the Settings page shows a "Restart Required" badge next to
a field (see below), saving it does nothing observable until the Admin process is
stopped and started again. This is a known, accepted piece of Technical Debt, not a bug —
see `docs/NEXT_STEPS.md` for the tracked follow-up (either add a visible restart notice, or
make the relevant modules re-resolve per call like `developer_mode.py` already does).

### The three Settings-page badges, precisely

| Badge | Means | Example |
|---|---|---|
| **Live** | Takes effect immediately, no restart | `DEVELOPER_MODE`, the entire Retrieval Settings card |
| **Restart Required** | Written to `.env` immediately, but the running process won't see it until restarted | `OPENAI_CHAT_MODEL`, `OPENAI_EMBEDDING_MODEL`, `SUPABASE_URL`, `ADMIN_USERNAME` |
| **Secret** | Sensitive credential — classified by sensitivity first, regardless of reload timing (most secrets here *also* require a restart, noted individually) | `OPENAI_API_KEY`, `SUPABASE_SERVICE_KEY`, `LINE_CHANNEL_SECRET`, `ADMIN_PASSWORD` |

The full field-by-field classification lives in `admin/routes.py::SETTINGS_FIELD_METADATA`
— one dictionary, one source of truth, so the badges can never silently drift out of sync
with a variable's actual behavior.

---

## Credential Store — the fourth storage location

Everything above is either a `.env` file or a settings-style database table. The
**Credential Store** (`services/credential_store.py`, table `integration_credentials`) is
a fourth, distinct mechanism: Fernet-encrypted, multi-tenant secret storage specifically
for **Business Action** (ERP/API integration) credentials — never for the platform's own
`.env`-backed secrets above.

- Master key: `CREDENTIAL_ENCRYPTION_KEY` (a `.env` value, generated once, never rotated
  casually — rotating it would make every already-encrypted credential unreadable).
- Every credential value is encrypted at rest; the plaintext is never logged, never
  returned in an API response, never shown in the Admin UI after creation (masked
  previews only).
- A Business Action can *also* reference a plain `.env` variable by name instead (the
  legacy `secret_configuration` mechanism, `services/business_action_registry.py::
  resolve_secret_ref()`) — this still works with zero encryption and no
  `CREDENTIAL_ENCRYPTION_KEY` dependency, but has no audit trail, no rotation support, and
  the raw value sits in plaintext in `.env`. New Business Actions should prefer the
  Credential Store.
- **Recommendation, not yet applied:** `LINE_CHANNEL_SECRET`/`LINE_CHANNEL_TOKEN`/
  `LINE_NOTIFY_TOKEN`, S3 credentials, and the Google Service Account JSON are all
  currently plain `.env` values with none of the Credential Store's protections. Moving
  them would be an architecture change to how `line_bot/webhook.py` and `storage/
  factory.py` resolve secrets — out of scope for a configuration-management-only pass,
  flagged here for a future, deliberately-scoped sprint.

---

## Putting It Together: the New-Machine Flow

1. `SETUP_NEW_MACHINE.md` walks a new developer from "install Git" to "logged into the
   Dashboard."
2. Step 7 of that guide has them copy `.env.setup.example` → `.env` and fill in the true
   minimum.
3. Step 9 has them run `scripts/preflight.py`, which composes `scripts/validate_env.py`'s
   complete health check and refuses to declare victory until every required variable is
   present, valid-shaped, and (where a live check exists) actually reachable.
4. Only after `PREFLIGHT — PASS` do they start the Admin app and open
   `/admin/login`.
5. Once running, `.env.example` and this document are the reference for anything beyond
   day-one setup; `LEGACY_ENV.md` is the reference for "why doesn't this variable I found
   somewhere do anything."

Nothing in this document changes what the running application does — it is entirely a map
of tooling and file responsibilities that already exist, written down in one place so the
next developer (human or AI) doesn't have to reverse-engineer it from six different files.
