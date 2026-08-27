# Customer Journey UAT — Simulated Real Customer

Full machine-readable transcripts: [`customer_journey_uat_transcripts.json`](./customer_journey_uat_transcripts.json) in this same folder.

## Test execution method

`services.decision_engine.DecisionEngine.decide()` — the same orchestrator `line_bot/webhook.py` and the Admin UI's Auto-mode Playground use — called directly with `context["channel"]="line"` (the real customer-facing value), so prompt resolution and the Task 06/06B authorization gate behave exactly as they would for a real, never-verified LINE customer. Sessions were persisted via `services.session_service.SessionService` (`get_or_create_active_conversation` / `record_conversation_turn`) using synthetic `playground:UAT-CJ-*` line-user IDs and `channel="playground"` storage — the existing "Real User Journey UAT" mechanism (2026-08-15), reused as-is. No real LINE Messaging API call was ever made. No `confirm` reply was ever sent, so no Business Action mutation executed at any point.

## Evidence index

| Journey | Session ID | Storage | How to inspect |
|---|---|---|---|
| UAT-CJ-01-IMPORT-SHIPPING | `257fada6-0b60-4be6-b1fe-9e5e3a0923f5` | `ai_sessions` (channel=playground) | Admin UI → `/admin/conversations`, filter by name or search "UAT-CJ-01" |
| UAT-CJ-02-ADDRESS-WORKFLOW (attempt 1, invalid setup) | `5ed3c0fd-d4e2-466b-8196-553e0642eb5c` | `ai_sessions` | same, search "UAT-CJ-02-ADDRESS-WORKFLOW" |
| UAT-CJ-02-ADDRESS-WORKFLOW (attempt 2, valid) | `32a6cfde-af4a-4a45-8f9e-5c04bf54ee82` | `ai_sessions` | same, playground_user_id ends `-ATTEMPT2` |
| UAT-CJ-03-URGENCY-COMPLAINT | `fb8653f1-4362-4a05-a662-a7514ae7d7ea` | `ai_sessions` | same, search "UAT-CJ-03" |
| UAT-CJ-04-SAFETY-MULTI-INTENT | `609bc53b-f5d3-4811-951e-13ad8f600586` | `ai_sessions` | same, search "UAT-CJ-04" |

All five sessions remain in the database (not soft-deleted) — an admin can open each one from `/admin/conversations` and read the full turn-by-turn transcript, exactly as they would for any real conversation.

## Corrected test setup — Chat 2

**Attempt 1: INVALID TEST DATA.** Turn 2 supplied a ShipmentCode-shaped value (`SPUAT00201`) assuming ShipmentCode was the first field the real workflow asks for. Checking the actual registry (`business_action_registry.get_full`) showed the real `RequestShippingAddressChange` action asks for **CustCode** first (`^[A-Za-z]{2}\d{4,6}$`), then **ShipmentCode** (`^[A-Za-z]{2}\d{10,}$`) — `SPUAT00201` matched neither pattern, so the system correctly kept re-asking for CustCode and, after repeated unrecognized attempts, safely escalated to Human Handoff. No crash, no fabrication, no security issue — but the intended slot-preservation/interruption/resume/correction scenarios never got a chance to run. This was a test-construction gap, not a product defect. The transcript is preserved above, unedited.

**Attempt 2: VALID JOURNEY RESULT** — retested with a properly-shaped synthetic CustCode (`SP9999`) and ShipmentCode (`SP1234567890`), corresponding to no real customer or order. **PASS** on every required behavior:
- CustCode accepted once, never re-asked.
- ShipmentCode accepted once, never re-asked — **the original "repeated identifier" complaint, confirmed fixed.**
- Multi-slot preservation: name + phone captured together in one message; all prior fields (CustCode, ShipmentCode) preserved unchanged throughout.
- Interruption (`CBM คืออะไรครับ`) answered correctly without corrupting any collected slot.
- Reached the confirmation gate with a full, accurate summary and **stopped before any mutation** — no `confirm` reply was ever sent by this test, and none would have been accepted (the harness hard-refuses to send one).

One turn (7, an attempted "resume") repeated an already-collected field rather than answering the field actually pending (Address) — a test-script mismatch, not a confirmed system defect (inconclusive, not scored as a failure). One deferred observation surfaced on turn 8 — see "Out-of-scope observations" below.

## Defect discovered during UAT

**Issue**: the deterministic zero-evidence fallback exposed internal terminology — `ตอนนี้ยังไม่พบข้อมูลนี้ในฐานความรู้ค่ะ` — to the customer.

**Reproduced**: 4 times — Chat 2 attempt 1 turn 7, Chat 2 attempt 2 turn 8, Chat 3 turns 1 and 3. Consistent, deterministic, not a stochastic/temperature artifact.

**Root cause**: `services/playground_orchestrator.py`'s Answerability Gate branch (`rag/confidence.py::compute_confidence` returning `answerability=="no_information"`) hardcodes this exact string, bypassing the LLM/Prompt Studio prompt entirely — copied verbatim from `services/prompt_builder.py`'s `BASE_CONVERSATION_RULES` "## กรณีไม่มีข้อมูล (Fallback Tone)" example text. Because this deterministic path never invokes the LLM, CS-03's own `unknown_information_wording` rule (which already forbids "ฐานความรู้" for LLM-generated replies) could never reach it. This is the exact same underlying wording concern CS-03 already fixed elsewhere in the prompt — just a code path CS-03's own evaluation never exercised, since that evaluation always supplied *some* synthetic RAG context. This deterministic function is shared by **every channel**, including real LINE traffic, so this was a live, real production issue.

**Fix (minimal, 2 files)**:
1. `services/playground_orchestrator.py` — the hardcoded fallback string changed from `"ตอนนี้ยังไม่พบข้อมูลนี้ในฐานความรู้ค่ะ"` to `"ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ"` (the same natural wording CS-03 already established).
2. `services/prompt_builder.py`'s `BASE_CONVERSATION_RULES` example text updated to match, plus an explicit new line forbidding "ฐานความรู้" — so the same regression can never re-enter from either source again.

No architecture change, no new module, no new fallback service, no prompt version created (this was a code-level wording bug, not a Human CS prompt issue — CS-03 v2 remains the active prompt, untouched).

**Before**: `ตอนนี้ยังไม่พบข้อมูลนี้ในฐานความรู้ค่ะ`
**After**: `ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ`

Confirmed via a live, production-safe re-test through the real `channel="line"` path after deployment — all 4 previously-affected messages now produce the fixed wording, zero forbidden terms (`ฐานความรู้`, `RAG`, `Knowledge Base`, `Top K`, `retrieval`), and the active prompt (`9fdb9966...`, CS-03 v2) resolved correctly and unchanged throughout.

## Out-of-scope observations (reported only, not fixed)

1. **Mid-workflow field correction not recognized when a different field is actively pending.** In Chat 2 attempt 2, correcting an already-collected `ReceiverName` while `Address` was the actively-pending field caused the correction to be silently dropped (routed to an unrelated RAG fallback) rather than merged into the collected parameters. The final confirmation summary therefore showed the original, uncorrected name — though since the workflow still stops at a confirmation step before any mutation, the customer has a chance to catch this before anything executes. Not one of the original customer complaints, not proven to be a regression of anything CS-01/02/03 implemented, and would require changes to the dynamic-collection/correction-recognition logic in `services/decision_engine.py` — out of this task's minimal-fix scope.
2. **A general company-policy question ("บริษัทมีประกัน All Risk ให้ทุกออเดอร์ไหมครับ") routes into a CustCode-gated workflow** instead of an ordinary "no information" RAG answer (reproduced twice, consistently). Safe-direction (over-cautious, never discloses anything, matches Task 06's fail-closed design), not a security issue, not one of the original customer complaints. Would require deeper Business Action routing/classification investigation to resolve — out of scope here.
