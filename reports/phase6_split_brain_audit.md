# Phase 6 — Split-Brain / Competing-Authority Audit (Phases E & H)

Every component on the live decision path that **independently re-derives**
a fact another component already decided. Found by reading the path end to
end and by the fixes this pass required: when the same defect has to be
fixed twice in two files, those two files are a split brain.

Live path audited:

```
USER MESSAGE -> normalization -> semantic interpretation -> entity extraction
 -> ConversationResolution -> current-turn slot updates -> state/frame
 -> intent/journey -> grounding -> route selection -> next-best-action
 -> RAG / ERP / workflow / Human CS -> response planning -> final response
```

---

## 1. Product-noun extraction — TWO implementations (STILL DUPLICATED)

| Owner | File | Used by |
|---|---|---|
| `_product_interest_noun` | `services/playground_orchestrator.py` | fresh IMPORT_INTEREST opener (pre-RAG branches in `decision_engine.py`, `service_intent_flow.import_interest_reply`) |
| `_bare_product_noun` | `services/conversation_semantics.py` | bare slot reply / correction path — **and reused by `services/conversation_resolution.py` (P1 imports it)** |

**Evidence they are a split brain:** the compound-noun truncation defect
(`ชั้นวางของ` → `ชั้นวาง`, `ของเล่น` → `เล่น`, `กล่องใส่ของ` → `กล่อง`) existed
in *both* and had to be fixed *twice*, in two different regexes, with the
same rule.

**State after this pass:** both now implement the *same single rule* — a
generic formant (`สินค้า` / `ของ`) is a placeholder **only when the entire
remnant is that bare word**, never when it is part of a longer surviving
span. Both are covered by the same adversarial matrix
(`tests/test_phase6_system_invariants.py::TestProductNounAdversarial`,
which asserts the two extractors *agree* on every noun).

**Not consolidated, and why:** the two have legitimately different filler
vocabularies (an opener carries interest verbs and an origin — "อยากสั่ง…
จากจีน"; a bare reply carries answer particles — "เป็น…ครับ"). Merging them
means designing one vocabulary that serves both roles, which is a
redesign of the entry point to every import-journey turn — out of
proportion to a bug-fix pass, and explicitly not something to attempt
without its own generalization lab. **Recommended next task (P2):** extract
a shared `placeholder_or_noun(remnant)` primitive both call, leaving the
strip vocabularies role-specific.

## 2. Quantity + unit vocabulary — TWO regexes (STILL DUPLICATED)

| Owner | File |
|---|---|
| `_USER_QTY_RE` | `services/conversation_semantics.py` (legacy runtime; **imported** by `playground_orchestrator.py`, so that consumer is already shared) |
| `_QTY_RE` / `_COUNT_UNIT` | `services/conversation_resolution.py` (P1 canonical) |

**Evidence:** the two had silently drifted — `ขวด` and `พาเลท` were
recognised by the legacy runtime and invisible to P1. Synced this round,
with a test that walks the legacy vocabulary and asserts P1 recognises
every unit in it
(`tests/test_phase6_slot_consumption.py::TestP1QuantityUnitVocabularySync`),
so the next drift fails a test instead of silently skewing P2.1 parity
telemetry.

**Not consolidated, and why:** P1's regex also carries canonicalisation
(weight→kg, dimension→cm) the legacy one does not. The drift risk is now
covered by a test, which is the cheap half of the fix.

## 3. Intent/family naming — deterministic vs gated LLM (ARBITRATED)

`_compose()` (deterministic) and `_llm_family()` (gated LLM) both name a
family; `_deterministic_context_authority()` (P2-STAB) arbitrates, and the
LLM may not override a high-confidence deterministic context.

**Residual weakness:** where the LLM names a family the deterministic tier
never reaches, there is no arbiter — only per-class exclusions
(`_TRANSIT_TIME_Q_RE`, and `_DELIVERY_CAPABILITY_Q_RE` added this pass for
"จัดส่งสินค้าถึงหน้าบ้านเลยไหม"). Each newly discovered LLM over-reach
currently needs a new exclusion. **Recommended:** invert it — require a
positive ownership/record signal before an LLM family name may synthesise
a *private* inquiry, instead of enumerating public exceptions.

## 4. Private-state recognition — TWO paths (BY DESIGN, NOW GUARDED)

`_classify_private_state_inquiry()` (deterministic) and the SEMANTIC-FIRST-2
synthesis block in `decision_engine.py` (from `semantic.intent_family`).
The second exists as a deliberate fallback for novel phrasings; it is the
one that mis-fired on the delivery-capability question. Guarded this pass.

## 5. Slot collection — THREE state machines (BY DESIGN)

| Engine | Scope |
|---|---|
| `services/slot_filling_engine.py` | the 6 narrow ERP intents (tracking/order/customer/warranty/invoice-lookup/payment), own schemas + validators |
| the IMPORT_INTEREST frame in `conversation_semantics.py` | product / quantity / method |
| `EstimateState` in `services/shipping_estimate_flow.py` | weight / dimensions / method |

Three independent notions of "what is still missing". They do not
currently contradict each other because their slot namespaces are
disjoint, but "known slot must not be re-asked" is enforced separately in
each. **Recommended (P3, not now):** one collection contract.

## 6. Conversation state — legacy vs P1 vs P2 (SHADOW, NOT CUT OVER)

`derive_active_frame()` (legacy, text-derived) remains the **sole read
authority**, exactly as instructed. P1 `ConversationResolution` and the P2
structured frame are shadow-only. No read cutover was performed and P3 was
not started.

---

## Consolidation actually performed this pass (Phase H)

* **Charge-basis truth** now renders from the single `RATES` table that
  `compute_estimate()` charges from (`rate_basis_answer()`), so the
  explanation can never drift from what the calculator applies — instead
  of restating rates by hand in a reply template.
* **One placeholder rule** now governs both product-noun extractors
  (§1), replacing two different ad-hoc strips.
* **P1/legacy unit vocabulary** synced and test-locked (§2).

## Deliberately NOT done

* No merge of the two product-noun extractors (§1) — redesign, not a fix.
* No global structured-state read cutover; no P3.
* No new parser was introduced anywhere: every fix this pass either
  reused an existing canonical source or removed a duplicate rule.
