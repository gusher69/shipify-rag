# CUSTOMER-ACTION-1 — operational change / workflow customer cases

Audited the 9 action/workflow cases (S02, S03, S04, S09, S11, S13, S15,
G18, G21) against production `301fed3` in the container (mocked verified
binding `FT3182`, mocked ERP). Source: `Ai.xlsx` sheets
`2.tongchecknairabop` (CSW2/3/4/9/11/13/15) and `1.thameuangton`
(rows 18, 21) — the customer-provided expected answers are the authority.

## Per-case audit

| CASE | request | READ/WRITE/… | AUTH | required input | current prod route | current behaviour | expected behaviour | ROOT CLASS | CHANGE |
|---|---|---|---|---|---|---|---|---|---|
| **S02** แก้จำนวนสินค้าในบิล | modify bill item qty | WRITE — no BA | YES | เลขบิลสั่งซื้อ | GENERAL | **no-info dead-end** ("ตอนนี้ยังไม่มีข้อมูล…") | acknowledge + ask เลขบิลสั่งซื้อ → Human CS | MISSING_WORKFLOW / MISSING_HANDOFF | **YES** |
| **S03** เปลี่ยนจัดส่งทางรถ/เรือ | change shipping method | WRITE — no BA | YES | เลขบิล | RAG | partial (confirms + rate info, no collect/handoff) | "สามารถเปลี่ยนได้ค่ะ แอดมินขอเลขบิลหน่อยนะคะ" → Human CS | ROUTING_FAILURE / MISSING_HANDOFF | **YES** |
| **S04** ลืมเลือก VAT / ต้องการ VAT | add VAT to a bill | WRITE — no BA | YES | เลขบิลสั่งซื้อ | RAG (invoice-issuance answer) | asks for taxpayer info + bill; not the approved wording | "แจ้งเลขบิลสั่งซื้อที่ต้องการ VAT" → Human CS | ROUTING_FAILURE | **YES** |
| **S09** เปลี่ยนที่อยู่จัดส่ง | change delivery address | WRITE — **BA exists** (`requestshippingaddresschange`) | YES | ShipmentCode + address fields | WORKFLOW | opens the address-change collection flow | collect fields → notify CS | ALREADY_PASS | no |
| **S11** เปลี่ยนเป็นรับเอง / ส่งเอกชน | change carrier / to self-pickup | WRITE — no BA | YES | เลขบิลขนส่ง | primary → CLARIFICATION dead-end; para → self-pickup FAQ | not the approved collect+handoff | acknowledge + ask เลขบิลขนส่ง → Human CS | ROUTING_FAILURE | **YES** |
| **S13** บิลซ้ำ | flag duplicate bill for deletion | WRITE — no BA | YES | เลขแทรคจีน | GENERAL | **no-info dead-end** | "แอดมินเช็คบิลซ้ำและลบบิลให้นะคะ" + ask แทรคจีน → Human CS | MISSING_WORKFLOW | **YES** |
| **S15** ใส่ที่อยู่โกดังจีนถูกไหม | verify warehouse address | RECORD-ONLY / verify | YES | address text + รหัสลูกค้า | primary → **FAKE SUCCESS** (`getdatacustomer` → "ดำเนินการเรียบร้อยค่ะ") | wrong ERP read + fake success | "แอดมินช่วยตรวจสอบความถูกต้องให้ค่ะ" + ask address → Human CS | FALSE_ACTION / FAKE_SUCCESS | **YES** |
| **G18** ยอดเงินไม่เข้า / เติมเงินแล้วรอตรวจสอบ | topup not credited | RECORD-ONLY | YES | สลิปการโอนเงิน | primary → `getdatacustomer` "not found"; para → generic bank advice | wrong ERP read / generic | "แอดมินรบกวนขอสลิปหน่อยนะคะ" → Human CS | FALSE_ACTION / MISSING_HANDOFF | **YES** |
| **G21** ยกเลิกบิลสั่งซื้อได้ไหม | cancellation policy | INFORMATIONAL | NO | — | RAG | policy answer ("cancel before status 'สั่งซื้อสำเร็จ', else contact staff") | same policy answer | ALREADY_PASS | no |

**ALREADY PASS:** S09, G21. **FIXED:** S02, S03, S04, S11, S13, S15, G18 (7).

## Fix — `services/operational_change_flow.py` (new)

A deterministic, history-derived collector (the SAME "assistant reply IS
the state" pattern as `charter_truck_flow.py` / `conversation_semantics.py`
— no LLM, no pending table). `classify_operational_request` is a small
COMPOSITIONAL verb + object model (a change verb *and* a request object
must both appear; two self-describing kinds — `duplicate_bill`,
`topup_not_credited` — need only their object). It **never** invents an
ERP write:

```
understood intent
  -> acknowledge with the CS-approved wording + ask for the ONE input
  -> when the identifier / detail is supplied -> HUMAN_HANDOFF
       reason  = "operational_change_request: <kind>"
       summary = "คำขอดำเนินการกับบัญชีลูกค้า — <kind> | เลขบิล/แทรค: … | …"
```

Wired into `DecisionEngine.decide()` immediately after the charter-truck
branch, before the calculator / SEM-GEN / RAG pipeline — so these
requests never reach the Answerability Gate / general-chat fallback and
never dead-end. A delivery-**address** change (S09) is explicitly
excluded (`_ADDRESS_CHANGE_RE`) so it keeps its own
`requestshippingaddresschange` Business Action. `interpret()` supplies
the natural-language intent; the deterministic flow decides what may
happen — **Semantic AI never executes an action**.

`line_bot/webhook.py`: `operational_change_request` joins
`_promises_followup` with its own coordination clause
(" เดี๋ยวเจ้าหน้าที่จะติดต่อดำเนินการให้นะคะ") — appended ONLY when a real
Human CS notification for THIS episode succeeds (this-turn send, or an
already-NOTIFIED/PENDING same episode). On send failure the customer
keeps the plain "รับเรื่องคำขอดำเนินการเรียบร้อยค่ะ" with no promise —
**never a fake success**. Dedupe is automatic: `is_same_handoff_episode`
keys on the reason class (`reason.split(":")[0]`), a 600 s episode
window. The same-turn `_charter_handoff` fresh-send coordination clause
was also made symmetric while there.

## Error / result truth

`operational_change_flow` never emits "ดำเนินการเรียบร้อยค่ะ". The
turn-2 reply is "รับเรื่องคำขอดำเนินการเรียบร้อยค่ะ" — a truthful
"request received", not "action done". The staff-coordination line is
notification-truthful (webhook). Authorization is untouched — a verified
`customer_channel_bindings` row is still the only source; these flows do
not read or write ERP data themselves.

## Regression

`test_customer_action1.py` (12 cases: recognizer, state/summary,
turn-1/turn-2/all-in-one routing, S09 not stolen, G21 stays RAG,
calculator route not stolen). Protected batch — `test_customer_erp_read1`,
`test_customer_rag2_1_charter_slots`, `test_customer_calc1/11`,
`test_semantic_first_1`, `test_customer_rag_audit`,
`test_customer_invoice1`, `test_sem1_private_state_routing`,
`test_identity_0_verification_boundary`,
`test_customer_uat_fix2_unsupported_fact_handoff`,
`test_customer_rag1_pickup_location`, `test_ppc1`, `test_slot_filling`,
`test_conversation_semantics`, `test_fix23`, `test_session_service`,
`test_webhook` → **373 OK**. Only the 6 known pre-existing failures in
`test_decision_engine` / `test_customer_uat_fix1`.

## Status

**CODE PASS / DEPLOYED / READY FOR FINAL MANUAL UAT.** No REAL LINE
testing performed. Customer acceptance not declared.
