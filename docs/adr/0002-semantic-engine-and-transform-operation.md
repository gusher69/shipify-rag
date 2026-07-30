# ADR 0001 — Semantic API Analysis Engine, TRANSFORM as a First-Class Runtime Operation, and the Review & Edit UI

Date: 2026-07-29 / 2026-07-30
Status: Accepted

## Context

The AI-Guided ERP Setup wizard (`services/ai_auto_setup_service.py`) had three separate, sometimes-disagreeing classification systems in play for the same Business Action:

1. A **hardcoded, per-domain regex taxonomy** (`_INTENT_TAXONOMY`/`_SYNONYM_TABLE`) — endpoint-specific patterns like `customer_lookup`, `order_lookup`, `wallet`, keyed to literal domain words. This directly violated the platform's "onboarding a new API is configuration, not code" principle: a genuinely new vendor/domain required new regex, not new configuration.
2. The **LLM's own free-form** `action_type`/`detected_action_type` output during `analyze_capability()` — non-deterministic (temperature > 0), unconstrained vocabulary, no confidence score.
3. `services/erp_test_harness.py::infer_operation_type_with_evidence()` — an already-built, deterministic, evidence-ranked runtime classifier used by the Decision Engine/Conversation defaults, but never consulted at setup time at all.

Symptom: "the semantic analysis is still heuristic-based and produces inconsistent classifications" (user report), plus a concrete misclassification — `GetUrlProductDetail` (a URL→product-detail conversion endpoint) was forced into either a hardcoded `product_lookup`-family label or the runtime's `UNKNOWN`/`LOOKUP`, because neither existing vocabulary had a genuine "transform/convert" concept.

## Problem

Design a single, deterministic, product-agnostic classification engine that:
- Works for an arbitrary REST API description with zero endpoint-specific code.
- Never hardcodes a field name (`CustCode`, `OrderNo`, etc.) or a domain word in actual logic.
- Produces the same output for the same input every time (no LLM call inside the classifier itself).
- Distinguishes "convert/parse/extract/derive information from an existing payload" (TRANSFORM) from "compute a value" (CALCULATION) — these are semantically different operations and must never share a runtime type.
- Lets an admin review, understand, and safely override the detected classification before saving, without a re-analysis silently discarding their override.

## Decision

1. **New module `services/semantic_api_analysis_engine.py`** — the ONE canonical, deterministic classifier for setup-time analysis (Steps 1-9/11/12): endpoint intent (14-value vocabulary: LOOKUP/SEARCH/LIST/DETAIL/TRANSFORM/COMMAND/NOTIFICATION/MUTATION/UPLOAD/DOWNLOAD/AUTHENTICATION/HEALTHCHECK/UTILITY/UNKNOWN), field role classification (multi-role: authentication/identifier/search/filter/date_start/date_end/enum/limit/pagination/message/url/credential/...), validation-group inference, operation safety, response-shape analysis, severity-tagged recommendations. Pure regex/structural pattern matching only — verified by a determinism test (10 repeated calls, byte-identical output).
2. **Removed** `_INTENT_TAXONOMY`/`_SYNONYM_TABLE` entirely — not extended, not kept alongside. `classify_intent()` now composes `erp_test_harness.detect_entities()` (already generic) + the new engine's `classify_endpoint_intent()`.
3. **`erp_test_harness.py`'s own runtime classifier was NOT touched or unified with the new engine.** Its vocabulary/tests/Decision-Engine-adjacent behavior have a large blast radius (dozens of dependent tests, the whole Conversation Form Generator/Strategy Engine cascade). Instead, `map_to_runtime_operation_type()` bridges the setup-time engine's richer vocabulary onto the runtime's existing vocabulary, and the bridge value flows into the runtime through the **already-existing** `setup_metadata.operation_type` override tier (`infer_operation_type_with_evidence()` tier 2) — zero code changes to the runtime classifier.
4. **TRANSFORM promoted to a first-class value in the runtime's own vocabulary** (`erp_test_harness.OPERATION_TYPES`), appended at the end (never reordering/removing existing values). It was initially mapped to `CALCULATION` as a stopgap; this was corrected once the semantic distinction was raised explicitly — `map_to_runtime_operation_type("TRANSFORM")` is now an identity mapping, with its own `_CONVERSATION_DEFAULTS_BY_OPERATION_TYPE` entry (a sibling dict to `CALCULATION`'s, not aliased, so the two can diverge independently later).
5. **Detected → Override → Effective precedence** (Step 11) reuses the *exact* 3-layer precedent already established by `integration_schema_service.py::resolve_effective_integration_schema()`, applied to per-field/per-section semantic analysis instead of conversation behavior. `apply_overrides()` is the single place this merge happens — both the Review & Edit UI's live preview (`POST /ai-auto-setup/semantic-overrides/apply`) and the save route call it, so there is no second, drifting implementation in JavaScript.
6. **Review & Edit UI** (`admin/templates/business_actions.html`) surfaces the engine's output in the *actual live* rendering path (`baAiRenderSummaryCard()`/`#ba-ai-summary-card` — see Known Tech Debt in `PROJECT_STATE.md` for why an older, dead `baAiRenderReview()`/`#ba-ai-review` function still exists unused). Authentication/credential field values are never displayed, only their names.

## Alternatives Considered

- **Extend `infer_operation_type_with_evidence()`'s existing vocabulary in place** instead of a separate setup-time engine. Rejected: its 12-value vocabulary and `LOOKUP_LIKE_OPERATION_TYPES`/`_DESTRUCTIVE_OPERATION_TYPES` gating logic are depended on by the Decision Engine, Conversation Form Generator, and dozens of existing tests — changing its meaning risks silently breaking already-verified runtime behavior for every existing Business Action.
- **Map TRANSFORM to CALCULATION permanently** (the first, quicker fix). Rejected once raised explicitly: "Calculation implies computing a value. Transformation means converting, parsing, extracting, resolving, or deriving information from an existing payload" — these are genuinely different operations for downstream consumers (Prompt Builder wording, confirmation/audit defaults, future Multi-ERP orchestration semantics).
- **A single unified vocabulary shared literally between setup-time and runtime.** Rejected: would require either a breaking rename of the runtime's existing values (blast radius) or forcing the richer setup-time distinctions (DETAIL vs LOOKUP, COMMAND vs MUTATION) into the runtime where they aren't currently needed.

## Trade-offs

- Two vocabularies (setup-time's 14 values, runtime's 14 values) that are *similar but not identical*, bridged by one explicit mapping function, is more moving parts than a single enum — accepted in exchange for zero blast radius on the tested runtime classifier.
- `apply_overrides()`'s per-field override storage (`setup_metadata.semantic_overrides`) is a second additive JSONB blob alongside `setup_metadata.semantic_analysis` and `setup_metadata.operation_type` — no new DB column/table was introduced, consistent with the project's existing `setup_metadata.*` convention (routing, detection_reason, conversation_behavior all already live there).
- The engine's field-role heuristics (regex/structural) are inherently imperfect for a truly novel API shape — Step 11's override mechanism exists precisely so an admin corrects, not so the engine never needs correcting. A validation-groups weak-fallback bug (a credential field wrongly bucketed as a substitutable search criterion) was found and fixed during manual UI verification of this exact ADR's feature — see `tests/test_semantic_review_edit_ui.py::TestValidationGroupsNeverIncludeCredentials`.

## Consequences

- Any NEW REST API can be onboarded with zero analyzer code changes (the acceptance criterion this whole sprint was scoped against).
- `classify_intent()`'s `intent_id` string shape changed for existing fixtures (e.g. `tracking_lookup`/`tracking_command` instead of a bare `tracking`) — a disclosed, intentional side effect of removing the hardcoded taxonomy. Any other consumer relying on the OLD exact id strings should be spot-checked (none found as of this ADR).
- `erp_test_harness.OPERATION_TYPES` now has 14 recognized values instead of 13 — any UI/doc/test that enumerated the old 13-value list by hand needed (and got, in this same sprint) an update: `admin/templates/integration_schema_studio.html`'s Operation Type dropdown now loads dynamically from `GET /admin/api/integration-contracts/operations` instead of a hardcoded array, and the Review & Edit panel's dropdowns load dynamically from the same route / from `GET /ai-auto-setup/semantic-endpoint-intents`.

## Migration

No DB migration was required — verified directly against the live database that **zero** existing rows had `setup_metadata.operation_type == "CALCULATION"` or a published/draft schema with `general.operation_type == "CALCULATION"` before this change, so there was nothing to reclassify. If a future environment ever does have such a row and it's genuinely a TRANSFORM mis-tagged as CALCULATION, that is a per-row admin correction via the Review & Edit UI's override (or a direct `setup_metadata.operation_type` edit) — not a blanket automated migration, per this ADR's explicit "do not silently convert existing records" constraint.

## Future Evolution

- Extend `apply_overrides()`/the Review & Edit panel with a dedicated Field Roles / Entity Mapping editing control (currently backend-supported via `field_overrides` but with no UI widget).
- Consider whether `map_to_runtime_operation_type()`'s bridge table itself should become admin-configurable per-tenant if a customer's own operational taxonomy diverges from the current 1:1-ish mapping.
- The dead `baAiRenderReview()`/`#ba-ai-review` code path (see `PROJECT_STATE.md` Known Tech Debt) should be removed in a dedicated cleanup sprint once every call site has been re-verified live, rather than as a side effect of this handover.
