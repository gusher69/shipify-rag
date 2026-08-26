# CS Architecture Map & Implementation Plan (CS-01)

This document maps CS-01's recommendations onto Modify.ai's ACTUAL existing
architecture (inspected directly in this repository — no module name below is
invented) and defines the smallest possible next step. CS-01 itself implements
nothing described here as "future work."

## EXISTING ARCHITECTURE INSPECTED

| Concern | Real module/file |
|---|---|
| LINE webhook / customer message entry point | `line_bot/webhook.py` |
| Conversation/session context | `services/session_service.py`, `profiles/manager.py` |
| Decision Engine (routing, slot filling, pending workflow) | `services/decision_engine.py` |
| Business Action selection & execution | `services/business_action_registry.py`, `services/action_executor.py` |
| Slot filling / dynamic collection | `services/decision_engine.py::_handle_dynamic_collection` and related |
| Pending/interrupted-workflow state | `services/pending_confirmation_service.py` |
| RAG retrieval | `rag/` (hybrid vector+keyword+heading scoring), `services/rag_service.py` |
| Answerability / grounding | `services/prompt_builder.py::STRICT_GROUNDING_RULES`, Task 04B's Answerability Gate work |
| Answer structuring (which facts to focus on) | `services/answer_planner.py` |
| LLM response generation / system prompt assembly | `services/prompt_builder.py::build_prompt` |
| Legacy (pre-Decision-Engine) reply generation | `line_bot/tone.py::generate_reply` |
| System prompt storage / versioning | Prompt Studio (`services/prompt_studio_service.py`, `ai_prompt_templates` / `ai_prompt_assignments`, migration `017_prompt_studio.sql`) |
| AI Playground (manual testing surface) | `admin/routes.py` Playground routes, `services/playground_orchestrator.py` |
| Business/escalation policy rules | `services/policy_engine.py`, `services/policy_studio_service.py` (AI Policies) |
| Human Handoff | `services/human_handoff_service.py` |
| Multi-message / human-paced reply splitting | `services/message_segmenter.py` |
| Multi-intent classification | `services/hybrid_question_classifier.py` |
| Customer segmentation (cold/warm/hot) | `services/customer_tier_service.py` |
| Authorization (who may see what) | `services/authorization_service.py`, `services/customer_binding_service.py` (Task 06/06B) |
| Golden Suite / regression tests | `tests/golden/`, `tests/test_golden_runner.py`, `tests/test_golden_assertions.py` |
| Production Validation | `docs/CHECKPOINT_*.md`, the Golden Suite + full `tests/` regression |

## CURRENT RESPONSE FLOW (real modules, LINE-live-routing path)

```
LINE MESSAGE
    ↓
line_bot/webhook.py (_handle_message_via_decision_engine)
    ↓
services/session_service.py (recent history) + profiles/manager.py (profile/segment)
    + services/customer_binding_service.py (verified binding, Task 06B)
    ↓
services/decision_engine.py :: DecisionEngine.decide()
    - Business Action match / slot filling / pending-workflow resume
    - services/authorization_service.py gate before any sensitive Business Action runs
    - RAG path: services/playground_orchestrator.py -> rag/ retrieval ->
      services/answer_planner.py -> services/prompt_builder.py::build_prompt()
      (system prompt = BASE_CONVERSATION_RULES + Prompt Studio's active template for
      the channel + AI Policies notes + grounding rules + answer plan) -> LLM call
    ↓
services/message_segmenter.py (splits into human-paced message bubbles if the
  answer's own structure supports it)
    ↓
line_bot/webhook.py replies via the LINE Messaging API
```

## WHERE IS THE SMALLEST SAFE PLACE TO APPLY HUMAN CS STYLE LATER?

**Two places, both existing, neither new:**

1. **Prompt Studio's LINE OA system prompt** (`ai_prompt_templates` row assigned to
   channel `"LINE OA"`, resolved via `get_active_prompt_for_channel`) — this is where
   the actual customer-facing VOICE (word choice, warmth, the ADOPT-list phrasing
   patterns from `01_HUMAN_CS_ANALYSIS.md`) belongs. It is already admin-editable,
   versioned, and already sits in the exact position the task brief describes ("Tone
   Guidance + Customer System Prompt... already folded into template.system_prompt").
2. **`services/answer_planner.py`'s response-shape vocabulary** — already the
   mechanism for "structure this answer a particular way" (it has
   `company_overview_structured` / `company_summary_structured` today). A future task
   would add one more shape (e.g. `case_summary_multi_item`) for Phase 20's structured
   claim-summary formatting — reusing the exact same pattern, not a new subsystem.

`BASE_CONVERSATION_RULES` (fixed, platform-wide, in `services/prompt_builder.py`)
ALREADY encodes the greeting policy, directness, fallback-tone discipline, context-
use discipline, and handoff-mention discipline this analysis calls for. **No changes
to that constant are recommended** — CS-01's job was to confirm the human chats
support it, which they do.

## MINIMUM IMPLEMENTATION MAP

### What already exists and needs no code change
- Greeting-once policy — `BASE_CONVERSATION_RULES`
- Directness / no-preamble — `BASE_CONVERSATION_RULES`
- "Never guess, state facts only from approved sources" — `BASE_CONVERSATION_RULES`
  + `STRICT_GROUNDING_RULES`
- Fallback tone (no reflexive "ขออภัย", no automatic hand-off tagline) —
  `BASE_CONVERSATION_RULES`
- Context-use discipline (history for interpretation only, never as fact) —
  `BASE_CONVERSATION_RULES`
- Human-paced multi-message splitting — `services/message_segmenter.py`
- Structured answer shaping — `services/answer_planner.py`
- Lightweight dissatisfaction/escalation detection — `services/policy_engine.py`'s
  `DISSATISFACTION_KEYWORDS` + `escalate_on_dissatisfaction`
- Pending/interrupted-workflow state (the "pending_action"/"case" context) —
  `services/pending_confirmation_service.py`
- Conversation-reference resolution ("แล้วของถึงหรือยัง") —
  `services/decision_engine.py::_resolve_conversation_reference`
- Response-derived identifier memory (a list/lookup teaches its own record code for
  the next "อันนี้"/"อันล่าสุด" reference) — existing Task 04B-era mechanism
- Customer segmentation (cold/warm/hot) — `services/customer_tier_service.py`
- Action-result-only reply composition (the structural basis of Action Truthfulness)
  — `services/decision_engine.py` composes replies only from real `exec_result`s;
  `services/authorization_service.py` (Task 06) already proves execution is gated
  separately from the LLM's own claims

### What needs only prompt/policy extension (no code change)
- The actual customer-facing VOICE/phrasing from the ADOPT list — a Prompt Studio
  system-prompt edit for the LINE OA channel assignment
- New `response_rules`/`fallback_rules` entries (already a free-form, admin-editable
  dict rendered generically by `_rules_block`) — e.g. an explicit
  "action_truthfulness_reminder" note, or an "urgency_acknowledgement" note
- AI Policies' escalation message wording, if the human-CS tone should also govern
  the escalation hand-off line itself

### What may need minimal code modification (only if a future evaluation shows a
real gap — not proposed as certain work)
- A new `answer_planner.py` response shape for the structured multi-item claim
  summary (Phase 20) — small, additive, same pattern as the two shapes that already
  exist
- Extending `DISSATISFACTION_KEYWORDS` (or a small sibling `URGENCY_KEYWORDS` set) if
  a future evaluation shows urgency detection specifically (not just dissatisfaction)
  needs its own signal — still inside the existing keyword-based mechanism, never a
  new sentiment service
- The optional `active_issue_type` context field described in `02_CS_RESPONSE_POLICY.md`
  — only if multi-case conversations turn out to need it in practice

### What does NOT need to be built (explicitly out of scope, per the Scope Lock)
- A new CRM, ticketing platform, workflow engine, or orchestration/agent framework
- A new RAG architecture, vector database, embedding model, or LLM provider
- Model fine-tuning or a training pipeline
- A new Decision Engine or Business Action architecture
- A generalized long-term customer-memory platform
- An advanced sentiment-analysis system or separate mood-detection service
- A new customer-profile/personalization engine
- Any account-linking, OTP, or customer-portal feature (already explicitly excluded,
  and already covered by Task 06/06B for the authorization question specifically)
- A new admin page/menu for this feature

## RECOMMENDED NEXT IMPLEMENTATION (smallest possible step — NOT implemented by CS-01)

**CS-02 — Minimal Human CS Prompt/Policy Integration**
- Scope: draft an updated LINE OA Prompt Studio system prompt incorporating the
  ADOPT-list phrasing patterns and the Action Truthfulness wording distinctions from
  `02_CS_RESPONSE_POLICY.md`, reviewed and activated through the existing Prompt
  Studio admin flow (never hardcoded in code).
- Files likely touched: a new `ai_prompt_templates` row (via the existing admin UI or
  a seed script, not a schema change), possibly new `response_rules` entries on that
  template.
- Minimal behavior change: LINE OA replies read closer to the human CS voice; no
  routing, retrieval, Business Action, or authorization logic changes.
- Tests required: AI Playground manual comparison against a representative subset of
  `03_CS_FEWSHOT_EXAMPLES.md`; full existing `tests/` regression (must stay green,
  since no runtime code changes); a spot-check that Action Truthfulness wording
  (Task 06/06B's authorization-denial message, NOTIFICATION-type action phrasing)
  is not altered in a way that implies false completion.
- Regression areas: Golden Suite, Task 03/03B/03C intent routing (must not shift
  which action wins), Task 04/04B RAG/answerability behavior.
- Rollback path: deactivate the new prompt assignment in Prompt Studio and revert to
  the previous active template — a data change, not a code deploy, so it is trivially
  reversible.

Further steps (CS-03 Response Policy states as `answer_planner.py` shapes, CS-04
Context/Greeting/Mood/Action-Truth minimal code additions, CS-05 Evaluation) are
NOT scoped here — CS-01 stops after producing this analysis and plan.

## EVALUATION SPEC (future, not implemented)

A future ~50-100 case evaluation set (single-turn and multi-turn), reusing the
existing Golden Suite infrastructure (`tests/golden/golden_runner.py`,
`tests/golden/golden_assertions.py`) rather than a new evaluation platform, should
check:
1. Factual accuracy 2. Groundedness 3. Directness 4. Human CS tone 5. Conciseness
6. Context continuity 7. Reference resolution 8. Correction handling
9. Multi-intent completeness 10. Urgency handling 11. Complaint handling
12. Action truthfulness 13. No fake completion claims 14. No repeated greeting
15. No regression in the existing Golden Suite

Multi-turn cases must include the reference-resolution patterns confirmed in this
analysis: "รายการนี้", "อันนี้", "ร้านตอบหรือยัง", "อันที่ส่งผิด", a pending claim
carried across turns, and an urgent item remembered from a prior turn.

## OUT_OF_SCOPE_OBSERVATIONS

**Finding**: `line_bot/tone.py::generate_reply` (the legacy, pre-Decision-Engine reply
path) still exists alongside the Decision-Engine-live-routing path, gated by a
feature flag (`DECISION_ENGINE_LIVE_ROUTING`, per `docs/CURRENT_STATUS.md`).
**Why it may matter**: a future style change applied only to Prompt Studio's LINE OA
template would affect BOTH paths (both call `get_active_prompt_for_channel`/
`build_prompt`), which is actually convenient — but worth knowing this is shared code,
not two independent prompt paths to update separately.
**Why it is not required for CS-01**: this is an existing architectural fact, not
something to change; CS-01 only needed to identify it so a future task doesn't
mistakenly think it needs a second prompt update.
**Possible future consideration**: none — flagged for awareness only.

**Finding**: All four source chats show staff actively negotiating price/compensation
with Chinese suppliers in real time.
**Why it may matter**: tempting to think of this as a future "AI negotiates with
suppliers" capability.
**Why it is not required for CS-01**: response STYLE only; live negotiation with an
external, human-operated supplier is a Business Action/workflow capability question,
entirely outside a style specification's scope.
**Possible future consideration**: none proposed — flagged only so it isn't silently
assumed into a future task's scope.

**Finding**: SA5061's own staff usage shows the SAME real customer addressed by two
different nicknames ("คุณตั้ม" then "คุณน้ำ") within one thread.
**Why it may matter**: it's the one clear example in the source material of a human
behavior an AI must be MORE disciplined than the humans who wrote these chats about.
**Why it is not required for CS-01**: this is a finding already fully captured as an
AVOID item in `01_HUMAN_CS_ANALYSIS.md`; no further action needed here.
**Possible future consideration**: none.

## PRIVACY REVIEW (before saving these documents)

Confirmed no real: phone numbers, addresses, bank account numbers, full customer
names/identities, real order histories, or exact tracking/PO numbers from the source
chats were copied into any of the four documents in `docs/customer-service/`. Every
identifier, name, amount, and ETA in `03_CS_FEWSHOT_EXAMPLES.md` is a placeholder.
Chat names (SP6918, FT1145, SA5061, SP7521) are referenced only as internal chat
labels for traceability (per the task's own "SUPPORTED BY" requirement), never
alongside any real personal detail.

## SCOPE CONFIRMATION

- Production runtime changed: **NO**
- Production prompt activated: **NO**
- Production deployed: **NO**
- RAG redesigned: **NO**
- Decision Engine redesigned: **NO**
- Business Actions redesigned: **NO**
- Database changed: **NO**
- Infrastructure changed: **NO**
- New large subsystem created: **NO**
- Raw LINE chats ingested into RAG: **NO**
- Fine-tuning introduced: **NO**
- Unrelated refactor: **NO**
- Recommendations incremental: **YES**
- Scope remained customer-service response quality only: **YES**

## CS-02 / CS-03 FINAL STATUS (added after implementation closed)

- **CS-02** (`tools/seed_cs02_human_style_prompt.py`) implemented this plan's
  recommended "smallest safe place" (Prompt Studio's LINE OA system prompt +
  `response_rules`, no new subsystem). One pre-existing code defect was found
  and fixed along the way: the real LINE webhook's runtime channel value
  (`"line"`) never matched the admin-facing Prompt Studio channel label
  (`"LINE OA"`), so channel-specific prompt assignment had never been able to
  take effect for real traffic — fixed in `services/prompt_builder.py` via a
  narrowly-scoped resolution-only mapping.
- **CS-03** (`tools/seed_cs03_human_style_prompt_v2.py`) ran a 53-scenario
  live evaluation (single-turn + multi-turn) through the real activated
  prompt-resolution path and found four reproducible wording gaps, fixed with
  minimal `response_rules` reinforcement only (no business fact changed):
  natural "no information" phrasing (dropped the internal-sounding
  "ฐานความรู้" term), broadened urgency acknowledgement to cover indirect
  signals, broadened complaint-tone acknowledgement to cover inconvenience
  complaints (not just formal complaints), and strengthened the
  customer-reported-vs-verified rule to explicitly cover multi-item cases.
  All four fixes were confirmed via reproducibility testing and a live
  production-safe UAT before activation.
- **Both versions remain in Prompt Studio's version history** (CS-02 as v1,
  CS-03 as v2, both versioned from the same lineage) and are rollback-able
  via `assign_channel("LINE OA", <version id>)` at any time.
- Human Customer Service Style work (CS-01 → CS-02 → CS-03) is considered
  **complete** as of CS-03's final report.
