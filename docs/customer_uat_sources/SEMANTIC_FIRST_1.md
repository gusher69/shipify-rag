# SEMANTIC-FIRST-1 — central conversational semantic interpretation

## What changed

`services/conversation_semantics.py` is generalised from an
import-interest follow-up backbone into the **one** semantic
interpretation layer. New public surface:

- `interpret(message, history, context) -> Interpretation`
- `Interpretation{intent_family, entities, is_private, follow_up_op, confidence, source}`
- `INTENT_FAMILIES`, `FOLLOWUP_OPS`, `FAMILY_TO_ACTIONABLE_INTENT`

`interpret()` is a **compositional meaning model** — orthogonal marker
dimensions (OBJECT / ACTION / ROLE / FOLLOW-UP) detected independently
and composed into a family, so paraphrases that share meaning collapse to
the same family without an exact-phrase rule. A single gated LLM call
(`_llm_family`, same pattern as `resolve_followup`) only disambiguates
genuinely novel / ambiguous phrasing and **always degrades** to the
deterministic result (and self-disables for the process after the first
unreachable-LLM error). Genuinely structural inputs (empty, bare id,
URL, numeric-only, postback) skip semantics entirely.

### The required conversational path (now in force)

```
Natural language
  → conversation_semantics.interpret()                (ONE central result)
  → normalised {intent_family, entities, is_private, follow_up_op}
  → deterministic business policy / workflow gates
  → RAG / ERP / Calculator / Charter / Human CS
```

`DecisionEngine.decide()` calls `interpret()` once, near the top
(right before intent resolution), stores it in
`developer_trace["semantic_interpretation"]`, and:

| Consumer | How it consumes the central result |
|---|---|
| `classify_actionable_intent(message, interpretation=…)` | `intent_family` → actionable bucket via `FAMILY_TO_ACTIONABLE_INTENT` is the **primary** signal; the regex `_classify_actionable` is the fallback (family `UNKNOWN` / no mapping). |
| `shipping_estimate_flow.derive_estimate_state(…, interpretation=…)` | `intent_family == SHIPPING_ESTIMATE` opens a fresh calculation; `follow_up_op ∈ {CORRECTION, COMPARISON}` continues the thread. `_CALC_VERB_RE` is now the fallback. |
| RAG answer-planner (`run_playground_turn(…, interpretation=…)` via `_run_rag_pipeline`) | same `classify_actionable_intent` call, so the RAG intent is the SAME central result. |
| SEM-GEN-1 frame branch | unchanged — already semantic; `interpret()` reuses `derive_active_frame` / `resolve_followup` for `follow_up_op`. |

Family → bucket map:

| family | actionable_intent | family | actionable_intent |
|---|---|---|---|
| SHIPMENT_STATUS | tracking_status | CHARTER_TRUCK | service_information |
| INVOICE | invoice_policy | SHIPPING_ESTIMATE | shipping_calculation |
| PICKUP_LOCATION | warehouse_location | ADDRESS_CHANGE | *(None → Business-Action routing)* |
| SELF_PICKUP | self_pickup_permission | MY_COUPONS | *(None → private-state routing)* |
| COUPON_USAGE | coupon_policy | IMPORT_INTEREST / GENERAL / UNKNOWN | *(None → regex / RAG)* |
| PRODUCT_POLICY | prohibited_goods | | |

## Remaining raw-text (regex) routes and why each stays

Per the spec, regex is retained **only** for structural extraction,
validation, and safe fallback. Every remaining raw-text route:

| Location | Role | Why it stays raw |
|---|---|---|
| `rag/query_understanding._classify_actionable` | actionable-intent **fallback** | Runs only when the central family is `UNKNOWN` / unmapped, or when the caller has no `Interpretation` (Playground, benchmark, `lead_stage_service`, `conversation_state` re-derivation). Deterministic, no LLM — safe default. |
| `services/decision_engine.classify_turn_intent` | SHIPIFY_INFORMATION vs PRIVATE_ACTION vs AMBIGUOUS for the `exclude_private` **security gate** | Public/private authorization must stay deterministic (spec). ~20 documented production fixes depend on its exact output. `interpret().is_private` is an independent deterministic cross-check available in the trace; it does not override this gate. |
| `services/decision_engine._classify_private_state_inquiry` | private-record-state detection for identity gating | Same deterministic-security rationale. |
| `services/hybrid_question_classifier.classify_question` | Business-Action **selection** scoring (keyword overlap with each configured action's own vocabulary) | This is config-driven action *matching*, not intent *understanding* — it scores against per-tenant Business Action metadata, which the central family vocabulary does not model. |
| `services/charter_truck_flow` (`_BILL_MARKER_RE`, `_DEST_MARKER_RE`, `_NAME_MARKER_RE`, `_PHONE_RE`, `_TC19_ANSWER_RE`, `_CHARTER_*`) | slot **extraction** + history-state reconstruction | Explicitly allowed: ShipmentCode / phone / destination / recipient extraction and "assistant-reply-is-the-state" scanning are structural, not intent classification. The fresh charter *intent* is now `CHARTER_TRUCK` from `interpret()`. |
| `services/shipping_estimate_flow` (`_WEIGHT_RE`, `parse_dimension_input`, `_method_of`, `_EST_*_RE`, `_CALC_VERB_RE`) | weight / dimensions / route-token **extraction** + episode lifecycle + **fallback** opener | Value extraction is explicitly allowed. `_CALC_VERB_RE` / `_CALC_TOPIC_RE` now only open the flow when no `Interpretation` says `SHIPPING_ESTIMATE` (degraded-LLM safety net). |
| `rag/canonical_query._PICKUP_LOCATION_INTENT_RE` | pre-retrieval canonical-query rewrite | Secondary: the central `PICKUP_LOCATION` family already drives `warehouse_location`; this regex only shapes the retrieval query string and stays as a degraded-path fallback. |
| `services/playground_orchestrator` pre-synthesis branches (`_is_invoice_issuance_question`, `is_self_pickup_permission`, bare-math, dimension-slot) | deterministic answer selection **on top of** `actionable_intent` | These branch on `intent_result["actionable_intent"]`, which is now semantic-first when `run_playground_turn` receives the `interpretation`. The extra regex only refines the *wording* of an already-chosen family (e.g. issuance vs download). |

## Acceptance

`tests/test_semantic_first_1.py`:

- **Unseen paraphrase per family** — 24 paraphrases, none appearing in
  the production examples, across all 10 families, all resolve
  SEMANTICALLY on the deterministic tier (LLM offline in CI).
- Structural inputs skip semantics; greetings / confirmations →
  `UNKNOWN` (never the LLM tier).
- Privacy stays deterministic (`is_private` from self-reference, does
  not change the family; SELF_PICKUP stays public even with a pronoun).
- Follow-up ops: CORRECTION / COMPARISON / TOPIC_CHANGE / SET_VALUE.
- `classify_actionable_intent` is semantic-first with an interpretation,
  byte-for-byte unchanged without one.
- Downstream E2E: `decide()` carries the interpretation on every turn;
  an estimate paraphrase opens the calculator (not Fix-2 no-info); a
  shipment-status paraphrase is `tracking_status`; a charter paraphrase
  is `service_information`.

Regression: `test_decision_engine`, `test_customer_uat_fix1/fix2`,
`test_webhook`, `test_intent_unification`, `test_p1_2a`, `test_p2`,
`test_p3`, `test_company_overview…`, `test_safe_query_understanding…`,
`test_conversation_semantics`, `test_customer_calc1`,
`test_customer_calc11…`, `test_customer_rag2_1_charter_slots`,
`test_customer_invoice1`, `test_customer_rag1_pickup_location`,
`test_sem1_private_state_routing`, `test_ppc1…`, `test_slot_filling…`,
`test_fix23…` — only the pre-existing known failures.

## Status

**CODE PASS / DEPLOYED / READY FOR FINAL MANUAL UAT.** Customer
acceptance is NOT declared. No REAL LINE testing was performed.
