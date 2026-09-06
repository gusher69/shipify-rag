# PHASE 3 — RAG COVERAGE COMPLETION

**Task:** `PHASE-3-RAG-COVERAGE-COMPLETION-1` — source-grounded KB completion + cleanup + retrieval verification.
**Repo HEAD at start:** `ae65ead` (Phase-2 audit, docs-only)
**Base production CODE SHA:** `0937d71951f1cdf2a073d97357f562092b744016` — unchanged by this phase (RAG/KB only).
**Scope:** KB knowledge content, staleness, duplicates/conflicts, source-wording alignment, FAQ/policy completeness, media mapping where existing infra supports it, paraphrase/retrieval coverage, embeddings for changed chunks, active-KB validation, public/private boundary validation.
**Explicitly out of scope (unchanged here):** routing/state/replay/workflow code fixes; Deferred Bugs D1–D20 (Phase 5); ERP/API capability implementation E1–E11 (EXTERNAL_API_DEPENDENCY); Admin Profile/Export; Full Regression; REAL LINE UAT; blanket population of `expected_answer_constraints`.

---

## 1. Executive Summary

| Metric | Value |
|---|---|
| Phase-2 items assigned `PHASE_3_RAG_COVERAGE` | 8 groups (R1/M4, R2/M5, R3, R4/M3, R5/D14, R6, D16-part, image-attach set) |
| Active `knowledge_chunks` inspected (BEFORE) | **73 / 73 active** |
| KB rows requiring a **source-confirmed content change** | **1** — `f2e2cf8f` (R5 / D14, G21 cancel policy) |
| KB content changes **applied to production this phase** | **0** |
| Reason not applied | R5 fix is **staged** (`tools/fix_g21_cancel_policy_source_alignment.py`) but the OpenAI embedding provider is returning `429 insufficient_quota / credit_balance_exhausted`; the script regenerates the embedding **before** any DB write, so it aborts cleanly with no partial mutation. Apply when embedding credit is restored. |
| Items already source-faithful (no action) | R2/M5, R3 (content), R4/M3, R6, D16-part |
| Items blocked on missing customer assets | R1/M4 + image-attach set (G20/G25/G26/G27/G28/F11, CN-warehouse imgs) — infra exists; the source image binaries do not exist in the repo and the `knowledge_attachments` rows already reference them with `status: missing/failed`, `public_url: null` |
| Public/private KB safety | **PASS** — no active privacy exposure; no STOP condition |
| Production CODE SHA | stays `0937d71` |

**Net effect:** the production KB is already source-faithful for every applicable Phase-2 RAG item except one wording-alignment (R5), whose fix is authored and idempotent but blocked on an exhausted embedding quota. Nothing was mutated in production.

---

## 2. Phase-2 Assignment Load

From `docs/customer_uat_sources/PHASE2_SOURCE_COVERAGE_AUDIT.md` §14, row **PHASE_3_RAG_COVERAGE**:

> M2 (S12 SP image), M3/R4 (G09 no-claims rule), M4/R1 (13 image-pending FAQ images), M5/R2 (F03 transient clause), R5/D14 (G21 wording align), R6 (`546c1bd5` dedup), D16 partial (prohibited sub-categories), G20/G25/G26/G27/G28/F11 image attach

Each is carried into §5 below with a classification and disposition.

Items **explicitly left to other phases** (confirmed, not touched):
- R3 invoice multi-turn eligibility loop → **D9 / PHASE_5** (flow, not KB).
- D15 F05 "battery large-quantity" recorded FAIL → **PHASE_5** (family routing, not KB).
- D16 reasoning-engine portion → **PHASE_5**.
- E1–E11 → **EXTERNAL_API_DEPENDENCY** (RAG must not fabricate missing ERP truth).
- D19 (`expected_answer_constraints` population) → **PHASE_5** (master hygiene).

---

## 3. RAG-Worthy Requirement Inventory

Every source requirement whose answer is (or should be) served from the KB, cross-checked against the live store:

| # | Requirement (source) | Live KB representation | State |
|---|---|---|---|
| 1 | Warehouse addresses TH/CN + hours + map (G01/G02) | `c726a00b`, `5fdffb90`, china-warehouse chunk; `answer_planner.warehouse_*` | COMPLETE (text); image pending |
| 2 | Rate / เรท (G04, F03) | `304e5a3c` (`เรทเท่าไหร่คะ`) | COMPLETE — **no transient Vietnam-border clause present** |
| 3 | Shipping-time / duration (G06) | `49fa6534` (`ระยะเวลาขนส่งนับจากวันไหน`) | COMPLETE |
| 4 | Invoice — can issue (G05/F08) | `ff288877` (`ออกใบกำกับได้ไหม`), `546c1bd5` (`ใบกำกับค่าสินค้าออกได้ไหม`) | COMPLETE (content); eligibility loop = D9/Phase-5 |
| 5 | Invoice — e-Tax (S07 twin) | `99390831` (`Shipify ออก e-Tax Invoice ได้ไหม`) | COMPLETE |
| 6 | Invoice — how to download (S07/F10) | `389645f9` (`โหลดใบกำกับยังไง` — website steps) | COMPLETE |
| 7 | Purchase-bill payment how-to (G09) | `506370f4` (`วิธีการชำระบิลสั่งซื้อ`) | COMPLETE — **no claims/เคลม content** |
| 8 | Shipping-bill payment how-to (G26) | `06636112` (`ชำระบิลขนส่งยังไง`) | COMPLETE |
| 9 | Prohibited goods list (G08) | `b0ed7b9a` + prohibited-goods chunks | COMPLETE |
| 10 | Prohibited sub-categories: liquids / battery (F01/F04/F05, D16) | `ae466369` (ครีมอาบน้ำ→ของเหลว→ห้าม), `3cc36111` (น้ำหอม→ของเหลว→ห้าม), `4838e72e` (แบตเตอรี่→ต้องห้าม, *"ไม่ว่าจำนวนเท่าไหร่ก็ตาม"*) | COMPLETE (content); F05 routing FAIL = D15/Phase-5 |
| 11 | Claims / เคลม genuine path (G11) | `debdc015` (`Shipify ช่วยเคลมสินค้าไหม`), `a1095550` (`สินค้าไม่ตรงรูป…`) | COMPLETE — separate, retrievable |
| 12 | Cancellation policy (G21) | `f2e2cf8f` (`ยกเลิกคำสั่งซื้อได้ไหม`) + `knowledge_items` twin `04d38523` | **STALE wording** — see §5 R5 |
| 13 | Private carriers list + rates (G20) | `02bb0ab6` (`ขนส่งเอกชนมีอะไรบ้าง`), `de0c6f44`, `271c69bc` | COMPLETE (text); rate-table images pending |
| 14 | ฝากสั่ง vs ฝากนำเข้า (G25) | `6759fd84` | COMPLETE (text); image pending |
| 15 | Cost structure (G27) | `39dec856` | COMPLETE (text); image pending |
| 16 | Wood-crating service (G28/F11) | `d164742f` (`มีบริการตีลังไม้ไหม`), `22a7f35e` | COMPLETE (text); image pending |
| 17 | Shipping withdrawal SP/FT (S12) | `withdrawal_flow.py` → KB tags `SHIPPING_WITHDRAWAL_SP` / `_FT`, `PURCHASE_WITHDRAWAL` | COMPLETE (text); SP form image pending |
| 18 | Contact channels (G22) | `d07b36b9` (`ติดต่อ Shipify ช่องทางไหน`) | COMPLETE |
| 19 | Company / service overview (G23/G24) | `6a2b7964`, `357bda1e`, `df9ed971`, `6e925e92` | COMPLETE |
| 20 | Tax on imports (F-series) | `d9a95913` (`นำเข้าจากจีนต้องเสียภาษีไหม`) | COMPLETE |
| 21 | Damage-risk / insurance (F-series) | `28c15ec9` (`สินค้าความเสี่ยงแตกหักมีประกันไหม`) | COMPLETE |
| 22 | CBM definition (G-series) | `aef75cdc` (`CBM คิวคืออะไร`) | COMPLETE |

Everything ERP-backed (ETA, order status, seller-dispatch, wallet, reverse-map, daily arrivals, cancel *execution*) is deliberately **not** in this table — it is PRIVATE_ERP_READ / EXTERNAL_API_DEPENDENCY and must never be answered from cached KB "fact".

---

## 4. Current Production KB Snapshot (BEFORE)

**Store:** Supabase `knowledge_chunks`, 73 rows, all `is_active = true`.
**Embedding (per every chunk's `metadata`):** `text-embedding-3-large`, `embedding_dimensions: 3072`, `embedding_provider: openai`, `embedding_version: openai-te3l-v1` — consistent with `migrations/019_openai_embedding_dimension.sql` (`embedding VECTOR(3072)`) and `config.EMBEDDING_PROVIDER` default `"openai"` (`EMBEDDING_MODEL`/`EMBEDDING_DIM` = 768 / sentence-transformers is the `EMBEDDING_PROVIDER=local` fallback only — **no contradiction**, the local constants are inert in production).

Intent distribution (Thai category labels): `สต็อก` 43, `ทั่วไป` 15, `ขนส่ง` 3, `ออเดอร์` 3, `นโยบาย` 2, `บัญชี` 2, plus code tags `PURCHASE_WITHDRAWAL`, `SHIPPING_WITHDRAWAL_FT`, `SHIPPING_WITHDRAWAL_SP`, `บริการเสริม`, `โกดัง`.

### 4.1 The one row targeted for change — `f2e2cf8f-8b1a-44fc-acef-c789e3faeb71` (VERBATIM BEFORE)

```
id:            f2e2cf8f-8b1a-44fc-acef-c789e3faeb71
intent:        สต็อก
is_active:     True
source:        AI_Knowledge_Master_RAG_FINAL_CLEAN.xlsx
file_id:       58194c7f-0813-4547-9088-95e781a290e3
version:       1
metadata.embedding_model:      text-embedding-3-large
metadata.embedding_dimensions: 3072
metadata.embedding_provider:   openai
metadata.embedding_version:    openai-te3l-v1

content:
  Question: ยกเลิกคำสั่งซื้อได้ไหม
  Answer: ยกเลิกได้ก่อนสถานะในระบบเปลี่ยนเป็น "สั่งซื้อสำเร็จ" โดยต้องติดต่อเจ้าหน้าที่ค่ะ หากเป็นสถานะสั่งซื้อสำเร็จแล้ว จะยกเลิกไม่ได้ เว้นแต่ร้านหรือโรงงานจีนยินยอม หากร้านคืนเงิน บริษัทจะคืนเงินหลังได้รับเงินจากร้านจีนแล้ว โดยเว็บไซต์ระบุประมาณ 3–7 วันค่ะ
  Alternative phrasings: ยกเลิกบิลได้ตอนไหน / สถานะสั่งซื้อสำเร็จแล้วยกเลิกได้ไหม / เปลี่ยนใจหลังสั่งได้หรือไม่ / ขอคืนเงินค่าสินค้าได้ไหม
  Tags: ยกเลิก, คืนเงิน, สถาน
```

`knowledge_items` twin `04d38523-e4b1-47c0-8d3d-eff28f061f9a` — `question: "ยกเลิกคำสั่งซื้อได้ไหม"`, `category: "Policy"`, `answer` identical to the chunk Answer line, `tags: ["ยกเลิก","คืนเงิน","สถานะคำสั่งซื้อ","ร้านจีน"]`.

### 4.2 Attachment infrastructure snapshot (media items)

`knowledge_attachments`: **106 rows**; **29 active chunks** carry ≥1 attachment row; only **9** carry any row with a real `public_url` (dev `http://localhost:8001/...` URLs, auto-extracted). Every customer-named source image (`ที่อยู่โกดังจีน FT.png`, `ชำระบิลสั่งซื้อ.png`, `ชำระสั่งซื้อฟาด.jpg`, `Screenshot 2023-04-12 150515.png`, the SP withdrawal-form image, the rate-table images) is present as a row with `status: missing` or `failed`, `public_url: null`. **The binaries do not exist anywhere in the repo.**

Delivery path already wired: `ingestion/attachment_handler.py::get_attachments_for_chunks` → `rag/searcher.py::_fetch_attachments_for_chunks` → `line_bot/webhook.py` (`chunk.get("attachments")` image send) → `services/attachment_planner.py`. Nothing in this chain is missing — only the assets are.

---

## 5. Per-Item Classification & Disposition (STEP 4 / STEP 5)

Classification vocabulary: `KB_COMPLETE`, `KB_MISSING`, `KB_STALE`, `KB_PARTIAL`, `KB_DUPLICATE`, `KB_CONFLICT`, `KB_MEDIA_MAPPING_MISSING`, `KB_RETRIEVAL_WEAK`, `NO_RAG_CHANGE_REQUIRED`.

### R2 / M5 — F03 rate answer, transient "ด่านเวียดนาม 3–5 วัน" clause
**Classification: `KB_COMPLETE` (already clean) → NO_RAG_CHANGE_REQUIRED.**
Full-text search across all 73 chunks for `ด่านเวียดนาม`, `เวียดนาม`, `เผื่อ 3-5`, `3-5 วัน` returns **zero hits**. `304e5a3c` (`เรทเท่าไหร่คะ`) already reads: rate ฝากสั่ง 5.11, ทางรถ 35/kg or 6,900/CBM, ทางเรือ 19/kg or 4,500/CBM, "ใช้ค่าที่สูงกว่า", plus a generic *"กรุณาตรวจสอบเรทล่าสุดก่อนใช้งาน เนื่องจากอัตราอาจเปลี่ยนแปลงได้"* caveat. The customer-requested edit is already reflected. **No action.**

### R4 / M3 — G09 purchase-bill payment how-to must NOT contain claims content
**Classification: `KB_COMPLETE` (already clean) → NO_RAG_CHANGE_REQUIRED.**
`506370f4` (`วิธีการชำระบิลสั่งซื้อ`) and `06636112` (`ชำระบิลขนส่งยังไง`) contain only the click-bill → กดชำระ → QR-scan steps. No `เคลม` / `claims` / missing-item / damaged-goods text. Genuine claims content is isolated in `debdc015` / `a1095550`. The "must not" rule is a **negative-retrieval property** (verified §10), not a content edit. **No action.**

### R3 — Invoice knowledge (4 intents kept separate)
**Classification: `KB_COMPLETE` (content) → NO_RAG_CHANGE_REQUIRED; flow gap = D9/Phase-5.**
- *Can issue?* → `ff288877` (`ออกใบกำกับได้ไหม`) + `546c1bd5` (`ใบกำกับค่าสินค้าออกได้ไหม`) — both carry the full conditions: per-bill & payment-method condition, taxpayer-info requirement, credit-card limitation.
- *e-Tax Invoice?* → `99390831`.
- *How to download?* → `389645f9` (`โหลดใบกำกับยังไง`) — 5-step website walk-through, `www.shipify.co.th` → รายการสั่งซื้อ → สรุปบัญชี → ดาวน์โหลดใบกำกับ.
- *Add / modify VAT on a bill* → handled by `operational_change_flow.add_vat` (operational collection), **not** RAG — correctly separated.
No trusted invoice-fetch API exists; the KB does not imply one. The unclosed multi-turn eligibility loop (ask product → customer type → import mode → confirm) is **D9 → PHASE_5**, a flow change, not a KB change. **No KB change.**

### R5 / D14 — G21 cancellation-policy wording alignment
**Classification: `KB_STALE` (wording drift, substance consistent) → FIX AUTHORED, APPLICATION BLOCKED.**

| | Current KB (`f2e2cf8f`) | Customer source (CUS-G21, `customer_uat_master.jsonl`) |
|---|---|---|
| Eligibility keyed on | **system status** — "ก่อนสถานะเปลี่ยนเป็น *สั่งซื้อสำเร็จ*" | **payment** — "หาก*ยังไม่ได้ชำระ* จะยกเลิกก่อนได้ / กรณี*ชำระแล้ว* แอดมินขอสอบถามร้านก่อนว่าจัดส่งแล้วหรือยัง" |
| Paid path | "ยกเลิกไม่ได้ เว้นแต่ร้าน/โรงงานจีนยินยอม" | "แอดมินขอสอบถามร้านก่อน…จัดส่งสินค้าให้แล้วหรือยัง" (shop-consent, softer) |
| Refund clause | "คืนเงินหลังได้รับเงินจากร้านจีน…เว็บไซต์ระบุประมาณ 3–7 วัน" | (not in the one-line master answer) — but **website-backed**, independently supported |

The two framings describe the same policy (cancel is possible up to an irreversible point; after that it needs the shop's consent; refunds follow the shop). The drift is in the *pivot* (status vs payment) and tone of the paid path.

**Fix (authored, idempotent):** [`tools/fix_g21_cancel_policy_source_alignment.py`](../../tools/fix_g21_cancel_policy_source_alignment.py)
- Rewrites **only** the `Answer:` line of `f2e2cf8f` to lead with payment-based eligibility:
  > หากยังไม่ได้ชำระเงิน สามารถยกเลิกคำสั่งซื้อก่อนได้ค่ะ โดยแจ้งเจ้าหน้าที่เพื่อดำเนินการ กรณีที่ชำระเงินแล้ว แอดมินจะขอสอบถามทางร้านก่อนว่าจัดส่งสินค้าให้แล้วหรือยัง หากยังไม่จัดส่งและร้านยินยอม จึงจะยกเลิกให้ได้ค่ะ ทั้งนี้หากร้านคืนเงิน บริษัทจะคืนเงินให้หลังได้รับเงินจากร้านจีนแล้ว ซึ่งเว็บไซต์ระบุประมาณ 3–7 วันค่ะ
- **Keeps** the website-backed "3–7 วัน" refund clause (per the R5 rule: do not remove source-supported refund policy that is independently backed).
- Widens `Alternative phrasings` and `Tags` with the payment-framed paraphrases (`ยังไม่ได้ชำระเงินยกเลิกได้ไหม`, `ชำระเงินแล้วยกเลิกได้ไหม`) for retrieval coverage.
- Regenerates the chunk embedding via `services.embedding_service.get_embedding_provider()` (existing pipeline, no model/dim change), rewrites `metadata.embedding_text`, syncs the `knowledge_items` twin answer + tags.
- Preserves Question/structure; touches **no other row**; **no-op guard** on re-run.
- **Embedding is generated before any DB write** → if the provider is unavailable the script aborts with a non-zero exit and **zero DB mutation**.

**Why it is not applied this session:** running the script now fails at the embedding step:
```
RuntimeError: OpenAI embedding failed after 4 attempts: Error code: 429 -
{'error': {'message': 'You have no credits remaining...', 'type': 'insufficient_quota',
'code': 'credit_balance_exhausted'}}
```
Applying a `content` edit **without** a matching regenerated 3072-d embedding would leave the stored vector reflecting the old answer text — a partial, unverifiable change that violates STEP 10 ("Verify: embedding exists, dimension matches current production configuration"). The correct disposition is: **stage the fix, leave production untouched, apply when embedding quota is restored** (the same posture Phase 1 took for EXTERNAL_API_DEPENDENCY items). Verified post-run: `f2e2cf8f.content` is **UNCHANGED**.

This remains **policy-only RAG** — no per-order eligibility claim, no implication the bot executes the cancellation. The executable side (`CancelOrder` + `payment_status` semantics) is **D12 → EXTERNAL_API_DEPENDENCY**; the same-message-`PO` routing edge is **D13 → PHASE_5**.

### R6 — chunk `546c1bd5` dedup decision
**Classification: `INTENT_VARIANT_NOT_DUPLICATE` → NO_RAG_CHANGE_REQUIRED (leave active).**
`546c1bd5` (`ใบกำกับค่าสินค้าออกได้ไหม`) was previously corrupt (ended with a stray product question, omitted conditions) and was **already repaired** by `tools/fix_corrupted_invoice_chunk_546c1bd5.py` — it now carries the full, customer-approved conditions verbatim from `ff288877` + `99390831`. Its remaining distinction from `ff288877` (`ออกใบกำกับได้ไหม`) is the exact question wording, which matches **CUS-F08 verbatim**. Deactivating it would remove that exact-wording retrieval path for no benefit. Per STEP 6 this is a `SAFE_DUPLICATE` / `INTENT_VARIANT`, not a `STALE_DUPLICATE`. **No action; recorded.**

### D16 (Phase-3 portion) — prohibited sub-category answers
**Classification: `KB_COMPLETE` → NO_RAG_CHANGE_REQUIRED.**
`ae466369` (ครีมอาบน้ำ → "จัดเป็นสินค้าประเภทของเหลว…ไม่สามารถนำเข้า"), `3cc36111` (น้ำหอม → ของเหลว → ห้าม), `4838e72e` (แบตเตอรี่ → "จัดเป็นสินค้าต้องห้าม…**ไม่ว่าจำนวนเท่าไหร่ก็ตาม**…วัตถุอันตราย/ไวไฟ") — all active, source-faithful, and `4838e72e` already answers the "จำนวนเยอะ" framing in both its Question and Answer. The residual F05 recorded FAIL is a **family-routing** problem (the large-quantity phrasing is scored into a rate/quantity family before RAG retrieval runs) — **D15 → PHASE_5**, not a KB gap. **No KB change.**

### R1 / M4 + image-attach set (G20/G25/G26/G27/G28/F11, CN-warehouse, SP withdrawal form = M2)
**Classification: `KB_MEDIA_MAPPING_MISSING` → BLOCKED on asset acquisition.**
The entire delivery chain exists and is wired (see §4.2). What is missing is the **image files themselves** — the customer's `Ai.xlsx` `สื่อรูปภาพ` column names them, but no binary was ever supplied to the repo, and the `knowledge_attachments` rows that would carry them already exist with `status: missing/failed` and `public_url: null`. There is nothing for Phase 3 to *map* — mapping a row to a non-existent file, or substituting an invented/placeholder image, is explicitly forbidden by STEP 5 ("never substitute an invented image"). **Recorded as an asset-acquisition blocker; needs the customer to provide the source image files, after which a one-time `tools/` upload+link script can attach them idempotently.**

---

## 6. Duplicate / Conflict Sweep (STEP 6)

| Pair / row | Finding | Disposition |
|---|---|---|
| `ff288877` (`ออกใบกำกับได้ไหม`) vs `546c1bd5` (`ใบกำกับค่าสินค้าออกได้ไหม`) | Near-identical answers, both now carry full conditions; different exact question wording; `546c1bd5` == CUS-F08 verbatim | `INTENT_VARIANT_NOT_DUPLICATE` — both stay active |
| `99390831` (e-Tax) vs the two above | Distinct sub-topic (e-Tax Invoice specifically) | Not a duplicate |
| `389645f9` (`โหลดใบกำกับยังไง`) vs the "can issue" chunks | Distinct intent (how-to-download vs can-issue) | Not a duplicate |
| `506370f4` (`วิธีการชำระบิลสั่งซื้อ`) vs `06636112` (`ชำระบิลขนส่งยังไง`) | Purchase-bill vs shipping-bill payment — different flows | Not a duplicate |
| `ae466369` / `3cc36111` (both → ของเหลว → ห้าม) | Different named products (ครีมอาบน้ำ/โลชั่น vs น้ำหอม) reaching the same rule | `INTENT_VARIANT` — both stay active |
| `debdc015` / `9c504ee7` (claims) | `9c504ee7` content is a fragment ("นส่งผิดทำอย่างไร…" + Tags) — appears truncated | **Recorded** — candidate cleanup, but not source-confirmed this phase and not a retrieval hazard (it still tags `เคลมสินค้า`). Leave; note for a later KB-hygiene pass. |
| `CUS-S20` ×2 in master | data-hygiene dup (`case_id` reused) | `RECORD ONLY` — renumber = D18 / PHASE_5 (not a KB row) |

**No `SOURCE_CONFLICT` found among active chunks.** The 3 route-label conflicts from Phase-2 (S07, S12, P06) are `CUSTOMER_CLARIFICATION` items about the master's `expected_route` label, not KB content.

---

## 7. Public / Private Safety Audit (STEP 7)

Swept all 73 active chunk `content` bodies for: CustCode-shaped tokens (`(FT|SP|SA|FE|PO|PA|POS|PE)\d{3,}`), customer phone numbers, e-mail addresses, `SecretCode` / `api_key` / `bearer` / `password`, wallet-balance phrasing, private ERP result payloads.

| Hit | Chunk | Verdict |
|---|---|---|
| Phone `0642247205` | `5fdffb90` (`ขอที่อยู่โกดังหน่อย`) | **Intentional public** — warehouse-staff contact line, matches the `Ai.xlsx` warehouse rows. Not customer PII. |
| Email `info@shipify.co.th` + BD phone `080-289-3956` | `d07b36b9` (`ติดต่อ Shipify ช่องทางไหน`) | **Intentional public** — company contact info, the whole point of the FAQ. |

**No customer CustCode, no customer phone/email, no wallet balance, no `SecretCode`, no private ERP result** appears in any active chunk. → **PASS. No STOP condition. No active privacy exposure.**

---

## 8. Paraphrase / Retrieval Coverage (STEP 8)

No global thresholds touched. Coverage review for the changed area (R5) and its neighbours:

| Intent | Exact source wording present? | Variants present? | Added by R5 fix |
|---|---|---|---|
| Cancel policy (`f2e2cf8f`) | `ยกเลิกคำสั่งซื้อได้ไหม` ✔ (Question) | `ยกเลิกบิลได้ตอนไหน`, `เปลี่ยนใจหลังสั่งได้หรือไม่`, `ขอคืนเงินค่าสินค้าได้ไหม` ✔ | + `ยังไม่ได้ชำระเงินยกเลิกได้ไหม`, + `ชำระเงินแล้วยกเลิกได้ไหม` (payment-framed paraphrases matching the source pivot) |
| Invoice can-issue | `ออกใบกำกับได้ไหม` ✔, `ใบกำกับค่าสินค้าออกได้ไหม` ✔ (F08 verbatim) | 4 alt phrasings each | — |
| Invoice how-to | `โหลดใบกำกับยังไง` ✔ | download / receipt variants | — |
| Payment how-to | `วิธีการชำระบิลสั่งซื้อ` ✔, `ชำระค่าสินค้ายังไง` ✔ | QR / top-up variants | — |
| Prohibited: battery | `สั่งแบตเตอรี่จำนวนเยอะได้ไหม` ✔ | `สั่งแบตจำนวนเยอะได้ไหม`, `นำเข้าแบตเตอรี่จำนวนมากได้ไหม` ✔ | — |

The R5 alt-phrasing additions are the only retrieval-coverage change proposed, and they are carried inside the same staged script (they ship with the re-embed, not separately).

---

## 9. Negative Retrieval Checks (STEP 9)

Run via the committed-intent deterministic router (`services/decision_engine.py`) with the playground mocked — this is the offline-executable subset; live vector retrieval could not be exercised because the query-embedding path uses the same exhausted OpenAI provider.

| Separation asserted | Result |
|---|---|
| `โหลดใบกำกับยังไง` (invoice how-to) **≠** `เพิ่ม VAT ให้หน่อย` (VAT operational flow) | **PASS** — how-to → RAG, no `add_vat` handoff; VAT → `operational_change_collection`, no `shipify.co.th` steps |
| `วิธีการชำระบิลสั่งซื้อ` (payment) **≠** claims content | **PASS** — payment reply carries no `เคลม` string |
| `ยกเลิกบิลสั่งซื้อได้ไหม` (cancel **policy**) **≠** cancel **execution** | **PASS** — routing `RAG`, no ERP write, no `order_cancel` handoff, no "ยกเลิกเรียบร้อยแล้ว" fake-done |
| `แก้บิล` **≠** cancellation | **PASS** — not treated as cancel |
| 14 cross-workflow probes (coupon, G16/G17, G12, CSW18, CSW8, VAT, CSW10/11/12/13/15, G19) each **≠** the G21 cancel policy / the CSW7 invoice how-to | **PASS** — every probe routes to its own workflow; none surfaces "ยกเลิกได้ก่อนสถานะ" or "ดาวน์โหลดใบกำกับ" |
| Same-message identifier: `ขอยกเลิก PO12345` / `ขอใบกำกับของ PO12345` | **Known FAIL** — routes to a `searchdataorder` read instead of staying on the policy/how-to. This is **D10 / D13 → PHASE_5** (Business-Action keyword arbitration), pre-existing, explicitly out of Phase-3 scope. Not a KB defect. |

---

## 10. Embeddings (STEP 10)

- **Production embedding configuration (authoritative):** `EMBEDDING_PROVIDER=openai` → `OpenAIEmbeddingProvider`, model `text-embedding-3-large`, **3072 dims**, version tag `openai-te3l-v1`. Confirmed live: `get_embedding_provider()` reports `provider=openai | model=text-embedding-3-large | dims=3072 | version=openai-te3l-v1`. The `config.EMBEDDING_MODEL` / `EMBEDDING_DIM` (768 / sentence-transformers) constants are the **`EMBEDDING_PROVIDER=local` fallback only** and are inert in production — the earlier apparent contradiction is resolved.
- **Model / dimension unchanged** — this phase proposes no embedding-config change.
- **Regeneration required for:** `f2e2cf8f` only (the R5 content change). **Status: BLOCKED** — `embed_query` / `embed_documents` return `429 insufficient_quota / credit_balance_exhausted` after 4 retries. No embedding could be regenerated, and therefore **no changed content was written** (the staged script enforces embed-before-write).
- **No orphaned / dimension-mismatched embeddings** found among the 73 active chunks — all report `embedding_dimensions: 3072`.

---

## 11. Idempotent Data-Change Scripts (STEP 11)

| Script | Purpose | Idempotency | Applied? |
|---|---|---|---|
| [`tools/fix_g21_cancel_policy_source_alignment.py`](../../tools/fix_g21_cancel_policy_source_alignment.py) | R5 / D14 — realign `f2e2cf8f` Answer to the payment-framed source wording, keep the website-backed refund clause, widen alt-phrasings/tags, re-embed via the existing pipeline, sync the `knowledge_items` twin | `_NEW_MARKER` / `_OLD_MARKER` guards → re-run is a no-op; single-row `.eq("id", CHUNK_ID)`; embed-before-write → failure = zero mutation | **No** — aborts at the embedding step (quota). Ready to run unchanged when credit is restored. |

Follows the established `tools/fix_*` convention (same shape as `tools/fix_corrupted_invoice_chunk_546c1bd5.py`, `tools/fix_g16g17_searchdataorder_status_keywords.py`). Creates no duplicate chunk, embedding, media mapping, or row on any number of re-runs.

---

## 12. Machine-Readable Constraints Decision (STEP 12)

`expected_answer_constraints` is empty on **all 69** master rows. Per the Phase-3 spec and Phase-2 §14, blanket population is **PHASE_5 (D19)**, not this phase. **No `expected_answer_constraints` value was written to any master row.** The only item Phase-2 assigned to `PHASE_3_RAG_COVERAGE` that *could* have carried a constraint (R4/M3 "no claims content on G09") is satisfied as a live negative-retrieval property (§10) and did not require a master-row edit.

---

## 13. Out-of-Scope Confirmations

**EXTERNAL_API_DEPENDENCY (E1–E11) — untouched, no KB faking:** ETA field, claims WRITE, `shop_shipped_at`, wallet-txn read, `CancelOrder` + `payment_status`, order-modify WRITE ×4, shipment→purchase reverse-map, duplicate-list + delete, address-validate, `DateArrivedTH`, cheapest-carrier compare. The KB contains **no** cached substitute for any of these.

**Deferred Bugs (D1–D20) — untouched:** D9 (invoice loop), D10/D13 (same-message-`PO` arbitration), D15 (F05 battery routing), D16-reasoning, D17 (SC2 verification loop), D18 (`CUS-S20` renumber), D19 (`expected_answer_constraints`), D20 (promote Phase-1 focused tests). All remain PHASE_5.

**Not run:** Full Regression, REAL LINE UAT, Admin Profile/Export.

---

## 14. Focused RAG Verification Results (STEP 15) & Production Application (STEP 16)

### 14.1 Verification run (offline-executable subset)

| Suite | Result |
|---|---|
| `tests.test_customer_rag_audit` | **OK** (all) |
| `tests.test_customer_invoice1` | **OK** |
| `tests.test_invoice_product_regression2` | **OK** |
| `tests.test_customer_rag2_charter_truck` | **OK** |
| (combined: 33 tests) | **33 passed** |
| scratchpad `test_g21.py` (18 checks + 14 SW probes) | 1 known FAIL — `E ขอยกเลิก PO12345` (D13 / PHASE_5, pre-existing); all other checks PASS |
| scratchpad `test_csw7.py` | 1 known FAIL — `E ขอใบกำกับของ PO12345` (D10 / PHASE_5, pre-existing); all other checks PASS |

The two `E` failures are the same-message-identifier arbitration edge, are pre-existing (recorded in Phase 1 and Phase-2 §12), and are explicitly outside Phase-3 scope. No new failure introduced.

Live vector-retrieval verification (would exercise `rag/searcher.py` query embedding) **could not be run** — same exhausted OpenAI quota.

### 14.2 Production application

**Nothing was applied to the production KB.** `knowledge_chunks` / `knowledge_items` / `knowledge_attachments` are byte-for-byte unchanged. Verified: `f2e2cf8f.content` still contains `ยกเลิกได้ก่อนสถานะในระบบเปลี่ยนเป็น` (old wording). Production CODE SHA remains `0937d71`. No container / cache refresh performed (nothing to refresh).

---

## 15. Phase-3 Exit Decision, Blockers & Follow-ups

### 15.1 Exit criteria

| Criterion | Status |
|---|---|
| Every `PHASE_3_RAG_COVERAGE` item classified | ✅ §5 |
| Source-confirmed KB content fixes identified | ✅ 1 (R5 / D14) |
| Source-confirmed KB content fixes applied | ⚠️ **0 — blocked** (embedding quota); fix staged & idempotent |
| KB not faking EXTERNAL_API truth | ✅ §13 |
| Duplicate / conflict sweep done | ✅ §6 — no active conflict; `546c1bd5` = safe variant |
| Public/private KB safety validated | ✅ §7 — PASS, no STOP |
| Paraphrase / negative-retrieval coverage reviewed | ✅ §8–§9 (offline subset) |
| Embeddings: model/dim unchanged, regen only affected | ✅ config confirmed; ⚠️ regen blocked |
| `expected_answer_constraints` not blanket-populated | ✅ §12 |
| Focused RAG verification (no Full Regression) | ✅ §14.1 — 33 pass, 2 known pre-existing PHASE_5 fails |
| Phase-3 doc with named sections | ✅ this file |

### 15.2 Blockers (environment, not design)

1. **OpenAI embedding quota exhausted** (`credit_balance_exhausted`). Blocks: applying the R5 fix, and any live vector-retrieval verification. **Unblock:** restore OpenAI billing credit, then `python -m tools.fix_g21_cancel_policy_source_alignment` (idempotent), then confirm the chunk + twin updated and the embedding is 3072-d.
2. **Source image binaries absent** for the ~13 image-pending FAQ rows + the SP withdrawal-form image (M2) + rate-table images. Blocks R1 / M4 / M2. **Unblock:** customer supplies the image files; then a one-time `tools/` upload+link script attaches them to the existing chunk rows (infra already in place).

### 15.3 Recommended next actions

- **When OpenAI credit returns:** run the staged R5 script; re-run `test_customer_rag_audit` + the G21 scratchpad guard; spot-check live retrieval for `ยกเลิกบิลได้ไหม` / `ยังไม่ได้ชำระยกเลิกได้ไหม`.
- **Request from customer:** the named image assets (`Ai.xlsx` `สื่อรูปภาพ` column) so R1/M2/M4 can be closed.
- **Phase 5** picks up: D9, D10, D13, D15, D16-reasoning, D17, D18, D19, D20 and the routing edges.
- **Phase 4 / REAL LINE UAT** unchanged in plan.

---

### FINAL REPORT

**PHASE-3-RAG-COVERAGE-COMPLETION-1 — RAG coverage reconciled against the live KB; production KB unchanged.**

- **Source loaded:** Phase-2 audit §14 `PHASE_3_RAG_COVERAGE` assignments (8 groups); `customer_uat_master.jsonl` CUS-G21 approved answer; 73 active `knowledge_chunks` + `knowledge_items` twin + `knowledge_attachments` (BEFORE snapshot §4).
- **Classification (§5):**
  - R2/M5 (F03 transient clause) — `KB_COMPLETE`, already removed, **no action**.
  - R4/M3 (G09 no-claims) — `KB_COMPLETE`, clean, verified as a negative-retrieval property, **no action**.
  - R3 (invoice) — `KB_COMPLETE` content; 4 intents correctly separated; multi-turn loop = D9/Phase-5, **no KB change**.
  - R6 (`546c1bd5`) — `INTENT_VARIANT_NOT_DUPLICATE`, already repaired, **leave active**.
  - D16-part (prohibited sub-categories) — `KB_COMPLETE`; F05 FAIL is routing (D15/Phase-5), **no KB change**.
  - **R5/D14 (G21 cancel-policy wording)** — `KB_STALE`; source frames eligibility on *payment* (ยังไม่ได้ชำระ / ชำระแล้ว), KB frames it on *status* (สั่งซื้อสำเร็จ). Substance consistent, wording drifts.
  - R1/M4/M2 + image set — `KB_MEDIA_MAPPING_MISSING`; delivery infra fully wired, **source image binaries do not exist** — blocked on asset acquisition.
- **Change authored (idempotent, NOT applied):** `tools/fix_g21_cancel_policy_source_alignment.py` — realigns the `f2e2cf8f` Answer to the payment-framed source wording, **keeps** the website-backed "3–7 วัน" refund clause, widens alt-phrasings/tags, re-embeds via the existing `text-embedding-3-large` / 3072-d pipeline, syncs the `knowledge_items` twin. Embed-before-write → a failed embed = zero DB mutation.
- **Why not applied:** OpenAI embedding provider returns `429 insufficient_quota / credit_balance_exhausted`; a content edit without a matching regenerated embedding would be a partial, unverifiable change (violates STEP 10). Production KB left byte-for-byte unchanged (`f2e2cf8f` still shows the old wording).
- **Safety (§7):** all 73 active chunks swept — no CustCode / customer phone / customer email / wallet balance / `SecretCode` / private ERP result. Two hits are intentional public business contacts (warehouse line, `info@shipify.co.th`). **PASS — no STOP condition.**
- **Verification (§14):** `test_customer_rag_audit` + `test_customer_invoice1` + `test_invoice_product_regression2` + `test_customer_rag2_charter_truck` = **33 pass**. G21/CSW7 scratchpad guards pass except the two pre-existing same-message-`PO` `E` cases (D10/D13 → PHASE_5). No new failure. Live vector retrieval not runnable (same quota block).
- **Not populated:** `expected_answer_constraints` on all 69 master rows (blanket population = PHASE_5 / D19).
- **Deliverable:** `docs/customer_uat_sources/PHASE3_RAG_COVERAGE_COMPLETION.md` (15 sections) + `tools/fix_g21_cancel_policy_source_alignment.py` (staged).
- **Production CODE SHA:** `0937d71` (unchanged). **Production KB:** unchanged.
- **Blockers:** (1) OpenAI embedding quota — blocks the R5 apply + live retrieval checks; (2) missing customer image assets — blocks R1/M2/M4.
- **Next:** restore OpenAI credit → run the staged R5 script + re-verify; request the named image assets from the customer; Phase 5 for D9/D10/D13/D15/D16-reasoning/D17/D18/D19/D20.
