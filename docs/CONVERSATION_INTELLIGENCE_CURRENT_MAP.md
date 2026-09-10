# Conversation Intelligence — Current Architecture Map

**Status:** AUDIT ONLY — no code changed. Produced as the §1 gate of the
"System-Wide Conversation Intelligence Upgrade" task. Nothing in
`services/` / `line_bot/` / `rag/` is modified by this document.

**Scope of the audit:** every component that can currently decide intent,
conversation act, pending flow, active journey, slot values, correction,
topic switch, or route (RAG / general / business action / ERP /
calculator / workflow / fallback).

**Baseline SHA:** `21be62322e616ffbea3ed193ade957a36ad317ca`
(`fix(owner-real-line-06)`).

---

## 1. End-to-end pipeline (as built today)

```
LINE inbound            Admin Hybrid Playground           Admin Playground
  event                   /admin/api/hybrid-playground/ask   /admin/playground/ask
    │                       │  (mode=auto)   │ (rag|erp|hybrid)   │
    ▼                       ▼                ▼                    ▼
line_bot/webhook.py     admin/routes.py   run_erp_test /       run_playground_turn
_handle_message_via_    3720: engine      run_playground_turn  (RAG pipeline ONLY —
  decision_engine       .decide(...)      directly             no decide(), no
  (flag DECISION_                          (bypass decide)      pre-RAG flows)
  ENGINE_LIVE_ROUTING                                          
  = true; legacy                                              
  path calls                                                  
  line_bot/intent.py                                          
  .classify — DEAD)                                           
    │                       │
    ├── webhook_event_dedup_service.claim_event  (idempotency)
    ├── _pending_texts_by_user  (same-user/same-text in-flight dedup)
    ├── session_service.get_recent_history(max_turns=20)  ── history is the ONLY state store
    ├── handoff_state_before  → decide_context["handoff_status"]
    ├── pending_confirmation_service.get_active()
    │     └── classify_confirmation_reply(text)  → confirm | cancel | other
    │           confirm  → decide(confirmed=True, confirmed_action_id, confirmed_parameters)
    │           cancel   → mark_cancelled, canned "ยกเลิกแล้ว"
    │           other    → decide(pending_action_id, pending_parameters)
    │     └── expired + confirm/cancel within 10 min → canned "หมดเวลายืนยัน"
    ▼
services/decision_engine.py :: DecisionEngine.decide(message, history, context)
    │
    │  [A] thai_text_normalizer.normalize_message(message)   → typo-normalised text
    │        (raw kept in context["raw_user_message"]; structured tokens frozen)
    │
    │  [B] conversation_semantics.interpret(message, history, context)  ── "SEMANTIC-FIRST-1"
    │        ├── _structural_kind()  → url | identifier | numeric | postback | empty  (fast path)
    │        ├── _followup_op()      → CORRECTION | COMPARISON | TOPIC_CHANGE | SET_VALUE | CONTINUE | NONE
    │        ├── _compose()          → deterministic (intent_family, conf, entities)
    │        ├── FIX-04 block        → bare product reply → IMPORT_INTEREST | PRODUCT_POLICY
    │        ├── _llm_family()       → gated LLM disambiguation (degrades to deterministic)
    │        ├── derive_active_frame(history)  → Frame(product,qty,method)  ── FROM REPLY TEXT
    │        └── returns Interpretation{intent_family, entities, is_private,
    │                                   follow_up_op, conversation_act, confidence, source}
    │
    │  [C] rag/query_understanding.classify_actionable_intent(message, interpretation=semantic)
    │        → {broad_intent, actionable_intent, entities, requested_attributes}
    │        (consumes `semantic` BUT `detect_intent()` re-runs its own regex classifier)
    │
    │  [D] _classify_private_state_inquiry(message)   ── SEM-1 / PPC, own regex
    │
    │  … then a long SEQUENTIAL cascade of return-on-first-match branches …
    │        (see §2 for the ordered list; each is an independent decision point)
    │
    ▼
  RAG pipeline  (playground_orchestrator.run_playground_turn)
    ├── request_grounding_classifier.classify_request_grounding(question, interpretation, history)
    ├── _g6c_promote_general_assistance(question, interpretation, history)
    ├── _is_product_import_interest(question)      ── 3rd import-interest recogniser
    ├── _is_invoice_issuance_question / _invoice_issuance_branch_applies
    ├── _is_ambiguous_rag_continuity_followup(question, history)
    ├── answer_planner.classify_actionable_intent + build plan
    ├── _firm_prohibited_category_hedge / _restore_verbatim_scalar_values / …
    └── grounding / retrieval / synthesis
    ▼
  final reply  → segmented (LINE) → sent
    ▼
  session_service persists {user turn, assistant reply TEXT}
  pending_confirmation_service persists a row IF WORKFLOW + incomplete
  handoff_state persisted IF HUMAN_HANDOFF
  ──> NO structured conversation-state row is persisted. Next turn
      reconstructs journey/slots by RE-PARSING the assistant reply text.
```

---

## 2. Interpretation-owner inventory

An "interpretation owner" = a function/branch that independently decides
what the turn means or where it routes, using its own logic (regex,
keyword set, LLM call, or history scan) rather than only consuming a
prior resolution.

### 2.1 Pre-decide (channel adapter layer)

| # | Owner | File:entry | Decides | Input | Can be overridden by |
|---|---|---|---|---|---|
| 1 | webhook dedup | `line_bot/webhook.py` `_should_skip_duplicate_pending` | drop turn entirely | user_id + normalised text | — |
| 2 | confirmation classifier | `services/pending_confirmation_service.py::classify_confirmation_reply` | confirm / cancel / other | current text only | — |
| 3 | expired-pending gate | `line_bot/webhook.py:638` | canned "expired" reply | confirm/cancel + age < 10 min | — |
| 4 | handoff-state gate | `services/handoff_state_service` → `decide_context["handoff_status"]` | NONE / NOTIFIED / … passthrough | issue/episode state | decide()'s Active-Handoff-Follow-up branch |
| 5 | legacy LINE intent | `line_bot/intent.py::classify` | สต็อก/ออเดอร์/นโยบาย/ทั่วไป (LLM) | text only | **DEAD** — only `DECISION_ENGINE_LIVE_ROUTING=false` |

### 2.2 Inside `interpret()` (`services/conversation_semantics.py`)

| # | Owner | Entry | Decides | Notes |
|---|---|---|---|---|
| 6 | structural fast-path | `_structural_kind` | url / identifier / numeric / postback | short-circuits before `_compose`; FIX-03 routes bare URL → LINK_CONVERSION here |
| 7 | follow-up op | `_followup_op` / `is_frame_followup` / `_FOLLOWUP_SHAPE_RES` | CORRECTION / COMPARISON / TOPIC_CHANGE / SET_VALUE / CONTINUE / NONE | pure regex shapes; FIX-05 added the substitution shape |
| 8 | compositional family | `_compose` | intent_family + confidence + entities | ~30 ordered regex composites (OBJ×ACT×ROLE) |
| 9 | bare-product-slot | `interpret()` FIX-04 block | UNKNOWN + assistant-asked-product → IMPORT_INTEREST \| PRODUCT_POLICY | uses `_assistant_asked_for_product`, `_import_journey_active`, `_HIGH_RISK_PRODUCT_RE` |
| 10 | gated LLM family | `_llm_family` | overrides family when det. only reached UNKNOWN/GENERAL | degrades to deterministic; **non-deterministic between test (offline) and prod** |
| 11 | active-frame derivation | `derive_active_frame` | Frame(product, quantity, method) | **reconstructed from assistant reply TEXT** via `_ASSIST_PRODUCT_RES` / `_ASSIST_QTY_RE` / `_ASSIST_METHOD_RE`; staleness by `_FRAME_EXIT_RE` + drift count over a 14-turn lookback |
| 12 | frame-followup resolver | `resolve_followup` (gated LLM) | CHANGE_TARGET / SET_QUANTITY / CORRECT_QUANTITY / CHANGE_METHOD / CHANGE_TOPIC | offline → UNKNOWN |
| 13 | deterministic correction | `resolve_frame_correction` (FIX-05) | CHANGE_TARGET / CORRECT_QUANTITY / SET_QUANTITY / CHANGE_METHOD / CHANGE_BRAND / REJECT / AMBIGUOUS | typo-tolerant regex; the offline/degraded fallback for #12 |

### 2.3 Standalone classifiers consumed by `decide()`

| # | Owner | Entry | Decides | Own logic? |
|---|---|---|---|---|
| 14 | actionable-intent | `rag/query_understanding.py::classify_actionable_intent` → `detect_intent` | RAG answer-planner bucket (tracking_status, invoice_policy, prohibited_goods, shipping_calculation, …) | **Yes** — `detect_intent()` regex, even though `interpretation=semantic` is passed |
| 15 | private-state inquiry | `decision_engine.py::_classify_private_state_inquiry` (SEM-1 / PPC) | is this a private-record status/value question | **Yes** — own regex + domain set |
| 16 | turn-intent primitive | `decision_engine.py::classify_turn_intent` (Root Change 1) | informational vs private-action, `exclude_private` flag | **Yes** — own regex |
| 17 | operational-change | `services/operational_change_flow.py` via `_derive_operational_state` | modify_bill_qty / add_vat / change_shipping_method / missing_item_claim / custom_production / duplicate_bill / warehouse_verify / … | **Yes** — own keyword/object markers; history-sticky via acks |
| 18 | business-action candidate search | `services/hybrid_question_classifier.py::classify_question` | which Business Action(s), HYBRID detection, ambiguity ratio, entity-continuation | **Yes** — scores every enabled action; `exclude_private` threaded in |
| 19 | ERP intent / slot filling | `services/slot_filling_engine.py::detect_erp_intent` / `resolve_active_erp_intent` / `INTENT_SCHEMAS` | ERP read/write intent + required slots (legacy schema path still reachable) | **Yes** — own regex + schemas |
| 20 | shipping-estimate flow | `services/shipping_estimate_flow.py` | calculator intent + weight/dims/method extraction + route ask | **Yes** — own regex (`_PARCEL_DESC_RE`, `_MEASURE_RE`, method words) — overlaps #8 |
| 21 | withdrawal flow | `services/withdrawal_flow.py` | PURCHASE vs SHIPPING withdrawal + SP/FT brand (from CustCode prefix OR bare "SP"/"FT" after the brand ask) | **Yes** — own regex |
| 22 | service-intent flow | `services/service_intent_flow.py` + `decide()` `_broad_china_buy` regex | HELP / SERVICE_DISCOVERY / MONEY_TRANSFER / WEBSITE_LINK / CONTACT_INFO / WAREHOUSE_INBOUND / broad-import-discovery | recognition is `semantic.intent_family`, BUT `_broad_china_buy` is a **parallel regex** in `decide()` (FIX-01/FIX-06) |
| 23 | link conversion | `services/link_conversion_flow.py::classify_link_request` / `is_link_conversion_signal` | supported/unsupported domain, VALID/HOME/INCOMPLETE/UNSUPPORTED | **Yes** — URL structural rules |
| 24 | referent-less clarify | `decision_engine.py::_is_referentless_underspecified` / `_recent_product_referent` / PPC-1 | ambiguous "อันนี้/แบบนี้" → clarify | **Yes** — own regex |
| 25 | pending-flow break | `decision_engine.py::_current_intent_breaks_pending_flow` | does the current turn break a sticky operational/charter/collection flow | **Yes** — own family + extract predicates |
| 26 | topic-change guard | `decision_engine.py` `_TOPIC_RE` + `_CORE_CONVERSATION` | explicit topic switch out of a flow | overlaps #7's TOPIC_CHANGE |
| 27 | reject / help-affirmation | `_REJECT_ACT_RE` / `is_help_affirmation` / `phase6b_reject_reevaluate` | REJECT → re-evaluate; bare "ใช่" after an offer → open convo | overlaps #7, #13 REJECT |

### 2.4 Inside the RAG pipeline (`services/playground_orchestrator.py::run_playground_turn`)

Runs AFTER `decide()` has already routed the turn to RAG — but re-derives
meaning:

| # | Owner | Entry | Decides |
|---|---|---|---|
| 28 | grounding class | `services/request_grounding_classifier.py::classify_request_grounding` | CONVERSATION / GENERAL / BUSINESS_TRUTH_REQUIRED |
| 29 | G6C general-assistance promotion | `_g6c_promote_general_assistance` | promote a "no KB" turn to Smart General Assistance vs honest no-info |
| 30 | import-interest (3rd copy) | `_is_product_import_interest` / `_product_interest_noun` | is this product-import interest (also imported BACK into `conversation_semantics` as the `_IMPORT_RECOGNIZERS`) |
| 31 | invoice-issuance branch | `_is_invoice_issuance_question` / `_invoice_issuance_branch_applies` | answer the trusted invoice-issuance text directly |
| 32 | RAG-continuity follow-up | `_is_ambiguous_rag_continuity_followup` | is this a follow-up to the previous RAG answer |
| 33 | answer planner | `services/answer_planner.py` | purpose (offer_alternative_product, answer_then_details, …), required/excluded fact slots |
| 34 | post-synthesis firmers | `_firm_prohibited_category_hedge`, `_restore_verbatim_scalar_values`, `_strip_contradictory_noinfo_hedge` | deterministically rewrite the LLM answer against trusted context |
| 35 | RAG conversation-state | `rag/conversation_state.py::build_conversation_state` + `rag/query_resolution` | topic bucket, entity carry-over (a SECOND active-state model, history-text based) |
| 36 | RAG spell-correction | `rag/spell_correction.py` | fuzzy query correction at retrieval time (separate from `thai_text_normalizer`) |

**Interpretation owners — BEFORE: 36** (33 live in the LINE path;
#5 dead, #19 legacy-partly, #14 partly-consumes). Of these, **at least
7 independently classify "import interest"/"product slot"** (#8, #9,
#11, #12, #13, #22, #30) and **at least 4 independently classify
"topic switch / reject"** (#7, #13, #26, #27).

---

## 3. Duplicated decision points

| Concept | Owners that each decide it independently | Consequence |
|---|---|---|
| **Import-interest / product-slot** | `_compose` (#8), FIX-04 block (#9), `derive_active_frame` (#11), `resolve_followup` (#12), `resolve_frame_correction` (#13), `_broad_china_buy` regex (#22), `_is_product_import_interest` (#30) | slot ownership disputes; a bare noun is IMPORT_INTEREST in one and PRODUCT_POLICY in another depending on which fired first |
| **Topic switch / rejection** | `_followup_op` TOPIC_CHANGE (#7), `resolve_frame_correction` REJECT (#13), `_TOPIC_RE`/CORE-CONVERSATION (#26), `_REJECT_ACT_RE`/`phase6b_reject_reevaluate` (#27) | "ไม่เอาแล้ว" handled by whichever branch is reached first in the cascade |
| **Actionable / RAG intent** | `interpret()`+`FAMILY_TO_ACTIONABLE_INTENT` (#8), `classify_actionable_intent`→`detect_intent` (#14), `answer_planner` re-classify (#33) | intent can differ between the pre-RAG gate and the answer planner |
| **Calculator / measurement** | `_compose` `_PARCEL_DESC_RE`/`_MEASURE_RE` (#8), `shipping_estimate_flow` own regex (#20) | two regex sets to keep in sync for the same "54x12x43 / 2 กิโล" |
| **Private-state / informational** | `_classify_private_state_inquiry` (#15), `classify_turn_intent` (#16), `classify_question` `exclude_private` (#18) | three gates, threaded together by hand at line 4447-4464 |
| **Grounding (truth vs chat)** | `interpret().is_private` + families (#8), `classify_request_grounding` (#28), `_g6c` promotion (#29), `request_grounding_classifier` PRODUCT_POLICY straddle (PHASE-6C) | grounding decided both before RAG (route) and inside RAG (answer) |
| **Active conversation state** | `derive_active_frame` (#11, from reply text), `rag/conversation_state.build_conversation_state` (#35, from history), operational-change history-sticky acks (#17), pending_confirmation row (#2/#18) | four partial state models, none authoritative |
| **Confirmation / cancel** | `classify_confirmation_reply` (#2), `resolve_frame_correction` REJECT (#13), CORE-CONVERSATION cancel vocabulary | |

---

## 4. Branches that can override each other (order matters)

`decide()` is a single ~3,800-line method of **sequential
return-on-first-match** branches. The order (by `selection_source`) is:

```
 1. typo normalise (mutates text for everyone downstream)
 2. interpret() → semantic                                 [B]
 3. INVOICE-PRODUCT-REGRESSION-2 rewrite (PRODUCT_POLICY+SET_VALUE → "<p>นำเข้าได้ไหม")
 4. classify_actionable_intent                             [C]
 5. _classify_private_state_inquiry                        [D]
 6. pending-confirmation / collection continuation  (3359, 3748)
 7. conversation_continuation / conversation_reference_detail (3813, 3817)
 8. charter_truck_collection                        (3940)
 9. shipping_withdrawal / purchase_withdrawal KB    (4001-4021)   ← guarded by _wd_yield_to_op (fresh operational wins)
10. FIX-05 frame correction / reject / ambiguous    (4038-4106)   ← _derive_active_frame + resolve_frame_correction
11. operational_change_collection                   (4122-4206)   ← _derive_operational_state (history-sticky) + _current_intent_breaks_pending_flow
12. shipping_estimate_flow                          (4226-4236)
13. SEM-GEN-1 frame follow-up (LLM + det. fallback) (4254-4295)   ← is_frame_followup gate
14. phase6b reject re-evaluate                      (4340)
15. phase6b help-affirmation                        (4356)
16. _broad_china_buy → import discovery             (4384-4404)   ← FIX-01/FIX-06; guarded by (frame is None OR not is_frame_followup)
17. _svc_pre_rag service-intent families            (4407-4441)   ← same FIX-06 guard
18. classify_turn_intent + SEMANTIC-FIRST-2.1 public-info gate (4464+)
19. PPC-1 referent-less clarify                     (4716)
20. classify_question → business action / hybrid    (4695-4698)
21. conversation_reference / private_state_inquiry / fresh_search (4811-5104)
22. Dynamic Information Collection (Single Source of Truth) (5254+)
23. Legacy INTENT_SCHEMAS path                      (5556+)
24. Execute Business Action                         (5604+)
25. Human Handoff                                   (6569+)
26. Fix-2 boundary                                  (6615+)
27. Safe Fallback                                   (6652+)
28. Production RAG pipeline                         (6702+)  ← run_playground_turn (owners #28-36)
29. Hybrid routing                                  (6825+)
```

Override hazards:
- **#10 (FIX-05) vs #11 (operational-change):** "เปลี่ยนเป็นทางเรือ" in an
  import frame is a slot correction (#10); the same words with NO import
  frame are `change_shipping_method` (#11). Correct today only because
  #10 gates on `_derive_active_frame(...).product`.
- **#16/#17 (FIX-06) vs #13 (SEM-GEN-1):** a fresh "อยากสั่งของจากจีน" must
  skip #13 and reach #16. Correct today only because #16's guard was
  relaxed to `not is_frame_followup(message)` (FIX-06).
- **#9 (shipping/purchase withdrawal) vs #11:** `_wd_yield_to_op` hand-checks
  that a fresh operational request beats a withdrawal family carried by
  the LLM/history.
- **#3 (INVOICE-PRODUCT-REGRESSION-2 rewrite) vs #20 (classify_question):**
  the rewrite of the message string means later branches see
  `"เสื้อผ้านำเข้าได้ไหม"`, not the user's `"เสื้อผ้า"`.
- **#28-36 re-interpret** a turn that #1-27 already decided is "RAG".

---

## 5. Stale-history risks

| Risk | Where | Current mitigation | Residual |
|---|---|---|---|
| Stale IMPORT_INTEREST frame owns a fresh opener | `derive_active_frame` over a 14-turn lookback; `_broad_china_buy`/`_svc_pre_rag` guards | FIX-06 relaxed the guards to `not is_frame_followup` | a fresh **product-slot** turn (`"เสื้อผ้า"`) mid-stale-frame still has no clean owner if `_assistant_asked_for_product` is false |
| History-sticky operational-change ack re-asks for the wrong thing | `_derive_operational_state(history, …)` | `_current_intent_breaks_pending_flow`, `_fresh_opreq.kind != _opreq.kind` | relies on a fresh decisive re-classification each turn |
| Frame staleness is drift-count heuristic | `_FRAME_MAX_GAP = 3`, `_FRAME_EXIT_RE` word list | word list | any journey word not in `_FRAME_EXIT_RE` doesn't expire the frame |
| No journey lifecycle | there is no `status: COMPLETED/CANCELLED/SUSPENDED/EXPIRED` | — | a completed journey is only "gone" once it drifts out of the lookback window |
| `session_service.get_recent_history(max_turns=20)` vs `_FRAME_MAX_LOOKBACK=14` vs RAG history | three different windows | — | the frame, the RAG state, and the operational state each see a different slice |
| Tests seed short synthetic history; prod is 100+ turns | `WebhookConversation` seeds ≤ ~30 | FIX-06 added 30/50-turn replay tests | behaviour past ~50 turns still under-tested |

---

## 6. Exact-regex dependencies (fragile string coupling)

| Dependency | Files | Why fragile |
|---|---|---|
| `_ASSIST_PRODUCT_RES` must match `frame_ack_reply()`'s exact wording | `conversation_semantics.py` | change the ack sentence → frame reconstruction silently breaks (this is exactly the FIX-06 `(สินค้า X)` change; had to add prose patterns in lockstep) |
| `_ASSIST_QTY_RE` / `_ASSIST_METHOD_RE` vs the ack's "จำนวนประมาณ N" / "ขนส่งทางX" | `conversation_semantics.py` | ditto for quantity/method |
| `_ASSISTANT_ASKED_PRODUCT_RE` must match the FIX-01 discovery reply's "…อยากสั่งสินค้าอะไร" | `conversation_semantics.py` | reword FIX-01's `import_interest_reply` → FIX-04 bare-product block stops firing |
| `_ASSIST_IMPORT_DISCOVERY_RE` (FIX-04) matches the FIX-01/frame-ack wording | `conversation_semantics.py` | " |
| `_FOLLOWUP_SHAPE_RES` — 9 hand-tuned shape regexes; `test_not_follow_up_shapes` pins negatives | `conversation_semantics.py` + `tests/test_conversation_semantics.py` | any new shape must be proven against the pinned negatives |
| `_broad_china_buy` regex duplicates the IMPORT_INTEREST recogniser | `decision_engine.py` | two places to widen for "want to buy from China" |
| calculator measurement regex in `_compose` vs `shipping_estimate_flow` | two files | " |
| `pending_confirmation` / `WebhookConversation` reconstruct "collected" params by replaying history text | `webhook.py`, harness | replay ≠ the row's own stored params (Address-Change UAT fix worked around this by passing `pending_parameters` explicitly) |
| PHASE-6D `thai_text_normalizer` must NOT fuzzy-fix at intent time | `thai_text_normalizer.py` | proven unsafe (verb flips); every correction regex downstream therefore has to be typo-tolerant itself (`_FZ_CHANGE_VB`, `_FZ_BE_TH`, …) |

---

## 7. State reconstructed from customer-facing text

**Today there is no structured conversation-state store.** The only
persisted artefacts are:

1. `session_service` — the raw `{role, content}` turn list (text).
2. `pending_confirmation_service` — a row with `pending_action_id` +
   `pending_parameters` + `question_text` (text), created only when the
   turn routed `WORKFLOW` and was incomplete.
3. `handoff_state_service` — issue/episode + status.

Everything else — **the active IMPORT_INTEREST journey, its product /
quantity / shipping-method slots, which slot was last requested, whether
a correction happened** — is recovered on the *next* turn by
`derive_active_frame()` **parsing the assistant's own previous reply
text** with `_ASSIST_PRODUCT_RES` / `_ASSIST_QTY_RE` / `_ASSIST_METHOD_RE`.

Consequences already observed:
- FIX-06 could not simply make the reply prettier — the `(สินค้า X)`
  token WAS the database; it had to be replaced with parseable prose +
  a backward-compat pattern.
- Quantity **unit is lost** (`Frame.quantity: int`) — `"20 คู่"` persists
  as `20`, the ack says `"20 ชิ้น"`. There is nowhere to store `คู่`.
- Weight / dimensions likewise carry no unit and no raw value.
- The RAG pipeline keeps a *second*, independent entity-carry model
  (`rag/conversation_state.py`) also parsed from history text.

---

## 8. Channel parity

| Channel | Entry | Uses `decide()`? | Uses pre-RAG flows (#6-27)? |
|---|---|---|---|
| LINE (prod) | `webhook.py` `_handle_message_via_decision_engine` | **Yes** | Yes |
| LINE (legacy) | `webhook.py` `_handle_message_legacy` → `line_bot/intent.classify` | No | No — **dead** (`DECISION_ENGINE_LIVE_ROUTING=true`) |
| Admin Hybrid Playground — `mode=auto` | `admin/routes.py:3720` | **Yes** (synthetic `playground:<label>` user) | Yes |
| Admin Hybrid Playground — `mode=rag/erp/hybrid` | `run_playground_turn` / `run_erp_test` directly | **No** | No — RAG-only owners #28-36 |
| Admin Playground — `/admin/playground/ask` | `run_playground_turn` directly | **No** | No |
| Admin ERP conversation-tester | `run_erp_test` | No | No |
| Web chat | *no dedicated endpoint found in this repo* | — | — |

**Parity gap:** the manual playground modes and `/admin/playground/ask`
exercise a different, smaller interpretation stack (RAG owners only) than
LINE. "Auto" mode is at parity. There is no third "smarter LINE" brain —
LINE and Playground-auto already share `decide()` — but the *manual*
playground routes are not representative of production.

---

## 9. Summary — what the upgrade has to converge

| Dimension | Now | Target (task §§2-22) |
|---|---|---|
| Interpretation owners (LINE path) | **~33 live** | fewer; ONE canonical `ConversationResolution` owner, regexes demoted to evidence |
| "import interest" recognisers | 7 | 1 |
| "topic switch / reject" recognisers | 4 | 1 |
| Active-state models | 4 partial (frame-from-text, RAG entity carry, operational-sticky, pending row) | 1 structured, persisted, unit-aware |
| Precedence rule | implicit in branch order (~29 branches), re-litigated per fix | 1 explicit ordered rule applied once |
| State store | assistant reply TEXT | structured row; text parsing kept read-only for legacy sessions |
| Units | dropped (`int`) | `{value, unit, canonical}` |
| Multi-intent in one turn | not parsed (`"รองเท้า 20 คู่ ส่งทางเรือ"` → asks all three again) | parsed once; ask only genuinely-missing slots |
| Channels | LINE + Playground-auto at parity; manual playground diverges | all channels via the same core |
| Route timing | message → (branch cascade) → RAG → re-interpret | message → meaning → state → source → execute → plan reply |
| Observability | `developer_trace` dict, ad-hoc keys | structured `resolution{…}` per turn |

---

## 10. Proposed phasing (for approval before any code)

The task is large; `docs/CLAUDE.md` (Architecture Decision Policy) and
task §23 both require no destructive migration without approval. Suggested
order, each phase independently shippable with `NEW REGRESSION DELTA = 0`:

- **P1 — Canonical `ConversationResolution` object (additive).**
  `services/conversation_resolution.py`: one function that composes the
  existing `interpret()` + `resolve_frame_correction()` +
  `_followup_op()` + frame derivation into the §2 struct. `decide()`
  builds it once at the top; existing branches start *reading* from it.
  No branch removed yet. No persistence change.

- **P2 — Structured conversation-state row (additive, non-destructive).**
  New table `conversation_frames` (or a JSON column on the existing
  session row) holding `{journey, status, slots{value,unit,canonical},
  requested_slot}`. `derive_active_frame()` becomes: read the row; if
  absent, fall back to today's text parse (legacy sessions). Write the
  row on every turn that touches a journey. Migration is additive only.

- **P3 — Unit-preserving slots.** `Frame` / resolution entities carry
  `{value, unit, canonical}`. Calculator keeps consuming `canonical`.
  Acks restate the original unit.

- **P4 — Demote duplicate recognisers to evidence.** The 7 import-interest
  / 4 topic-switch recognisers become inputs to the P1 resolver, which
  makes the single call. Delete none until the resolver is proven.

- **P5 — One precedence function.** `resolve_precedence(resolution,
  active_frame, pending)` returning the ordered §4 winner; every branch
  that currently re-checks precedence calls it.

- **P6 — Response Planner split** (§14): branches return
  `(facts, next_action, missing_slots)`; a planner renders Thai.

- **P7 — Generalisation lab** (§19-21): 500 unseen cases, channel-parity
  suite, long-session replay.

- **P8 — Retire legacy path + manual-mode divergence** (§15): route
  `/admin/playground/ask` through `decide()`; delete
  `_handle_message_legacy` + `line_bot/intent.py`.

**Interpretation owners — target AFTER P4: ~12** (the standalone
business classifiers #14, #17-21, #23 stay — they answer "which action /
which policy", not "what does the sentence mean"; the ~15 meaning
owners collapse to 1 resolver + its evidence inputs).
