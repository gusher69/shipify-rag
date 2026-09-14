# Customer Feedback Master Inventory — Phase 6

Built from the ORIGINAL customer sources. Every record traces to a source file/sheet/row; nothing is invented.

## Terminal status accounting

Exactly **one** terminal status per requirement; the statuses sum to the total.

| Terminal status | Count | Meaning |
|---|---|---|
| FIXED_CODE | 3 | a code defect in this repo, fixed and test-locked |
| COVERED_EXISTING | 24 | already handled by shipped architecture, with mapped tests |
| API_GAP | 26 | blocked on a backend endpoint that does not exist yet; safe-fallback behaviour asserted separately |
| KB_CONTENT_GAP | 28 | needs a Knowledge Base entry; authoritative text prepared + dry-run audited |
| NEEDS_CUSTOMER_CONFIRMATION | 0 | the customer sources are genuinely silent; must not be guessed |
| HUMAN_CS_ONLY | 12 | a person must perform the operation (the customer sources say so) |
| OUT_OF_SCOPE_SAFE | 7 | needs a capability this repo does not implement; behaviour stays safe |
| NOT_FIXED | 1 | a real code gap deliberately deferred (owner ruling), with safe behaviour tested |
| NEEDS_LIVE_VERIFICATION | 1 | depends on live-LLM-tier behaviour; covered by the live-tier run |
| **TOTAL** | **102** | |

## Full record table

| FEEDBACK_ID | Source | Original problem | Subsystem | Terminal status | Test IDs |
|---|---|---|---|---|---|
| DOCX-T01 | แก้ไขเคส Shipify Part 2.docx | "ใช่ครับ" (bare affirmation after assistant offered help) triggered KB_N | INTENT | COVERED_EXISTING | tests/test_service_intent_flow.py::is_help_affirmation |
| DOCX-T02 | แก้ไขเคส Shipify Part 2.docx | "มีบริการอะไรบ้าง" answered the service list but with no next-step quest | RESPONSE | COVERED_EXISTING | services/service_intent_flow.py::SERVICE_DISCOVERY_REP |
| DOCX-T03 | แก้ไขเคส Shipify Part 2.docx | "ต้องการนำเข้าเครื่องจักร" searched KB and answered "no data" instead of | INTENT | COVERED_EXISTING | tests/test_fix23_product_interest.py |
| DOCX-T04 | แก้ไขเคส Shipify Part 2.docx | service-inquiry turn then import-interest turn processed as unrelated me | CONTEXT | COVERED_EXISTING | tests/test_p1_conversation_resolution.py; tests/test_p |
| DOCX-T05 | แก้ไขเคส Shipify Part 2.docx | "งั้นโอนเงินให้ร้านที่จีน" answered generic service list, did not recogn | TOPIC_SWITCH | COVERED_EXISTING | services/service_intent_flow.py::MONEY_TRANSFER_REPLY |
| DOCX-T06 | แก้ไขเคส Shipify Part 2.docx | customer expressed intent to use the service, AI asked nothing further | NEXT_ACTION | COVERED_EXISTING | services/service_intent_flow.py (reply_for_family alwa |
| DOCX-T07 | แก้ไขเคส Shipify Part 2.docx | KB has no data for a machinery import question, AI just says no info | GROUNDING | COVERED_EXISTING | tests/test_customer_uat_fix2_unsupported_fact_handoff. |
| DOCX-HELP-01 | แก้ไขเคส Shipify Part 2.docx | "ต้องการความช่วยเหลือ" triggered KB_NOT_FOUND fallback ("ตอนนี้ยังไม่มีข | INTENT | COVERED_EXISTING | services/service_intent_flow.py::HELP_REPLY (verbatim  |
| DOCX-IMPORT-01 | แก้ไขเคส Shipify Part 2.docx | "การนำเข้าส่งออกของค่ะ" answered no-info, but the immediate follow-up "ถ | INTENT | COVERED_EXISTING | services/service_intent_flow.py::_broad_china_buy guar |
| DOCX-LINK-01 | แก้ไขเคส Shipify Part 2.docx | "ขอลิงก์เว็บ Taobao และ Tmall" (wants the SITE URL) answered with "ส่งลิ | RESPONSE | COVERED_EXISTING | services/service_intent_flow.py::WEBSITE_LINK_REPLY; i |
| DOCX-WH-01 | แก้ไขเคส Shipify Part 2.docx | "ร้านส่งของไปคลังจีน แล้วจะรู้ได้ยังไงว่าเป็นลูกค้าคนไหน" answered with  | GROUNDING | COVERED_EXISTING | services/service_intent_flow.py::WAREHOUSE_INBOUND_FAL |
| DOCX-WH-02 | แก้ไขเคส Shipify Part 2.docx | "ต้องแจ้งอะไรไหมว่าจะมีของไปส่งที่คลัง" (pre-arrival notification requir | INTENT | COVERED_EXISTING | services/service_intent_flow.py::WAREHOUSE_INBOUND_FAL |
| DOCX-CONTACT-01 | แก้ไขเคส Shipify Part 2.docx | "ติดต่อช่องทางไหน"/"ขออีเมล และเว็บไซต์" (a PUBLIC contact-info question | RAG | COVERED_EXISTING | services/service_intent_flow.py::CONTACT_INFO / fetch_ |
| AI-API-S1-1.0 | AI_API_Requirement_For_Clien | ขอที่อยู่โกดังหน่อย | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-2.0 | AI_API_Requirement_For_Clien | ขอที่อยู่โกดังจีน,ต้องการส่งของผ่านชิปปิ้ง ส่งไปที่อยู่ไหน | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-3.0 | AI_API_Requirement_For_Clien | สินค้าจะเข้าไทยตอนไหน | ERP | API_GAP | — |
| AI-API-S1-4.0 | AI_API_Requirement_For_Clien | ค่าขนส่งคิดยังไง คำนวนค่าส่งให้หน่อย | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-5.0 | AI_API_Requirement_For_Clien | ออกใบกำกับได้ไหม | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-6.0 | AI_API_Requirement_For_Clien | CBM คิวคืออะไร | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-7.0 | AI_API_Requirement_For_Clien | มีขั้นต่ำในการสั่งไหม | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-8.0 | AI_API_Requirement_For_Clien | สินค้าที่ห้ามนำเข้ามีอะไรบ้าง | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-9.0 | AI_API_Requirement_For_Clien | วิธีการชำระบิลสั่งซื้อ,ชำระค่าสินค้ายังไง,จ่ายบิลสั่งซื้อยังไง | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-10.0 | AI_API_Requirement_For_Clien | บิลขนส่งสามารถชำระได้เลยไหม | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-11.0 | AI_API_Requirement_For_Clien | ได้รับสินค้าไม่ครบ, เคลมสินค้ายังไงคะ | ERP | API_GAP | — |
| AI-API-S1-12.0 | AI_API_Requirement_For_Clien | สินค้าถึงโกดังหรือยัง , ได้รับสินค้าหรือยัง | ERP | API_GAP | — |
| AI-API-S1-13.0 | AI_API_Requirement_For_Clien | ระยะเวลาการส่งจากร้านจีน -โกดังจีน | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-14.0 | AI_API_Requirement_For_Clien | ชำระบัตรเครดิตได้ไหม | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-15.0 | AI_API_Requirement_For_Clien | ขอเบอร์ติดต่อ | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-16.0 | AI_API_Requirement_For_Clien | ร้านส่งหรือยังคะ | ERP | API_GAP | — |
| AI-API-S1-17.0 | AI_API_Requirement_For_Clien | ติดตามสถานะ สินค้า | ERP | API_GAP | — |
| AI-API-S1-18.0 | AI_API_Requirement_For_Clien | ยอดเงินไม่เข้า, เติมเงินแล้วรอตรวจสอบ | ERP | API_GAP | — |
| AI-API-S1-19.0 | AI_API_Requirement_For_Clien | เรียกรถให้ได้ไหม ,เหมารถให้ได้ไหม | HUMAN_CS | HUMAN_CS_ONLY | — |
| AI-API-S1-20.0 | AI_API_Requirement_For_Clien | ขนส่งเอกชนมีอะไรบ้าง | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-21.0 | AI_API_Requirement_For_Clien | ยกเลิกบิลสั่งซื้อได้ไหม | ERP | API_GAP | — |
| AI-API-S1-22.0 | AI_API_Requirement_For_Clien | เรทเท่าไหร่คะ ,เรทนำเข้าเท่าไหร่ ,เรทฝากสั่งเท่าไหร่ | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-23.0 | AI_API_Requirement_For_Clien | คูปองใช้ไม่หมด คืนได้ไหมคะ | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-24.0 | AI_API_Requirement_For_Clien | มีบริการอะไรบ้าง | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-25.0 | AI_API_Requirement_For_Clien | ฝากสั่งกับฝากนำเข้าต่างกันอย่างไง | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-26.0 | AI_API_Requirement_For_Clien | ชำระบิลขนส่งยังไง,ชำระค่านำเข้ายังไง | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-27.0 | AI_API_Requirement_For_Clien | สนใจฝากสั่งสินค้าค่าใช้จ่ายคิดยังไง | RAG | KB_CONTENT_GAP | — |
| AI-API-S1-28.0 | AI_API_Requirement_For_Clien | มีบริการตีลังไม้ไหม,ตีลังได้ไหม | RAG | KB_CONTENT_GAP | — |
| AI-API-S2-1.0 | AI_API_Requirement_For_Clien | สินค้าจะเข้าไทยตอนไหนคะ | ERP | API_GAP | — |
| AI-API-S2-2.0 | AI_API_Requirement_For_Clien | ต้องการแก้จำนวนสินค้าในบิล | ERP | API_GAP | — |
| AI-API-S2-3.0 | AI_API_Requirement_For_Clien | สามารถเปลี่ยนเป็นจัดส่งทางรถ,ทางเรือได้ไหมคะ | ERP | API_GAP | — |
| AI-API-S2-4.0 | AI_API_Requirement_For_Clien | ลืมเลือก VAT ไปค่ะ ,ต้องการVATด้วยค่ะ | ERP | API_GAP | — |
| AI-API-S2-5.0 | AI_API_Requirement_For_Clien | ถอนเงินสั่งซื้อยังไง,เงินที่ร้านคืนมา จะถอนยังไง | ERP | API_GAP | — |
| AI-API-S2-6.0 | AI_API_Requirement_For_Clien | ต้องการสั่งผลิตตามสเปค ,สั่งสกรีนโลโก้ได้ไหมคะ | HUMAN_CS | HUMAN_CS_ONLY | — |
| AI-API-S2-7.0 | AI_API_Requirement_For_Clien | โหลดใบกำกับยังไง , ขอใบกำกับค่าสินค้าหน่อย | ERP | API_GAP | — |
| AI-API-S2-8.0 | AI_API_Requirement_For_Clien | บิลขนส่งนี้ หรือแทรคนี้เป็นของบิลสั่งซื้อไหน | ERP | API_GAP | — |
| AI-API-S2-9.0 | AI_API_Requirement_For_Clien | บิลขนส่ง FTxxx ต้องการเปลี่ยนที่อยู่จัดส่ง | ERP | API_GAP | — |
| AI-API-S2-10.0 | AI_API_Requirement_For_Clien | รีเเพ็คค่ะ | HUMAN_CS | HUMAN_CS_ONLY | — |
| AI-API-S2-11.0 | AI_API_Requirement_For_Clien | บิลขนส่ง FT ต้องการเปลี่ยนเป็นรับเอง หรือเปลี่ยนส่งเอกชน | ERP | API_GAP | — |
| AI-API-S2-12.0 | AI_API_Requirement_For_Clien | ถอนเงินขนส่งยังไงคะ | ERP | API_GAP | — |
| AI-API-S2-13.0 | AI_API_Requirement_For_Clien | บิลซ้ำค่ะ | ERP | API_GAP | — |
| AI-API-S2-14.0 | AI_API_Requirement_For_Clien | ใช้คูปองยังไง | HUMAN_CS | HUMAN_CS_ONLY | — |
| AI-API-S2-15.0 | AI_API_Requirement_For_Clien | ใส่ที่อยู่โกดังจีนถูกไหมคะ | ERP | API_GAP | — |
| AI-API-S2-16.0 | AI_API_Requirement_For_Clien | รวมบิลเหมารถค่ะ | HUMAN_CS | HUMAN_CS_ONLY | — |
| AI-API-S2-17.0 | AI_API_Requirement_For_Clien | ขอแทรคไทยค่ะ (กรณีลูกค้าไม่เข้ามาเช็คในระบบเอง) | ERP | API_GAP | — |
| AI-API-S2-18.0 | AI_API_Requirement_For_Clien | วันนี้มีของเข้าไทยไหมคะ | ERP | API_GAP | — |
| TRAIN-01 | ปัญหาที่เจอในการตอบ (1).xlsx | ครีมอาบน้ำนำเข้าได้ไหม | PRODUCT_POLICY | KB_CONTENT_GAP | — |
| TRAIN-02 | ปัญหาที่เจอในการตอบ (1).xlsx | ทางรถกับทางเรือระยะเวลากี่วัน | RAG | KB_CONTENT_GAP | — |
| TRAIN-03 | ปัญหาที่เจอในการตอบ (1).xlsx | แล้วเรทนำเข้าเท่าไหร่คะ | RAG | KB_CONTENT_GAP | — |
| TRAIN-04 | ปัญหาที่เจอในการตอบ (1).xlsx | ฝากสั่งน้ำหอมได้ไหมคะ | PRODUCT_POLICY | KB_CONTENT_GAP | — |
| TRAIN-05 | ปัญหาที่เจอในการตอบ (1).xlsx | เราสามารถสั่งแบตเตอรี่จำนวนเยอะได้ไหมคะ | PRODUCT_POLICY | NEEDS_LIVE_VERIFICATION | tests/phase6_lab/run_live_tier.py (SHIPIFY_LIVE_TIER=1 |
| TRAIN-06 | ปัญหาที่เจอในการตอบ (1).xlsx | โกดังอ่อนนุชเปิดทุกวันหรอคะ | RAG | KB_CONTENT_GAP | — |
| TRAIN-07 | ปัญหาที่เจอในการตอบ (1).xlsx | มีขนส่งทางเครื่องบินไหม | RAG | KB_CONTENT_GAP | — |
| TRAIN-08 | ปัญหาที่เจอในการตอบ (1).xlsx | ใบกำกับค่าสินค้าออกได้ไหม | SLOT_FILLING | NOT_FIXED | tests/test_phase6_owner_contracts.py::TestInvoiceConce |
| TRAIN-09 | ปัญหาที่เจอในการตอบ (1).xlsx | จัดส่งสินค้าถึงหน้าบ้านเลยไหม | ENTITY_EXTRACTION | FIXED_CODE | tests/test_phase6_delivery_capability_routing.py |
| TRAIN-10 | ปัญหาที่เจอในการตอบ (1).xlsx | โหลดใบกำกับยังไง | RAG | KB_CONTENT_GAP | — |
| TRAIN-11 | ปัญหาที่เจอในการตอบ (1).xlsx | ตีลังไม้ได้ไหม | RAG | KB_CONTENT_GAP | — |
| LINE-01 | FT1145.txt (sanitized) | customer replies just ครับ/ค่ะ after a status update -- ambiguous ack vs | CONTEXT | COVERED_EXISTING | — |
| LINE-02 | FT1145.txt (sanitized) | a block of 5-15 bare tracking codes with one question ('ถึงไทยวันไหน') | MULTI_ORDER | API_GAP | — |
| LINE-03 | FT1145.txt (sanitized) | customer sends an image then a one-line question with no order/tracking  | MULTI_INTENT | OUT_OF_SCOPE_SAFE | — |
| LINE-04 | FT1145.txt (sanitized) | a paid bill's fulfillment method changed post-payment, needing re-pricin | ERP | API_GAP | — |
| LINE-05 | FT1145.txt (sanitized) | 'เอ้ย ผมส่งผิดเลขครับ' followed instantly by the corrected number | CORRECTION | COVERED_EXISTING | — |
| LINE-06 | FT1145.txt (sanitized) | flat 'ไม่รับครับ'/'ไม่โอเคค่ะ' after a proposed alternative, no reason g | CORRECTION | COVERED_EXISTING | — |
| LINE-07 | FT1145.txt (sanitized) | 'ขาดรายการที่ 1 จำนวน N ชิ้น และขาดรายการที่ 2/1 จำนวน N ชิ้น' -- two cl | MULTI_INTENT | HUMAN_CS_ONLY | — |
| LINE-08 | FT1145.txt (sanitized) | one message folds 3 asks together (prepare parcels, combine with another | MULTI_INTENT | HUMAN_CS_ONLY | — |
| LINE-09 | FT1145.txt (sanitized) | bot states a count, customer disputes and recounts, count revised again  | STATE | HUMAN_CS_ONLY | — |
| LINE-10 | FT1145.txt (sanitized) | '15 ครบ' then 'อ่อ ใช่ๆ โทษครับ 11 ครับ 555' -- self-correction, informa | CORRECTION | COVERED_EXISTING | — |
| LINE-11 | FT1145.txt (sanitized) | 'อันนี้'/'อีกออเดอร์' repeated without restating which order | CONTEXT | OUT_OF_SCOPE_SAFE | — |
| LINE-12 | FT1145.txt (sanitized) | customer compares two records' shipping costs and expects reconciliation | ACTION_TRUTH | HUMAN_CS_ONLY | — |
| LINE-13 | FT1145.txt (sanitized) | customer conflates purchase-credit and shipping-credit wallet pools afte | ERP | API_GAP | — |
| LINE-14 | FT1145.txt (sanitized) | a question ('ถ้าร้านไม่ส่งเราคืนเงินได้ไหม') becomes an instruction days | CONTEXT | OUT_OF_SCOPE_SAFE | — |
| LINE-15 | FT1145.txt (sanitized) | two order codes reference the identical tracking number, flagged as a po | ERP | API_GAP | — |
| LINE-16 | FT1145.txt (sanitized) | customer names the exact correct SKU themselves rather than waiting to b | ENTITY_EXTRACTION | OUT_OF_SCOPE_SAFE | — |
| LINE-17 | FT1145.txt (sanitized) | customer repeatedly rejects compensation offers over several days, idiom | HUMAN_CS | COVERED_EXISTING | — |
| LINE-18 | FT1145.txt (sanitized) | a product-ban broadcast and an unrelated shipping-method question in the | TOPIC_SWITCH | COVERED_EXISTING | — |
| LINE-19 | FT1145.txt (sanitized) | words like รุ้สึก/ขะลอง/เเป็นน้า go untreated; spell-correction must not | TYPO | COVERED_EXISTING | — |
| LINE-20 | FT1145.txt (sanitized) | 3+ consecutive messages while only an auto-reply fires between them | STATE | OUT_OF_SCOPE_SAFE | — |
| LINE-21 | FT1145.txt (sanitized) | damage complaint and a forward-looking carrier-preference question in on | MULTI_INTENT | HUMAN_CS_ONLY | — |
| LINE-22 | FT1145.txt (sanitized) | one box has two different tracking labels, needs de-duplication under on | ERP | API_GAP | — |
| LINE-23 | FT1145.txt (sanitized) | link, quantity, and a cross-reference to a different earlier order in on | MULTI_ENTITY | COVERED_EXISTING | — |
| LINE-24 | FT1145.txt (sanitized) | frustration that a promised notification never arrived -- a trust/compla | HUMAN_CS | COVERED_EXISTING | — |
| LINE-25 | FT1145.txt (sanitized) | high-intensity language phrased as an instruction to relay verbatim to t | HUMAN_CS | COVERED_EXISTING | — |
| LINE-26 | FT1145.txt (sanitized) | the same underlying pricing-sync bug restated slightly differently acros | HUMAN_CS | HUMAN_CS_ONLY | — |
| LINE-27 | FT1145.txt (sanitized) | customer refers to their own parallel accounts by short numeric nickname | ENTITY_EXTRACTION | OUT_OF_SCOPE_SAFE | — |
| LINE-28 | FT1145.txt (sanitized) | customer adopts the bot's own item/sub-item fraction notation, sometimes | ENTITY_EXTRACTION | OUT_OF_SCOPE_SAFE | — |
| LINE-29 | FT1145.txt (sanitized) | 3 different fulfillment preferences stated within one conversation as co | CORRECTION | COVERED_EXISTING | — |
| LINE-30 | FT1145.txt (sanitized) | customer presents their own manual ledger calculation and asks the bot t | ACTION_TRUTH | HUMAN_CS_ONLY | — |
| PHASE6-SLOT-PRODUCT-NOUN | Real LINE OWNER_TEST repro,  | 20 คู่อยากสั่งของจากจีน -> quantity treated as product, quantity re-aske | ENTITY_EXTRACTION | FIXED_CODE | tests/test_phase6_slot_consumption.py |
| PHASE6-SLOT-COMPOUND-NOUN | owner completion-round instr | ชั้นวางของ -> ชั้นวาง (and the equivalent bug in the separate bare-reply | ENTITY_EXTRACTION | FIXED_CODE | tests/test_phase6_slot_consumption.py::TestCompoundPro |