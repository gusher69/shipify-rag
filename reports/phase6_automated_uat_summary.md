# PHASE 6 — Automated UAT Pre-check

- generated: 2026-09-07T01:40:39.220714+00:00
- git SHA: `958b0d4ffb37e611f4a8eadd8fe91a39ec282909`
- production SHA: `958b0d4ffb37e611f4a8eadd8fe91a39ec282909`
- environment: headless backend pre-check — real DecisionEngine/registry/auth; mocked safe ERP HTTP; mocked RAG (OpenAI credit exhausted); no real LINE

**TOTAL 69 — PASS 64 · FAIL 0 · BLOCKED 0 · NEEDS_REAL_LINE 5**

> This is a regression/blocker pre-check, NOT Final REAL LINE acceptance. Every case still requires the Product Owner's real-LINE confirmation (`requires_real_line=true` on all 69).


## PASS (64)

- **CUS-G01** — routes RAG; media/image delivery deferred to real LINE (asset binaries absent)  
  _asset:_ TH/CN warehouse map + FT/SP step images not in repo  
  _note:_ text answer routes RAG; media delivery unverifiable (TH/CN warehouse map + FT/SP step images not in repo)
- **CUS-G02** — routes RAG; media/image delivery deferred to real LINE (asset binaries absent)  
  _asset:_ CN warehouse SP/FT step images not in repo  
  _note:_ text answer routes RAG; media delivery unverifiable (CN warehouse SP/FT step images not in repo)
- **CUS-G03** — stage-1 identifier ask for a case whose full capability needs an absent API (no trusted estimated_arrival_th field in SearchDataShipment)  
  _external:_ no trusted estimated_arrival_th field in SearchDataShipment  
  _asset:_ 5 shipment/ETA screenshots not in repo
- **CUS-G04** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-G05** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-G06** — routes RAG; media/image delivery deferred to real LINE (asset binaries absent)  
  _asset:_ 3 CBM diagram images not in repo  
  _note:_ text answer routes RAG; media delivery unverifiable (3 CBM diagram images not in repo)
- **CUS-G07** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-G08** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-G09** — routes RAG; media/image delivery deferred to real LINE (asset binaries absent)  
  _asset:_ 2 payment-step images not in repo  
  _note:_ text answer routes RAG; media delivery unverifiable (2 payment-step images not in repo)
- **CUS-G10** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-G11** — stage-1 identifier ask for a case whose full capability needs an absent API (no claims WRITE API (POST /claims))  
  _external:_ no claims WRITE API (POST /claims)
- **CUS-G12** — routes PRIVATE (WORKFLOW), grounded reply, auth-gated, no leak
- **CUS-G13** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-G14** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-G15** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-G16** — stage-1 identifier ask for a case whose full capability needs an absent API (no shop_shipped_at field in SearchDataOrder)  
  _external:_ no shop_shipped_at field in SearchDataOrder
- **CUS-G18** — honest Human-CS / no-confirmed-data fallback for a case whose full capability needs an absent API (no wallet-transactions READ API; inbound slip image not ingested)  
  _external:_ no wallet-transactions READ API; inbound slip image not ingested
- **CUS-G19** — routes RAG; media/image delivery deferred to real LINE (asset binaries absent)  
  _asset:_ 2 charter-truck images not in repo  
  _note:_ text answer routes RAG; media delivery unverifiable (2 charter-truck images not in repo)
- **CUS-G20** — routes RAG; media/image delivery deferred to real LINE (asset binaries absent)  
  _asset:_ 4 private-carrier rate-table images not in repo  
  _note:_ text answer routes RAG; media delivery unverifiable (4 private-carrier rate-table images not in repo)
- **CUS-G21** — honest Human-CS / no-confirmed-data fallback for a case whose full capability needs an absent API (no executable CancelOrder + payment_status eligibility action)  
  _external:_ no executable CancelOrder + payment_status eligibility action
- **CUS-G22** — routes RAG; media/image delivery deferred to real LINE (asset binaries absent)  
  _asset:_ 1 rate image not in repo  
  _note:_ text answer routes RAG; media delivery unverifiable (1 rate image not in repo)
- **CUS-G23** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-G24** — routes RAG; media/image delivery deferred to real LINE (asset binaries absent)  
  _asset:_ 1 services image not in repo  
  _note:_ text answer routes RAG; media delivery unverifiable (1 services image not in repo)
- **CUS-G25** — routes RAG; media/image delivery deferred to real LINE (asset binaries absent)  
  _asset:_ 2 ฝากสั่ง/ฝากนำเข้า images not in repo  
  _note:_ text answer routes RAG; media delivery unverifiable (2 ฝากสั่ง/ฝากนำเข้า images not in repo)
- **CUS-G26** — routes RAG; media/image delivery deferred to real LINE (asset binaries absent)  
  _asset:_ 3 payment images not in repo  
  _note:_ text answer routes RAG; media delivery unverifiable (3 payment images not in repo)
- **CUS-G27** — routes RAG; media/image delivery deferred to real LINE (asset binaries absent)  
  _asset:_ 2 cost-structure images not in repo  
  _note:_ text answer routes RAG; media delivery unverifiable (2 cost-structure images not in repo)
- **CUS-G28** — routes RAG; media/image delivery deferred to real LINE (asset binaries absent)  
  _asset:_ 2 wood-crating images not in repo  
  _note:_ text answer routes RAG; media delivery unverifiable (2 wood-crating images not in repo)
- **CUS-G29** — link-conversion intent handled (ack / converted-link path)
- **CUS-S01** — stage-1 identifier ask for a case whose full capability needs an absent API (no trusted estimated_arrival_th field)  
  _external:_ no trusted estimated_arrival_th field
- **CUS-S02** — stage-1 identifier ask for a case whose full capability needs an absent API (no qty-modify WRITE API)  
  _external:_ no qty-modify WRITE API
- **CUS-S03** — stage-1 identifier ask for a case whose full capability needs an absent API (no shipping-method-change WRITE API)  
  _external:_ no shipping-method-change WRITE API
- **CUS-S04** — stage-1 identifier ask for a case whose full capability needs an absent API (no add-VAT WRITE API)  
  _external:_ no add-VAT WRITE API
- **CUS-S05** — routes PRIVATE (WORKFLOW), grounded reply, auth-gated, no leak
- **CUS-S06** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-S07** — honest Human-CS / no-confirmed-data fallback for a case whose full capability needs an absent API (no invoice PDF/URL fetch API)  
  _external:_ no invoice PDF/URL fetch API
- **CUS-S08** — stage-1 identifier ask for a case whose full capability needs an absent API (no shipment/tracking -> purchase-bill reverse-map field)  
  _external:_ no shipment/tracking -> purchase-bill reverse-map field
- **CUS-S09** — routes PRIVATE (WORKFLOW), grounded reply, auth-gated, no leak
- **CUS-S10** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-S11** — stage-1 identifier ask for a case whose full capability needs an absent API (no carrier/self-pickup-change WRITE API)  
  _external:_ no carrier/self-pickup-change WRITE API
- **CUS-S12** — routes PRIVATE (WORKFLOW), grounded reply, auth-gated, no leak  
  _asset:_ SP withdrawal-form image not in repo
- **CUS-S13** — stage-1 identifier ask for a case whose full capability needs an absent API (no duplicate-bill delete API)  
  _external:_ no duplicate-bill delete API
- **CUS-S14** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-S15** — honest Human-CS / no-confirmed-data fallback for a case whose full capability needs an absent API (no CN warehouse-address validate API)  
  _external:_ no CN warehouse-address validate API
- **CUS-S16** — routes PRIVATE (WORKFLOW), grounded reply, auth-gated, no leak
- **CUS-S17** — routes PRIVATE (WORKFLOW), grounded reply, auth-gated, no leak
- **CUS-S18** — honest Human-CS / no-confirmed-data fallback for a case whose full capability needs an absent API (no per-record DateArrivedTH for a real 'today' filter)  
  _external:_ no per-record DateArrivedTH for a real 'today' filter
- **CUS-S20a** — link-conversion intent handled (ack / converted-link path)
- **CUS-S20b** — link-conversion intent handled (ack / converted-link path)
- **CUS-F01** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-F02** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-F03** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-F04** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-F05** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)  
  _note:_ Phase-5 D15 fix: 'สั่ง...จำนวนเยอะ' now classifies PRODUCT_POLICY
- **CUS-F06** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-F07** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-F09** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-F10** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-F11** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-SC1** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-SC3** — bare-dimensions input asks the expected clarifying question
- **CUS-P07** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-P06** — routes RAG/GENERAL as expected (live retrieval content unverifiable this run — OpenAI credit exhausted)
- **CUS-P20** — link-conversion intent handled (ack / converted-link path)
- **CUS-RL-genuine_continuation** — routes PRIVATE (API), grounded reply, auth-gated, no leak

## FAIL (0)


## External / API / asset blocker (0)


## Needs REAL LINE (5)

- **CUS-G17** — generic ambiguity prompt for 'ติดตามสถานะ สินค้า' (D8, routing edge — safe); confirm identifier-ask on real LINE
- **CUS-F08** — multi-turn invoice-eligibility loop closure over real channel turns (recorded 50% pass / 50% fail)
- **CUS-SC2** — real self-service LINE verification / rebinding experience  
  _external:_ no trusted estimated_arrival_th field (case is really a verification-flow probe)
- **CUS-RL-p0_01_repeat_after_completed_cycle** — multi-turn stale-identifier replay-cycle behaviour; not assertable single-turn headless  
  _note:_ committed test_real_line_replay_fixtures for this case fails (pre-existing, stale-cycle state) — verify on real LINE
- **CUS-RL-p0_01_stale_cycle** — multi-turn stale-identifier replay-cycle behaviour; not assertable single-turn headless  
  _note:_ committed test_real_line_replay_fixtures for this case fails (pre-existing, stale-cycle state) — verify on real LINE
