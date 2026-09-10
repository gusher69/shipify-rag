# Conversation Intelligence — P2: structured conversation state (SHADOW-WRITE)

**Stage:** P2, shadow-write only. No routing cutover. Runtime authority
for journey/slot reads stays with the legacy
`services/conversation_semantics.py::derive_active_frame` (assistant-reply
text parsing) until frame parity ≥ 99% (P2 read-cutover, separately
gated).

**Baseline runtime:** `a82a47259c1dfe05d29771a7d981cc2eaa5b97df` (P1).

---

## 1. Storage choice

**One nullable additive JSONB column, `ai_sessions.conversation_frame`.**

`ai_sessions` is already the per-conversation object — migration
`037_handoff_state.sql` made exactly this call ("Reuses the SAME
ai_sessions row as the conversation object rather than a new table") and
added `handoff_status` / `handoff_reason` / `handoff_notified_at` the same
way. P2 follows that precedent:

- no new table, no new infrastructure;
- `session_service.get_conversation_frame()` / `save_conversation_frame()`
  mirror `get_handoff_state()` / `set_handoff_status()` — both degrade to
  `None` / no-op if the column is absent (pre-migration);
- rollback = stop touching the column (feature is inert without the code
  path) or `DROP COLUMN IF EXISTS conversation_frame` — no data
  migration, no backfill.

## 2. Migration

`migrations/048_conversation_frame_shadow_state.sql` —
`ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS conversation_frame JSONB;`
plus a `COMMENT ON COLUMN`. Idempotent. **Additive only. No destructive
ALTER, no backfill, rollback documented in the file header.**

The code is deploy-safe *before* the migration runs: every read returns
`None` and every write is a caught no-op, so shadow-write simply does
nothing until the column exists. Apply the migration in Supabase when
ready.

## 3. Structured frame schema (`version: 1`)

```
{
  "version": 1,
  "journey": "IMPORT_INTEREST" | null,
  "status":  "ACTIVE" | "SUSPENDED" | "COMPLETED" | "CANCELLED" | "EXPIRED" | null,
  "requested_slot": "product" | "quantity" | "shipping_method" | ... | null,
  "slots": {
    "<name>": {
      "value": ..., "unit": ..., "canonical_value": ..., "canonical_unit": ..., "raw": "..."
    }
  },
  "updated_at": "<iso>",
  "turn_seq": <int>,
  "source": "conversation_resolution",
  "source_turn_id": "<optional>"
}
```

Slots a journey may (not must) carry: `product, quantity, shipping_method,
weight, dimensions, brand, platform`. Quantity/weight/dimensions keep the
**raw semantic unit** (`20 คู่` → `value 20, unit คู่`) and a canonical
conversion where it is mathematically valid (`5000 กรัม` → `canonical
5.0 kg`; `520 mm` → `canonical 52 cm`). Units are never normalised to
`ชิ้น`.

## 4. Build & precedence (`services/conversation_frame_store.py::build_frame`)

Pure function `build_frame(prev_frame, resolution)` — next structured
frame from the previously-persisted one + this turn's P1
`ConversationResolution`, applying the P1 precedence order:

| precedence | frame effect |
|---|---|
| 1 explicit NEW intent (journey) | create/RESET the frame for that journey; slots := this turn's updates |
| 1 explicit NEW intent (non-journey) | SUSPEND the previous journey; never mutate its slots |
| 2 REJECTION | previous journey → `CANCELLED` |
| 2 TOPIC_SWITCH | previous journey → `SUSPENDED`, slots untouched |
| 2 CORRECTION | apply `slot_corrections` to the frame (unit of a bare-value correction inherited; a unit the customer gives this turn wins) |
| 3 requested-slot ANSWER / 4 active-journey continuation | merge `slot_updates` |
| 5 pending workflow / 6 stale history | carry the previous frame unchanged (only refresh `requested_slot`) |

`build_frame` never mutates `prev`.

## 5. Shadow-write wiring (3 points, all additive)

1. `line_bot/webhook.py` — loads `ai_sessions.conversation_frame` into
   `decide_context["conversation_frame"]` before `decide()`; after
   `decide()` persists `result["conversation_frame"]` back. Both wrapped,
   non-fatal.
2. `services/decision_engine.py::decide()` — right after building the P1
   `resolution`, builds `structured_frame = build_frame(prev, resolution)`
   and `parity = frame_parity(legacy_frame, structured_frame, resolution)`;
   logs both at `developer_trace["conversation_frame"]`; returns
   `result["conversation_frame"]` for the adapter. Entire block is
   `try/except` — it can never break a turn. **Not read by any routing
   branch.**
3. `services/session_service.py` — the two degrade-safe accessors.

## 6. Parity classification (`frame_parity`)

`MATCH | STRUCTURED_IMPROVEMENT | LEGACY_CORRECT | STRUCTURED_WRONG |
AMBIGUOUS`, with the P1 resolution as a tiebreaker:

- a slot the CURRENT turn corrected/answered is *expected* to differ from
  the legacy text-derived frame (legacy lags a turn) → STRUCTURED_IMPROVEMENT;
- a fresh explicit opener where the legacy frame still exposes a stale
  product but structured reset it → STRUCTURED_IMPROVEMENT (this is the
  stale-journey-takeover P2 prevents);
- a legacy value containing digits (`_import_noun` garbage like `"20คู่"`)
  vs a clean structured value → STRUCTURED_IMPROVEMENT.

## 7. Lab results (`tests/test_p2_conversation_frame.py`, `reports/p2_frame_*.{json,md}`)

`_llm_family` forced off in the lab so it is deterministic regardless of
any real key in the environment.

- **205 cases / 1155 turn-level parity checks**
  (75 corrections · 37 multi-turn · 34 topic-switch · 26 lifecycle ·
   22 unit-preservation · 11 long-history).
- FRAME PARITY (MATCH + STRUCTURED_IMPROVEMENT): **100%**
- STRUCTURED_WRONG: **0**
- UNIT PRESERVATION: **100%**
- overall case pass: **100%**
- §10 replays A–G: PASS · long-history (empty/10/30/50/100-turn) no stale
  takeover: PASS · lifecycle ACTIVE/SUSPENDED/CANCELLED: PASS.

## 8. What P2 does NOT change

RAG facts · ERP · auth · calculator formulas · link conversion · product
policy · withdrawal · invoice · shipment · human handoff · **any routing
decision**. `conversation_frame` is written and compared only.

## 9. Read-cutover gate (next stage, not this one)

Cut a runtime consumer over to the structured frame only when, over a
production shadow window: FRAME PARITY ≥ 99% sustained, STRUCTURED_WRONG
= 0 on the protected suites, and the frame is present for the session
(else fall back to legacy). Consumers migrate one at a time, smallest
first (the FIX-05 correction block, then SEM-GEN-1, then FIX-06 guards),
each its own bounded delta.
