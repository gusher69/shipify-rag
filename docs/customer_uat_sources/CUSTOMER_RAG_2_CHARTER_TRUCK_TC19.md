# CUSTOMER-RAG-2 — charter-truck (เหมารถ / TC19)

## Real LINE failure
`มีบริการเหมารถไหมคะ` → *"ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องบริการเหมารถค่ะ"* (no-info / Human CS).
Customer feedback: *"บริการเหมารถ ไม่ควรตอบว่าไม่มีในระบบ ต้องดู TC19 ดูคำตอบที่ควรตอบด้วยค่ะ"*.

## TC19 — located
| | |
|---|---|
| Source | `Ai.xlsx` sheet `'1.thameuangton'` **row 19.0** (= "TC19" / customer PDF `เคสที่ต้องแก้ใน 1.คำถามทั่วไป+2.ต้องเช็คในระบบ.pdf` p5 #8) |
| UAT master cases | `CUS-G19` (status `SOURCE_CONFIRMED`) · `CUS-SC1` (screenshot `messageImage_1788318551530.jpg`, LINE 2/9/2568 09:23) |
| Customer-approved answer (`customer_provided_expected_answer`, verbatim) | *"สวัสดีค่ะ ทางเรามีบริการเหมารถให้ได้นะคะ คุณลูกค้าแจ้งเลขบิล และโลเคชั่นปลายทาง พร้อมกับชื่อผู้รับ และเบอร์โทรผู้รับมาได้เลยนะคะ"* |
| Required inputs | เลขบิล · โลเคชั่นปลายทาง · ชื่อผู้รับ · เบอร์โทรผู้รับ |
| Expected behaviour (CUS-SC1) | confirm the service exists → collect bill_no + destination + receiver + phone → hand to Human CS. **Must NOT** say "no confirmed information". |

## Production RAG audit (`เหมารถ`)
| chunk | source record | relevance |
|---|---|---|
| `1e0f7f00` (+ tag-continuation `481d02ea`) | RAG-036 — *"ส่งต่อในไทยคิดค่าใช้จ่ายอะไรบ้าง"* (`AI_Knowledge_Master_RAG_FINAL_CLEAN.xlsx`) | COST breakdown of Shipify-coordinated Thai-domestic forwarding: 2 parts — ค่าเหมารถโกดัง→บริษัทขนส่ง + ค่าบริการปลายทางคิดตามจริง; asks จังหวัด/น้ำหนัก/ขนาด/บริษัท for assessment. **Related, but a cost question, not a service-existence confirmation, and asks different inputs.** |

- Direct `มีบริการเหมารถไหม` coverage in Production RAG: **NO**.
- RAG-036 is the **only** existing evidence.
- The exact TC19 customer-approved fact (service confirmation + the 4 booking inputs): **ABSENT** from Production RAG.
- Retrieval for `มีบริการเหมารถไหมคะ` / `เหมารถให้ได้ไหม` returned only irrelevant rows (`มีบริการตีลังไม้ไหม`, `ออกใบกำกับได้ไหม`, `ขอที่อยู่โกดัง`) → answerability = no_information.

## Root class
**COMBINATION** — KNOWLEDGE_MISSING (the TC19 service-confirmation fact) + KNOWLEDGE_INCOMPLETE (RAG-036 covers only cost) + RETRIEVAL (the direct wording does not retrieve RAG-036).

## Decision — CASE B
One clean trusted FAQ row derived **only** from the approved TC19 source, ingested via the **existing** Quick_FAQ_Patch mechanism (`knowledge_chunks` + `knowledge_items`), no new architecture, no code path.

- **Canonical question:** `มีบริการเหมารถไหม`
- **Paraphrases:** มีบริการเหมารถไหมคะ / เหมารถให้ได้ไหม / เรียกรถให้ได้ไหม / สามารถเหมารถได้ไหม / รับเหมารถส่งของไหม / เหมารถส่งต่อในไทยได้ไหม / มีบริการเรียกรถส่งของไหม / เหมารถส่งของให้หน่อยได้ไหม
- **Approved answer:** *(verbatim, above)*
- **Required input:** เลขบิล, โลเคชั่นปลายทาง, ชื่อผู้รับ, เบอร์โทรผู้รับ
- **Category / tags:** `quick_faq_patch` · เหมารถ, เรียกรถ, ขนส่งในไทย, บริการเหมารถ
- **Provenance:** `Ai.xlsx` sheet `'1.thameuangton'` row 19 (TC19) / CUS-G19 / CUS-SC1 — recorded in `metadata.provenance` and `tools/seed_charter_truck_faq_tc19.py`.

Seed tool: `python -m tools.seed_charter_truck_faq_tc19` (idempotent). RAG-036 is untouched.
