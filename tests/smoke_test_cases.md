# Production Smoke Test Cases — AI Engine v1.0.1

Documentation only — no automation implemented here. These are manual (or future-automatable) checks to run against `/admin/preview` (AI Playground) or the live LINE OA channel before/after any deployment.

---

### SM-01 — Greeting
- **Question**: "สวัสดีค่ะ"
- **Expected Behaviour**: Friendly greeting response, no retrieval needed, no hallucinated claim about services.
- **Expected Citation**: None required.
- **Expected Result**: PASS if the reply is a natural greeting with no fabricated facts.

### SM-02 — FAQ (simple)
- **Question**: "มีขั้นต่ำในการสั่งไหม"
- **Expected Behaviour**: Answers directly from the FAQ row about minimum order quantity.
- **Expected Citation**: `AI Knowledge Master.xlsx`.
- **Expected Result**: PASS if the answer matches the FAQ content and cites the source.

### SM-03 — RAG (multi-fact)
- **Question**: "ค่าขนส่งคิดยังไง"
- **Expected Behaviour**: Explains cost is calculated from weight/volume (CBM), includes sea/land rates.
- **Expected Citation**: `AI Knowledge Master.xlsx`.
- **Expected Result**: PASS if all rate figures match the source exactly (no invented numbers).

### SM-04 — Retrieval (Thai warehouse)
- **Question**: "ขอที่อยู่โกดังไทย"
- **Expected Behaviour**: Deterministic clarification if location is ambiguous, else returns the correct Thai warehouse address.
- **Expected Citation**: `AI Knowledge Master.xlsx`.
- **Expected Result**: PASS if address matches source and no address is invented.

### SM-05 — Shipping rate
- **Question**: "เรทเท่าไหร่คะ"
- **Expected Behaviour**: Returns import/deposit rate (5.11 บาท/หยวน), land rate (35 บาท/kg), sea rate (19 บาท/kg).
- **Expected Citation**: `AI Knowledge Master.xlsx`.
- **Expected Result**: PASS if all three rates are correct and cited.

### SM-06 — Tracking status
- **Question**: "เช็คสถานะพัสดุได้ที่ไหน"
- **Expected Behaviour**: Explains tracking channel/steps if in KB, otherwise correctly abstains (no_information).
- **Expected Citation**: `AI Knowledge Master.xlsx` if present, else none.
- **Expected Result**: PASS if the system never invents a tracking number or status.

### SM-07 — Warehouse (China)
- **Question**: "ขอที่อยู่โกดังจีน"
- **Expected Behaviour**: Directs the customer to the website's China warehouse address menu, matching current KB content (no fabricated city name).
- **Expected Citation**: `AI Knowledge Master.xlsx`.
- **Expected Result**: PASS if the answer matches the real FAQ row exactly.

### SM-08 — OCR placeholder
- **Question**: (customer sends an image of a shipping label)
- **Expected Behaviour**: OCR is not yet implemented — system should gracefully state it cannot read images, or escalate to human handoff. Must NOT hallucinate label contents.
- **Expected Citation**: None.
- **Expected Result**: PASS if no fabricated text is returned for the image; FAIL if any invented label content appears. (Placeholder — OCR is a future LINE OA integration phase.)

### SM-09 — Vision placeholder
- **Question**: (customer sends a photo asking "นี่คือกล่องแบบไหน")
- **Expected Behaviour**: Vision is not yet implemented — system should decline gracefully or escalate. Must NOT guess the box type.
- **Expected Citation**: None.
- **Expected Result**: PASS if no fabricated visual description appears. (Placeholder — Vision is a future LINE OA integration phase.)

### SM-10 — Conversation (topic continuity)
- **Question sequence**: "โกดังจีน" → "มีแผนที่ไหม" → "ขอเบอร์" → "กี่โมงเปิด"
- **Expected Behaviour**: Topic stays `warehouse` across all 4 turns; subtopic changes map→contact→hours correctly.
- **Expected Citation**: `AI Knowledge Master.xlsx` per turn.
- **Expected Result**: PASS if conversation state never drifts to an unrelated topic.

### SM-11 — Multi-turn (entity replacement)
- **Question sequence**: "โกดังไทย" → "แล้วจีนล่ะ"
- **Expected Behaviour**: Location correctly switches ไทย → จีน; previous location is not incorrectly retained.
- **Expected Citation**: `AI Knowledge Master.xlsx`.
- **Expected Result**: PASS if the second answer is about China, not Thailand.

### SM-12 — No-information (correct abstention)
- **Question**: "What Python version is required for Google Drive?"
- **Expected Behaviour**: Correctly states no information is available; must not invent a version number.
- **Expected Citation**: None (correct abstention has nothing to cite).
- **Expected Result**: PASS if the system abstains cleanly (this is the exact case the Grounding fix in v1.0.1 addressed).

### SM-13 — Escalation
- **Question**: "ขอคุยกับเจ้าหน้าที่หน่อย"
- **Expected Behaviour**: Human-handoff acknowledgement triggered via Policy Engine, no attempt to answer as if it were a knowledge question.
- **Expected Citation**: None.
- **Expected Result**: PASS if escalation message is returned and flagged.

### SM-14 — Attachments
- **Question**: "ขอรูปตัวอย่างการแพ็คสินค้า"
- **Expected Behaviour**: Attachment Planner selects a real attachment from retrieved evidence only, never an arbitrary or unrelated file.
- **Expected Citation**: Matches the attachment's source chunk.
- **Expected Result**: PASS if no attachment is sent when none exists in the KB for this query; FAIL if an unrelated file is attached.

### SM-15 — Citation correctness
- **Question**: "What is the shipping cost?"
- **Expected Behaviour**: Answer is grounded in the shipping-cost FAQ row.
- **Expected Citation**: `AI Knowledge Master.xlsx` — must NOT be empty (this is the exact case the Citation Attribution fix in v1.0.1 addressed).
- **Expected Result**: PASS if a citation is present and matches the retrieved evidence.

### SM-16 — Policy enforcement
- **Question**: "ขอส่วนลดเพิ่มได้ไหม ผมไม่พอใจมาก"
- **Expected Behaviour**: Dissatisfaction-keyword policy triggers appropriately (e.g. escalation note), answer still stays within Standard Policy rules.
- **Expected Citation**: As applicable.
- **Expected Result**: PASS if policy verdict is logged and behavior matches configured policy rules.

### SM-17 — Prompt template behaviour
- **Question**: "ขอบคุณค่ะ"
- **Expected Behaviour**: Response tone matches the active default prompt template ("Production Default Prompt (v1.0)") — concise, warm, welcoming.
- **Expected Citation**: None required.
- **Expected Result**: PASS if tone is consistent with the configured template.

### SM-18 — Confidence / answerability
- **Question**: "มีบริการอะไรบ้าง"
- **Expected Behaviour**: `direct_answer` answerability with high retrieval confidence, citing the services FAQ row.
- **Expected Citation**: `AI Knowledge Master.xlsx`.
- **Expected Result**: PASS if confidence/answerability is reported as `direct_answer`, not `partial_answer`/`no_information`.

### SM-19 — Hallucination prevention
- **Question**: "ค่าธรรมเนียมบัตรเครดิตกี่เปอร์เซ็นต์"
- **Expected Behaviour**: Returns the exact real figure (3%) with the real minimum (500 บาท/บิล) — never a different, invented number.
- **Expected Citation**: `AI Knowledge Master.xlsx`.
- **Expected Result**: PASS only if the number matches source exactly; any deviation is a hallucination FAIL.

### SM-20 — Human handoff continuity
- **Question sequence**: "ขอคุยกับเจ้าหน้าที่" → (agent takes over) → customer sends a follow-up
- **Expected Behaviour**: System does not attempt to auto-answer once escalation is active (per Policy Engine rules); conversation history remains intact for the human agent.
- **Expected Citation**: None (human-handled turn).
- **Expected Result**: PASS if the AI does not re-insert itself into an escalated conversation inappropriately.

---

**Note**: These cases are documentation only, per this task's scope — no test automation was implemented. They are intended as the baseline smoke-test checklist for every future deployment, and as the starting point for a future automated smoke-test suite if one is built.
