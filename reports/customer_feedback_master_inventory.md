# Customer Feedback Master Inventory — Phase 6 Completion Round

Generated from REAL source content read this session (docx extracted via unzip+strip, both .xlsx read via openpyxl, 4 raw LINE chat exports read and sanitized by a read-only sub-agent). No record here is invented; every row traces to a source file/sheet/location.

## Totals

- **TOTAL SOURCE FILES**: 8 (docx ×1, xlsx ×3 incl. 1 duplicate, LINE chat exports ×4)
- **TOTAL RAW CUSTOMER COMMENTS / REQUIREMENTS**: 102
- **COVERED / FIXED**: 27
- **KB_CONTENT_GAP** (needs a Knowledge Base entry, not a code change): 28
- **API_GAP** (needs a backend endpoint that does not exist yet): 30
- **OUT_OF_SCOPE** (needs a capability this repo does not implement — OCR/Vision, catalog matching, cross-session correlation, etc.): 13
- **NOT_FIXED_THIS_PASS / NEEDS_LIVE_VERIFICATION / PARTIAL**: 5

By subsystem: RAG=27, ERP=25, HUMAN_CS=9, ENTITY_EXTRACTION=6, INTENT=5, CONTEXT=4, MULTI_INTENT=4, CORRECTION=4, PRODUCT_POLICY=3, RESPONSE=2, TOPIC_SWITCH=2, GROUNDING=2, STATE=2, ACTION_TRUTH=2, NEXT_ACTION=1, SLOT_FILLING=1, MULTI_ORDER=1, TYPO=1, MULTI_ENTITY=1

## Full record table

| ID | Source | Issue | Subsystem | Route | Status |
|---|---|---|---|---|---|
| DOCX-T01 | แก้ไขเคส Shipify Part 2.docx | "ใช่ครับ" (bare affirmation after assistant offered help) triggered KB | INTENT | GENERAL | COVERED (pre-existing, PHASE-6B) |
| DOCX-T02 | แก้ไขเคส Shipify Part 2.docx | "มีบริการอะไรบ้าง" answered the service list but with no next-step que | RESPONSE | GENERAL | COVERED (pre-existing, PHASE-6B) |
| DOCX-T03 | แก้ไขเคส Shipify Part 2.docx | "ต้องการนำเข้าเครื่องจักร" searched KB and answered "no data" instead  | INTENT | GENERAL | COVERED (pre-existing, PHASE-6B/FIX-2. |
| DOCX-T04 | แก้ไขเคส Shipify Part 2.docx | service-inquiry turn then import-interest turn processed as unrelated  | CONTEXT | GENERAL | COVERED (P1/P2 architecture, this sess |
| DOCX-T05 | แก้ไขเคส Shipify Part 2.docx | "งั้นโอนเงินให้ร้านที่จีน" answered generic service list, did not reco | TOPIC_SWITCH | GENERAL | COVERED (pre-existing, PHASE-6B) |
| DOCX-T06 | แก้ไขเคส Shipify Part 2.docx | customer expressed intent to use the service, AI asked nothing further | NEXT_ACTION | GENERAL | COVERED (pre-existing, PHASE-6B) |
| DOCX-T07 | แก้ไขเคส Shipify Part 2.docx | KB has no data for a machinery import question, AI just says no info | GROUNDING | HUMAN_HANDOFF (soft) | COVERED (pre-existing Fix-2 Answerabil |
| DOCX-HELP-01 | แก้ไขเคส Shipify Part 2.docx | "ต้องการความช่วยเหลือ" triggered KB_NOT_FOUND fallback ("ตอนนี้ยังไม่ม | INTENT | GENERAL | COVERED (pre-existing, PHASE-6B — verb |
| DOCX-IMPORT-01 | แก้ไขเคส Shipify Part 2.docx | "การนำเข้าส่งออกของค่ะ" answered no-info, but the immediate follow-up  | INTENT | GENERAL/RAG | COVERED (pre-existing, OWNER-REAL-LINE |
| DOCX-LINK-01 | แก้ไขเคส Shipify Part 2.docx | "ขอลิงก์เว็บ Taobao และ Tmall" (wants the SITE URL) answered with "ส่ง | RESPONSE | GENERAL | COVERED (pre-existing, PHASE-6B — both |
| DOCX-WH-01 | แก้ไขเคส Shipify Part 2.docx | "ร้านส่งของไปคลังจีน แล้วจะรู้ได้ยังไงว่าเป็นลูกค้าคนไหน" answered wit | GROUNDING | RAG or HUMAN_HANDOFF | COVERED (pre-existing, PHASE-6B — hone |
| DOCX-WH-02 | แก้ไขเคส Shipify Part 2.docx | "ต้องแจ้งอะไรไหมว่าจะมีของไปส่งที่คลัง" (pre-arrival notification requ | INTENT | RAG or HUMAN_HANDOFF | COVERED (pre-existing, PHASE-6B) |
| DOCX-CONTACT-01 | แก้ไขเคส Shipify Part 2.docx | "ติดต่อช่องทางไหน"/"ขออีเมล และเว็บไซต์" (a PUBLIC contact-info questi | RAG | RAG | COVERED (pre-existing, PHASE-6B — this |
| AI-API-S1-1.0 | AI_API_Requirement_For_Clien | ขอที่อยู่โกดังหน่อย | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-2.0 | AI_API_Requirement_For_Clien | ขอที่อยู่โกดังจีน,ต้องการส่งของผ่านชิปปิ้ง ส่งไปที่อยู่ไหน | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-3.0 | AI_API_Requirement_For_Clien | สินค้าจะเข้าไทยตอนไหน | ERP | API (not yet available) | API_GAP |
| AI-API-S1-4.0 | AI_API_Requirement_For_Clien | ค่าขนส่งคิดยังไง คำนวนค่าส่งให้หน่อย | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-5.0 | AI_API_Requirement_For_Clien | ออกใบกำกับได้ไหม | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-6.0 | AI_API_Requirement_For_Clien | CBM คิวคืออะไร | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-7.0 | AI_API_Requirement_For_Clien | มีขั้นต่ำในการสั่งไหม | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-8.0 | AI_API_Requirement_For_Clien | สินค้าที่ห้ามนำเข้ามีอะไรบ้าง | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-9.0 | AI_API_Requirement_For_Clien | วิธีการชำระบิลสั่งซื้อ,ชำระค่าสินค้ายังไง,จ่ายบิลสั่งซื้อยังไง | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-10.0 | AI_API_Requirement_For_Clien | บิลขนส่งสามารถชำระได้เลยไหม | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-11.0 | AI_API_Requirement_For_Clien | ได้รับสินค้าไม่ครบ, เคลมสินค้ายังไงคะ | ERP | API (not yet available) | API_GAP |
| AI-API-S1-12.0 | AI_API_Requirement_For_Clien | สินค้าถึงโกดังหรือยัง , ได้รับสินค้าหรือยัง | ERP | API (not yet available) | API_GAP |
| AI-API-S1-13.0 | AI_API_Requirement_For_Clien | ระยะเวลาการส่งจากร้านจีน -โกดังจีน | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-14.0 | AI_API_Requirement_For_Clien | ชำระบัตรเครดิตได้ไหม | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-15.0 | AI_API_Requirement_For_Clien | ขอเบอร์ติดต่อ | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-16.0 | AI_API_Requirement_For_Clien | ร้านส่งหรือยังคะ | ERP | API (not yet available) | API_GAP |
| AI-API-S1-17.0 | AI_API_Requirement_For_Clien | ติดตามสถานะ สินค้า | ERP | API (not yet available) | API_GAP |
| AI-API-S1-18.0 | AI_API_Requirement_For_Clien | ยอดเงินไม่เข้า, เติมเงินแล้วรอตรวจสอบ | ERP | API (not yet available) | API_GAP |
| AI-API-S1-19.0 | AI_API_Requirement_For_Clien | เรียกรถให้ได้ไหม ,เหมารถให้ได้ไหม | HUMAN_CS | HUMAN_CS | API_GAP |
| AI-API-S1-20.0 | AI_API_Requirement_For_Clien | ขนส่งเอกชนมีอะไรบ้าง | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-21.0 | AI_API_Requirement_For_Clien | ยกเลิกบิลสั่งซื้อได้ไหม | ERP | API (not yet available) | API_GAP |
| AI-API-S1-22.0 | AI_API_Requirement_For_Clien | เรทเท่าไหร่คะ ,เรทนำเข้าเท่าไหร่ ,เรทฝากสั่งเท่าไหร่ | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-23.0 | AI_API_Requirement_For_Clien | คูปองใช้ไม่หมด คืนได้ไหมคะ | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-24.0 | AI_API_Requirement_For_Clien | มีบริการอะไรบ้าง | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-25.0 | AI_API_Requirement_For_Clien | ฝากสั่งกับฝากนำเข้าต่างกันอย่างไง | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-26.0 | AI_API_Requirement_For_Clien | ชำระบิลขนส่งยังไง,ชำระค่านำเข้ายังไง | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-27.0 | AI_API_Requirement_For_Clien | สนใจฝากสั่งสินค้าค่าใช้จ่ายคิดยังไง | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S1-28.0 | AI_API_Requirement_For_Clien | มีบริการตีลังไม้ไหม,ตีลังได้ไหม | RAG | RAG | KB_CONTENT_GAP |
| AI-API-S2-1.0 | AI_API_Requirement_For_Clien | สินค้าจะเข้าไทยตอนไหนคะ | ERP | API (not yet available) | API_GAP |
| AI-API-S2-2.0 | AI_API_Requirement_For_Clien | ต้องการแก้จำนวนสินค้าในบิล | ERP | API (not yet available) | API_GAP |
| AI-API-S2-3.0 | AI_API_Requirement_For_Clien | สามารถเปลี่ยนเป็นจัดส่งทางรถ,ทางเรือได้ไหมคะ | ERP | API (not yet available) | API_GAP |
| AI-API-S2-4.0 | AI_API_Requirement_For_Clien | ลืมเลือก VAT ไปค่ะ ,ต้องการVATด้วยค่ะ | ERP | API (not yet available) | API_GAP |
| AI-API-S2-5.0 | AI_API_Requirement_For_Clien | ถอนเงินสั่งซื้อยังไง,เงินที่ร้านคืนมา จะถอนยังไง | ERP | API (not yet available) | API_GAP |
| AI-API-S2-6.0 | AI_API_Requirement_For_Clien | ต้องการสั่งผลิตตามสเปค ,สั่งสกรีนโลโก้ได้ไหมคะ | HUMAN_CS | HUMAN_CS | API_GAP |
| AI-API-S2-7.0 | AI_API_Requirement_For_Clien | โหลดใบกำกับยังไง , ขอใบกำกับค่าสินค้าหน่อย | ERP | API (not yet available) | API_GAP |
| AI-API-S2-8.0 | AI_API_Requirement_For_Clien | บิลขนส่งนี้ หรือแทรคนี้เป็นของบิลสั่งซื้อไหน | ERP | API (not yet available) | API_GAP |
| AI-API-S2-9.0 | AI_API_Requirement_For_Clien | บิลขนส่ง FTxxx ต้องการเปลี่ยนที่อยู่จัดส่ง | ERP | API (not yet available) | API_GAP |
| AI-API-S2-10.0 | AI_API_Requirement_For_Clien | รีเเพ็คค่ะ | HUMAN_CS | HUMAN_CS | API_GAP |
| AI-API-S2-11.0 | AI_API_Requirement_For_Clien | บิลขนส่ง FT ต้องการเปลี่ยนเป็นรับเอง หรือเปลี่ยนส่งเอกชน | ERP | API (not yet available) | API_GAP |
| AI-API-S2-12.0 | AI_API_Requirement_For_Clien | ถอนเงินขนส่งยังไงคะ | ERP | API (not yet available) | API_GAP |
| AI-API-S2-13.0 | AI_API_Requirement_For_Clien | บิลซ้ำค่ะ | ERP | API (not yet available) | API_GAP |
| AI-API-S2-14.0 | AI_API_Requirement_For_Clien | ใช้คูปองยังไง | HUMAN_CS | HUMAN_CS | API_GAP |
| AI-API-S2-15.0 | AI_API_Requirement_For_Clien | ใส่ที่อยู่โกดังจีนถูกไหมคะ | ERP | API (not yet available) | API_GAP |
| AI-API-S2-16.0 | AI_API_Requirement_For_Clien | รวมบิลเหมารถค่ะ | HUMAN_CS | HUMAN_CS | API_GAP |
| AI-API-S2-17.0 | AI_API_Requirement_For_Clien | ขอแทรคไทยค่ะ (กรณีลูกค้าไม่เข้ามาเช็คในระบบเอง) | ERP | API (not yet available) | API_GAP |
| AI-API-S2-18.0 | AI_API_Requirement_For_Clien | วันนี้มีของเข้าไทยไหมคะ | ERP | API (not yet available) | API_GAP |
| TRAIN-01 | ปัญหาที่เจอในการตอบ (1).xlsx | ครีมอาบน้ำนำเข้าได้ไหม | PRODUCT_POLICY | RAG/PRODUCT_POLICY | KB_CONTENT_GAP |
| TRAIN-02 | ปัญหาที่เจอในการตอบ (1).xlsx | ทางรถกับทางเรือระยะเวลากี่วัน | RAG | RAG | KB_CONTENT_GAP |
| TRAIN-03 | ปัญหาที่เจอในการตอบ (1).xlsx | แล้วเรทนำเข้าเท่าไหร่คะ | RAG | RAG | KB_CONTENT_GAP |
| TRAIN-04 | ปัญหาที่เจอในการตอบ (1).xlsx | ฝากสั่งน้ำหอมได้ไหมคะ | PRODUCT_POLICY | PRODUCT_POLICY | KB_CONTENT_GAP / PARTIAL |
| TRAIN-05 | ปัญหาที่เจอในการตอบ (1).xlsx | เราสามารถสั่งแบตเตอรี่จำนวนเยอะได้ไหมคะ | PRODUCT_POLICY | PRODUCT_POLICY | NEEDS_LIVE_VERIFICATION |
| TRAIN-06 | ปัญหาที่เจอในการตอบ (1).xlsx | โกดังอ่อนนุชเปิดทุกวันหรอคะ | RAG | RAG | KB_CONTENT_GAP |
| TRAIN-07 | ปัญหาที่เจอในการตอบ (1).xlsx | มีขนส่งทางเครื่องบินไหม | RAG | RAG | KB_CONTENT_GAP |
| TRAIN-08 | ปัญหาที่เจอในการตอบ (1).xlsx | ใบกำกับค่าสินค้าออกได้ไหม | SLOT_FILLING | RAG (after slot collection) | NOT_FIXED_THIS_PASS |
| TRAIN-09 | ปัญหาที่เจอในการตอบ (1).xlsx | จัดส่งสินค้าถึงหน้าบ้านเลยไหม | ENTITY_EXTRACTION | RAG | FIXED THIS PASS |
| TRAIN-10 | ปัญหาที่เจอในการตอบ (1).xlsx | โหลดใบกำกับยังไง | RAG | RAG | KB_CONTENT_GAP |
| TRAIN-11 | ปัญหาที่เจอในการตอบ (1).xlsx | ตีลังไม้ได้ไหม | RAG | RAG | KB_CONTENT_GAP |
| LINE-01 | FT1145.txt (sanitized) | customer replies just ครับ/ค่ะ after a status update -- ambiguous ack  | CONTEXT | varies | COVERED (existing conversation-state a |
| LINE-02 | FT1145.txt (sanitized) | a block of 5-15 bare tracking codes with one question ('ถึงไทยวันไหน') | MULTI_ORDER | varies | PARTIAL |
| LINE-03 | FT1145.txt (sanitized) | customer sends an image then a one-line question with no order/trackin | MULTI_INTENT | varies | OUT_OF_SCOPE |
| LINE-04 | FT1145.txt (sanitized) | a paid bill's fulfillment method changed post-payment, needing re-pric | ERP | varies | API_GAP |
| LINE-05 | FT1145.txt (sanitized) | 'เอ้ย ผมส่งผิดเลขครับ' followed instantly by the corrected number | CORRECTION | varies | COVERED |
| LINE-06 | FT1145.txt (sanitized) | flat 'ไม่รับครับ'/'ไม่โอเคค่ะ' after a proposed alternative, no reason | CORRECTION | varies | COVERED (existing rejection detection, |
| LINE-07 | FT1145.txt (sanitized) | 'ขาดรายการที่ 1 จำนวน N ชิ้น และขาดรายการที่ 2/1 จำนวน N ชิ้น' -- two  | MULTI_INTENT | varies | OUT_OF_SCOPE |
| LINE-08 | FT1145.txt (sanitized) | one message folds 3 asks together (prepare parcels, combine with anoth | MULTI_INTENT | varies | OUT_OF_SCOPE |
| LINE-09 | FT1145.txt (sanitized) | bot states a count, customer disputes and recounts, count revised agai | STATE | varies | OUT_OF_SCOPE |
| LINE-10 | FT1145.txt (sanitized) | '15 ครบ' then 'อ่อ ใช่ๆ โทษครับ 11 ครับ 555' -- self-correction, infor | CORRECTION | varies | COVERED (generalization) |
| LINE-11 | FT1145.txt (sanitized) | 'อันนี้'/'อีกออเดอร์' repeated without restating which order | CONTEXT | varies | OUT_OF_SCOPE |
| LINE-12 | FT1145.txt (sanitized) | customer compares two records' shipping costs and expects reconciliati | ACTION_TRUTH | varies | OUT_OF_SCOPE |
| LINE-13 | FT1145.txt (sanitized) | customer conflates purchase-credit and shipping-credit wallet pools af | ERP | varies | API_GAP |
| LINE-14 | FT1145.txt (sanitized) | a question ('ถ้าร้านไม่ส่งเราคืนเงินได้ไหม') becomes an instruction da | CONTEXT | varies | OUT_OF_SCOPE |
| LINE-15 | FT1145.txt (sanitized) | two order codes reference the identical tracking number, flagged as a  | ERP | varies | API_GAP |
| LINE-16 | FT1145.txt (sanitized) | customer names the exact correct SKU themselves rather than waiting to | ENTITY_EXTRACTION | varies | OUT_OF_SCOPE |
| LINE-17 | FT1145.txt (sanitized) | customer repeatedly rejects compensation offers over several days, idi | HUMAN_CS | varies | COVERED |
| LINE-18 | FT1145.txt (sanitized) | a product-ban broadcast and an unrelated shipping-method question in t | TOPIC_SWITCH | varies | COVERED (generalization) |
| LINE-19 | FT1145.txt (sanitized) | words like รุ้สึก/ขะลอง/เเป็นน้า go untreated; spell-correction must n | TYPO | varies | COVERED |
| LINE-20 | FT1145.txt (sanitized) | 3+ consecutive messages while only an auto-reply fires between them | STATE | varies | OUT_OF_SCOPE |
| LINE-21 | FT1145.txt (sanitized) | damage complaint and a forward-looking carrier-preference question in  | MULTI_INTENT | varies | PARTIAL |
| LINE-22 | FT1145.txt (sanitized) | one box has two different tracking labels, needs de-duplication under  | ERP | varies | API_GAP |
| LINE-23 | FT1145.txt (sanitized) | link, quantity, and a cross-reference to a different earlier order in  | MULTI_ENTITY | varies | COVERED (generalization) |
| LINE-24 | FT1145.txt (sanitized) | frustration that a promised notification never arrived -- a trust/comp | HUMAN_CS | varies | COVERED |
| LINE-25 | FT1145.txt (sanitized) | high-intensity language phrased as an instruction to relay verbatim to | HUMAN_CS | varies | COVERED |
| LINE-26 | FT1145.txt (sanitized) | the same underlying pricing-sync bug restated slightly differently acr | HUMAN_CS | varies | OUT_OF_SCOPE |
| LINE-27 | FT1145.txt (sanitized) | customer refers to their own parallel accounts by short numeric nickna | ENTITY_EXTRACTION | varies | OUT_OF_SCOPE |
| LINE-28 | FT1145.txt (sanitized) | customer adopts the bot's own item/sub-item fraction notation, sometim | ENTITY_EXTRACTION | varies | OUT_OF_SCOPE |
| LINE-29 | FT1145.txt (sanitized) | 3 different fulfillment preferences stated within one conversation as  | CORRECTION | varies | COVERED (generalization) |
| LINE-30 | FT1145.txt (sanitized) | customer presents their own manual ledger calculation and asks the bot | ACTION_TRUTH | varies | OUT_OF_SCOPE |
| PHASE6-SLOT-PRODUCT-NOUN | Real LINE OWNER_TEST repro,  | 20 คู่อยากสั่งของจากจีน -> quantity treated as product, quantity re-as | ENTITY_EXTRACTION | GENERAL | FIXED (commit 5c08011, this session) |
| PHASE6-SLOT-COMPOUND-NOUN | owner completion-round instr | ชั้นวางของ -> ชั้นวาง (and the equivalent bug in the separate bare-rep | ENTITY_EXTRACTION | GENERAL | FIXED THIS PASS |