# AI Middleware Decision Engine v2

The central orchestration layer. It **decides**; it never executes an API
directly. Implementation: `services/decision_engine.py::DecisionEngine`.

This task only **orchestrates existing, frozen components** — AI Core,
the Information Collection Engine, Contextual Slot Binding, RAG
Retrieval, Prompt Studio, AI Policies, Benchmark, the Business Action
Registry, and the Generic Action Executor are all used exactly as they
already exist. Nothing in any of those modules was modified.

## Architecture

```
Channel Adapter (LINE / Playground / future channels)
        ↓
AI Middleware Decision Engine   (services/decision_engine.py — NEW)
        ↓
Conversation State              (recomputed from `history`, same convention
        ↓                        every other module in this codebase uses —
Intent Resolution                no separate persistence table)
        ↓                        (rag/query_understanding.py::classify_actionable_intent
        ↓                         + services/slot_filling_engine.py::resolve_active_erp_intent
        ↓                         — both existing, both read-only here)
Workflow Resolution
        ↓
Information Collection Engine   (services/slot_filling_engine.py — existing,
        ↓                        never duplicated)
Business Action Registry        (services/business_action_registry.py — existing)
        ↓
Generic Action Executor         (services/action_executor.py — existing)
        ↓
Response Builder                (inside decision_engine.py — new, unifies
        ↓                        every routing type into one reply shape)
Channel Adapter
```

## Single Source of Truth (Architecture Refactor)

**As of this refactor, the Business Action Registry — not `INTENT_SCHEMAS` — is the authoritative source of required parameters, required groups, optional parameters, and validation rules for any Business Action that has parameter metadata configured.** This closes a real inconsistency risk: previously, a workflow's required slots (`services/slot_filling_engine.py::INTENT_SCHEMAS`) and a Business Action's own registered parameters (`business_action_parameters`/`parameter_groups`) were two independently-maintained definitions that could silently disagree — the Information Collection Engine could report "complete" while the Executor's own `validate_can_execute()` then rejected the call as missing a parameter it never knew to ask about.

**The fix:** both the Information Collection step and the eventual Executor call the exact same `registry.validate_can_execute(action_id, collected)` (existing, unmodified, in `services/business_action_registry.py`). Because it's the identical function against the identical stored configuration, the Executor can no longer discover a missing required parameter the Decision Engine believed was already complete — by construction, not by convention.

`INTENT_SCHEMAS` is **kept, but demoted** — its remaining job is: (a) intent classification / workflow-hint detection (`resolve_active_erp_intent`), and (b) a **legacy fallback** for any Business Action that has no parameter metadata configured yet (Safe Migration — see below). Its own `required_groups` are no longer authoritative for any Business Action that has its own parameters configured.

## New Execution Flow

```
Old:  Intent → Information Collection (INTENT_SCHEMAS) → Business Action → Executor
New:  Intent → Search Candidate Business Actions → Select Best Business Action
            → Read Business Action Parameters → Information Collection → Execute
```

Selection now happens **before** Information Collection — the Registry's own parameter definitions drive what gets asked, not a separately-maintained schema keyed by intent name.

## Conversation Flow (per turn)

1. Read the current message + `history` (list of `{"role","content"}` dicts — caller-supplied, no new DB table).
2. Check for an explicit human request (`services/slot_filling_engine.py::_HUMAN_REQUEST_RE`) — routes straight to Human Handoff.
3. Resolve **intent only** — `classify_actionable_intent()` (customer intent) and `resolve_active_erp_intent()` (a workflow *hint*, still one of the 6 legacy names, used purely to narrow/score candidates and for Developer Mode display — **it no longer defines required slots**).
4. Resolve **conversation continuation**: if the LAST assistant turn is exactly the question some enabled Business Action would currently ask (Registry-driven, see below), that action is still active this turn — no separate persistence table, purely recomputed from `history`.
5. Otherwise, **search candidate Business Actions** (`search_candidate_actions()`, any type — RAG/TOOL/API/etc.) and **select the best one**.
6. If none found and a workflow hint IS set → **Safe Migration**: legacy `INTENT_SCHEMAS`-driven collection (unchanged behavior from before this refactor).
7. If the selected action is `API`/`WEBHOOK`:
   - If it has **no parameter metadata** configured and a workflow hint is set → **Safe Migration**: legacy fallback, tagged in Developer Mode with a warning.
   - If it **has parameter metadata** → **dynamic, Registry-driven Information Collection** (see below).
8. Otherwise (`RAG`/`TOOL`/`NOTIFICATION`/`HUMAN_HANDOFF`/`WORKFLOW`, or an API/WEBHOOK action needing nothing) → execute directly.
9. If nothing matches at all → Safe Fallback.

### Dynamic, Business-Action-Driven Information Collection

For a selected Business Action with parameter metadata:

1. **Read Business Action Parameters** — `action.parameters` (name, display_name, required, validation_type, input_source, description) and `action.parameter_groups` (`ALL`/`AT_LEAST_ONE`/`EXACTLY_ONE`/`OPTIONAL`).
2. **Reconstruct Collected Parameters from `history`** (`_replay_business_action_collection`) — same "no persistence table" convention as everything else in this codebase: walks prior turns, and whenever a prior assistant turn's text matches EXACTLY the question this engine would generate for whatever parameter was next-expected at that point, the following user turn is bound as that parameter's value.
3. **Bind the CURRENT message** (`_bind_message_to_action`) — for an ungrouped missing required parameter, binds against that parameter only; for a still-unsatisfied group, tries every askable member (a customer may spontaneously answer with any one alternative, e.g. email instead of customer code) — members with a **specific** `validation_type` (email/phone_number) are tried before a permissive catch-all (`non_empty`/unset), so a loosely-validated sibling never greedily swallows a value meant for a stricter one.
4. **Check completion** via `registry.validate_can_execute(action_id, collected)` — the SAME function the Executor calls.
5. If incomplete → **auto-generate the follow-up question** from the next missing parameter's `display_name` (`"กรุณาแจ้ง{display_name}ค่ะ"`), **unless** the parameter's own `description` field already holds custom text (an admin-configured manual override) — then that text is used verbatim.
6. If complete → execute via the Generic Action Executor with `collected_slots=<the reconstructed dict>`.

Escalation (repeated failure / explicit refusal) reuses `services/slot_filling_engine.py::_REFUSAL_RE` (never redefined) and reads `max_retry`/`message` from the Business Action's own existing `retry_rules`/`escalation_rules` JSONB columns — no new schema.

### Conversation Continuation Example (Registry-driven)

```
User: "เช็ค PO"
  → no ERP workflow keyword hit → generic search → selects "SearchDataOrder"
    (has parameters: CustCode, OrderCode, both required)
  → Information Collection: 0/2 collected → asks "กรุณาแจ้งรหัสลูกค้าค่ะ" (from CustCode's display_name)

User: "C00001"
  → _resolve_continuation_action: last assistant turn == the question
    SearchDataOrder would currently ask → same action stays active
  → binds "C00001" to CustCode → 1/2 collected → asks "กรุณาแจ้งเลขคำสั่งซื้อ (PO)ค่ะ"

User: "PO-99887"
  → binds to OrderCode → validate_can_execute → ok=True
  → ActionExecutor.execute(...) → Response
```

**Known limitation (flagged, with a regression test):** two unrelated Business Actions can legitimately generate the *identical* auto-question (e.g. both have a `CustCode` parameter with the same Display Name). When more than one match is found, continuation resolution breaks the tie using (1) the workflow hint's category, then (2) a keyword/category score against the conversation's original triggering message — never an arbitrary "whichever the registry returned first" order.

## Business Action Selection Strategy

`services/decision_engine.py::search_candidate_actions()` — generic, reusable scoring, **never** an `if tracking:` / `if customer:` branch:

| Signal | Weight | Status |
|---|---|---|
| `category` == active workflow hint | +3.0 | implemented |
| Keyword / example-question / AI-description overlap with the message | variable | implemented (plain substring matching, deterministic) |
| Parameter names overlap with already-collected slots | +1 per match | implemented |
| `priority` (tiebreaker only) | +0.01×priority | implemented |
| Semantic score (`_semantic_score`) | +0.0 | **interface only** — always returns 0.0 |
| Embedding score (`_embedding_score`) | +0.0 | **interface only** — always returns 0.0, reads the same `business_action_embeddings.embedding_source_text` the Business Action Center already prepares |

`select_best_action()` picks the top-scored **enabled** candidate above a minimum threshold (stricter for generic/non-workflow routing, to avoid a stray keyword hit hijacking an unrelated conversation).

## Executor Integration

Every routing type maps 1:1 to an existing `services/action_executor.py` adapter — the Decision Engine's job ends at `self.executor.execute(action_id, context)`:

| Business Action Type | Decision Engine behavior |
|---|---|
| `API` / `WEBHOOK` | Execute; on success, summarize `mapped_fields` (or raw result) into the reply; on error, structured Thai error message |
| `RAG` | Execute; if the returned answer is empty, treat as **Safe Fallback**, never invent an answer |
| `TOOL` | Execute; summarize result generically (works for the calculator, the URL converter, and any future tool) |
| `NOTIFICATION` | Execute (returns `status="not_implemented"` today — interface only); Decision Engine still attaches Alert metadata |
| `HUMAN_HANDOFF` | Execute (returns `status="handoff_prepared"`); Decision Engine surfaces the handoff payload, does not notify anyone |
| `WORKFLOW` | Execute (returns `status="not_implemented"` — placeholder) |

## Safe Fallback

If no Business Action matches well enough:
1. Look for an **enabled RAG-type Business Action** and execute it.
2. If none is registered, fall back to calling `services/rag_service.py::get_rag_service()` directly (read-only reuse — retrieval itself is never modified).
3. If RAG returns nothing, return a fixed, honest "ไม่พบคำตอบที่ชัดเจน" message. **Never a synthesized/hallucinated answer** — the Decision Engine does not call an LLM to fabricate a response; it only ever surfaces retrieved, grounded content or an honest fallback.

## Human Handoff

Triggered by: (a) explicit customer request (regex, existing `_HUMAN_REQUEST_RE`), (b) Information Collection Engine escalation (repeated failure / refusal / max retry — existing logic, untouched), (c) selection of a configured `HUMAN_HANDOFF`-type Business Action. In every case the Decision Engine calls the Generic Action Executor's handoff adapter (which prepares a payload and notifies no one — that remains future work) and **decides whether to surface it**, never notifies directly itself.

## Alerts

Deterministic, small keyword vocabulary (`_COMPLAINT_RE`, `_LEGAL_THREAT_RE`) plus a VIP-customer-context check — attaches `{"alert_type", "priority"}` metadata to the response regardless of routing outcome (RAG answer, Business Action execution, or Safe Fallback all still carry the alert if the message warrants one). **No Notification Adapter exists** — this is explicitly future work; the alert is structured metadata only.

## Developer Mode

When `context["developer_mode"]` is true, the response includes a `developer` block: `intent`, `workflow`, `selection_source` (`"conversation_continuation"` or `"fresh_search"`), `information_collection_status`, `candidate_business_actions` (top 5, with score + reasons), `selected_business_action`, `selection_reason`, `executor_type`, `execution_result`, `latency_ms`, and — only when the legacy path is used — `legacy_fallback_used`/`legacy_fallback_reason`/`legacy_fallback_warning`. Nothing here is ever shown to the customer.

For a dynamic (Registry-driven) collection turn, `information_collection_status` is:
```python
{
  "source": "business_action_registry",
  "selected_business_action": str,
  "required_parameters": [str, ...],       # ungrouped required parameter names
  "parameter_groups": [{"name","rule","members"}, ...],
  "collected_parameters": {name: value, ...},
  "missing_parameters": [str, ...],         # missing required names + unsatisfied group names
  "ambiguous_candidates": [str, ...],
  "is_complete": bool,
  "completion_reason": str | None,
  "missing_reason": "ambiguous_candidate" | "awaiting_customer_input" | None,
  "retry_count": int, "max_retry": int,
}
```

## Error Handling

- Business Action Registry failure (e.g. DB down) → caught in `search_candidate_actions()` → empty candidate list → Safe Fallback. Never an unhandled exception.
- Generic Action Executor failure → the Executor itself never raises (per its own contract); if it somehow does, the Decision Engine still catches it and returns a structured `{"status": "error", ...}`.
- Any other unexpected exception inside `DecisionEngine.decide()` → caught at the top level → Safe Fallback response with a sanitized (never raw-exception) developer trace.

## Response Shape (Response Builder)

```python
{
  "reply": {"text": str, "message_parts": None, "buttons": [], "quick_replies": [], "images": [], "files": []},
  "routing": {"type": "RAG"|"API"|"TOOL"|"WORKFLOW"|"NOTIFICATION"|"HUMAN_HANDOFF"|"WEBHOOK"|"SAFE_FALLBACK"},
  "workflow": Optional[str],
  "alert": Optional[dict],
  "handoff_payload": Optional[dict],
  "error": Optional[str],
  "developer": {...}  # only when developer_mode is on
}
```
`message_parts`/`buttons`/`quick_replies`/`images`/`files` are present in the shape today for forward-compatibility with richer channel adapters (e.g. LINE Flex Messages) — no channel currently populates them beyond `text`.

## Legacy Compatibility

`INTENT_SCHEMAS` (`services/slot_filling_engine.py`) is used **only** when:
1. A workflow hint is detected but **no Business Action exists** for that workflow's category at all (`legacy_fallback_reason: "no_business_action_for_workflow"`), or
2. A Business Action was selected but **has no parameter metadata configured yet** (`legacy_fallback_reason: "business_action_has_no_parameter_metadata"`).

In both cases, behavior is **identical to before this refactor** — same follow-up questions, same retry/escalation rules — and Developer Mode surfaces `legacy_fallback_used: true` plus a warning naming the workflow (and action, if one was found) so an admin knows to configure that Business Action's Parameters in the Business Action Center to make it the source of truth going forward.

## Migration Strategy

No data migration is required — this is purely additive, config-driven behavior:
1. **Do nothing** → every existing workflow keeps working exactly as before, via the legacy fallback.
2. **Configure Parameters on a Business Action** (Business Action Center → Parameters tab, or the Simple Wizard's Step 3) → the very next conversation touching that action automatically switches from legacy `INTENT_SCHEMAS` to Registry-driven collection, with zero code change and zero deploy.
3. Add/remove/rename required parameters or parameter groups at any time — the Information Collection Engine re-derives its questions from the current configuration on every turn (nothing is cached across the refactor's own request lifecycle).

## Tests

`tests/test_decision_engine.py` — 35 tests: original 25 (Business Action search/selection, conversation continuation, execution by type, fallback, human handoff, developer mode, regression) **all still pass unmodified** — proving backward compatibility was preserved by construction, not by re-writing tests to match new behavior. Plus 10 new tests covering this refactor: two required parameters asked sequentially then executed, changing Business Action parameters without any code change, `AT_LEAST_ONE` group stops asking once satisfied, `AT_LEAST_ONE` group prefers a specific validator over a permissive one, optional parameters never block completion, legacy fallback (no Business Action / no parameter metadata — 2 tests), Executor/Information-Collection validation consistency, full Developer Mode dynamic-collection trace, and continuation disambiguation when two actions share an identical generated question. Full suite: **944/944 passing**.

## Live Verification

All 4 required flows confirmed against the real Supabase-backed registry, using a temporary `SearchDataOrder`-equivalent demo Business Action (deleted afterward) plus the existing `customer_data_lookup` (GetDataCustomer):

| Flow | Result |
|---|---|
| SearchDataOrder (CustCode + OrderCode) — sequential collection | Turn 1 asked for CustCode, Turn 2 asked for OrderCode, Turn 3 executed successfully (`GET https://httpbin.org/get?CustCode=C00001&OrderCode=PO-99887`, HTTP 200) |
| Add BranchCode as a third required parameter — no code change | The exact same conversation immediately started asking for BranchCode instead of executing, purely from the Registry edit |
| AT_LEAST_ONE group (CustCode/CustEmail/CustName/CustPhone) — customer provides email | Bound correctly to `CustEmail` (not `CustCode`, despite it being listed first) and did not ask for phone or customer code — required a fix (see below) |
| Legacy Business Action without parameter metadata (`warranty`, no registered action) | Correctly fell back to legacy `INTENT_SCHEMAS`, asking for the Serial Number, with `legacy_fallback_used: true` in Developer Mode |

**Two real bugs found and fixed during live verification** (both now covered by regression tests):
1. `has_parameter_metadata`/the continuation pre-filter checked `selected.get("parameters")` against the CHEAP `enabled_actions()` row, which never includes the joined parameters table — every action with only ungrouped required parameters (no `parameter_groups`) was misdetected as having no metadata at all. Fixed by fetching real parameters via `registry.get_parameters()`/`get_full()` before deciding.
2. Binding only tried the single "next expected" parameter, so a customer volunteering an alternative `AT_LEAST_ONE` group member (email) was rejected outright, and the group's own permissively-validated first member (`CustCode`, `validation_type: non_empty`) then greedily miscategorized it. Fixed by trying every askable group member, most-specific validator first.

## Future Phases (explicitly NOT implemented here)

- **Semantic Routing** — `_semantic_score()` is a no-op interface, ready to be wired to a real similarity model.
- **Action Embeddings** — `_embedding_score()` is a no-op interface; `business_action_embeddings.embedding_source_text` is already prepared by the Business Action Center, no vector is computed or compared yet.
- **LINE OA / Notification Adapter** — Alerts and Notification-type Business Actions are structured-metadata-only; no channel actually sends anything yet.
- **OAuth / Secrets Marketplace** — unchanged, still out of scope.

The Decision Engine's call sites (`search_candidate_actions`, `ActionExecutor.execute`) are written so that plugging in any of the above later requires no change to this module's control flow — only the body of the relevant interface function.
