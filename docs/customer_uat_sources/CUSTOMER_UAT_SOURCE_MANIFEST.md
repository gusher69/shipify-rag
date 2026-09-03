# Customer UAT Source Manifest

> AUTHORITATIVE customer feedback / UAT source set for the Shipify AI platform.

> These are **customer requirements / feedback / real failure examples** — NOT RAG knowledge documents.

> Do NOT ingest into the production knowledge base. Do NOT modify the originals.


## Source files

| File | Type | Sheets / Pages / Images | Notes |
|---|---|---|---|
| `Ai.xlsx` | Excel | 7 sheets: `Api Overall` (31), `all` (7), `1.ถามเบื้องต้น` (29 cases), `2.ต้องเช็คในระบบ` (~19 cases / CSW1–CSW20), `3.Policy` (2), `API Summary for Client` (design), `API` (base_url + **SecretCode**) | Primary case catalogue with CS-approved standard answers + RAG/API/Human-CS type + media refs. Sheet `API` contains a real `SecretCode` — **MASKED** everywhere in the derived artifacts, never stored. |
| `ปัญหาที่เจอในการตอบ (1).xlsx` | Excel | 1 sheet `สรุปการเทรน AI` (11 rows) | REAL 2/9/2025 UAT run: test question / what AI answered / desired behaviour / PASS·FAIL verdict. |
| `เคสที่ต้องแก้ใน 1.คำถามทั่วไป+2.ต้องเช็คในระบบ.pdf` | PDF | 17 pages, ~40 embedded screenshots + a thin annotation text layer | Reviewer annotations on chat screenshots: case #1–#10, `TC9/TC11/TC12/TC16/TC17/TC18`, `CSW1/2/5/6/8/10/11/12/16/17/18/20`. Recurrent note: *“ตอบผิด — ต้องตอบตามตัวอย่างไฟล์”* (use the sheet's canned answer). Specific asks captured: send the `https://www.shipify.co.th/Rate` link for calc; separate general vs account-deep; don't require identity for link conversion; ask for tracking before saying 'no data'. |
| `screenshot/messageImage_1788318551530.jpg` | Image | 1 | `มีบริการเหมารถไหมคะ` → AI wrongly said *no confirmed info*. |
| `screenshot/messageImage_1788321460773.jpg` | Image | 1 | Self-service identity verification loop (phone→email mismatch→handoff prompt→re-loops on next private question). |
| `screenshot/messageImage_1788321878908.jpg` | Image | 1 | `ค่าขนส่งคิดยังไง` → bot asks weight+size → `54x12x43` → AI wrongly asked for the **registered phone / identity**. |
| `screenshot/messageImage_1788323949934.jpg` | Image | 1 | Artifact only — a spreadsheet cell listing attachment filenames. No case. |
| `screenshot/messageImage_1788324389650.jpg` | Image | 1 | Artifact only — screenshot of `Ai.xlsx` sheet `1.ถามเบื้องต้น` open in Google Sheets (confirms rows 22–31 + the 13/7/9 verdict tally). No new case. |

### Verdict tallies recorded by the customer

* `Ai.xlsx / 1.ถามเบื้องต้น` (29 cases): 13 = *correct answer but image not yet attached in LINE*; 7 = *wrong, fix per file*; 9 = *correct*.
* `Ai.xlsx / 2.ต้องเช็คในระบบ` (~19 cases): 12 = *wrong, fix per file*; 7 = *correct*.

## Logical UAT cases

Total logical cases: **69**. Full machine-readable set: [`tests/customer_uat/customer_uat_master.jsonl`](../../tests/customer_uat/customer_uat_master.jsonl).

| case_id | source (file · location) | user message | route | pub/priv | category | expected answer supplied | status |
|---|---|---|---|---|---|---|---|
| CUS-G01 | Ai.xlsx · sheet '1.thameuangton' row 1.0 | ขอที่อยู่โกดังหน่อย | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G02 | Ai.xlsx · sheet '1.thameuangton' row 2.0 | ขอที่อยู่โกดังจีน,ต้องการส่งของผ่านชิปปิ้ง ส่งไปที่อยู่ไหน | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G03 | Ai.xlsx · sheet '1.thameuangton' row 3.0 | สินค้าจะเข้าไทยตอนไหน | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-G04 | Ai.xlsx · sheet '1.thameuangton' row 4.0 | ค่าขนส่งคิดยังไง คำนวนค่าส่งให้หน่อย | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G05 | Ai.xlsx · sheet '1.thameuangton' row 5.0 | ออกใบกำกับได้ไหม | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G06 | Ai.xlsx · sheet '1.thameuangton' row 6.0 | CBM คิวคืออะไร | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G07 | Ai.xlsx · sheet '1.thameuangton' row 7.0 | มีขั้นต่ำในการสั่งไหม | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G08 | Ai.xlsx · sheet '1.thameuangton' row 8.0 | สินค้าที่ห้ามนำเข้ามีอะไรบ้าง | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G09 | Ai.xlsx · sheet '1.thameuangton' row 9.0 | วิธีการชำระบิลสั่งซื้อ,ชำระค่าสินค้ายังไง,จ่ายบิลสั่งซื้อยัง | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G10 | Ai.xlsx · sheet '1.thameuangton' row 10.0 | บิลขนส่งสามารถชำระได้เลยไหม | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G11 | Ai.xlsx · sheet '1.thameuangton' row 11.0 | ได้รับสินค้าไม่ครบ, เคลมสินค้ายังไงคะ | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-G12 | Ai.xlsx · sheet '1.thameuangton' row 12.0 | สินค้าถึงโกดังหรือยัง , ได้รับสินค้าหรือยัง | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-G13 | Ai.xlsx · sheet '1.thameuangton' row 13.0 | ระยะเวลาการส่งจากร้านจีน -โกดังจีน | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G14 | Ai.xlsx · sheet '1.thameuangton' row 14.0 | ชำระบัตรเครดิตได้ไหม | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G15 | Ai.xlsx · sheet '1.thameuangton' row 15.0 | ขอเบอร์ติดต่อ | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G16 | Ai.xlsx · sheet '1.thameuangton' row 16.0 | ร้านส่งหรือยังคะ | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-G17 | Ai.xlsx · sheet '1.thameuangton' row 17.0 | ติดตามสถานะ สินค้า | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-G18 | Ai.xlsx · sheet '1.thameuangton' row 18.0 | ยอดเงินไม่เข้า, เติมเงินแล้วรอตรวจสอบ | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-G19 | Ai.xlsx · sheet '1.thameuangton' row 19.0 | เรียกรถให้ได้ไหม ,เหมารถให้ได้ไหม | HUMAN_CS | NA | Operational/Human CS | yes | SOURCE_CONFIRMED |
| CUS-G20 | Ai.xlsx · sheet '1.thameuangton' row 20.0 | ขนส่งเอกชนมีอะไรบ้าง | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G21 | Ai.xlsx · sheet '1.thameuangton' row 21.0 | ยกเลิกบิลสั่งซื้อได้ไหม | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-G22 | Ai.xlsx · sheet '1.thameuangton' row 22.0 | เรทเท่าไหร่คะ ,เรทนำเข้าเท่าไหร่ ,เรทฝากสั่งเท่าไหร่ | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G23 | Ai.xlsx · sheet '1.thameuangton' row 23.0 | คูปองใช้ไม่หมด คืนได้ไหมคะ | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G24 | Ai.xlsx · sheet '1.thameuangton' row 24.0 | มีบริการอะไรบ้าง | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G25 | Ai.xlsx · sheet '1.thameuangton' row 25.0 | ฝากสั่งกับฝากนำเข้าต่างกันอย่างไง | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G26 | Ai.xlsx · sheet '1.thameuangton' row 26.0 | ชำระบิลขนส่งยังไง,ชำระค่านำเข้ายังไง | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G27 | Ai.xlsx · sheet '1.thameuangton' row 27.0 | สนใจฝากสั่งสินค้าค่าใช้จ่ายคิดยังไง | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G28 | Ai.xlsx · sheet '1.thameuangton' row 28.0 | มีบริการตีลังไม้ไหม,ตีลังได้ไหม | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-G29 | Ai.xlsx · sheet '1.thameuangton' row 29.0 | ช่วยแปลงลิงก์ให้หน่อยค่ะ | NA | NA | Operational/Human CS | yes | SOURCE_CONFIRMED |
| CUS-S01 | Ai.xlsx · sheet '2.tongchecknairabop' row 1.0 (CSW1) | สินค้าจะเข้าไทยตอนไหนคะ | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S02 | Ai.xlsx · sheet '2.tongchecknairabop' row 2.0 (CSW2) | ต้องการแก้จำนวนสินค้าในบิล | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S03 | Ai.xlsx · sheet '2.tongchecknairabop' row 3.0 (CSW3) | สามารถเปลี่ยนเป็นจัดส่งทางรถ,ทางเรือได้ไหมคะ | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S04 | Ai.xlsx · sheet '2.tongchecknairabop' row 4.0 (CSW4) | ลืมเลือก VAT ไปค่ะ ,ต้องการVATด้วยค่ะ | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S05 | Ai.xlsx · sheet '2.tongchecknairabop' row 5.0 (CSW5) | ถอนเงินสั่งซื้อยังไง,เงินที่ร้านคืนมา จะถอนยังไง | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S06 | Ai.xlsx · sheet '2.tongchecknairabop' row 6.0 (CSW6) | ต้องการสั่งผลิตตามสเปค ,สั่งสกรีนโลโก้ได้ไหมคะ | HUMAN_CS | NA | Operational/Human CS | yes | SOURCE_CONFIRMED |
| CUS-S07 | Ai.xlsx · sheet '2.tongchecknairabop' row 7.0 (CSW7) | โหลดใบกำกับยังไง , ขอใบกำกับค่าสินค้าหน่อย | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S08 | Ai.xlsx · sheet '2.tongchecknairabop' row 8.0 (CSW8) | บิลขนส่งนี้ หรือแทรคนี้เป็นของบิลสั่งซื้อไหน | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S09 | Ai.xlsx · sheet '2.tongchecknairabop' row 9.0 (CSW9) | บิลขนส่ง FTxxx ต้องการเปลี่ยนที่อยู่จัดส่ง | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S10 | Ai.xlsx · sheet '2.tongchecknairabop' row 10.0 (CSW10) | รีเเพ็คค่ะ | HUMAN_CS | NA | Operational/Human CS | yes | SOURCE_CONFIRMED |
| CUS-S11 | Ai.xlsx · sheet '2.tongchecknairabop' row 11.0 (CSW11) | บิลขนส่ง FT ต้องการเปลี่ยนเป็นรับเอง หรือเปลี่ยนส่งเอกชน | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S12 | Ai.xlsx · sheet '2.tongchecknairabop' row 12.0 (CSW12) | ถอนเงินขนส่งยังไงคะ | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S13 | Ai.xlsx · sheet '2.tongchecknairabop' row 13.0 (CSW13) | บิลซ้ำค่ะ | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S14 | Ai.xlsx · sheet '2.tongchecknairabop' row 14.0 (CSW14) | ใช้คูปองยังไง | RAG | PUBLIC | Other | yes | SOURCE_CONFIRMED |
| CUS-S15 | Ai.xlsx · sheet '2.tongchecknairabop' row 15.0 (CSW15) | ใส่ที่อยู่โกดังจีนถูกไหมคะ | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S16 | Ai.xlsx · sheet '2.tongchecknairabop' row 16.0 (CSW16) | รวมบิลเหมารถค่ะ | HUMAN_CS | PRIVATE | Operational/Human CS | yes | SOURCE_CONFIRMED |
| CUS-S17 | Ai.xlsx · sheet '2.tongchecknairabop' row 17.0 (CSW17) | ขอแทรคไทยค่ะ (กรณีลูกค้าไม่เข้ามาเช็คในระบบเอง) | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S18 | Ai.xlsx · sheet '2.tongchecknairabop' row 18.0 (CSW18) | วันนี้มีของเข้าไทยไหมคะ | ERP | PRIVATE | Private ERP | yes | SOURCE_CONFIRMED |
| CUS-S20 | Ai.xlsx · sheet '2.tongchecknairabop' row 20.0 (CSW20) | ตัวอย่างแปลงลิงก์ | NA | NA | Other | yes | SOURCE_CONFIRMED |
| CUS-S20 | Ai.xlsx · sheet '2.tongchecknairabop' row 20.0 (CSW20) | ช่วยแปลงลิงก์ให้หน่อยค่ะ | NA | NA | Other | yes | SOURCE_CONFIRMED |
| CUS-F01 | panha_tee_jer_nai_kan_tob (1).xlsx · sheet 'sarup_train_AI' row (test 2/9/2025) | ครีมอาบน้ำนำเข้าได้ไหม | RAG | PUBLIC | Prohibited goods | yes | SOURCE_CONFIRMED |
| CUS-F02 | panha_tee_jer_nai_kan_tob (1).xlsx · sheet 'sarup_train_AI' row (test 2/9/2025) | ทางรถกับทางเรือระยะเวลากี่วัน | RAG | PUBLIC | Shipping rate/calculation | yes | SOURCE_CONFIRMED |
| CUS-F03 | panha_tee_jer_nai_kan_tob (1).xlsx · sheet 'sarup_train_AI' row (test 2/9/2025) | แล้วเรทนำเข้าเท่าไหร่คะ | RAG | PUBLIC | Shipping rate/calculation | yes | SOURCE_CONFIRMED |
| CUS-F04 | panha_tee_jer_nai_kan_tob (1).xlsx · sheet 'sarup_train_AI' row (test 2/9/2025) | ฝากสั่งน้ำหอมได้ไหมคะ | RAG | PUBLIC | Prohibited goods | yes | SOURCE_CONFIRMED |
| CUS-F05 | panha_tee_jer_nai_kan_tob (1).xlsx · sheet 'sarup_train_AI' row (test 2/9/2025) | เราสามารถสั่งแบตเตอรี่จำนวนเยอะได้ไหมคะ | RAG | PUBLIC | Prohibited goods | yes | SOURCE_CONFIRMED |
| CUS-F06 | panha_tee_jer_nai_kan_tob (1).xlsx · sheet 'sarup_train_AI' row (test 2/9/2025) | โกดังอ่อนนุชเปิดทุกวันหรอคะ | RAG | PUBLIC | Warehouse | yes | SOURCE_CONFIRMED |
| CUS-F07 | panha_tee_jer_nai_kan_tob (1).xlsx · sheet 'sarup_train_AI' row (test 2/9/2025) | มีขนส่งทางเครื่องบินไหม | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-F08 | panha_tee_jer_nai_kan_tob (1).xlsx · sheet 'sarup_train_AI' row (test 2/9/2025) | ใบกำกับค่าสินค้าออกได้ไหม | RAG | PUBLIC | Invoice | yes | SOURCE_CONFIRMED |
| CUS-F09 | panha_tee_jer_nai_kan_tob (1).xlsx · sheet 'sarup_train_AI' row (test 2/9/2025) | จัดส่งสินค้าถึงหน้าบ้านเลยไหม | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-F10 | panha_tee_jer_nai_kan_tob (1).xlsx · sheet 'sarup_train_AI' row (test 2/9/2025) | โหลดใบกำกับยังไง | RAG | PUBLIC | Invoice | yes | SOURCE_CONFIRMED |
| CUS-F11 | panha_tee_jer_nai_kan_tob (1).xlsx · sheet 'sarup_train_AI' row (test 2/9/2025) | ตีลังไม้ได้ไหม | RAG | PUBLIC | Public FAQ | yes | SOURCE_CONFIRMED |
| CUS-SC1 | screenshot/messageImage_1788318551530.jpg · LINE chat 2/9/2568 09:23 | มีบริการเหมารถไหมคะ | RAG | PUBLIC | Operational/Human CS | — | SOURCE_CONFIRMED |
| CUS-SC2 | screenshot/messageImage_1788321460773.jpg · LINE chat ~10:11-10:54 | สินค้าจะเข้าไทยตอนไหน | ERP | PRIVATE | No-information/handoff | — | NEEDS_INTERPRETATION |
| CUS-SC3 | screenshot/messageImage_1788321878908.jpg · LINE chat ~10:55 | 54x12x43 | CLARIFY | PUBLIC | Correction/change target | — | SOURCE_CONFIRMED |
| CUS-P07 | kase_tee_tong_kae.pdf · page 5 item 7 / page 7 item 9 | คิดค่านำเข้าให้หน่อย / คำนวนค่าขนส่ง | RAG | PUBLIC | Shipping rate/calculation | yes | SOURCE_CONFIRMED |
| CUS-P06 | kase_tee_tong_kae.pdf · page 6 | (company fact genuinely not in KB) | HUMAN_CS | PUBLIC | No-information/handoff | yes | SOURCE_CONFIRMED |
| CUS-P20 | kase_tee_tong_kae.pdf · page 17 CSW20 + Ai.xlsx 1.thameuangton #29 | ช่วยแปลงลิงก์ให้หน่อยค่ะ | WORKFLOW | PUBLIC | Link conversion | yes | SOURCE_CONFIRMED |
| CUS-RL-genuine_continuation | tests/fixtures/real_line/genuine_continuation.json · real production replay fixture | FT318220260726001 | ERP | NA | Context/follow-up | — | SOURCE_CONFIRMED |
| CUS-RL-p0_01_repeat_after_completed_cycle | tests/fixtures/real_line/p0_01_repeat_after_completed_cycle.json · real production replay fixture | ขอเช็กพัสดุเดียวครับ | WORKFLOW | NA | Context/follow-up | — | SOURCE_CONFIRMED |
| CUS-RL-p0_01_stale_cycle | tests/fixtures/real_line/p0_01_stale_cycle.json · real production replay fixture | ขอเช็กพัสดุเดียวครับ | WORKFLOW | NA | Context/follow-up | — | SOURCE_CONFIRMED |

## Could not read / partial

* PDF embedded screenshots (~40) were **not individually OCR'd** — the PDF text layer + cross-reference to the `Ai.xlsx` canned answers was used instead. The annotations name the case (TC/CSW ids), which resolve to the sheet rows already captured.
* `Ai.xlsx` sheets `Api Overall`, `all`, `API Summary for Client` are project meta / API design, not customer test cases — summarised above, not turned into UAT cases.
* `Ai.xlsx` sheet `API` row `SecretCode` = a real 64-char credential — **read, recognised, and MASKED**; it is not written to the manifest or the dataset.

