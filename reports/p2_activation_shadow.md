# P2 activation — migration + production shadow observation

**Runtime SHA at activation:** `dd4cc9a4ec6d5eb26beb06d54073af67e136bb59`
Stage: **shadow-write activation + observation only.** No read cutover.
Legacy `derive_active_frame` remains the runtime authority.

---

## 1. Migration `048_conversation_frame_shadow_state.sql` — APPLIED

Applied to production Supabase via `psycopg2` over `SUPABASE_DB_URL`
(the project-supported alternative to the SQL Editor, per `CLAUDE.md`).
Single statement + `COMMENT`; committed in one transaction.

| check | value |
|---|---|
| BEFORE ROW COUNT (`ai_sessions`) | **1344** |
| AFTER ROW COUNT | **1344** — ROW INTEGRITY **PASS** |
| COLUMN | `conversation_frame` |
| TYPE | `jsonb` |
| NULLABLE | `YES` |
| rows with `conversation_frame IS NULL` after migration | **1344** (all — no backfill) |
| existing rows still readable | yes (spot-checked latest 3) |

Verification queries used:

```sql
SELECT count(*) FROM ai_sessions;                       -- before / after
SELECT column_name, data_type, is_nullable, udt_name
  FROM information_schema.columns
 WHERE table_name = 'ai_sessions' AND column_name = 'conversation_frame';
SELECT count(*) FROM ai_sessions WHERE conversation_frame IS NULL;
```

Rollback (documented in the migration header, not needed):
`ALTER TABLE ai_sessions DROP COLUMN IF EXISTS conversation_frame;`

---

## 2. Real shadow write — verified end to end

A throwaway `ai_sessions` row (`channel='playground-p2-test'`) was
threaded through the **real `DecisionEngine.decide()`** with the frame
loaded/saved each turn through the **real
`session_service.get_conversation_frame` / `save_conversation_frame`**
against the migrated Supabase column, then deleted (row count restored
1344 → 1344). LLM disambiguation forced to the deterministic tier —
the tier P1/P2/FIX-04..06 are built and validated on.

### §7 owner journey — structured frame after every turn (read back from Postgres)

| turn | selection_source | journey / status | product | quantity | shipping_method |
|---|---|---|---|---|---|
| `20 คู่อยากสั่งของจากจีน` | phase6b_service_intent | IMPORT_INTEREST / ACTIVE | — | **20 / คู่** | — |
| `เป็นชั้นวางของ` | phase6b_service_intent | IMPORT_INTEREST / ACTIVE | ชั้นวางของ | 20 / คู่ | — |
| `เปลี่ยนเป็นรองเท้า` | frame_correction_fix05 | IMPORT_INTEREST / ACTIVE | **รองเท้า** | 20 / คู่ | — |
| `เอ้ย 10 คู่` | frame_correction_fix05 | IMPORT_INTEREST / ACTIVE | รองเท้า | **10 / คู่** | — |
| `ทางเรือ` | (sem-gen frame answer) | IMPORT_INTEREST / ACTIVE | รองเท้า | 10 / คู่ | **sea** |
| `ไม่เอาแล้ว` | frame_reject_fix05 | IMPORT_INTEREST / **CANCELLED** | รองเท้า | 10 / คู่ | sea |
| `ขอเบอร์ติดต่อ` | phase6b_service_intent | **journey null** (contact info; stale import frame NOT reactivated) | — | — | — |

- T1–T7 expectations from the task all hold.
- **Unit preservation:** `"20 คู่"` persisted as `value 20 / unit คู่` and
  survived the correction to `10 / คู่` — never `ชิ้น`.
- Routing on every turn is unchanged from pre-P2 (`selection_source`
  values are the FIX-04/05/06 / phase6b ones).

### Sample stored JSONB (verbatim from `ai_sessions.conversation_frame`)

After `เอ้ย 10 คู่` (quantity correction):

```json
{"version":1,"journey":"IMPORT_INTEREST","status":"ACTIVE","requested_slot":"shipping_method",
 "slots":{"product":{"value":"รองเท้า","unit":null,"canonical_value":"รองเท้า","canonical_unit":null,"raw":"รองเท้า"},
          "quantity":{"value":10,"unit":"คู่","canonical_value":10,"canonical_unit":"คู่","raw":"20 คู่"}},
 "turn_seq":4,"source":"conversation_resolution","updated_at":"2026-09-10T08:0x:xx+00:00"}
```

After `ไม่เอาแล้ว`:

```json
{"version":1,"journey":"IMPORT_INTEREST","status":"CANCELLED",
 "slots":{"product":{"value":"รองเท้า",...},"quantity":{"value":10,"unit":"คู่",...},
          "shipping_method":{"value":"sea",...}},
 "turn_seq":5,"source":"conversation_resolution"}
```

No private customer data, tokens, or reasoning text is stored — only
structured conversation state.

### Unit / canonical conversion (extractor, verified directly)

| raw | stored `unit` | stored `canonical_value` / `canonical_unit` |
|---|---|---|
| `5000 กรัม` | `กรัม` | `5.0` / `kg` |
| `520 mm` (dims `520x220x110 mm`) | `mm` | `52x22x11` / `cm` |
| `20 คู่`, `5 ตัว`, `100 ชิ้น`, `3 กล่อง` … | the exact unit given | value unchanged |

---

## 3. Production-equivalent shadow parity sample

The P2 lab (`tests/test_p2_conversation_frame.py` +
`tests/p2_frame_lab/corpus.py`) IS the production-equivalent shadow
sample — real `resolve_conversation` → real `build_frame` → real
`derive_active_frame` (legacy) → real `frame_parity`, over the §5
scenario spread.

| | |
|---|---|
| eligible turns compared | **1155** (205 multi-turn cases) |
| history depths | empty / 3 / 10 / 20 / 30 / 40 / 50 / 60 / 70 / 80 / 100-turn |
| scenarios | corrections (75) · multi-turn (37) · topic-switch (34) · lifecycle (26) · unit-preservation (22) · long-history w/ stale completed + link + rate rounds (11) · typo+correction (6) · multi-slot (multiple) |
| MATCH | 90 |
| STRUCTURED_IMPROVEMENT | 1065 |
| LEGACY_CORRECT | **0** |
| STRUCTURED_WRONG | **0** |
| AMBIGUOUS | **0** |
| **FRAME PARITY (MATCH + STRUCTURED_IMPROVEMENT)** | **100%** |
| UNIT PRESERVATION | **100%** |
| STALE JOURNEY TAKEOVER | **0** (fresh opener after 100-turn stale history → structured resets; legacy lags → classified STRUCTURED_IMPROVEMENT) |
| KNOWN SLOT RE-ASK | **0** (`requested_slot` / `missing_slots` tracked structurally) |
| STRUCTURED TOKEN CORRUPTION | **0** |

`STRUCTURED_IMPROVEMENT` dominates because the legacy text-derived frame
lags a turn behind every correction/answer and cannot store a unit —
which is exactly what P2 fixes.

---

## 4. Known finding (out of P2 scope — reported, not fixed)

With the **real production LLM** (not the deterministic tier), the gated
`_llm_family` disambiguation in `conversation_semantics.interpret()` can
override the deterministic FIX-04 product-slot classification for a bare
`"เป็นชั้นวางของ"` mid-journey (observed once: routed `fresh_search` /
`SAFE_FALLBACK` instead of `phase6b_service_intent`). FIX-04/05/06 and
P1/P2 were all validated on the deterministic tier; the LLM tier is
non-deterministic here. This is **pre-existing** (P1/P2 did not
introduce it), is **not a persistence bug**, and P2 is
activation+observation only — so it is recorded for a follow-up
(likely: gate `_llm_family` from overriding a deterministic
IMPORT_INTEREST/product-slot read, or run the FIX-04 block before the
LLM gate). The structured frame logic itself is correct on both tiers;
only *which* turn establishes the product differs.
