# SYSTEM-STATE-EMERGENCY-1 — central state / route authority

Production baseline: `7859b7f`. Exact real session: `6c9b9026-434e-403d-b513-e2c90752754b`
(verified tester `Uc5f5717bc090934f9eaa067513388178`, CustCode FT3182), turns **t780–t809**
(2026-09-04 ~19:00–19:03 ICT). Runtime SHA at capture: `7859b7f` (host `git rev-parse HEAD`).
The `line-webhook` container had `decision_engine.py` / `shipping_estimate_flow.py`
TEMP-MODIFIED by an earlier `docker compose cp` (CALCULATOR-REGRESSION-2 verification);
restored to the `7859b7f` image with `docker compose up -d --force-recreate line-webhook`
before any fresh verification.

## Trace of the failing session (from `ai_session_messages` / no dev-trace persisted for LINE)

| t | user | bot (baseline `7859b7f`) | should be |
|---|---|---|---|
| 780 | ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43 | **charter slot prompt** | calculator |
| 782 | ช่วยคิดค่าส่งใหม่ น้ำหนัก 5 โล | charter slot prompt | new calculator episode |
| 784 | ร้านส่งหรือยังคะ | charter slot prompt | ask for order id |
| 786 | ใบกำกับค่าสินค้าออกได้ไหม | charter slot prompt | invoice policy |
| 788 | ผมมีคูปองอะไรบ้าง | charter slot prompt | MY_COUPONS |
| 790 | ไปรับของแถวไหนครับ | warehouse clarification | (ok) |
| 792 | กล่องพลาสติกนำเข้าได้ไหมครับ | `รับทราบค่ะ เป็นกล่องพลาสติกนำเข้าได้ไหมครับนะคะ 😊` + road/sea | product-policy verdict |
| 794 | มีบริการเหมารถไหมคะ | TC19 FAQ + ask | (ok) |
| 796 | ร้านส่งหรือยังคะ | charter slot prompt | ask for order id |
| 798 | คูปองใช้ยังไงครับ | charter slot prompt | coupon-usage RAG |
| 802 | ทางเรือกี่วันครับ | charter slot prompt | transit-time RAG |
| 804 | บิลนี้อยากเปลี่ยนที่อยู่จัดส่งครับ | charter slot prompt | ADDRESS_CHANGE |
| 806 | ของผมล่ะ | **full profile + Purchase Wallet 69,616.82 + email + phone** | CLARIFY |
| 808 | แล้วอันนี้ล่ะ | same full profile again | CLARIFY |

**FIRST WRONG TURN: t780.** The charter collection legitimately opened at t778/t779
(`มีบริการเหมารถไหมคะ` → TC19 FAQ answer); from t780 it consumed every subsequent turn.

## Root causes

1. **Sticky flow authority.** `charter_truck_flow.derive_charter_state(history)` opens on a
   TC19 FAQ answer (or a charter prompt) anywhere in the last 10 turns and never closes
   until `_CHARTER_DONE_RE` fires. The `decide()` charter branch then returned the
   missing-slot prompt for **any** message, with no check that the current turn is a
   compatible continuation. The calculator (`_resolve_continuation_action` /
   `derive_estimate_state`) and operational-change flows share the pattern.
   `interpret()` was CORRECT for every stuck turn — the failure was pure route/state
   arbitration.
2. **Referent-less private-data dump.** A bare possessive / demonstrative follow-up
   (`ของผมล่ะ`, `แล้วอันนี้ล่ะ`) with no referent in recent context resolved — via
   `_resolve_conversation_reference` resurrecting the remembered `getdatacustomer` — into a
   full customer-profile / wallet ERP read. Authorization alone (a verified binding) is not
   sufficient; a private ERP read needs an explicit or context-resolved private intent too.
3. **Product-policy noun.** `conversation_semantics._compose`'s PRODUCT_POLICY branch set
   `entities["product"]` to `split_on_whitespace(t)[0]`, which for a space-less Thai
   phrase (`กล่องพลาสติกนำเข้าได้ไหมครับ`) is the whole sentence — echoed back downstream.

## Fixes (one central rule, not per-sentence regex)

### `services/decision_engine.py`
- **`_ACTIONABLE_INTENT_FAMILIES`** / **`_FLOW_ONLY_INTENT_FAMILIES`** + **`_current_intent_breaks_pending_flow(semantic, message, *, flow_family, flow_extract, families)`** — the ONE arbitration function. Returns True when the CURRENT message is a decisive NEW actionable intent (`follow_up_op == "NONE"`, an actionable family, `confidence >= 0.55`) that is not the flow's own family and not a value the flow's slot extractor accepts. A follow-up op (SET_VALUE / CORRECTION / COMPARISON / CONTINUE), an ambiguous GENERAL / UNKNOWN read, or a valid slot value always CONTINUES the flow.
- Applied at: the **charter** branch (plus `private_state_inquiry is not None` and a last-resort non-charter-question regex `_CHARTER_NONCONTINUATION_Q_RE`, so a degraded-LLM `GENERAL` read of `ร้านส่งหรือยัง` still breaks it); the **operational-change** branch (only when HISTORY-sticky — `_derive_operational_state(None, message)` would not open one — so a FRESH operational request is untouched); the **`_resolve_continuation_action`** guard (restricted to `_FLOW_ONLY_INTENT_FAMILIES` so a same-domain SHIPMENT_STATUS / INVOICE elaboration is not misread as a topic change).
- **Referent-less private guard** — `_ELLIPTICAL_NO_REFERENT_RE` message + no `_PRIVATE_REFERENT_OFFER_RE` in the last 4 assistant turns → a WORKFLOW clarification (`ขออภัยค่ะ ไม่แน่ใจว่าหมายถึงข้อมูลส่วนไหน รบกวนระบุเพิ่มเติม เช่น สถานะสินค้า คูปอง หรือยอด Wallet ค่ะ`), returned BEFORE `_resolve_conversation_reference` / the private-state path / `getdatacustomer`. When the assistant DID just offer a specific private-data topic (`ต้องการให้ตรวจสอบคูปองในบัญชี…ไหมคะ`), the possessive resolves normally.

### `services/shipping_estimate_flow.py` (CALCULATOR-REGRESSION-2, reconciled & retained)
- `_is_explicit_new_request` no longer treats a bare route answer (`เอารถครับ`) as a fresh calculation even when the widened SEMANTIC-FIRST-2 LLM gate labels it `SHIPPING_ESTIMATE` / `SET_VALUE` — it carries no weight / dims / calc verb, so it accumulates the active thread.
- `_method_of` learns the comparison-connector shape (`ถ้าเป็นเรือล่ะ` → sea) and strips comparison tails; the unrelated-sentence regression test (`เรือสินค้ามาถึงยัง` etc.) still returns None.

### `services/conversation_semantics.py`
- PRODUCT_POLICY `entities["product"]` = the noun BEFORE the ship / permit verb, via `_bare_product_noun(t[:cut])` — `กล่องพลาสติกนำเข้าได้ไหมครับ` → `กล่องพลาสติก`, `แก้วน้ำนำเข้าได้ไหม` → `แก้วน้ำ`.

## Container verification (REAL LLM; no REAL LINE)

The 15-turn journey re-run as ONE accumulated session, seeded with the TC19 FAQ answer that
opened the sticky charter:

| turn | result |
|---|---|
| 1 calc | `shipping_estimate_flow` → asks method (`broke=charter->SHIPPING_ESTIMATE`) |
| 2 calc-new | new episode, weight 5, asks dims + method (no inheritance) |
| 3 seller-dispatch | `private_state_inquiry` → `กรุณาแจ้งเลขที่คำสั่งซื้อค่ะ` |
| 4 invoice | RAG invoice policy |
| 5 my coupons | API MY_COUPONS → `ไม่พบข้อมูลคูปองค่ะ` |
| 6 warehouse | RAG warehouse clarification |
| 7 product policy | RAG; noun clean (`กล่องพลาสติก`) — see Remaining regression |
| 8 charter | TC19 FAQ (enters charter) |
| 9 seller-dispatch (charter active) | `private_state_inquiry` (broke charter even with a degraded GENERAL read) |
| 10 coupon usage (charter active) | RAG coupon steps |
| 11 charter re-ask | charter collection prompt (same family — continues) |
| 12 transit time (charter active) | RAG `14–20 วัน` |
| 13 address change (charter active) | ADDRESS_CHANGE → asks for bill |
| 14 `ของผมล่ะ` | **CLARIFY** — no profile/wallet dump |
| 15 `แล้วอันนี้ล่ะ` | **CLARIFY** — no profile/wallet dump |

Valid continuations preserved: charter slot values (`เลขบิล FT… ปลายทางบางนา`,
`ชื่อผู้รับสมชาย เบอร์ 08…`), partial charter (`ปลายทางบางนา`), charter re-trigger,
calculator route answer (`เอารถครับ` → ROAD 192.26), calculator comparison
(`ถ้าเป็นเรือล่ะ` → SEA 125.39), new calculator episode (`ช่วยคิดค่าส่งใหม่ น้ำหนัก 5 โล`
inherits nothing). A possessive WITH a recent private-data offer is NOT force-clarified.

CALCULATOR-REGRESSION-2 journey re-verified unchanged (T1 ask method / T2 ROAD 192.26 /
T3 SEA 125.39 / T4 new episode) and cross-flow A–F.

## Remaining regression (recorded, NOT fixed here — separate RAG/answerability issue)

`กล่องพลาสติกนำเข้าได้ไหมครับ` as a fresh PRODUCT_POLICY **question** (follow_up_op NONE)
still reaches `run_playground_turn`'s no-information branch and returns the
`_product_answer_service_continuation` service-ack (`รับทราบค่ะ เป็นกล่องพลาสติกนะคะ 😊 …
สนใจส่งทางรถหรือทางเรือคะ`) rather than a prohibited-goods verdict. The product NOUN is now
clean and the ROUTE is correct (PRODUCT_POLICY / RAG, no flow theft, no invented fact); the
"service-ack vs. verdict" framing is pre-existing INVOICE-PRODUCT-REGRESSION-2 /
answerability-gate territory and out of scope for this state-authority blocker.

## Regression

- `test_decision_engine` + `test_conversation_semantics` + `test_semantic_first_1/2/2_1` + `test_customer_calc11`: 441 tests, 4 failures — 3 pre-existing `_CONFIG_CACHE` cross-test leak (pass in isolation), 1 pre-existing co-run pollution (`test_full_sequence`, passes in isolation).
- 16 protected suites (calc1, calc11, charter/TC19, action1, erp_read, fix2, invoice/product, rag_audit, pickup, authorization, slot_filling, sem1, ppc1, fix1, fix23, hybrid, synonym): 345 tests, 3 failures — the 3 pre-existing explicit-new-Business-Action boundary failures in `test_customer_uat_fix1`.
- `tests/test_system_state_emergency_1.py` (new, 7 tests) + `tests/test_calculator_regression_2.py` (new, 16 tests): all pass.
- **69-case CUSTOMER MASTER** (`tests/customer_uat/run_baseline`, degraded harness): 47 pass / 22 fail — **byte-identical per-case dimensions to `7859b7f`** (zero cases changed).

**NEW REGRESSION COUNT: 0. PRE-EXISTING FAILURE COUNT: 6** (3 `_CONFIG_CACHE`, 3 Fix-1 boundary) + the 22 master-baseline case failures (unchanged).

## Status

**CODE PASS / DEPLOYED / READY FOR PRODUCT-OWNER MANUAL UAT.** No REAL LINE testing by
Claude. Link Conversion NOT resumed.
