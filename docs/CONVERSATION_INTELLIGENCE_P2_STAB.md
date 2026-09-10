# Conversation Intelligence — P2 stabilization: LLM authority rule

**Stage:** P2 stabilization. No read cutover. No P3. Legacy
`derive_active_frame` still the runtime authority.
**Baseline:** `211f2336ba716ed6d398a41ec7882888f107ed89`.

---

## 1. The finding — LLM override of a high-confidence deterministic context

The owner reported: with the **real production LLM tier**, the gated
`_llm_family` call inside `conversation_semantics.interpret()` could
reclassify a turn whose deterministic contextual meaning was already
high-confidence.

### What actually reproduces

| trace point | value for `"เป็นชั้นวางของ"` after the FIX-01 discovery reply |
|---|---|
| normalizer | unchanged |
| deterministic `_compose` | `UNKNOWN` (0.0) |
| requested-slot / FIX-04 block | `_assistant_asked_for_product` **True**, `_looks_like_bare_product` **True**, `_import_journey_active` **True** → returns `IMPORT_INTEREST` (source `deterministic`) — **before the LLM gate** |
| `_llm_family` (real key, ×3) | also `IMPORT_INTEREST` |
| **final family** | `IMPORT_INTEREST` (deterministic) |

With the **correct** production history present, `"เป็นชั้นวางของ"` does
**not** reach `_llm_family` — the FIX-04 block returns first. The
`SAFE_FALLBACK` seen once during P2 activation was a **test-harness
artefact** (the assistant reply was not threaded into history, so
`_assistant_asked_for_product` / `_import_journey_active` were both
false and the FIX-04 block was skipped).

### Where an override CAN still happen (and did, for other inputs)

The LLM gate (`if fam in ("UNKNOWN","GENERAL") or conf < 0.5: llm =
_llm_family(...)`) runs whenever `_compose` was inconclusive **and** the
FIX-04 block did not return. That leaves these high-confidence
deterministic context signals unprotected:

- a compatible answer to a **quantity** / **shipping-method** slot the
  assistant just requested (`"30 คู่"`, `"ทางเรือ"`);
- an active-frame **follow-up op** (`CORRECTION` / `COMPARISON` /
  `CONTINUE` / `SET_VALUE`);
- an explicit **journey rejection** (`"ไม่เอาแล้ว"`, `"ยกเลิก"`) —
  observed: the real LLM classified a bare `"ไม่เอาแล้ว"` as
  `PURCHASE_WITHDRAWAL`, and `"ยกเลิก"` likewise.

## 2. The authority rule (task §2/§3)

`services/conversation_semantics.py::_deterministic_context_authority()`
— consulted immediately **before** the `_llm_family` gate. When a
high-confidence deterministic CONTEXT resolution exists it is returned
as canonical and the LLM is **not consulted**:

| signal | canonical result |
|---|---|
| `_FRAME_CANCEL_RE` match while a journey is open | `intent_family = UNKNOWN`, `conversation_act = "REJECT"` (the decision engine's frame-reject / `phase6b_reject_reevaluate` path owns it — never a guessed family) |
| active frame + `follow_up_op ∈ {CORRECTION, COMPARISON, TOPIC_CHANGE, CONTINUE, SET_VALUE}` | `IMPORT_INTEREST` attributed to the frame product, op preserved |
| assistant asked **quantity** + turn is a bare number/quantity | `IMPORT_INTEREST`, `op = SET_VALUE` |
| assistant asked **method** + turn is a bare method word | `IMPORT_INTEREST` (+ `method` entity), `op = SET_VALUE` |

It **never** fires on an explicit topic switch (`_TOPIC_RE`), a
restriction/eligibility question (`_RESTRICTION_Q_RE`), or a
reject-of-previous-answer (`_REJECT_ACT_RE`) — those keep routing on
their own deterministic `_compose` families. The `product`-slot case is
still owned by the FIX-04 block just above it.

The gated LLM is **not removed or weakened** — it still runs for genuine
`UNKNOWN` / low-confidence / no-context turns (`"ร้านส่งของมาหรือยังคะ"`,
`"งงอะ อธิบายอีกที"`, unseen wording, referents, typo recovery).

## 3. P1 precedence alignment

The rule enforces P1 precedence at the meaning layer:

```
NEW_INTENT > CORRECTION_REJECTION_TOPIC > REQUESTED_SLOT_ANSWER
          > ACTIVE_JOURNEY > PENDING_WORKFLOW > STALE_HISTORY
```

`REQUESTED_SLOT_ANSWER` (tier 3) and `CORRECTION_REJECTION_TOPIC`
(tier 2) now beat a conflicting generic LLM family, which is tier-6
evidence at best.

## 4. Scope

Only `conversation_semantics.py` changed (one helper + one guard before
the LLM gate). No routing branch, no auth, no ERP, no business truth, no
DB, no P2 persistence change. Legacy `derive_active_frame` stays the
runtime authority — this is still shadow-only for structured state.
