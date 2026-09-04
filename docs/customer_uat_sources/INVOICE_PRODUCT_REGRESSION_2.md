# INVOICE-PRODUCT-REGRESSION-2

## The reported REAL LINE failure

```
User:      ใบกำกับค่าสินค้าออกได้ไหม
Assistant: สวัสดีค่ะ ทางเราสามารถออกใบกำกับค่าสินค้า และใบเสร็จค่าขนส่งให้ได้นะคะ
           ไม่ทราบว่าสินค้าของลูกค้าเป็นอะไรคะ
User:      กล่องพลาสติกค่ะ
Assistant: ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ เดี๋ยวทางเราประสานเจ้าหน้าที่…
```

## Audit at `3ed46a0` (before this change)

| Question | Answer |
|---|---|
| Production runtime SHA verified? | YES — `git rev-parse HEAD` and the container's file hashes (`decision_engine`, `playground_orchestrator`) all match `3ed46a0`. Not a stale deployment. |
| Can the corrupted product question be emitted? | **YES.** Its ONLY runtime source is the ACTIVE KB chunk `546c1bd5` (Quick_FAQ_Patch row 6), whose stored `Answer` was `"สวัสดีค่ะ ทางเราสามารถออกใบกำกับค่าสินค้า และใบเสร็จค่าขนส่งให้ได้นะคะ ไม่ทราบว่าสินค้าของลูกค้าเป็นอะไรคะ"`. It is the FAQ-exact match for `ใบกำกับค่าสินค้าออกได้ไหม` and its alt-phrasings (`ออกใบกำกับค่าสินค้าได้ไหม`, `มีใบกำกับให้ไหม`, `ขอใบกำกับค่าสินค้าได้ไหม`). No hardcoded copy exists in any `.py`/`.html` — grep for the sentence hits only a code *comment*, test files and dataset dumps. |
| Where does it escape? | The INVOICE-REGRESSION-1 deterministic branch (`_invoice_issuance_branch_applies` in `run_playground_turn`) only *routes around* it. For the exact wording `ใบกำกับค่าสินค้าออกได้ไหม`, the branch fires and `decide()` returns the clean `_INVOICE_ISSUANCE_ANSWER` — verified in the container at `3ed46a0`. But any invoice phrasing the branch does not catch (`actionable_intent != "invoice_policy"`, a general-chat fallback, or a caller with no `interpretation`) still surfaces `546c1bd5` verbatim. **Routing around it is not sufficient** while the row is active. |
| INVOICE B (`ขอใบกำกับภาษีหน่อยครับ`) | **FAIL** at `3ed46a0` — `slot_filling_engine._INVOICE_LOOKUP_EVIDENCE_RE` counted a bare polite marker `(ขอ\|ช่วย\|รบกวน)…(หน่อย\|ด้วย)` as a genuine invoice *lookup*, so `detect_erp_intent` returned `"invoice"` and the turn dead-ended on `"รบกวนแจ้งเลขใบกำกับภาษี หรือเลขใบสั่งซื้อค่ะ"` (there is no invoice Business Action to complete). |
| Turn 2 (`กล่องพลาสติกค่ะ`) | **FAIL** — a bare product noun is `UNKNOWN` to the central semantic layer (no product entity, no frame — `derive_active_frame` only opens on an assistant product *acknowledgement*, not a *question*), and `_is_product_import_interest` needs an import verb, so the Answerability Gate promotes it to `unsupported_company_fact` → Fix-2 → Human CS. `กล่องพลาสติก / ชั้นวางของ / อะไหล่รถยนต์ / แบตเตอรี่` all did this; only `น้ำหอม` (a liquid, retrieval hit the policy FAQ) survived. |

**Root cause:** (A) an active corrupted KB row, not a code path; (B) the central Semantic-First layer had no generic PRODUCT-entity / requested-slot-response understanding — a novel product noun fell through to a company-fact no-info.

## Fix

### Problem A

1. **KB — replace the corrupted answer at its source.** `tools/fix_corrupted_invoice_chunk_546c1bd5.py` (idempotent) rewrites ONLY the `Answer:` line of `546c1bd5` to the clean, customer-approved `_INVOICE_ISSUANCE_ANSWER` (composed verbatim from the active trusted rows `ff288877` + `99390831`), keeping Question / Alternative phrasings / Tags, re-embeds the chunk, and syncs its `knowledge_items` (`5cb1feb7`) answer. The sentence `ไม่ทราบว่าสินค้าของลูกค้าเป็นอะไรคะ` no longer exists in the KB — it cannot escape any path. The deterministic INVOICE-REGRESSION-1 branch is now belt-and-suspenders.
2. **INVOICE B — `slot_filling_engine._INVOICE_LOOKUP_EVIDENCE_RE`** now requires self-reference (`ของผม`…), a real invoice/order number, OR an explicit RETRIEVAL verb (`ส่ง / เช็ค / ตรวจสอบ / ดู / โหลด`) on the document. A bare polite issuance request (`ขอใบกำกับภาษีหน่อยครับ`) is no longer a "lookup" → it reaches the trusted invoice policy via RAG. `"ช่วยส่งใบเสร็จให้หน่อยครับ"` / `"ขอใบกำกับภาษีของผมหน่อย"` / with a number still route to the lookup collection.

### Problem B — generic PRODUCT entity via the CENTRAL Semantic-First layer

`services/conversation_semantics.py::interpret()` gains one deterministic
step (no product dictionary, one central call unchanged):

- `_assistant_asked_for_product(history)` — a small structural marker set
  for the previous assistant turn asking which product
  (`สินค้า…อะไร` / `นำเข้าอะไร` / `สินค้าที่ต้องการ…คืออะไร` / …).
- `_looks_like_bare_product(t)` — a short Thai noun phrase, not a
  question, not a greeting/confirm, not a structural value
  (`_MEASURE_RE`), not another intent family.
- `_bare_product_noun(t)` — strips leading `เป็น/พวก/ประมาณ/สินค้า/…` and
  trailing particles.

When all hold → `Interpretation(intent_family="PRODUCT_POLICY",
entities={"product": <noun>}, follow_up_op="SET_VALUE",
source="deterministic")`. Only fires as a reply to a product question —
a bare noun with no such context stays `UNKNOWN`.

Downstream (the trusted policy, never the semantic layer, decides the
verdict):

- `decide()` — when `semantic` is that PRODUCT-slot response, it
  normalises the message to the canonical `"<product>นำเข้าได้ไหม"` so
  the existing, well-tested prohibited-goods / import-eligibility
  retrieval decides prohibited vs. allowed. Structural rewrite only;
  the entity is preserved in `semantic`.
- `run_playground_turn()` Answerability-Gate `no_information` branch —
  a new `elif`: when `interpretation.intent_family == "PRODUCT_POLICY"`
  with a `product` entity, use the safe service-continuation ack for
  the real product noun (the same `_product_answer_service_continuation`
  the FIX-2.3 path uses) — **never** `unsupported_company_fact` /
  Human CS just because the noun is novel.

## Container verification (no REAL LINE)

| Case | Result |
|---|---|
| INVOICE A `ใบกำกับค่าสินค้าออกได้ไหม` | trusted policy, no product question, no Human CS |
| INVOICE B `ขอใบกำกับภาษีหน่อยครับ` | trusted policy (was a WORKFLOW dead-end) |
| INVOICE C `บริษัทมี tax invoice ไหมครับ` | trusted policy |
| INVOICE D `โหลดใบกำกับยังไง` | download flow, unchanged |
| `546c1bd5` alt-phrasings (`มีใบกำกับให้ไหม`, `ออกใบกำกับค่าสินค้าได้ไหม`, `ขอใบกำกับค่าสินค้าได้ไหม`) | all → clean trusted answer; the corrupted sentence cannot escape |
| Product slot-reply `กล่องพลาสติกครับ` | "กล่องพลาสติกสามารถฝากนำเข้าได้ค่ะ เนื่องจากไม่ได้จัดเป็นสินค้าประเภทของเหลวหรือสินค้าต้องห้าม…" — no Fix-2, no Human CS |
| `เสื้อผ้าครับ` / `ชั้นวางของ` / `อะไหล่รถยนต์ครับ` (unseen) / `โคมไฟตั้งโต๊ะค่ะ` | product entity, ordinary → allowed / "not in the prohibited list" (honest when no explicit allow evidence) |
| `น้ำหอมครับ` | trusted prohibited (liquid) verdict |
| `แบตเตอรี่ครับ` | trusted prohibited verdict (was Fix-2 Human CS before) |
| `แก้วน้ำ` | PRODUCT entity `แก้วน้ำ` — never flagged prohibited by the semantic layer for containing `น้ำ` |
| `น้ำหนัก 2 กิโล` | not a product (structural value) |

## Status

**CODE PASS / DEPLOYED / READY FOR FINAL MANUAL UAT.** No REAL LINE
testing performed — the product owner performs final acceptance.
