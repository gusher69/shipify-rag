# Conversation Intelligence — P1: Canonical Conversation Resolver

**Status:** implemented, shadow-first, additive. No DB / schema change
(P2 not yet approved). Baseline runtime before P1: `21be623`; audit:
`af3a323`.

## What P1 adds

`services/conversation_resolution.py` — one function,
`resolve_conversation(message, history, context, *, semantic=…)`, that
**composes the already-trusted capabilities** into a single typed
`ConversationResolution`:

| field | source it composes |
|---|---|
| `conversation_act` (ANSWER / CORRECTION / CONFIRMATION / REJECTION / TOPIC_SWITCH / NEW_INTENT / QUESTION / GENERAL_CHAT / UNKNOWN) | `interpret().conversation_act` + `_followup_op` + `resolve_frame_correction` + confirm/greet shapes |
| `primary_intent` | `interpret().intent_family` (+ GENERAL_ASSISTANCE label, + journey carry on a correction) |
| `active_journey` | `derive_active_frame(history)` |
| `topic_switch` | family, when an explicit non-journey family fires inside an active journey |
| `entities` / `slot_updates` / `slot_corrections` | new deterministic multi-entity extractor + `resolve_frame_correction` + `interpret().entities` |
| `known_slots` / `requested_slot` / `missing_slots` | frame slots + last-assistant-turn "what did we ask" |
| `grounding_requirement` (CONVERSATION / GENERAL / BUSINESS_RAG / PRIVATE_ERP / CALCULATOR / WORKFLOW / MIXED) | `request_grounding_classifier` + family map |
| `precedence_winner` | `resolve_precedence()` — the ONE explicit order |
| `evidence` | structured signals only (`llm_semantic_signal`, `frame_signal`, `frame_correction_signal`, `regex_signal`, `requested_slot_match`) — no prose, no chain-of-thought |

### Unit-preserving value model

Every measured slot is a `SlotValue{value, unit, canonical_value,
canonical_unit, raw}`. `"20 คู่"` → `value=20, unit="คู่"`;
`"520x240x120 mm"` → `value="520x240x120", unit="mm",
canonical_value="52x24x12", canonical_unit="cm"`. **No DB write in P1** —
this only prepares P2/P3.

### Explicit precedence (task §3/§4)

`resolve_precedence()` returns one of, in order:
`NEW_INTENT` › `CORRECTION_REJECTION_TOPIC` › `REQUESTED_SLOT_ANSWER` ›
`ACTIVE_JOURNEY` › `PENDING_WORKFLOW` › `STALE_HISTORY`. Nothing here
reads `decide()` branch ordering.

## Integration (once per turn)

`DecisionEngine.decide()` builds `resolution` **once**, right after
`semantic` / `actionable_intent`, wrapped in try/except so the shadow
layer can never break a turn. It is logged at
`developer_trace["conversation_resolution"]`.

### Migrated safe consumer (P1)

The **FIX-05 frame-correction / reject / clarify block** now reads
`resolution.frame_correction` instead of calling
`_resolve_frame_correction()` a second time (identical value — computed
once). Nothing else is migrated. Auth, ERP truth, business policy,
money, and confirmed execution are untouched.

## Small enabling change to `resolve_frame_correction`

Two additive recognitions (needed for §5-D / §8 / the lab, typo-tolerant,
no phrase maps):
- a method the customer already picked, re-named to a different one
  without an explicit "เปลี่ยน" verb — `"ทางเรือดีกว่า"`,
  `"ไม่เอา เอาทางเรือ"` — is a method correction;
- `"เอาเป็น X"` (take-as, no `"แทน"`) is a product correction.

## Results

### Generalisation lab (`reports/p1_resolution_summary.md`)

302 unseen cases (100 multi-turn, 51 corrections, 65 topic-switch/reject,
25 multi-entity, 25 typo+context, 20 long-history, 12 referents, 4 §5
anchors):

| dimension | accuracy |
|---|---|
| conversation act | 100% |
| contextual intent | 100% |
| active journey | 100% |
| topic switch | 100% |
| correction | 100% |
| multi-entity | 92% |
| unit preservation | 100% |
| structured-token corruption | 0 |

### Shadow divergence (`reports/p1_resolution_divergences.json`)

13 curated production turns (FIX-01…06 repros + topic switch + private
status + policy question + link + calculator + greeting): **13 MATCH,
0 RESOLVER_WRONG, 0 divergence**. The resolver's conversational
decisions agree with existing production routing on every turn.

## Not in P1

- no `conversation_frames` table / schema change (P2 — needs approval);
- legacy text-based frame recovery kept as the store;
- the other ~30 interpretation owners still run — P4 demotes them once
  the resolver is proven in production.
