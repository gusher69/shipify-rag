# PHASE 6B — Customer "แก้ไขเคส Shipify Part 2" gap audit

**Acceptance source:** `แก้ไขเคส Shipify Part 2.docx` (9 pages, 5 screenshots — image1 T01–T05 transcript, image2 the *correct* SP/FT withdrawal answers, image3 the *current buggy* SP/FT run, image4 the *old* link-conversion failure, image5 the mm-not-converted calculator).
**Baseline deployed SHA:** `434fc07c6bc78fea3e650501ceeaacfcc919a115`
**Production behaviour below** captured by driving the real `DecisionEngine` + real registry + real KB retrieval on the deployed container (mocked ERP HTTP; no REAL LINE).

Classification vocabulary: `ALREADY_FIXED_AND_PROVEN` · `REPRODUCES_FAIL` · `PARTIAL` · `KB_CONTENT_GAP` · `RETRIEVAL_GAP` · `CORE_INTENT_CONTEXT_GAP` · `PUBLIC_VS_PRIVATE_ROUTING_GAP` · `CALCULATOR_UNIT_GAP` · `EXTERNAL_DEPENDENCY`.

---

## Page 1–4 — the four numbered items

### P2-1 — Calculator: mm dimensions not converted to cm — `CALCULATOR_UNIT_GAP`
- **Customer input:** `520mm x 220mm x 110mm ส่งทางเรือ หนัก 2 กิโลค่ะ`
- **Expected:** interpret as `52 cm × 22 cm × 11 cm` → sea ≈ `max(0.012584 CBM × 4500, 2 kg × 19)` ≈ **57 บาท**. (Chinese sellers give mm; Shipify computes in cm.)
- **Current production:** `ประเมินเบื้องต้นสำหรับทางเรือประมาณ 56628.00 บาทค่ะ โดยคิดจากปริมาตร 12.584 CBM` — `520 × 220 × 110` treated as **cm**. `300มม x 200มม x 100มม` → collected as `ขนาด 300x200x100 mm` (unit captured but never canonicalized before CBM).
- **Root cause family:** the estimate flow records `dim_unit` ("mm"/"cm"/"m") but the CBM computation and the reply formatter never fold mm→cm (÷10) or m→cm (×100). `_dimension_unit` recognises "มม"/"mm" but `EstimateState`/`_estimate_reply`/`_compute` use the raw numbers.
- **Fix required:** canonicalize every dimension to cm at capture time (mm ÷10, m ×100), keep the *displayed* unit as cm, protect explicit units / mixed Thai wording / corrections / follow-ups / new cycles. Generalized tests (not only 520×220×110).

### P2-2 — Link conversion (`qr.1688.com/s/…`, `m.1688.com/offer/<id>.html?…`) — `ALREADY_FIXED_AND_PROVEN`
- **Customer input:** `https://qr.1688.com/s/AcByukF7`, `https://m.1688.com/offer/860351622421.html?ptow=…`
- **Customer's own note in the doc:** "*แก้แล้ว".
- **Current production:** fixed in `FIX-LINK-CONVERSION-1688-REAL-URL` (commit `5d9fbee`) — `m.1688` normalizes to `detail.1688`, `qr.1688` is SSRF-guard resolved, a public guest CustCode is supplied so FastTrade no longer 400s. Deployed‑prod probe of all five URL families passes.
- **Fix required:** none. Protect it in the Part-2 regression (Conversation B).

### P2-3 — Shipping-withdrawal SP/FT per-brand answer — `REPRODUCES_FAIL`
- **Customer input:** `ถอนเงินขนส่งยังไงคะ` → bot asks `SP หรือ FT` → customer replies `SP1008`, then `FT1324`.
- **Expected (image2, verbatim customer source):**
  - ask the brand first ("ต้องให้เอไอถามกลับเรื่องโค้ดไปก่อนแล้วค่อยตอบให้ตรงตามโค้ด"), then
  - **SP:** "คุณลูกค้าสามารถกรอกข้อมูลรายละเอียดในแบบฟอร์มรูปภาพที่แอดมินส่งให้ และส่งเอกสารสำเนาบัตรประชาชนมาให้แอดมินได้เลยค่ะ"
  - **FT:** "คุณลูกค้าสามารถเข้าที่เมนู ประวัติการชำระเงินขนส่ง และขวามือจะมีปุ่ม ถอนเงินจากระบบ นะคะ สามารถกดถอนเข้ามาได้เลยค่ะ"
- **Current production:** brand ask fires correctly; then `SP1008` / `FT1324` → `RAG fresh_search` → "ตอนนี้ยังไม่มีข้อมูล(ยืนยัน)เกี่ยวกับ…".
- **Root cause family:** `withdrawal_flow._BRAND_ANSWER_RE` only matches a **bare** brand token (`^…(SP|FT)…$`). A CustCode-shaped reply (`SP1008`, `FT1324` — a brand prefix + digits, the natural way a customer answers "which brand / code?") never matches → falls through to RAG. The brand is never derived from the code the customer just typed.
- **Fix required:** on the pending SP‑or‑FT question, also accept a brand‑prefixed CustCode (`^(SP|FT)\d{2,}$`) and derive the brand from its prefix. Do **not** authorize anything with it (this only picks a PUBLIC KB answer); the verified‑binding brand still wins when present; `customer_channel_bindings` stays the auth source of truth. Once SP/FT is known, do not re‑ask on the next turn.

### P2-4 — Intent / context regression T01–T07 — `CORE_INTENT_CONTEXT_GAP` (T02/T05 `PARTIAL`)
Transcript (image1): `สวัสดีครับ` → `มีอะไรให้ช่วยไหมครับ?` → `ใช่ครับ` → **"ตอนนี้ยังไม่มีข้อมูลเพิ่มเติมในระบบค่ะ"**.

| # | Input | Expected | Current production | Class |
|---|---|---|---|---|
| T01 Context handling | `ใช่ครับ` (bare affirmation to "need help?") | continue the conversation; **not** a KB lookup | `RAG fresh_search` → KB‑not‑found reply | `CORE_INTENT_CONTEXT_GAP` |
| T02 Service inquiry | `มีบริการอะไรบ้าง` | describe services **+ ask a useful next step** | describes services (KB `1.0`, score 1.0) — no next step | `PARTIAL` |
| T03 Intent detection | `ต้องการนำเข้าเครื่องจักร` | detect Import Intent → discovery question | `RAG fresh_search` → KB‑not‑found (top chunk 0.30) | `CORE_INTENT_CONTEXT_GAP` |
| T04 Context continuity | services → นำเข้าเครื่องจักร | one customer journey | each message routed independently | `CORE_INTENT_CONTEXT_GAP` |
| T05 Intent switch | `งั้นโอนเงินให้ร้านที่จีน` | detect **Money‑Transfer** intent → its discovery/help flow | routed to `purchase_withdrawal_kb` → gives a *withdrawal* how‑to (wrong direction — customer wants to **send** money to a China shop) | `REPRODUCES_FAIL` (mis‑route) + `PARTIAL` |
| T06 Next best action | any service‑intent turn | ask the next needed detail | no follow‑up question after describing a service | `CORE_INTENT_CONTEXT_GAP` |
| T07 Fallback | KB has no machinery info | no hallucination **and** capture requirement / offer Human CS | flat "ไม่มีข้อมูล" dead‑end | `CORE_INTENT_CONTEXT_GAP` (fallback logic) |

**Root cause family (all of P2-4):** routing is `message → KB retrieve → (found ? answer : "no data")`. There is no **conversational / service‑intent layer before RAG**. `IMPORT_INTEREST` and `GENERAL` families already exist in `conversation_semantics` but a `HELP_INTENT` / `SERVICE_DISCOVERY` / `MONEY_TRANSFER_INTEREST` classification and a pre‑RAG "open the conversation / ask the discovery question" branch do not.

---

## Page 5 — "ต้องการความช่วยเหลือ" — `CORE_INTENT_CONTEXT_GAP`
- **Customer input:** `ต้องการความช่วยเหลือ` (also `ช่วยหน่อย`, `สอบถามหน่อยครับ`, `มีเรื่องอยากถาม`, `สนใจใช้บริการ`, `อยากนำเข้าสินค้า`).
- **Expected:** open the conversation and ask what help is needed — e.g. *"ได้ค่ะ ต้องการให้ช่วยเรื่องไหนคะ เช่น สั่งซื้อสินค้าจากจีน นำเข้าสินค้า โอนเงินให้ร้านค้าจีน เช็กค่าขนส่ง หรือสอบถามเรื่องอื่น แจ้งมาได้เลยค่ะ"*. Acceptance: **`KB_NOT_FOUND` ≠ conversation cannot continue.**
- **Current production:** `ต้องการความช่วยเหลือ` / `ช่วยหน่อย` / `สอบถามหน่อยครับ` → `RAG fresh_search` → KB‑not‑found reply.
- **Fix required:** the STEP‑4 pre‑RAG intent layer. `HELP_INTENT` → open + list service options as a question. Same layer covers T01/T03/T06/T07.

---

## Page 6–9 — the ฝากนำเข้า / ฝากสั่ง multi‑turn conversation (10 sub‑items)

### P2-B1 — broad "import/export" question — `RETRIEVAL_GAP`
- **Input:** `การนำเข้าส่งออกของค่ะ` → "ยังไม่มีข้อมูลยืนยัน"; the very next `ถ้าอยากนำเข้าสินค้าต้องทำยังไง` → answers the import steps in detail.
- **KB check:** `อยากนำเข้าสินค้าต้องทำยังไง` retrieves "ขั้นตอนการนำเข้าสินค้าจากจีนเข้าไทยทำอย่างไร" at 0.465 — **the chunk exists**; the broad wording just retrieves it below threshold.
- **Root cause family:** retrieval / query understanding for a broad topical phrasing; and (again) no service‑discovery layer to keep the journey alive when retrieval is weak.
- **Fix required:** `SERVICE_DISCOVERY` / `IMPORT_INTEREST` recognition of "การนำเข้าส่งออก" so the turn gets a discovery answer instead of "no data"; paraphrase coverage on the import‑steps chunk.

### P2-B2 — website‑link request misread as product‑link conversion + clarification LOOP — `URL intent disambiguation` + `CORE_INTENT_CONTEXT_GAP`
- **Input:** `ขอลิงก์เว็บ Taobao และ Tmall` → `ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ` (product‑conversion prompt). Customer clarifies `ไม่ค่ะ ขอลิงก์ที่จะเข้าไปดูของ` → **same reply**. Customer says `คุณไม่เข้าใจลูกค้า` → **same reply, 3rd time**.
- **Current production:** both the request and the clarification select `geturlproductdetail` and return the "ส่งลิงก์…แปลง" prompt.
- **Root cause family:** (a) no `WEBSITE_LINK_REQUEST` vs `PRODUCT_LINK_CONVERSION` split — "ลิงก์เว็บ Taobao/Tmall" = the *homepage / navigation URL*, not "convert my product URL"; (b) no `REJECT_PREVIOUS_ANSWER` / `CLARIFY_INTENT` conversation act → the same answer is resent instead of re‑evaluating; repeated‑answer loop not prevented.
- **Fix required:** STEP 7 (website vs conversion) + STEP 6 (rejection/clarification acts + loop guard).

### P2-B3 — Taobao URL after the "website" context → generic error — `URL routing` / `CORE_INTENT_CONTEXT_GAP`
- **Input:** a Taobao product URL sent while the customer is asking about *websites*.
- **Current production:** `geturlproductdetail` executes; historically returned "ไม่สามารถดำเนินการได้" (the CustCode‑400 cause — now fixed by `5d9fbee`, so it would now *convert*). But per the conversation context the customer wanted the *website*, not a conversion.
- **Fix required:** once `WEBSITE_LINK_REQUEST` context is active, a bare platform URL is treated as "here is the site I mean" — answer with the site info, do not force a conversion. Protect the standalone product‑conversion path (no context) — must not regress.

### P2-B4 — "how does Shipify know whose parcel it is" — `KB_CONTENT_GAP`
- **Input:** `ร้านส่งของไปคลังจีน แล้วจะรู้ได้ยังไงว่าเป็นลูกค้าคนไหน`
- **Current production:** answers "ไปดูที่อยู่โกดังจีนจากหน้าเว็บ" (a nearby FAQ) — does not answer the identification mechanism.
- **KB check:** best chunk 0.353 = "ขั้นตอนฝากนำเข้า…". **No chunk about customer‑code / marking / shipping‑address format / tracking identification.**
- **Fix required:** honest "ยังไม่มีข้อมูลยืนยันในระบบ" + Human‑CS next step. **Do not** answer the nearby warehouse‑address FAQ. (KB content is a customer‑authoring task — flagged, not invented.)

### P2-B5 — "do I need to notify before sending to the warehouse" — `RETRIEVAL_GAP` (wrong FAQ) + `KB_CONTENT_GAP`
- **Input:** `ต้องแจ้งอะไรไหมว่าจะมีของไปส่งที่คลัง`
- **Current production:** answers "ไม่มีจำนวนขั้นต่ำ" (MOQ — a completely different intent).
- **KB check:** best 0.453 = "ขอที่อยู่โกดังจีน". **No pre‑arrival‑notification chunk.**
- **Fix required:** must not answer the MOQ FAQ. Honest "no confirmed info" + Human‑CS. Flag the KB gap.

### P2-B6 — "will you contact me when it arrives" — `KB_CONTENT_GAP`
- **Input:** `ของถึงแล้วจะติดต่อกลับไหม`
- **Current production:** routes to the **`SendLineNotiCS` confirmation gate** ("ยืนยันการดำเนินการ 'SendLineNotiCS แจ้งเตือนเข้า LINE OA'…") — *worse* than the doc's observed "ไม่มีข้อมูลยืนยัน": it is trying to fire a notification action.
- **KB check:** best 0.429 = "ขอที่อยู่โกดังหน่อย". **No arrival‑notification chunk.**
- **Fix required:** must not select `SendLineNotiCS`. Honest "no confirmed info" + Human‑CS. Flag the KB gap.

### P2-B7 — "which contact channels" grounding — `RETRIEVAL_GAP`
- **Input:** `ติดต่อช่องทางไหน`
- **KB check:** best chunk 0.505 = **"ติดต่อ Shipify ช่องทางไหนได้บ้าง / LINE @Shipify, ฝ่ายบริการลูกค้า 02‑026‑64…"** — the content IS in the KB; 0.505 is around/below the answerability threshold for this exact phrasing.
- **Current production:** `RAG fresh_search` → (mock shows KB‑miss; real score 0.505 borderline).
- **Fix required:** paraphrase coverage on the contact chunk so `ติดต่อช่องทางไหน` / `ติดต่อทางไหนคะ` retrieve it confidently; ensure it is PUBLIC (no CustCode).

### P2-B8 — `ขออีเมล และเว็บไซต์` → asks `กรุณาแจ้งรหัสลูกค้าค่ะ` — `PUBLIC_VS_PRIVATE_ROUTING_GAP` (SEVERE)
- **Input:** `ขออีเมล และเว็บไซต์`
- **Current production:** `WORKFLOW fresh_search` → **"กรุณาแจ้งรหัสลูกค้าค่ะ"** — a public contact request pulled into a CustCode‑collection flow (a Business Action whose keyword/param search matched "อีเมล").
- **Root cause family:** the Business‑Action candidate search matched an identity‑gated action (likely `getdatacustomer`, which has a `CustEmail` param) on the token "อีเมล", and no public/private gate stopped it because the turn was not recognised as PUBLIC company info.
- **Fix required:** classify a contact/email/website/address request as PUBLIC company info (`conversation_semantics` PUBLIC_INFO family / a contact‑request marker) → keep identity‑gated actions out of the candidate pool → route to the contact KB chunk. Never ask CustCode / phone / email verification for it.

### P2-B9 — customer supplies `123456`, system tries to verify — consequence of B8 — `PUBLIC_VS_PRIVATE_ROUTING_GAP`
- **Input:** `123456` (after the bogus CustCode ask).
- **Current production:** `getdatacustomer` executes → authorization denied → "ติดต่อเจ้าหน้าที่เพื่อยืนยันตัวตน".
- **Fix required:** disappears once B8 no longer asks for a code. Additionally: a lone `123456` right after a *public* turn must not start a verification flow.

### P2-B10 — `ติดต่อทางไหนคะ` re‑asks CustCode → LOOP — `state retention` + `loop`
- **Input:** `ติดต่อทางไหนคะ` (after `123456`).
- **Current production:** asks รหัสลูกค้า again → loop.
- **Fix required:** covered by B7 (retrieve the contact chunk) + B8 (never route contact to auth) + STEP 6 loop guard + STEP 10 (don't re‑ask an already‑answered slot).

---

## Summary matrix

| Item | Class | Fix owner |
|---|---|---|
| P2-1 mm→cm | CALCULATOR_UNIT_GAP | `services/shipping_estimate_flow.py` — canonicalize dims to cm |
| P2-2 link conversion | ALREADY_FIXED_AND_PROVEN | — (regression only) |
| P2-3 SP/FT withdrawal | REPRODUCES_FAIL | `services/withdrawal_flow.py` — accept a brand‑prefixed CustCode reply |
| P2-4 T01/T03/T04/T06/T07 + Page 5 help | CORE_INTENT_CONTEXT_GAP | `services/conversation_semantics.py` + `services/decision_engine.py` — pre‑RAG service‑intent layer |
| P2-4 T02/T05 | PARTIAL | same layer — next‑best‑action question; money‑transfer recognition |
| P2-B1 broad import | RETRIEVAL_GAP + discovery | same layer + `SERVICE_DISCOVERY` |
| P2-B2 website vs conversion + loop | URL disambiguation + CORE_INTENT | `link_conversion_flow` + rejection/clarification acts |
| P2-B3 URL in website context | URL routing | link routing respects `WEBSITE_LINK_REQUEST` context |
| P2-B4 whose‑parcel | KB_CONTENT_GAP | honest fallback; flag KB gap |
| P2-B5 pre‑arrival notify | RETRIEVAL_GAP + KB_CONTENT_GAP | honest fallback (no MOQ FAQ); flag KB gap |
| P2-B6 arrival contact | KB_CONTENT_GAP | honest fallback (no SendLineNotiCS); flag KB gap |
| P2-B7 contact channels | RETRIEVAL_GAP | paraphrase coverage on the contact chunk |
| P2-B8 email/website → CustCode | PUBLIC_VS_PRIVATE_ROUTING_GAP (SEVERE) | PUBLIC contact family → no identity‑gated actions |
| P2-B9 `123456` verify | PUBLIC_VS_PRIVATE_ROUTING_GAP | resolved by B8 |
| P2-B10 contact re‑ask loop | state / loop | resolved by B7 + B8 + loop guard |

**KB content gaps to hand back to the customer (not inventable):** whose‑parcel identification mechanism (customer code / marking / warehouse‑address format), pre‑arrival warehouse‑notification requirement, arrival‑notification / contact‑back policy.

**External dependencies:** none new. (The `EXTERNAL_API_DEPENDENCY` set from Phase 2 is unchanged.)

---

## Resolution (PHASE‑6B implementation)

| Item | Fix landed | Where |
|---|---|---|
| P2-1 mm→cm | mm/cm/m unit regexes fixed (`\bmm\b` needed a boundary a digit doesn't give); every dimension canonicalised to **cm** at capture (`mm ÷10`, `m ×100`, `inch ×2.54`) before the CBM formula, prompt, corrections and new cycles | `rag/slot_filling_flow.py`, `services/shipping_estimate_flow.py` |
| P2-2 link conversion | already fixed (`5d9fbee`); protected by `TestConversationB.test_real_product_url_still_converts` / `test_explicit_convert_verb_still_conversion` | — |
| P2-3 SP/FT withdrawal | `_BRAND_ANSWER_RE` now accepts a brand‑PREFIXED CustCode reply (`SP1008`, `FT1324`); brand derived from the prefix, authorises nothing | `services/withdrawal_flow.py` |
| P2-4 T01 | bare affirmation after an assistant help‑offer → `HELP_INTENT` (pre‑RAG), never a KB lookup | `service_intent_flow.is_help_affirmation` + DE branch |
| P2-4 T02 | `SERVICE_DISCOVERY` → describe + Next‑Best‑Action question | `conversation_semantics`, `service_intent_flow.SERVICE_DISCOVERY_REPLY` |
| P2-4 T03/T04/T07 | circular‑import bug fixed (deterministic `IMPORT_INTEREST` recognition was dead in production); fresh `IMPORT_INTEREST` answered pre‑RAG with a discovery ack that re‑states the frame | `conversation_semantics._import_recognizers`, DE branch |
| P2-4 T05 | `งั้นโอนเงินให้ร้านที่จีน` → `MONEY_TRANSFER_INTEREST` (deterministic, before the withdrawal block) → money‑transfer discovery, NOT a withdrawal how‑to | `conversation_semantics._MONEY_TRANSFER_RE`, `service_intent_flow` |
| P2-4 T06 | every service‑intent reply ends with the next‑useful question | `service_intent_flow` replies |
| Page‑5 help | `HELP_INTENT` → the doc's verbatim "ได้ค่ะ ต้องการให้ช่วยเรื่องไหนคะ …" | `service_intent_flow.HELP_REPLY` |
| P2-B1 broad import | `SERVICE_DISCOVERY` catches "การนำเข้าส่งออก" → discovery reply, not "no data" | `conversation_semantics._SERVICE_DISCOVERY_RE` |
| P2-B2 website vs conversion + loop | `WEBSITE_LINK_REQUEST` split (checked before the link‑conversion signal; guarded by "no real URL / no แปลงลิงก์ verb"); a pending "send the link" prompt no longer swallows a website request or a REJECT; `conversation_act = REJECT` + repeated‑answer guard re‑evaluate instead of resending | `conversation_semantics`, `service_intent_flow`, DE link‑pending guard + REJECT branch |
| P2-B3 URL in website context | the CustCode‑400 error is already fixed (`5d9fbee`), so a platform URL now converts instead of erroring; a pending website turn no longer forces the conversion | DE `_link_was_pending` guard |
| P2-B4/B5/B6 warehouse journey | `WAREHOUSE_INBOUND_JOURNEY` family → honest "ยังไม่มีข้อมูลยืนยันในระบบ … ติดต่อเจ้าหน้าที่" + `HUMAN_HANDOFF`; never the warehouse‑address FAQ, never MOQ, never `SendLineNotiCS` | `conversation_semantics`, `service_intent_flow.WAREHOUSE_INBOUND_FALLBACK`, DE branch |
| P2-B7 contact channels | `CONTACT_INFO` → deterministic KB fetch of the contact chunk (direct content lookup, not vector similarity) with a chunk‑sourced fallback | `service_intent_flow.fetch_contact_kb_answer` |
| P2-B8 email/website → CustCode | `CONTACT_INFO` is a `PUBLIC_INFO` family → pre‑RAG branch answers it before any Business‑Action search; identity‑gated actions never enter the pool | `conversation_semantics.PUBLIC_INFO_FAMILIES`, DE branch |
| P2-B9 `123456` verify | consequence of B8 — gone; a public turn now never starts a verification flow | — |
| P2-B10 contact re‑ask loop | `CONTACT_INFO` / `REJECT` now break a pending identity‑gated collection (`_current_intent_breaks_pending_flow`) | `services/decision_engine.py` |

**Still handed back to the customer as KB‑authoring tasks** (flagged, not invented): whose‑parcel identification mechanism, pre‑arrival warehouse‑notification requirement, arrival contact‑back policy. Until those chunks exist the bot gives the honest fallback + Human CS.

**Phrase‑specific customer patches: 0.** Every fix is a compositional family / dialogue‑act / unit‑canonicalisation change with paraphrase coverage in `tests/test_customer_part2_conversations.py`.
