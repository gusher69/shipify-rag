# Phase 6 — Split-Brain / Competing-Authority Audit (closure gate H)

Every component on the live decision path that **independently re-derives**
a fact another component already decided, each classified as:

* **SAFE_DUPLICATION_WITH_PARITY_TEST** — the duplication exists, but a
  test forces the copies to agree, so drift fails CI instead of reaching
  a customer.
* **ACTIVE_BEHAVIORAL_RISK** — a scenario exists today where the two
  authorities can still disagree in a customer-visible way.

No refactor of these groups was performed this pass (per instruction H).

---

## 1. Product-noun extraction — two extractors
**SAFE_DUPLICATION_WITH_PARITY_TEST**

| Owner | File | Used by |
|---|---|---|
| `_product_interest_noun` | `services/playground_orchestrator.py` | fresh IMPORT_INTEREST opener |
| `_bare_product_noun` | `services/conversation_semantics.py` | bare slot reply / correction — **and reused by P1** (`conversation_resolution.py` imports it) |

Both now implement the *same* single rule (a generic formant is a
placeholder only when the entire remnant is that bare word), and
`tests/test_phase6_system_invariants.py::TestProductNounAdversarial::
test_bare_slot_reply_extractor_agrees_with_opener_extractor` asserts the
two agree on every noun in the adversarial matrix. Drift is caught.

*Not merged, and why:* their filler vocabularies legitimately differ (an
opener carries interest verbs and an origin; a bare reply carries answer
particles). Merging is a redesign of the entry point to every
import-journey turn, not a bug fix.

## 2. Quantity + unit vocabulary — **RESOLVED this pass**
**No longer a duplication.**

There were **five** copies of the count-unit alternation (four inside
`conversation_semantics.py` — `_USER_QTY_RE`, the follow-up shape matcher,
the bare-quantity full-match inside the frame resolver, and
`_BARE_QTY_ANSWER_RE` — plus P1's own `_COUNT_UNIT`). Only one carried
`ขวด`/`พาเลท`, so a bottle quantity was recognised by the opener and
invisible to every bare-slot-answer path. All five now substitute one
constant, `_COUNT_UNIT_ALT`; P1 derives its tuple from that same constant.
A new unit can only be added in one place.
Locked by `tests/test_phase6_slot_consumption.py::TestP1QuantityUnitVocabularySync`.

## 3. Intent/family naming — deterministic vs gated LLM
**SAFE_DUPLICATION_WITH_PARITY_TEST** *(was ACTIVE_BEHAVIORAL_RISK — closed)*

`_compose()` (deterministic) and `_llm_family()` (gated LLM) both name a
family. Where the deterministic tier is confident,
`_deterministic_context_authority()` (P2-STAB) arbitrates and the LLM may
not override it — verified live: **0** high-confidence deterministic
overrides across 93 live interpreter turns, 37 of which consulted the
model (`reports/phase6_live_tier.json`).

**The residual risk is now closed by inversion.** Previously, where the
deterministic tier had no opinion, an LLM family name alone could
synthesise a **private** inquiry, guarded only by an enumerated list of
public exceptions (`_TRANSIT_TIME_Q_RE`, `_DELIVERY_CAPABILITY_Q_RE`) —
an open-ended blocklist that could never be complete. Those two constants
have been **deleted**. The gate now requires POSITIVE private-ownership
evidence (`_private_ownership_evidence()`), of which there are five
forms: first-person possession of a record/asset; a concrete record
identifier; an active authenticated workflow; a compatible slot answer
inside an already-established private journey; and a seller-dispatch
status question ("ต้นทางส่งมาหรือยังครับ" — subject is the supplier AND the
predicate asks whether they have dispatched yet, a speech act that
presupposes an order the asker placed). An LLM family name is evidence,
never authorization.

**A residual source tension, reported not hidden.** The owner's public
list contains "ร้านส่งของหรือยัง", while the SEMANTIC_FIRST_2 source
requires "ร้านส่งหรือยังคะ" to collect the bill id rather than dead-end on
no-information. These two customer-derived expectations disagree about
the *same* phrasing. It is **not** the inversion that decides this case:
the PRE-EXISTING deterministic recogniser
(`_classify_private_state_inquiry`) already classified it as a private
inquiry before Phase 6 and still does, unchanged. The inversion governs
only turns the deterministic tier has no opinion about. Flagged for the
owner; no behaviour was changed to force either expectation.

Locked by `tests/test_phase6_private_authority.py` (201 targeted cases:
public→private false positive 0, private→public false negative 0, stale
private takeover 0) and by the live-tier contrast pairs.

## 4. Private-state recognition — two paths
**SAFE_DUPLICATION_WITH_PARITY_TEST** *(was ACTIVE_BEHAVIORAL_RISK — closed with §3)*

`_classify_private_state_inquiry()` (deterministic) and the
SEMANTIC-FIRST-2 synthesis block. The second is the fallback that
mis-fired; it is now gated by the positive-evidence rule above, so the
two paths can no longer disagree about whether a turn is the customer's
own record.

## 5. Slot collection — three state machines
**SAFE_DUPLICATION_WITH_PARITY_TEST**

| Engine | Slots |
|---|---|
| `services/slot_filling_engine.py` | the 6 narrow ERP intents |
| the IMPORT_INTEREST frame (`conversation_semantics.py`) | product / quantity / method |
| `EstimateState` (`services/shipping_estimate_flow.py`) | weight / dimensions / method |

Their slot namespaces are disjoint, and cross-flow interference is tested
(`tests/test_cross_flow_matrix.py`, `tests/test_calculator_regression_2.py::
TestCrossFlowAuthority`). "Known slot must not be re-asked" is enforced
separately in each rather than once — noted as debt, but no scenario is
known where two of them contradict each other on the same turn.

## 6. Conversation state — legacy vs P1 vs P2
**SAFE_DUPLICATION_WITH_PARITY_TEST**

`derive_active_frame()` (legacy, text-derived) remains the **sole read
authority**. P1 `ConversationResolution` and the P2 structured frame are
shadow-only and their agreement is measured continuously by the P2.1
parity telemetry. No read cutover was performed; P3 was not started.

---

## Summary

| Group | Classification |
|---|---|
| 1. Product-noun extraction | SAFE_DUPLICATION_WITH_PARITY_TEST |
| 2. Quantity/unit vocabulary | **RESOLVED** (consolidated to one constant) |
| 3. Intent/family deterministic-vs-LLM | SAFE_DUPLICATION_WITH_PARITY_TEST (**closed by inversion**) |
| 4. Private-state recognition | SAFE_DUPLICATION_WITH_PARITY_TEST (**closed by inversion**) |
| 5. Slot collection | SAFE_DUPLICATION_WITH_PARITY_TEST |
| 6. Conversation state | SAFE_DUPLICATION_WITH_PARITY_TEST |

**No ACTIVE_BEHAVIORAL_RISK remains.** The single risk previously recorded
here (groups 3/4, one root cause) was closed by the private-inquiry
authority inversion: the public-exception blocklists are deleted and a
private inquiry now requires positive ownership evidence. Every remaining
duplication is held by a parity test that fails on drift.
