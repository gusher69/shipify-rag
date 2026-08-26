# CS Response Policy Specification (CS-01)

This document is the implementation-ready behavior model derived from
`01_HUMAN_CS_ANALYSIS.md`. It defines states, intents, context requirements, and the
hard Action Truthfulness rule — mapped onto Modify.ai's ACTUAL existing architecture
(see `04_CS_IMPLEMENTATION_PLAN.md` for the full current-flow map). Nothing in this
document is implemented by CS-01 itself.

## SOURCE OF TRUTH PRINCIPLE (must be preserved by any future implementation)

```
Verified Business Action Result
        ↓
API / Database / Operational Data
        ↓
Current Trusted Knowledge / RAG
        ↓
Conversation Context
        ↓
Response Policy
        ↓
Customer Service Style
```

Style controls **how** the AI says something. Evidence controls **what** the AI is
allowed to say. This is already the platform's real architecture — `STRICT_GROUNDING_RULES`
in `services/prompt_builder.py` already enforces "answer only from the provided
Context," and `services/answer_planner.py`'s own docstring states it "never generates
or invents a fact — it only picks from fact labels." CS-01's style recommendations sit
entirely at the bottom of this hierarchy and must never be implemented in a way that
lets style override evidence.

## RESPONSE BEHAVIOR MODEL (minimal state set)

| State | Definition | Entry condition | Allowed claims | Forbidden claims | Data requirement | Business Action required |
|---|---|---|---|---|---|---|
| **DIRECT_ANSWER** | Answers the actual question immediately | A concrete question with an available, grounded answer | Only what Retrieved Context / ERP data actually contains | Anything not present in evidence | RAG or ERP result | No (RAG) or Yes (ERP lookup) |
| **ACKNOWLEDGE** | Confirms the specific request was understood | Any incoming message, especially a complex/multi-part one | Restating what was asked | New facts | None | No |
| **ASK_INFO** | Requests only the specific missing piece | A required identifier/detail is missing | Nothing about the case yet | A guess at the missing value | None | No |
| **CASE_SUMMARY** | Summarizes a complex/ambiguous case before acting, and asks for confirmation | Multi-item claim, ambiguous evidence, or a case with several known facts | Facts already stated by the customer or already confirmed | The claim's outcome (not yet decided) | Customer-supplied facts collected so far | No |
| **PENDING_EXTERNAL** | Distinguishes "not yet asked" / "asked, no answer" / "answer received" | Waiting on supplier/warehouse/accounting/another team | Only the confirmed state of the check itself | "แจ้งแล้ว" without a real request having been sent; a supplier answer that hasn't arrived | None (this is a state description, not a data claim) | No |
| **NEXT_STEP** | States what the customer can do / what happens next | Any reply where a concrete next action exists | The actually-supported next action | An action the platform can't really do | Depends on the step | Depends on the step |
| **URGENCY_ACK** | Explicitly acknowledges urgency before continuing | Urgency language detected (see mood table in 01_HUMAN_CS_ANALYSIS.md) | Same as whatever state follows | Generic cheerful language in place of a real acknowledgement | None | No |
| **APOLOGY** | A brief, specific apology tied to a real issue | A real error/delay is confirmed (system, staff, or 3rd-party fault) | The specific issue apologized for | Apologizing merely because information is incomplete (already forbidden by `BASE_CONVERSATION_RULES`'s Fallback Tone section) | Confirmation the issue is real | No |
| **ACTION_SUCCESS** | Reports a completed action | A real Business Action / API / DB result confirms success | Exactly what the result confirms | Any outcome beyond what the result confirms | Confirmed executor result | **Yes — see Action Truthfulness below** |
| **ACTION_FAILED** | Reports an action did not succeed | A real result confirms failure/error | The failure and, if known, why | A false "will retry automatically" unless that's real | Confirmed executor result | Yes |
| **ESCALATE** | Hands off to a human | AI Policies' escalation rule fires (dissatisfaction keyword, low confidence, explicit request) | That the case is being handed to a human | That the human has already resolved it | Escalation decision (existing `services/policy_engine.py`) | Existing Human Handoff mechanism |
| **RESOLVED** | Closes out a case | The case's outcome is confirmed (refund posted, replacement sent, etc.) | The confirmed final outcome | An outcome that hasn't actually posted yet | Confirmed executor/RAG result | Depends |

This list intentionally merges several of the originally-proposed states
(GREETING, CONFIRM, CHECKING, ACTION_REQUIRED_FROM_CUSTOMER, ACTION_IN_PROGRESS,
FOLLOW_UP, CLOSING) into the ones above, per the task's own "keep this small" and
"merge where sensible" instruction — GREETING is governed entirely by the greeting
policy (a prompt-level rule, not a response state), CONFIRM/CHECKING collapse into
CASE_SUMMARY and PENDING_EXTERNAL, ACTION_REQUIRED_FROM_CUSTOMER is a variant of
ASK_INFO, ACTION_IN_PROGRESS is a variant of PENDING_EXTERNAL, and FOLLOW_UP/CLOSING
are variants of NEXT_STEP/RESOLVED.

### Sanitized example (ACTION_SUCCESS vs. PENDING_EXTERNAL contrast)
```
context: a real Business Action result shows the supplier's refund has posted
customer_message: "มีอัพเดทไหมครับ"
preferred_response (ACTION_SUCCESS): "ร้านคืนเงินมาให้แล้วนะคะ ยอด {{AMOUNT}} บาท
  เข้าเครดิตสั่งซื้อเรียบร้อยค่ะ"
anti_pattern: "แอดมินดำเนินการให้เรียบร้อยแล้วค่ะ" (vague — doesn't confirm what
  actually happened; also risks sounding complete when only a request was sent)
```
```
context: a Business Action was just triggered (e.g. a staff-notification action),
         no supplier response exists yet
customer_message: "แจ้งร้านหรือยังครับ"
preferred_response (PENDING_EXTERNAL): "ส่งเรื่องให้ทีมงานตรวจสอบกับร้านให้แล้วนะคะ
  หากร้านตอบกลับมา จะรีบแจ้งให้ทราบทันทีค่ะ"
anti_pattern: "แจ้งร้านแล้ว ร้านจะรีบจัดส่งให้ค่ะ" (claims the SUPPLIER's own response —
  never observed, never confirmed)
```

## INTENT CATALOG

| Intent | Customer goal | Required context | Likely existing source | Response behavior | Confirmation required | Escalation condition |
|---|---|---|---|---|---|---|
| Tracking Lookup | Know where a shipment is | tracking/order identifier | ERP Business Action (order/shipment lookup) | DIRECT_ANSWER | No | If ERP has no data and customer is frustrated |
| Shipment ETA | Know when it will arrive | tracking/order identifier | ERP Business Action + current RAG duration knowledge | DIRECT_ANSWER | No | No |
| Order Status | Know if an order is confirmed/paid/shipped | order identifier | ERP Business Action | DIRECT_ANSWER | No | No |
| Order ↔ Shipment Mapping | Match a PO to its shipment/tracking | order + shipment identifiers | ERP Business Action | DIRECT_ANSWER (often a compact list) | No | No |
| Warehouse Arrival / Pickup | Know if goods are ready to collect, arrange pickup | order/shipment identifier | ERP Business Action | DIRECT_ANSWER + NEXT_STEP | No (unless mutating an address/method) | No |
| Missing Item | Report a quantity shortfall | order identifier, ordered vs. received qty, evidence | Customer-supplied facts + real claim workflow | CASE_SUMMARY → confirm → hand off to real claim process | Yes | If repeated/unresolved |
| Wrong Item | Report a different item than ordered | order identifier, expected vs. received item, evidence | Customer-supplied facts + real claim workflow | CASE_SUMMARY → confirm → hand off | Yes | If supplier refuses |
| Damaged / Used Item | Report condition issue | order identifier, evidence (photo/video) | Customer-supplied facts + real claim workflow | ASK_INFO (evidence) → CASE_SUMMARY → hand off | Yes | If supplier refuses / marketplace report needed |
| Claim / Refund | Request compensation | claim already summarized | Real claim/refund workflow result | PENDING_EXTERNAL until confirmed, then ACTION_SUCCESS | Yes | If unresolved after a real follow-up |
| Cancellation | Cancel an order/line item | order identifier | Real cancellation workflow result | PENDING_EXTERNAL / ACTION_SUCCESS | Yes | No |
| Shipping Method Change | Switch sea/road/pickup method | order identifier, target method | Real Business Action if one exists; otherwise NEXT_STEP describing what's needed | ACTION_SUCCESS only once confirmed | Yes | No |
| Urgent Delivery | Expedite a pending shipment | order/shipment identifier | Real Business Action / current data only | URGENCY_ACK + DIRECT_ANSWER (never invent a faster ETA) | No | If truly cannot be expedited and customer is upset |
| Payment / Credit Balance | Check payment status or account credit | account context | ERP Business Action | DIRECT_ANSWER | No | No |
| Invoice / Tax Invoice | Get billing documents | order identifier | Real document-retrieval workflow if one exists | DIRECT_ANSWER or NEXT_STEP | No | No |
| Website / System Issue | Report the site/app not working | none required | Current status info if available; otherwise honest "checking" | PENDING_EXTERNAL, never a fabricated ETA for the fix | No | If it blocks a real transaction |
| Address / Warehouse Address | Get the correct shipping address to use | destination (Thailand vs. China warehouse) | Current trusted RAG content | DIRECT_ANSWER | No | No |

This list is derived from what the four chats actually contain — not the full
illustrative list in the task brief. Categories the chats did not clearly support
(e.g. a distinct "Supplier Negotiation" self-service intent) are intentionally
omitted; supplier negotiation in the source chats is always a HUMAN staff action, not
something an intent catalog entry should imply the AI itself performs.

## CONTEXT CONTINUITY

### Existing capabilities (confirmed by inspecting the current codebase — reuse these)
- `services/decision_engine.py::_resolve_conversation_reference` already resumes a
  topic-free follow-up ("แล้วของถึงหรือยัง") using `customer_context.last_business_action`
  — this already covers a large share of "รายการนี้"/"ร้านตอบหรือยัง"-style references.
- `services/pending_confirmation_service.py` already persists `pending_action_id` /
  `pending_parameters` across turns — the existing mechanism for "the case /
  pending_action" field the task brief asks about (Task 02C's own "interrupted workflow
  resume" work is this exact capability, already regression-tested).
- Response-Derived Identifier Memory (`identity_concept` on response_mapping rows,
  Task 04B-era work) already lets a successful list/lookup teach the Decision Engine
  its OWN record's code (e.g. the shipment code from a list result) for the next turn's
  "อันนี้"/"อันล่าสุด" reference, without the customer repeating it.
- `services/customer_tier_service.py` already provides `cold`/`warm`/`hot` segment
  tiering — an existing, appropriately lightweight personalization signal.

### Important constraint from Task 06 / 06B (must not be reversed)
Prior to Task 06, a customer-TYPED CustCode/OrderCode/ShipmentCode/Tracking was
persisted into `user_profiles` as a convenience cache and silently reused as if it were
verified. Task 06 identified this as a real cross-customer data-exposure vulnerability
(a customer-typed identifier is never proof of ownership) and removed the persistence;
Task 06B replaced it with a real, staff-verified account binding
(`services/customer_binding_service.py`) as the only trusted source of a customer's own
CustCode. **Any future CS-02+ context-continuity work must build on the Task 06B
verified-binding model, never resurrect the old "remember whatever the customer typed"
convenience cache** — doing so would reopen a fixed security hole purely for a
response-style improvement, which is explicitly out of CS-01's scope to cause.

### Minimum missing context (if genuinely needed by a future task)
Everything below should first be checked against the existing capabilities above
before being added — most references in the chats ("รายการนี้", "อันนี้", "ตัวนี้") are
already resolvable through `last_business_action` + response-derived identifiers.
The one genuinely new, minimal field this analysis surfaces:
```
active_issue_type   — e.g. "missing_item" / "wrong_item" / "damaged_item" — so a
                       follow-up like "อันนี้ยังไม่ได้คำตอบเลยครับ" resolves to the
                       CORRECT open claim when more than one exists in the same
                       conversation, without re-litigating which case it is.
```
No generalized long-term memory, no new customer-profile system, no new session
storage — this is a single optional field alongside what `pending_confirmation_service`
already tracks, and only worth adding if a future evaluation run shows the existing
mechanisms are actually insufficient for multi-case conversations.

## CORRECTION / CHANGE OF MIND

Observed pattern (SUPPORTED BY: SP6918's "เอ้ย ผมส่งผิดเลขครับ" self-correction; FT1145's
"ขอโทษทีครับ รับเหมือนเดิมครับ ใช้ได้ๆ ครับ"):
```
accept the correction
    ↓
update ONLY the affected field/context
    ↓
preserve every other already-confirmed field (identifiers, quantities, etc.)
    ↓
continue naturally — no re-asking of unrelated already-known information
```
This is already the exact behavior Task 02B/02C's regression suite protects
(`tests/test_webhook.py::TestTask02CInterruptedWorkflowAutoResume` — a field
correction during an in-progress workflow updates only the corrected slot and
preserves the rest). No new mechanism is needed; a future CS-02+ task's job is only to
make the PHRASING around this ("อ้อ ปรับให้เรียบร้อยแล้วนะคะ" style) match the human tone,
never to touch the underlying slot-preservation logic.

## ACTION TRUTHFULNESS (hard rule)

| Human phrase pattern | Classification | Rule |
|---|---|---|
| "ตอนนี้ยังไม่พบเลขแทรคที่ร้านยืนยันเข้ามาค่ะ" / "ข้อมูลล่าสุดในระบบคือ..." / "กรณีนี้ต้องตรวจสอบเพิ่มเติมค่ะ" | SAFE_WITHOUT_ACTION | May be said directly from current trusted evidence (RAG/ERP), no action required |
| "ขอส่งเรื่องให้เจ้าหน้าที่ตรวจสอบต่อค่ะ" | REQUIRES_HANDOFF | Wording must reflect only that a handoff/request was RAISED — never imply the other party has already responded, unless a real result confirms that |
| "แจ้งร้านแล้วค่ะ" / "แจ้งบัญชีให้แล้วค่ะ" / "แจ้งโกดังแล้วนะคะ" | REQUIRES_HANDOFF (mid-state) | Safe only to mean "a request was sent" — a supplier's/team's own reply must never be implied until it actually exists in the record |
| "ยกเลิกเรียบร้อยแล้วค่ะ" / "คืนเงินเรียบร้อยแล้วค่ะ" / "แก้ไขบิลเรียบร้อยแล้วค่ะ" / "ปรับเป็น...เรียบร้อยค่ะ" | REQUIRES_CONFIRMED_SUCCESS | Only sayable once a real Business Action / API / DB result explicitly confirms success. **No successful execution result = no completion claim, ever.** |

This is not a new rule for Modify.ai to build from scratch — it is largely already
structural: the Decision Engine only ever composes a reply from
`exec_result["result"]` (a real executor/API/DB outcome), and Task 06's Authorization
Gate already proved the platform's own architecture treats "the action actually ran
and returned a confirmed result" as a hard, separately-checked fact, not something the
LLM is trusted to assert on its own. The genuine, actionable gap CS-01 surfaces: a
future human-CS style pass must be careful that NOTIFICATION-type / handoff-type
Business Actions (e.g. `SendLineNotiCS`, which only notifies staff — it does not itself
carry back a supplier's answer) are phrased as "a request was raised," never
"แจ้งร้านแล้ว ร้านจะรีบจัดส่งให้ค่ะ" (which implies the supplier already answered) unless
a later Business Action result genuinely carries that supplier answer back.

## MULTI-INTENT (compact rule)

```
ANSWER REQUEST A (from evidence)
ANSWER REQUEST B (from evidence)
ASK ONLY for whichever piece is still genuinely missing
```
Never silently answer only the first of several questions in one message. This is
consistent with — and should reuse — Task 03's existing multi-intent classification
work in `services/hybrid_question_classifier.py` and `services/decision_engine.py`'s
HYBRID routing path; no new multi-intent detector is proposed.

## PROACTIVE SERVICE

```
PROACTIVE_WHEN:
- The suggestion is backed by CURRENT trusted knowledge, a real Business Action
  result, or real operational data (e.g. "ทางรถตอนนี้ยังตรวจเข้มอยู่ค่ะ แนะนำทางเรือ" — only
  said when that's the CURRENT operational reality, not because a 2024 chat once said it)

DO_NOT_PROACTIVELY_SUGGEST_WHEN:
- The only source for the suggestion is a historical LINE conversation (an old rate,
  an old supplier behavior, an old promotion) — those are explicitly NOT current
  business knowledge (see the module docstring in `01_HUMAN_CS_ANALYSIS.md`)
```
