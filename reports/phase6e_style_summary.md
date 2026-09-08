# PHASE-6E — Response Style Lab

- generated: 2026-09-08T17:44:59  (mode=live, 445.0s)

- STYLE CASES: 103  ·  PASS: 79  ·  FAIL: 24  ·  rate: 76.7%

## Hard acceptance counters

- INTERNAL TERMINOLOGY LEAK: 0
- FAKE ACTION / HANDOFF: 0
- REPEATED KNOWN SLOT: 0
- PRIVATE DATA LEAK: 0

## Axis pass rates

- CONCISENESS: 84/103
- CONTEXT_CONTINUITY: 103/103
- KEEP_OPEN: 8/10
- NATURALNESS: 103/103
- NEXT_ACTION_QUALITY: 96/103
- NO_INTERNAL_LANGUAGE: 103/103
- NO_PRIVATE_LEAK: 3/3
- NO_REPEATED_SLOT: 103/103
- POLITE_GATE: 3/3
- TRUTH_PRESERVED: 103/103

## Non-passing cases

- `help-1` [greeting_help] FAIL_SOFT — len=149 sents=7
  reply: 'ได้ค่ะ ต้องการให้ช่วยเรื่องไหนคะ เช่น สั่งซื้อสินค้าจากจีน นำเข้าสินค้า โอนเงินให้ร้านค้าจีน เช็กค่าขนส่ง หรือต้องการสอบถามเรื่องอื่น แจ้งมาได้เลยค่ะ'
- `help-2` [greeting_help] FAIL_SOFT — len=149 sents=7
  reply: 'ได้ค่ะ ต้องการให้ช่วยเรื่องไหนคะ เช่น สั่งซื้อสินค้าจากจีน นำเข้าสินค้า โอนเงินให้ร้านค้าจีน เช็กค่าขนส่ง หรือต้องการสอบถามเรื่องอื่น แจ้งมาได้เลยค่ะ'
- `help-affirm` [greeting_help] FAIL_SOFT — len=149 sents=7
  reply: 'ได้ค่ะ ต้องการให้ช่วยเรื่องไหนคะ เช่น สั่งซื้อสินค้าจากจีน นำเข้าสินค้า โอนเงินให้ร้านค้าจีน เช็กค่าขนส่ง หรือต้องการสอบถามเรื่องอื่น แจ้งมาได้เลยค่ะ'
- `disc-1` [service_discovery] FAIL_SOFT — len=287 sents=7; no next question where one was expected
  reply: 'Shipify เป็นผู้ช่วยสั่งซื้อและนำเข้าสินค้าจากจีนแบบครบวงจรค่ะ มีบริการฝากสั่งซื้อสินค้าจากจีน ฝากนำเข้า ฝากโอนเงินให้ร้านค้าจีน ช่วยประสานงานกับร้าน/โรงงานจีน แ'
- `calc-slot-method-left` [calculator] FAIL_SOFT — no next question where one was expected
  reply: 'รับทราบค่ะ (น้ำหนัก 5 กก. • ขนาด 40x30x20 ซม.) ต้องการประเมินทางรถหรือทางเรือคะ'
- `ga-1` [general_assistance] FAIL_SOFT — len=566 sents=11
  reply: 'การแพ็กของที่แตกง่ายให้ปลอดภัยมีขั้นตอนดังนี้ค่ะ:\n\n1. **ใช้วัสดุกันกระแทก**: เช่น บับเบิ้ลกันกระแทก หรือโฟม เพื่อห่อหุ้มสินค้าทั้งหมด\n2. **กล่องที่เหมาะสม**: เล'
- `ga-2` [general_assistance] FAIL_SOFT — len=671 sents=8
  reply: 'การห่อจานเซรามิกเพื่อส่งไกลๆ ควรให้ความสำคัญกับการป้องกันการกระแทกและการแตกหัก โดยทั่วไปแล้วสามารถทำได้ดังนี้:\n\n1. **ห่อด้วยบับเบิ้ล**: ใช้แผ่นบับเบิ้ลห่อรอบจาน'
- `ga-3` [general_assistance] FAIL_SOFT — len=572 sents=10
  reply: 'โดยทั่วไปแล้ว การแพ็กของบอบบางเพื่อป้องกันการกระแทกสามารถทำได้ดังนี้ค่ะ:\n\n1. **ใช้วัสดุกันกระแทก**: เช่น บับเบิ้ลแรป โฟม หรือแผ่นกันกระแทก เพื่อห่อหุ้มสินค้าก่อ'
- `topic-switch` [topic_switch] FAIL_SOFT — len=232 sents=7
  reply: 'ค่าตีลังไม้จะอยู่ที่ 2,000 บาทต่อ 1 คิวบิกเมตร (CBM) โดยมีขั้นต่ำที่ 150 บาทค่ะ ตัวอย่างเช่น สินค้าที่มีขนาด 0.5 คิว ค่าตีลังไม้จะอยู่ที่ 1,000 บาท แนะนำให้ตรวจ'
- `link-1` [link_conversion] FAIL_SOFT — no next question where one was expected
  reply: 'ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ'
- `ga-more-p0` [general_assistance] FAIL_SOFT — len=744 sents=10
  reply: 'การแพ็กของมีคมให้ปลอดภัยและป้องกันความเสียหายสามารถทำได้ตามขั้นตอนเหล่านี้ค่ะ:\n\n1. **ห่อหุ้มของมีคม**: ใช้วัสดุที่แข็งแรง เช่น กระดาษแข็ง หรือพลาสติกแข็ง ห่อหุ้'
- `ga-more-p6` [general_assistance] FAIL_SOFT — len=658 sents=9
  reply: 'การพันผ้าเป็นม้วนเพื่อป้องกันการกระแทกสามารถทำได้ตามขั้นตอนดังนี้ค่ะ:\n\n1. **เลือกผ้าที่เหมาะสม**: ใช้ผ้าที่มีความหนาและนุ่ม เช่น ผ้าห่มหรือผ้าขนหนู เพื่อช่วยลดแ'
- `ga-more-p7` [general_assistance] FAIL_SOFT — len=543 sents=8
  reply: 'โดยทั่วไปแล้ว การส่งของเหลวมีข้อจำกัดบางอย่างที่ต้องพิจารณา เช่น\n\n1. **ประเภทของเหลว**: ของเหลวบางประเภท เช่น สารเคมีอันตรายหรือของเหลวไวไฟ อาจถูกห้ามส่งหรือจำเ'
- `nofact-more-p1` [business_unavailable] FAIL_SOFT — KB miss dead-ended without keeping the conversation open
  reply: 'ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ'
- `disc-more-p0` [service_discovery] FAIL_SOFT — len=932 sents=14; no next question where one was expected
  reply: 'หากคุณต้องการฝากนำเข้าเสื้อผ้ามาขาย สามารถทำตามขั้นตอนเหล่านี้ได้ค่ะ:\n\n1. **สั่งซื้อสินค้า**: คุณสามารถสั่งซื้อเสื้อผ้าจากร้านจีนที่คุณต้องการได้ด้วยตัวเอง\n2. *'
- `disc-more-p1` [service_discovery] FAIL_SOFT — no next question where one was expected
  reply: 'ใช่ค่ะ เรามีบริการจัดส่งสินค้าจากจีนมาไทย โดยลูกค้าสามารถเลือกใช้บริการฝากนำเข้าหรือฝากสั่งกับ Shipify ได้ตามความต้องการค่ะ'
- `disc-more-p2` [service_discovery] FAIL_SOFT — len=287 sents=7; no next question where one was expected
  reply: 'Shipify เป็นผู้ช่วยสั่งซื้อและนำเข้าสินค้าจากจีนแบบครบวงจรค่ะ มีบริการฝากสั่งซื้อสินค้าจากจีน ฝากนำเข้า ฝากโอนเงินให้ร้านค้าจีน ช่วยประสานงานกับร้าน/โรงงานจีน แ'
- `disc-more-p3` [service_discovery] FAIL_SOFT — len=689 sents=18; no next question where one was expected
  reply: 'หากคุณต้องการเริ่มนำเข้าสินค้าจากจีน สามารถทำตามขั้นตอนหลักดังนี้ค่ะ:\n\n1. สั่งสินค้ากับร้านจีนด้วยตัวเอง\n2. คัดลอกที่อยู่โกดังจีนจากหน้าเว็บ Shipify และส่งให้ร้'
- `greet-more-p2` [greeting_help] FAIL_SOFT — len=149 sents=7
  reply: 'ได้ค่ะ ต้องการให้ช่วยเรื่องไหนคะ เช่น สั่งซื้อสินค้าจากจีน นำเข้าสินค้า โอนเงินให้ร้านค้าจีน เช็กค่าขนส่ง หรือต้องการสอบถามเรื่องอื่น แจ้งมาได้เลยค่ะ'
- `ga-more2-p0` [general_assistance] FAIL_SOFT — len=677 sents=9
  reply: 'การส่งรูปภาพที่มีกรอบเพื่อป้องกันไม่ให้แตก ควรทำตามขั้นตอนดังนี้:\n\n1. **ห่อหุ้มรูปภาพ**: ใช้กระดาษฟองอากาศหรือวัสดุกันกระแทกห่อหุ้มรูปภาพให้ทั่วถึง เพื่อป้องกัน'
- `ga-more2-p2` [general_assistance] FAIL_SOFT — len=717 sents=10
  reply: 'การแพ็กของให้ประหยัดพื้นที่สามารถทำได้ด้วยวิธีต่อไปนี้ค่ะ:\n\n1. **เลือกกล่องขนาดพอดี**: ใช้กล่องที่มีขนาดพอดีกับสินค้าที่จะส่ง เพื่อไม่ให้มีพื้นที่ว่างมากเกินไป\n'
- `ga-more2-p4` [general_assistance] FAIL_SOFT — len=485 sents=8
  reply: 'โดยทั่วไปแล้ว การส่งอาหารแห้งไปต่างประเทศอาจมีข้อจำกัดและกฎระเบียบที่แตกต่างกันไปในแต่ละประเทศค่ะ บางประเทศอาจมีข้อห้ามหรือข้อกำหนดเฉพาะเกี่ยวกับการนำเข้าอาหารแ'
- `ga-more2-p5` [general_assistance] FAIL_SOFT — len=684 sents=12
  reply: 'การแพ็กของมือสองให้ดูเรียบร้อยและน่าสนใจสามารถทำได้ตามขั้นตอนต่อไปนี้ค่ะ:\n\n1. **ทำความสะอาดสินค้า**: ตรวจสอบและทำความสะอาดสินค้าก่อนแพ็ก เพื่อให้สินค้าดูใหม่และ'
- `nofact-more2-p1` [business_unavailable] FAIL_SOFT — KB miss dead-ended without keeping the conversation open
  reply: 'ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ'