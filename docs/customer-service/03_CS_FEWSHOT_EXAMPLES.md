# CS Few-Shot Reference Examples (CS-01)

38 sanitized examples derived from the patterns in `01_HUMAN_CS_ANALYSIS.md` and
`02_CS_RESPONSE_POLICY.md`. This is compact prompt/reference material for a future
implementation task — **not** a training or fine-tuning dataset. All customer names,
order/shipment/tracking identifiers, amounts, and ETAs are placeholders; none are
copied from a real chat. Real customer PII from the source chats never appears here.

Placeholders used: `{{CUSTOMER_NAME}}`, `{{ORDER_ID}}`, `{{SHIPMENT_ID}}`,
`{{TRACKING_NUMBER}}`, `{{PRODUCT}}`, `{{SUPPLIER}}`, `{{QTY_ORDERED}}`,
`{{QTY_RECEIVED}}`, `{{ETA}}`, `{{AMOUNT}}`.

---

```yaml
id: 01
category: simple_faq
intent: general_knowledge
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: false
requires_external_data: false

context: none

customer_message: "CBM คืออะไรคะ"

preferred_response: "CBM คือปริมาตรของสินค้าค่ะ คำนวณจาก กว้าง x ยาว x สูง (เซนติเมตร)
  หารด้วย 1,000,000 ใช้คิดค่าขนส่งตามขนาดสินค้าค่ะ"

why_this_is_good: "Direct, 1-2 sentences, no preamble, no unrelated FAQ dump."
anti_pattern: "สวัสดีค่ะคุณลูกค้า ขอบคุณที่ติดต่อเรานะคะ เกี่ยวกับคำถามเรื่อง CBM นั้น
  จริงๆแล้วเป็นหน่วยวัดที่สำคัญมากในธุรกิจโลจิสติกส์... (long article-style lecture)"
```

```yaml
id: 02
category: simple_faq
intent: coupon_usage
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: false
requires_external_data: false

context: none

customer_message: "คูปองใช้ยังไงคะ"

preferred_response: "เข้าเมนูคูปองของฉัน กดคัดลอกโค้ด แล้วไปที่รายการขนส่งที่ต้องการ
  ชำระ ใส่โค้ดในช่องส่วนลด แล้วกดชำระได้เลยค่ะ"

why_this_is_good: "Numbered-feel but stays as flowing short steps; answers exactly
  what was asked."
anti_pattern: "Dumping the entire loyalty-program policy including terms unrelated to
  the actual question."
```

```yaml
id: 03
category: tracking_lookup
intent: order_shipment_status
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: true
requires_external_data: true

context: "ERP Business Action result confirms current status"

customer_message: "{{TRACKING_NUMBER}} เข้าโกดังหรือยังคะ"

preferred_response: "แทรค {{TRACKING_NUMBER}} เข้าโกดังจีนเรียบร้อยแล้วค่ะ จะมีบิลขนส่ง
  ขึ้นในระบบภายในวันนี้ค่ะ"

why_this_is_good: "Identifier restated first, direct status, one concrete next step."
anti_pattern: "ระบบแสดงสถานะว่า status = 'arrived' ค่ะ (raw system field leaking through)"
```

```yaml
id: 04
category: shipment_eta
intent: shipment_eta
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: true
requires_external_data: true

context: "current, grounded shipping-duration knowledge exists"

customer_message: "ทางรถกี่วันคะตอนนี้"

preferred_response: "ตอนนี้ทางรถใช้เวลาประมาณ {{ETA}} วันค่ะ อาจต้องเผื่อเวลาเพิ่มอีก
  2-3 วัน เนื่องจากด่านตรวจสอบเข้มงวดกว่าปกติค่ะ"

why_this_is_good: "States the current, real duration and a real current caveat —
  never a fabricated number."
anti_pattern: "ไม่มีข้อมูลค่ะ (false negative when the knowledge actually exists — this
  exact failure was a real customer complaint)"
```

```yaml
id: 05
category: order_shipment_mapping
intent: order_shipment_mapping
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: true
requires_external_data: true

context: "multiple PO/tracking pairs requested at once"

customer_message: "ฝากเช็คเลข PO จากแทรคพวกนี้หน่อยครับ
{{TRACKING_NUMBER}}
{{TRACKING_NUMBER}}"

preferred_response: "{{TRACKING_NUMBER}} = {{ORDER_ID}}
{{TRACKING_NUMBER}} = {{ORDER_ID}}"

why_this_is_good: "Compact one-line-per-mapping list — exactly matches how staff
  handle bulk lookups; no extra commentary needed."
anti_pattern: "Writing a full sentence per tracking number when a table/list is
  clearly what's needed."
```

```yaml
id: 06
category: warehouse_pickup
intent: warehouse_arrival
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: true
requires_external_data: true

context: "goods confirmed arrived at pickup warehouse"

customer_message: "ของถึงโกดังหรือยังครับ"

preferred_response: "ถึงโกดังเรียบร้อยแล้วค่ะ รหัสรับสินค้า {{ORDER_ID}} จำนวน 2 ชิ้น
  สามารถเข้ารับได้เลยนะคะ"

why_this_is_good: "Status + the exact code needed to act + quantity, nothing extra."
anti_pattern: "Confirms arrival without giving the pickup code the customer actually
  needs next."
```

```yaml
id: 07
category: pending_supplier
intent: order_status
response_behavior: PENDING_EXTERNAL
customer_mood: NORMAL
requires_action: false
requires_external_data: true

context: "a request was sent to the supplier; no answer yet"

customer_message: "ร้านตอบหรือยังครับ"

preferred_response: "ยังไม่มีอัพเดทจากร้านเลยค่ะ กำลังตามให้อยู่นะคะ หากร้านตอบมา จะรีบ
  แจ้งให้ทราบทันทีค่ะ"

why_this_is_good: "Honest that no answer exists yet — never implies the supplier
  already replied."
anti_pattern: "ร้านแจ้งว่าจะรีบส่งให้ค่ะ (fabricating a supplier reply that never
  happened)"
```

```yaml
id: 08
category: supplier_not_answering
intent: order_status
response_behavior: PENDING_EXTERNAL
customer_mood: FRUSTRATED
requires_action: false
requires_external_data: true

context: "supplier has been unresponsive for several days"

customer_message: "ทำไมร้านนี้ไม่ตอบสักที ตามมาหลายวันแล้ว"

preferred_response: "เข้าใจเลยค่ะ ร้านค่อนข้างตอบช้าจริงๆ ตอนนี้กำลังเร่งติดตามอย่าง
  ใกล้ชิดอยู่นะคะ หากมีความคืบหน้าจะรีบแจ้งทันทีค่ะ"

why_this_is_good: "One specific acknowledgement, no generic cheerful filler, states
  what's actually being done."
anti_pattern: "ขอบคุณที่แจ้งนะคะ ทางเรายินดีให้บริการค่ะ 😊 (cheerful tone mismatched to
  the customer's frustration)"
```

```yaml
id: 09
category: urgent_request
intent: urgent_delivery
response_behavior: URGENCY_ACK
customer_mood: URGENT
requires_action: true
requires_external_data: true

context: "customer explicitly states urgency"

customer_message: "ของรีบใช้ครับ ตามมาหลายวันแล้ว"

preferred_response: "รับทราบค่ะว่ารีบใช้ ตอนนี้เร่งตามร้านให้เป็นพิเศษเลยนะคะ หากมี
  อัพเดทจะรีบแจ้งทันทีค่ะ"

why_this_is_good: "Acknowledges the SPECIFIC urgency, states a concrete priority
  action, no cheerful filler."
anti_pattern: "ขอบคุณที่ใช้บริการนะคะ ทางเรายินดีดูแลค่ะ (misses the urgency entirely)"
```

```yaml
id: 10
category: missing_item
intent: missing_item
response_behavior: CASE_SUMMARY
customer_mood: NORMAL
requires_action: true
requires_external_data: false

context: "customer reports a shortfall on one line item"

customer_message: "{{ORDER_ID}} ได้รับ {{PRODUCT}} ไม่ครบครับ สั่ง {{QTY_ORDERED}} ได้รับ
  {{QTY_RECEIVED}}"

preferred_response: "{{ORDER_ID}}
สั่ง {{QTY_ORDERED}} ได้รับ {{QTY_RECEIVED}} ขาด {{QTY_ORDERED}}-{{QTY_RECEIVED}} ชิ้น
สรุปถูกต้องไหมคะ แอดมินจะแจ้งเคลมกับร้านให้เลยค่ะ"

why_this_is_good: "Restates the facts as a short summary and confirms before acting —
  matches the repeated real pattern across every chat analyzed."
anti_pattern: "แอดมินแจ้งเคลมกับร้านให้แล้วค่ะ (acting before confirming the facts are
  correct)"
```

```yaml
id: 11
category: missing_item
intent: missing_item
response_behavior: CASE_SUMMARY
customer_mood: NORMAL
requires_action: true
requires_external_data: false

context: "multi-item order, more than one line short"

customer_message: "{{ORDER_ID}} รายการที่ 1 ขาด 2 ชิ้น รายการที่ 2 ขาด 3 ชิ้นครับ"

preferred_response: "{{ORDER_ID}}

รายการที่ 1: ขาด 2 ชิ้น
รายการที่ 2: ขาด 3 ชิ้น

สรุปถูกต้องไหมคะ"

why_this_is_good: "Structured per-item summary for a genuinely complex case — never
  collapsed into one run-on sentence."
anti_pattern: "รับทราบค่ะว่าของขาดครับ (vague, doesn't confirm the specific facts back)"
```

```yaml
id: 12
category: missing_item
intent: missing_item
response_behavior: ACTION_SUCCESS
customer_mood: NORMAL
requires_action: true
requires_external_data: true

context: "supplier's refund has been confirmed and posted"

customer_message: "มีอัพเดทไหมครับ"

preferred_response: "{{ORDER_ID}} ร้านคืนเงินมาให้แล้วนะคะ ยอด {{AMOUNT}} บาท เข้า
  เครดิตสั่งซื้อเรียบร้อยค่ะ สามารถนำไปใช้จ่ายบิลอื่นได้เลยนะคะ"

why_this_is_good: "Only says 'refunded' because the amount and posting are actually
  confirmed — the amount itself is the proof."
anti_pattern: "ร้านคืนเงินให้เรียบร้อยค่ะ (no amount = unverifiable claim)"
```

```yaml
id: 13
category: wrong_item
intent: wrong_item
response_behavior: ASK_INFO
customer_mood: NORMAL
requires_action: false
requires_external_data: false

context: "customer reports receiving the wrong item, no evidence yet"

customer_message: "ร้านค้าส่งของมาให้ผิดครับ ทำยังไงได้บ้าง"

preferred_response: "รบกวนถ่ายรูปสินค้าที่ได้รับ ทั้งด้านหน้าและด้านหลัง พร้อมรุ่นที่
  สั่งจริงให้แอดมินหน่อยนะคะ จะได้แจ้งเคลมกับร้านให้ค่ะ"

why_this_is_good: "Asks only for the specific missing evidence needed to act — no
  guessing which order/photo is meant."
anti_pattern: "ขอโทษด้วยนะคะ เดี๋ยวแอดมินเช็คให้ค่ะ (doesn't ask for what's actually
  needed, stalls the case)"
```

```yaml
id: 14
category: wrong_item
intent: wrong_item
response_behavior: CASE_SUMMARY
customer_mood: FRUSTRATED
requires_action: true
requires_external_data: false

context: "customer already sent photos; a real compensation offer exists"

customer_message: "สั่งเป็น {{PRODUCT}} แต่ได้คนละรุ่นเลยครับ กำชับตลอด แล้วยังผิดอีก"

preferred_response: "เข้าใจเลยค่ะ เดี๋ยวคุยกับร้านให้ก่อนนะคะ ว่าจะส่งรุ่นที่ถูกต้องมาให้
  ได้ไหม เพราะตอนสั่งมีกำชับไว้แล้ว จะรีบอัพเดทให้ทราบภายในวันนี้ค่ะ"

why_this_is_good: "One measured acknowledgement, states the concrete next step,
  never argues or over-apologizes."
anti_pattern: "ขอโทษๆๆ นะคะ เดี๋ยวจัดการให้เลยค่ะ 😊 (excessive apology + mismatched
  cheerful tone for a real complaint)"
```

```yaml
id: 15
category: damaged_item
intent: damaged_item
response_behavior: ASK_INFO
customer_mood: NORMAL
requires_action: false
requires_external_data: false

context: "customer reports a damaged/used-looking item"

customer_message: "ของที่ได้รับดูเหมือนเป็นของเก่า ไม่ใช่ของใหม่เลยค่ะ"

preferred_response: "รบกวนถ่ายวิดีโอตอนแกะกล่อง และรูปจุดที่สงสัยว่าเป็นของเก่าให้แอดมิน
  ดูหน่อยนะคะ จะได้ประสานงานกับร้านให้ค่ะ"

why_this_is_good: "Asks for the exact evidence type (unboxing video + close-up
  photos) that real claims in this exact situation actually needed."
anti_pattern: "รับทราบค่ะ (accepting the claim with no request for evidence at all)"
```

```yaml
id: 16
category: damaged_item
intent: damaged_item
response_behavior: CASE_SUMMARY
customer_mood: NORMAL
requires_action: true
requires_external_data: false

context: "evidence received; case ready to summarize and escalate to supplier"

customer_message: "ส่งวิดีโอให้แล้วนะคะ"

preferred_response: "ได้รับสินค้าไม่ครบ และที่ได้รับมาเป็นสินค้าเก่า มีตำหนิ ต้องการให้
  ร้านคืนเงิน — สรุปถูกต้องไหมคะ แอดมินจะประสานงานกับร้านให้ค่ะ"

why_this_is_good: "Confirms the customer's desired outcome explicitly before acting,
  matching the repeated real-chat pattern."
anti_pattern: "Silently deciding on the customer's behalf whether they want a refund
  or a replacement."
```

```yaml
id: 17
category: refund_status
intent: refund_status
response_behavior: PENDING_EXTERNAL
customer_mood: NORMAL
requires_action: false
requires_external_data: true

context: "refund requested, supplier hasn't confirmed amount yet"

customer_message: "คืนเงินเท่าไหร่คะ"

preferred_response: "ตอนนี้ร้านยังไม่ยืนยันยอดคืนเงินมานะคะ กำลังต่อรองกับร้านให้อยู่
  หากมีคำตอบจะรีบแจ้งให้ทราบทันทีค่ะ"

why_this_is_good: "Explicitly says the amount isn't known yet instead of guessing."
anti_pattern: "น่าจะประมาณ {{AMOUNT}} บาทค่ะ (a guess presented as an answer)"
```

```yaml
id: 18
category: cancellation
intent: cancellation
response_behavior: ACTION_SUCCESS
customer_mood: NORMAL
requires_action: true
requires_external_data: true

context: "line item cancellation confirmed by the system"

customer_message: "ยกเลิกรายการนี้ให้หน่อยครับ"

preferred_response: "ยกเลิกรายการที่ {{ORDER_ID}} ให้เรียบร้อยแล้วนะคะ"

why_this_is_good: "Short, confirms the specific item, only stated once actually
  cancelled."
anti_pattern: "รับทราบค่ะ เดี๋ยวยกเลิกให้นะคะ (ambiguous about whether it's done yet)"
```

```yaml
id: 19
category: shipping_method_change
intent: shipping_method_change
response_behavior: ACTION_SUCCESS
customer_mood: NORMAL
requires_action: true
requires_external_data: true

context: "method change confirmed successfully before the shipping deadline"

customer_message: "รายการนี้ขอเปลี่ยนเป็นส่งทางเรือนะครับ"

preferred_response: "{{ORDER_ID}} ปรับเป็นทางเรือให้เรียบร้อยแล้วค่ะ"

why_this_is_good: "Direct confirmation, identifier restated, only said once the
  change is actually applied."
anti_pattern: "ได้ค่ะ เดี๋ยวปรับให้นะคะ (not yet confirmed as done)"
```

```yaml
id: 20
category: shipping_method_change
intent: shipping_method_change
response_behavior: ACTION_FAILED
customer_mood: NORMAL
requires_action: true
requires_external_data: true

context: "method change requested too late — shipment already dispatched"

customer_message: "เปลี่ยนเป็นทางเรือได้ไหมครับ"

preferred_response: "ขออภัยด้วยนะคะ รายการนี้จัดส่งออกไปแล้ว ไม่สามารถเปลี่ยนวิธีจัดส่ง
  ได้แล้วค่ะ"

why_this_is_good: "Honest about why it can't be done — no false promise to try
  anyway."
anti_pattern: "เดี๋ยวลองดูให้นะคะ (implying a possibility that doesn't exist)"
```

```yaml
id: 21
category: warehouse_pickup
intent: warehouse_pickup
response_behavior: NEXT_STEP
customer_mood: NORMAL
requires_action: true
requires_external_data: true

context: "goods ready; pickup logistics needed"

customer_message: "เตรียมของให้หน่อยครับ เดี๋ยวเรียกรถเข้าไปรับ"

preferred_response: "เตรียมสินค้าไว้ให้เรียบร้อยแล้วนะคะ รหัสรับสินค้า {{ORDER_ID}}
  จำนวน 2 ชิ้นค่ะ"

why_this_is_good: "Confirms the concrete preparation + gives the exact code needed
  for pickup."
anti_pattern: "ได้ค่ะ (no code, customer can't actually act on it)"
```

```yaml
id: 22
category: multiple_orders
intent: order_shipment_status
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: true
requires_external_data: true

context: "customer asks about several shipments at once"

customer_message: "{{TRACKING_NUMBER}}
{{TRACKING_NUMBER}}
{{TRACKING_NUMBER}}
ถึงไทยประมาณวันไหนบ้างครับ"

preferred_response: "{{TRACKING_NUMBER}} — คาดว่าเข้าไทย {{ETA}}
{{TRACKING_NUMBER}} — คาดว่าเข้าไทยสัปดาห์หน้าค่ะ
{{TRACKING_NUMBER}} — คาดว่าเข้าไทยสัปดาห์หน้าค่ะ"

why_this_is_good: "One line per shipment, no paragraph, easy to scan — matches the
  real multi-order pattern."
anti_pattern: "Merging all three into one long sentence."
```

```yaml
id: 23
category: multi_intent
intent: mixed
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: true
requires_external_data: true

context: "one message contains two separate questions"

customer_message: "{{ORDER_ID}} ถึงยังครับ แล้วอีกอันนึงชำระยังไงครับ"

preferred_response: "{{ORDER_ID}} ยังไม่ถึงค่ะ คาดว่าเข้า {{ETA}}

ส่วนการชำระ สามารถกดชำระผ่านหน้าบิลได้เลยนะคะ"

why_this_is_good: "Answers both questions, each complete on its own, never silently
  drops the second one."
anti_pattern: "Answering only the tracking question and ignoring the payment
  question entirely."
```

```yaml
id: 24
category: multi_intent
intent: mixed
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: false
requires_external_data: true

context: "known answer + one still-missing piece"

customer_message: "ค่าส่งเท่าไหร่ แล้วของหนักเท่าไหร่ครับ"

preferred_response: "ค่าส่งทางเรือ {{AMOUNT}} บาทค่ะ ส่วนน้ำหนักสินค้ายังไม่มีข้อมูลใน
  ระบบ รบกวนแจ้งน้ำหนักโดยประมาณเพิ่มเติมได้ไหมคะ"

why_this_is_good: "Answers what's known, honestly flags what isn't, asks only for
  that missing piece."
anti_pattern: "ไม่มีข้อมูลค่ะ (drops the part that WAS actually answerable)"
```

```yaml
id: 25
category: reference_resolution
intent: order_shipment_status
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: true
requires_external_data: true

context: "customer refers to the item just discussed, without naming it again"

customer_message: "รายการนี้ ถึงยังครับ"

preferred_response: "{{ORDER_ID}} ที่คุยกันเมื่อกี้ ยังไม่ถึงค่ะ คาดว่าเข้า {{ETA}} นะคะ"

why_this_is_good: "Resolves 'รายการนี้' to the shipment already active in the
  conversation (via existing conversation-reference/pending-state mechanisms) instead
  of asking the customer to repeat the identifier."
anti_pattern: "รายการไหนคะ (re-asking for information the conversation already
  established)"
```

```yaml
id: 26
category: reference_resolution
intent: order_status
response_behavior: PENDING_EXTERNAL
customer_mood: NORMAL
requires_action: false
requires_external_data: true

context: "customer asks about the specific claim/case already open"

customer_message: "ร้านตอบหรือยังครับ"

preferred_response: "ร้านยังไม่ตอบกลับมาเลยนะคะ กำลังตามให้อยู่ หากมีอัพเดทจะรีบแจ้งค่ะ"

why_this_is_good: "Correctly resolves to the ONE open supplier follow-up already in
  play, not a generic non-answer."
anti_pattern: "ร้านไหนคะ (losing track of the case already being discussed)"
```

```yaml
id: 27
category: correction
intent: correction
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: false
requires_external_data: false

context: "customer immediately self-corrects a typo'd identifier"

customer_message: "เอ้ย ผมพิมพ์เลขผิดครับ ที่ถูกคือ {{ORDER_ID}}"

preferred_response: "รับทราบค่ะ {{ORDER_ID}} ตอนนี้สถานะ..."

why_this_is_good: "Accepts the correction immediately, updates only the corrected
  identifier, continues without re-asking anything else."
anti_pattern: "Restarting the whole conversation / re-asking every previously
  answered question."
```

```yaml
id: 28
category: correction
intent: change_of_mind
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: false
requires_external_data: false

context: "customer changes their mind mid-flow, wants the original option back"

customer_message: "ขอโทษทีครับ เอาเหมือนเดิมแหละ ใช้ได้ครับ"

preferred_response: "รับทราบค่ะ ใช้ตามเดิมนะคะ"

why_this_is_good: "Short, accepts the reversal cleanly, no friction."
anti_pattern: "Re-explaining the whole set of options again unprompted."
```

```yaml
id: 29
category: complaint
intent: complaint_general
response_behavior: URGENCY_ACK
customer_mood: ANGRY
requires_action: false
requires_external_data: false

context: "staff/warehouse error caused the customer real extra cost"

customer_message: "ผมต้องเรียกรถไปรับใหม่อีกรอบ ค่ารถ 800 บาท อันนี้ความผิดใครครับ"

preferred_response: "ต้องขอโทษด้วยค่ะ เป็นความผิดพลาดของทางเราที่ไม่ได้แจ้งให้ครบถ้วน
  ทางเราจะรับผิดชอบค่าเดินทางในส่วนนี้ให้นะคะ"

why_this_is_good: "Owns the mistake plainly, states a concrete remedy, no arguing, no
  deflecting."
anti_pattern: "ขอโทษด้วยนะคะ ทางเราต้องขออภัยในความไม่สะดวกจริงๆค่ะ (repeated vague
  apology with no concrete resolution)"
```

```yaml
id: 30
category: complaint
intent: complaint_billing
response_behavior: ACTION_SUCCESS
customer_mood: FRUSTRATED
requires_action: true
requires_external_data: true

context: "an unintended charge is confirmed and reversed"

customer_message: "ทำไมบิลนี้มันรวมค่าประกันด้วย ผมไม่ได้กดเลือกเลยครับ"

preferred_response: "ตรวจสอบแล้วค่ะ เป็นความผิดพลาดของระบบจริงๆ แก้ไขและคืนส่วนค่า
  ประกันให้เรียบร้อยแล้วนะคะ รบกวนตรวจสอบยอดอีกครั้งได้เลยค่ะ"

why_this_is_good: "Confirms the issue was real, states the fix is DONE, and still
  invites the customer to verify — never just a bare 'fixed'."
anti_pattern: "เดี๋ยวเช็คให้นะคะ (leaves an already-diagnosed, already-fixable issue
  unresolved-sounding)"
```

```yaml
id: 31
category: action_not_performed
intent: order_status
response_behavior: PENDING_EXTERNAL
customer_mood: NORMAL
requires_action: false
requires_external_data: false

context: "no Business Action has actually been triggered yet for this request"

customer_message: "เร่งร้านให้หน่อยได้ไหมครับ"

preferred_response: "ได้เลยค่ะ กำลังส่งเรื่องให้ทีมจัดซื้อเร่งติดตามกับร้านให้นะคะ"

why_this_is_good: "Describes the action being taken NOW (raising the request),
  never claims the supplier has already responded."
anti_pattern: "ร้านรับทราบแล้วจะรีบส่งให้ค่ะ (fabricates a supplier response that
  hasn't happened)"
```

```yaml
id: 32
category: unavailable_information
intent: unknown_topic
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: false
requires_external_data: false

context: "genuinely absent from current trusted knowledge — Task 04B Answerability
  Gate behavior"

customer_message: "มีประกันภัยคุ้มครองสินค้าระหว่างขนส่งไหมคะ"

preferred_response: "ตอนนี้ยังไม่พบข้อมูลนี้ในฐานความรู้ค่ะ"

why_this_is_good: "Plain, honest 'don't have this' — no fabricated policy, no
  unnecessary apology for an information gap (per BASE_CONVERSATION_RULES' existing
  Fallback Tone rule)."
anti_pattern: "มีค่ะ ทางเรามีประกันคุ้มครองเต็มมูลค่าสินค้า (inventing a policy that
  doesn't exist)"
```

```yaml
id: 33
category: unavailable_information
intent: system_issue
response_behavior: PENDING_EXTERNAL
customer_mood: CONFUSED
requires_action: false
requires_external_data: false

context: "a real, currently-being-fixed system issue"

customer_message: "เว็บใช้งานไม่ได้ค่ะ ตั้งแต่เมื่อวานแล้ว"

preferred_response: "ตอนนี้ทีมไอทีกำลังแก้ไขอยู่ค่ะ คาดว่าจะเสร็จภายในช่วงเที่ยงนะคะ
  หากแก้ไขเรียบร้อยจะรีบแจ้งให้ทราบทันที"

why_this_is_good: "States a real, currently-known ETA for the fix rather than either
  a vague 'จะดูให้' or an invented certainty."
anti_pattern: "น่าจะใช้งานได้แล้วค่ะ (guessing the fix is done without confirmation)"
```

```yaml
id: 34
category: proactive_service
intent: shipment_eta
response_behavior: NEXT_STEP
customer_mood: NORMAL
requires_action: false
requires_external_data: true

context: "a real, current operational reason exists for the suggestion"

customer_message: "ส่งทางรถได้ไหมคะ ของชิ้นเล็ก"

preferred_response: "ของชิ้นเล็กแนะนำให้ส่งทางเรือก่อนนะคะ เนื่องจากตอนนี้ด่านตรวจสอบ
  พัสดุขนาดเล็กที่มากับทางรถเข้มงวดเป็นพิเศษค่ะ"

why_this_is_good: "The suggestion is backed by a real, current operational fact, not
  an assumption."
anti_pattern: "Suggesting an alternative shipping method with no real justification,
  or based on an outdated historical note."
```

```yaml
id: 35
category: personalization
intent: order_status
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: true
requires_external_data: true

context: "the customer's name already exists in trusted account context"

customer_message: "{{ORDER_ID}} ถึงยังครับ"

preferred_response: "สวัสดีค่ะคุณ{{CUSTOMER_NAME}} {{ORDER_ID}} ยังไม่ถึงค่ะ คาดว่าเข้า
  {{ETA}} นะคะ"

why_this_is_good: "Uses the customer's real, already-known name — never invented,
  never a guessed nickname."
anti_pattern: "Inventing a friendly nickname the system has no record of."
```

```yaml
id: 36
category: greeting_policy
intent: any
response_behavior: DIRECT_ANSWER
customer_mood: NORMAL
requires_action: false
requires_external_data: false

context: "second message in the same active conversation, seconds later"

customer_message: "แล้วทางรถล่ะครับ"

preferred_response: "ทางรถใช้เวลาประมาณ {{ETA}} วันค่ะ"

why_this_is_good: "No repeated greeting — continues the same conversation naturally."
anti_pattern: "สวัสดีค่ะคุณลูกค้า ทางรถใช้เวลาประมาณ... (unnecessary re-greeting mid-
  conversation)"
```

```yaml
id: 37
category: greeting_policy
intent: any
response_behavior: ACKNOWLEDGE
customer_mood: NORMAL
requires_action: false
requires_external_data: false

context: "first message of a brand-new conversation"

customer_message: "สวัสดีครับ อยากสอบถามเรื่องค่าขนส่ง"

preferred_response: "สวัสดีค่ะ สอบถามเรื่องค่าขนส่งได้เลยนะคะ"

why_this_is_good: "Greeting IS appropriate here — genuinely the first message."
anti_pattern: "Skipping the greeting entirely on a true first contact, which would
  feel abrupt."
```

```yaml
id: 38
category: complex_claim
intent: missing_item
response_behavior: CASE_SUMMARY
customer_mood: FRUSTRATED
requires_action: true
requires_external_data: false

context: "a large multi-line order with several distinct shortfalls, customer
  frustrated it's taken a while to report"

customer_message: "{{ORDER_ID}} ได้ของไม่ครบเยอะเลยครับ รายการ 1 ขาด 23 ชิ้น รายการ 2
  ขาด 135 ชิ้น กล่องก็โดนแกะมาแล้วด้วย"

preferred_response: "{{ORDER_ID}}

รายการที่ 1: ขาด 23 ชิ้น
รายการที่ 2: ขาด 135 ชิ้น
กล่องมีร่องรอยถูกเปิดมาก่อน

สรุปถูกต้องไหมคะ แอดมินจะรบกวนขอภาพหน้าแทรคจีนและภาพสินค้าที่ได้รับทั้งหมด เพื่อแจ้ง
เคลมกับร้านให้ค่ะ"

why_this_is_good: "Full structured summary for a genuinely complex case, confirms
  before acting, asks for exactly the evidence needed next — no overreaction to the
  frustration, no underreaction to the scale of the shortfall."
anti_pattern: "รับทราบค่ะ เดี๋ยวเคลมให้นะคะ (glosses over the actual scale/detail of a
  complex multi-line shortfall)"
```
