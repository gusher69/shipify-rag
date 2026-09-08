# PHASE-6D — Thai Typo Robustness (FINAL Reconciliation)

- generated: 2026-09-08T16:35:45
- production SHA: `2b5714686a8c36f7ce7b3a44a1ad654dcb46377a`
- denominator rule: a typo case passes when its production-equivalent
  routing is not WORSE than the identical conversation with no typo
  (ideal family, OR == clean-input family, OR identical last-turn
  (source, family) to the clean journey).

## Reconciliation across all 302 typo cases

TOTAL TYPO CASES: 302

PASS_DETERMINISTIC: 289
PASS_LIVE_RECOVERY: 0
FAIL_LIVE: 13
EXPECTED_LIMITATION: 0

ACCEPTED PASS (deterministic + live recovery): 289
FINAL PRODUCTION-EQUIVALENT ACCURACY: 289/302 = 95.7%

## Safety counters

STRUCTURED VALUE CORRUPTION: 0
PRIVATE DATA LEAK: 0
BUSINESS HALLUCINATION: 0
UNSAFE ACTION: 0
STALE STATE FAIL: 0
NEW REGRESSION DELTA: 0
ADVERSARIAL structured-value suite: 24/24 clean

## PASS GATE: PASS  (production-equivalent accuracy >= 95% AND all safety counters = 0)

## PHASE 6D STATUS: COMPLETE

## FAIL_LIVE cases (typo made routing strictly worse than clean)

- `wd-ship-1-duplicated_char-4` [duplicated_char] 'ถอนเงินขนสสส่งยังไงคะ'
  clean=SHIPPING_WITHDRAWAL  det=PURCHASE_WITHDRAWAL/purchase_withdrawal_kb  live=PURCHASE_WITHDRAWAL/purchase_withdrawal_kb  family 'PURCHASE_WITHDRAWAL' not in ['SHIPPING_WITHDRAWAL'] and != clean 'SHIPPING_WITHDRAWAL'
- `link-1-separated_words-0` [separated_words] 'ช่วยแปล งลิงก์ให้หน่อย'
  clean=LINK_CONVERSION  det=WEBSITE_LINK_REQUEST/phase6b_service_intent  live=WEBSITE_LINK_REQUEST/phase6b_service_intent  family 'WEBSITE_LINK_REQUEST' not in ['LINK_CONVERSION'] and != clean 'LINK_CONVERSION'
- `link-3-missing_consonant-2` [missing_consonant] 'เอาลิงก์นี้ไปแลงเป็นภาษาไทย'
  clean=LINK_CONVERSION  det=WEBSITE_LINK_REQUEST/phase6b_service_intent  live=WEBSITE_LINK_REQUEST/phase6b_service_intent  family 'WEBSITE_LINK_REQUEST' not in ['LINK_CONVERSION'] and != clean 'LINK_CONVERSION'
- `j-wd-brand-v2` [multi] 'ถอนเงินขนส่งยังไง || SP1008 || เปป็นแบรนด์ FT ค่ะ'
  clean=('shipping_withdrawal_kb', 'SHIPPING_WITHDRAWAL')  det=PURCHASE_WITHDRAWAL/purchase_withdrawal_kb  live=PURCHASE_WITHDRAWAL/purchase_withdrawal_kb  family 'PURCHASE_WITHDRAWAL' not in ['SHIPPING_WITHDRAWAL', 'GENERAL', 'UNKNOWN']
- `j-wd-brand-v5` [multi] 'ถอนเงินขนส่งยังไง || SP1008 || เป็นแบ รนด์ FT ค่ะ'
  clean=('shipping_withdrawal_kb', 'SHIPPING_WITHDRAWAL')  det=PURCHASE_WITHDRAWAL/purchase_withdrawal_kb  live=PURCHASE_WITHDRAWAL/purchase_withdrawal_kb  family 'PURCHASE_WITHDRAWAL' not in ['SHIPPING_WITHDRAWAL', 'GENERAL', 'UNKNOWN']
- `j-wd-brand-v10` [multi] 'ถอนเงินขนส่งยังไง || SP1008 || เป็นแบรนด์ FTค่ะ'
  clean=('shipping_withdrawal_kb', 'SHIPPING_WITHDRAWAL')  det=PURCHASE_WITHDRAWAL/purchase_withdrawal_kb  live=PURCHASE_WITHDRAWAL/purchase_withdrawal_kb  family 'PURCHASE_WITHDRAWAL' not in ['SHIPPING_WITHDRAWAL', 'GENERAL', 'UNKNOWN']
- `j-wd-brand-v13` [multi] 'ถอนเงินขนส่งยังไง || SP1008 || เป็นแบรนด์ FT ค่'
  clean=('shipping_withdrawal_kb', 'SHIPPING_WITHDRAWAL')  det=PURCHASE_WITHDRAWAL/purchase_withdrawal_kb  live=PURCHASE_WITHDRAWAL/purchase_withdrawal_kb  family 'PURCHASE_WITHDRAWAL' not in ['SHIPPING_WITHDRAWAL', 'GENERAL', 'UNKNOWN']
- `j-topic-switch-v1` [multi] 'ขอแปลงลิงก์ || ไม่เอาแ ล้ว ขอถามค่าตีลังไม้แทน'
  clean=('fresh_search', 'LINK_CONVERSION')  det=LINK_CONVERSION/WORKFLOW  live=LINK_CONVERSION/WORKFLOW  family 'LINK_CONVERSION' not in ['SHIPPING_ESTIMATE', 'GENERAL', 'UNKNOWN', 'PRODUCT_POLICY']
- `j-topic-switch-v4` [multi] 'ขอแปลงลิงก์ || ไมม่เอาแล้ว ขอถามค่าตีลังไม้แทน'
  clean=('fresh_search', 'LINK_CONVERSION')  det=LINK_CONVERSION/WORKFLOW  live=LINK_CONVERSION/WORKFLOW  family 'LINK_CONVERSION' not in ['SHIPPING_ESTIMATE', 'GENERAL', 'UNKNOWN', 'PRODUCT_POLICY']
- `j-topic-switch-v6` [multi] 'ขอแปลงลิงก์ || ไม่เอสแล้ว ขอถามค่าตีลังไม้แทน'
  clean=('fresh_search', 'LINK_CONVERSION')  det=LINK_CONVERSION/WORKFLOW  live=LINK_CONVERSION/WORKFLOW  family 'LINK_CONVERSION' not in ['SHIPPING_ESTIMATE', 'GENERAL', 'UNKNOWN', 'PRODUCT_POLICY']
- `j-topic-switch-v7` [multi] 'ขอแปลงลิงก์ || ไม่เราแล้ว ขอถามค่าตีลังไม้แทน'
  clean=('fresh_search', 'LINK_CONVERSION')  det=LINK_CONVERSION/WORKFLOW  live=LINK_CONVERSION/WORKFLOW  family 'LINK_CONVERSION' not in ['SHIPPING_ESTIMATE', 'GENERAL', 'UNKNOWN', 'PRODUCT_POLICY']
- `j-topic-switch-v8` [multi] 'ขอแปลงลิงก์ || ไม่เอาแลว ขอถามค่าตีลังไม้แทน'
  clean=('fresh_search', 'LINK_CONVERSION')  det=LINK_CONVERSION/WORKFLOW  live=LINK_CONVERSION/WORKFLOW  family 'LINK_CONVERSION' not in ['SHIPPING_ESTIMATE', 'GENERAL', 'UNKNOWN', 'PRODUCT_POLICY']
- `j-topic-switch-v11` [multi] 'ขอแปลงลิงก์ || ไม่เอาแลว ขอถามค่าตีลังไม้แทน'
  clean=('fresh_search', 'LINK_CONVERSION')  det=LINK_CONVERSION/WORKFLOW  live=LINK_CONVERSION/WORKFLOW  family 'LINK_CONVERSION' not in ['SHIPPING_ESTIMATE', 'GENERAL', 'UNKNOWN', 'PRODUCT_POLICY']

## PASS_LIVE_RECOVERY cases

## FAIL_LIVE characterization (all 13 — safe adjacent-family mis-routes)

Every FAIL_LIVE is a genuine typo-caused mis-route between ADJACENT / related
families, from a SEVERE single-word mangling. None is unsafe:
STRUCTURED_VALUE_CORRUPTION 0, BUSINESS_HALLUCINATION 0, PRIVATE_DATA_LEAK 0,
UNSAFE_ACTION 0, STALE_STATE_FAIL 0 across all 13. The replies are real,
honest, on-topic-adjacent content; the customer's next turn re-orients each.

- 6x `j-topic-switch-v*` — the switch phrase "ไม่เอาแล้ว" is mangled
  ("ไม่เอาแ ล้ว" / "ไมม่เอาแล้ว" / "ไม่เอสแล้ว" / "ไม่เอาแลว") past the
  point _TOPIC_RE matches, so the just-finished link-conversion flow
  re-asks "send the product link" instead of yielding to the new
  crate-fee question. Still LINK-domain; no wrong fact.
- 5x withdrawal (`j-wd-brand-v2/v5/v10/v13`, `wd-ship-1-dup`) — a mangled
  "เป็น" / "แบรนด์" / a mid-word "ขนสสส่ง" triple flips the withdrawal
  type SHIPPING_WITHDRAWAL -> PURCHASE_WITHDRAWAL; both answers are about
  withdrawing credit, only the credit type differs.
- 2x link (`link-1-sep`, `link-3-missing-consonant`) — "แปลง" (convert)
  mangled to "แปล ง" / "แลง" reads as "which website", so the 3 platform
  URLs are returned instead of asking for the product link to convert.

Root: these words are 2+ edits from any curated non-word form, and by
design NO general edit-distance pass runs at intent time (it would flip
near-homophone routing verbs — "โอนเงิน"/"ถอนเงิน", "เข้า"/"นำเข้า").
Recorded as Technical Debt (widen the curated non-word map per confirmed
real-LINE occurrence), not a deploy blocker: the gate (>=95% AND all
safety counters 0) is met at 95.7%.
