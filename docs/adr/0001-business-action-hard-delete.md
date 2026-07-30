# ADR — Business Action Deletion: Soft-Delete → Permanent Hard Delete

Date: 2026-07-29
Status: Accepted

## Context

`business_actions` originally used a soft-delete convention (`deleted_at` timestamp; `get_by_key()`/`get()` filtered `deleted_at IS NULL`). A real production incident occurred: a customer tried to onboard a genuinely new API under the key `getdatacustomer` and got "Business Action Already Exists," even though no visible, active action used that key.

## Problem

Postgres's unique constraint on `action_key` applies regardless of soft-delete status — `get_by_key()` filters it out, but the raw unique index does not. Root cause: a stale, never-configured, AI-auto-created draft had been soft-deleted earlier and left `deleted_at` set but the row (and the unique key) still present, permanently blocking any future action from reusing that key. The generic "duplicate" dialog also didn't distinguish a real published collision from a harmless stale draft from a genuinely-restorable soft-deleted row — same UI regardless of which.

## Decision

1. Convert Business Action deletion from soft-delete to a genuine physical `DELETE`, relying on Postgres's own `ON DELETE CASCADE` FKs (verified directly against live `information_schema`/`pg_attribute`/`pg_constraint`, not just the hardcoded `_DEPENDENT_TABLES` list) across 9 dependent tables for atomic cleanup — Postgres's FK cascade IS the transaction; the Supabase REST layer doesn't expose a manual client-side multi-table transaction anyway.
2. Tiered delete confirmation in the UI: Draft (single confirmation) vs. Published/has-execution-history (stronger confirmation explaining full permanent removal) vs. protected fixture categories (`Internal Test Fixtures` — blocked entirely without an explicit `force=True`, which is reachable ONLY via direct Python/registry calls, never through any public HTTP route — a real security gap in the original route, which accepted `?force=true` with no privilege gate, was found and fixed as part of this same change).
3. Differentiated duplicate-resolution dialog by state: Published → Update Existing/Save As New/Rename; Draft → Continue Editing/Discard/Rename; Soft Deleted (legacy rows created before this change) → Restore/Delete Permanently/Create New.
4. New `business_action_audit_log` table (migration 034, additive-only, mirrors the existing `credential_audit_log` shape/RLS convention) — every hard delete writes one immutable record (`action_id`, `action_key`, event, actor, detail JSONB with previous status/dependent counts/whether forced past protection). Never stores secrets/credentials/request-response payloads. A write failure never blocks the already-completed delete — it surfaces as `audit_log_warning` in the API response instead (stricter than `credential_audit_log`'s silent-swallow precedent, per explicit requirement).

## Alternatives Considered

- **Keep soft-delete, fix only the duplicate-check to look past `deleted_at`.** Rejected: doesn't solve the underlying problem that a customer's intended "delete" doesn't actually free up the key for reuse, which is what the report was actually about — restoring the ability to genuinely delete something.
- **Add a periodic cleanup job for soft-deleted rows instead of hard-delete.** Rejected: still leaves a window where a fresh onboarding attempt collides with a not-yet-cleaned-up row, and adds a new scheduled-job dependency for a problem a direct `DELETE` solves immediately.

## Trade-offs

- Hard delete is irreversible — there is no "recycle bin" anymore. Mitigated by the tiered confirmation dialog (explicit "Delete permanently" wording) and the protected-fixture gate for anything the platform itself depends on.
- Relying on `ON DELETE CASCADE` instead of a manual transaction means the *only* atomicity guarantee is whatever Postgres's own FK cascade provides — verified live rather than assumed, but this is a dependency on the DB schema staying in sync with `_DEPENDENT_TABLES`; a future new dependent table must add both the FK (`ON DELETE CASCADE`) and the constant, or orphan detection (see below) will catch it as a `RuntimeError` rather than silently missing it.

## Consequences

- `services/business_action_registry.py::delete(action_id, *, hard=False)` (the old soft-delete method) is kept only as a DEPRECATED fallback for any caller still wanting it — `hard_delete_action()` is the real, current path.
- `admin/routes.py`'s `DELETE /admin/api/business-actions/{id}` route no longer accepts a `force` query param at all (removed entirely, not just gated) — the only way to force-delete a protected fixture is a direct Python call, by design.

## Migration

`migrations/034_business_action_audit_log.sql` — additive-only, no existing table altered. Applied to the live database directly by the project owner (not by an agent), then independently verified (table/RLS/policies/indexes exist; a disposable non-protected action created+deleted through the real flow produced exactly one audit record with all required fields and no secrets; the deleted action was confirmed fully unresolvable/unexecutable/schema-less/contract-less/cache-clean/key-reusable).

## Future Evolution

- If a genuine "trash/restore" UX need re-emerges, it should be a NEW, explicit feature (e.g. an export-before-delete, or a time-boxed retention table) rather than reintroducing the old `deleted_at` convention this ADR deliberately removed.
