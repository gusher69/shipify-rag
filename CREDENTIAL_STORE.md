# Secure Multi-Tenant Credential Store

Eliminates the need for one environment variable per customer API
secret. An admin can paste an API document/cURL, review the detected
secret, save it encrypted, test the API, and enable the Business
Action — no `.env` edit, no server restart.

## Architecture

```
Paste (API doc / cURL / plain description)
  -> services/secret_detector.py       (LOCAL detection, before any LLM call)
  -> services/ai_auto_setup_service.py (redacts, calls LLM, proposes config)
  -> Review (masked preview, save/select/skip)
  -> services/credential_store.py      (encrypts, stores, tenant-scoped)
  -> Business Action parameter: input_source=credential_store, credential_ref=<key>
  -> services/action_executor.py       (resolves + decrypts at request time only)
```

Before: `SecretCode` -> `FASTTRADE_AI_CHAT_SECRET_CODE` env var, one per
secret, requires a server restart to add/rotate.
After: `SecretCode` -> `credential_ref: fasttrade_erp_secret`, created/
rotated through the Admin UI/API, no restart, shareable across
Business Actions, tenant-isolated.

## Master Encryption Key

`config.CREDENTIAL_ENCRYPTION_KEY` (env var `CREDENTIAL_ENCRYPTION_KEY`)
— a Fernet key (`cryptography.fernet`, an existing project dependency;
no custom encryption was invented). Loaded once at call time from the
environment inside `services/credential_store.py::_get_fernet()`.
Never stored in the database, never returned by any API, never logged.
If unset or invalid, every encrypted-credential operation fails with a
structured `encryption_key_unavailable`/`encryption_key_invalid` error
— legacy `secret_configuration` (env var) parameters are completely
unaffected, since they never touch this key at all.

Generate one with:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Credential Data Model

Migration `030_credential_store.sql` — table `integration_credentials`:
`id, tenant_id, integration_id, credential_key, display_name,
credential_type, encrypted_value, masked_preview, metadata, status,
created_by, updated_by, created_at, updated_at, rotated_at,
last_used_at`. Unique on `(tenant_id, credential_key)`. Types: `api_key
| bearer_token | basic_auth | secret_code | username_password |
client_credentials | custom_header | custom_body_secret`. Status:
`enabled | disabled | revoked`.

`business_action_parameters` gets one additive column: `credential_ref`
(alongside the existing `secret_ref`, untouched).

`credential_audit_log` — safe audit trail (`credential_created`,
`credential_rotated`, `credential_disabled`, `credential_revoked`,
`credential_used`, `credential_tested`) — never records a raw or
encrypted value, only event metadata.

## Secret Detection (before any LLM call)

`services/secret_detector.py` — purely local/deterministic, **never
relies on the LLM to find a secret**. Detects `SecretCode`,
`Authorization`, `APIKey`, `Api-Key`, `X-API-Key`, `Token`,
`AccessToken`, `Bearer`, `Password`, `ClientSecret`, `private_key`,
`credential` in: cURL headers (`-H`), cURL form fields (`-d`/`-F`),
JSON bodies, URL-encoded bodies, query strings, HTTP Basic Auth, and
plain `Name: value`/`Name=value` documentation lines.

`services/ai_auto_setup_service.py::detect_and_redact_secrets()` runs
this detector, replaces each found value with `[REDACTED_SECRET_N]`
(preserving name/location so the LLM can still classify it correctly),
then runs the existing regex-based `redact_secrets()` as a defense-in-
depth second pass. **The raw value is never included in what's sent to
the LLM, logged, or returned from `analyze_capability()`** — only a
masked preview (`mask_value()`, last-4-chars only) plus a suggested
credential key/display name.

## Smart Setup Review (business-friendly labels)

For every detected secret, Review shows: **ข้อมูลเชื่อมต่อที่เป็นความลับ**
(name, location, masked value like `********8151`, a suggested
credential name), with three choices per the spec — **บันทึกอย่างปลอดภัย**
(save new), **ใช้ข้อมูลเชื่อมต่อเดิม** (select existing, populated live
from `GET /admin/api/credentials`), or skip. The raw value is typed
once into a password-masked field and is never shown again after that.
Legacy `secret_configuration` parameters are labeled **Legacy /
Infrastructure-managed**.

## Business Action Parameter Resolution Priority

`services/business_action_registry.py::resolve_secret_parameters()` /
`resolve_secret_parameter_errors()`, per parameter:
1. `input_source == "credential_store"` → `CredentialStore.resolve(tenant_id, credential_ref)`.
2. If that didn't resolve AND a legacy `secret_ref` is also present on the same row → environment variable.
3. Neither resolves → structured error (`credential_not_found` /
   `credential_disabled` / `credential_revoked` /
   `encryption_key_unavailable` / `environment_variable_not_set`).

This lets a parameter carry **both** `credential_ref` and `secret_ref`
during a migration window without ever needing the secret stored
twice — exactly how `customer_data_lookup` and `search_data_order` were
migrated (see below).

## Executor Integration

`services/action_executor.py` calls the same `resolve_secret_parameters()`
— decryption happens only at that point, the value lives in a local
Python dict for the duration of building the HTTP request, and is never
written to the Execution Result, logs, or the request preview (always
`[MASKED]`, never partial). Exception text has every resolved value
string-replaced with `[MASKED]` before it's returned. `last_used_at` is
updated by `CredentialStore.resolve()` itself.

## Tenant Isolation

Every `CredentialStore` method takes `tenant_id` explicitly; there is
no "list everything" method. Two tenants can both have a credential
keyed `erp_secret` — they are different rows (`UNIQUE(tenant_id,
credential_key)`), fully isolated: Tenant A can never list, resolve, or
see Tenant B's masked preview. Single-tenant deployments use
`config.DEFAULT_TENANT_ID` ("default") everywhere and never need to
think about this.

## Credential Lifecycle & Rotation

`create` (idempotent via `get_or_create` — Smart Setup never creates a
duplicate on repeated analysis of the same input) → `rotate`/
`update_value` (replaces `encrypted_value` and `masked_preview`, sets
`rotated_at`, **keeps the same `credential_key`** — no Business Action
needs to change) → `disable`/`enable`/`revoke` (blocks resolution
without deleting) → `delete_if_unused` (hard-delete, refuses if any
Business Action parameter still references it).

## Admin APIs

`GET/POST /admin/api/credentials`, `PUT|POST /admin/api/credentials/{key}/rotate`,
`POST .../disable`, `.../enable`, `.../revoke`, `POST .../test` (masked-preview
only, never the value), `DELETE /admin/api/credentials/{key}` (409 if
still referenced). **No endpoint anywhere returns `encrypted_value` or
a decrypted value** — list/get responses are metadata-only
(`display_name, credential_key, credential_type, masked_preview,
status, last_used_at`).

## FastTrade Migration

Both `customer_data_lookup` and `search_data_order`'s `SecretCode`
parameter now have `input_source=credential_store`,
`credential_ref=fasttrade_erp_secret` — sharing one credential
reference, never storing the secret twice. Their legacy `secret_ref
(FASTTRADE_AI_CHAT_SECRET_CODE)` is preserved as a fallback per the
resolution priority above. **No credential value has been created yet**
in this environment (no real FastTrade secret was ever configured, in
either form) — both actions honestly report `credential_not_found`
until an admin pastes the real value once via Smart Setup or
`POST /admin/api/credentials`.

## Enable Rules

`registry.validate_can_enable()` blocks Enable when a `credential_store`
parameter has no `credential_ref`, or the referenced credential doesn't
exist / belongs to another tenant / is disabled or revoked — in
addition to the existing endpoint/method/follow-up-question checks.
Save Draft is always allowed regardless.

## Security Limitations

- No OAuth authorization-code flow, no external cloud secret manager
  integration, no secret-value reveal UI — explicitly out of scope.
- The master key itself has no rotation mechanism in this task —
  rotating `CREDENTIAL_ENCRYPTION_KEY` would require re-encrypting every
  existing row (future work).
- `credential_audit_log` is a plain table with no retention policy —
  operators should define one for their compliance needs.

## Operational Backup Requirements

`integration_credentials.encrypted_value` is only ever decryptable with
the exact `CREDENTIAL_ENCRYPTION_KEY` that encrypted it. **Back up the
master key separately from the database, with the same rigor as any
other production secret** — losing it makes every stored credential
permanently unrecoverable (this is the correct, expected behavior of
authenticated encryption, not a bug).
