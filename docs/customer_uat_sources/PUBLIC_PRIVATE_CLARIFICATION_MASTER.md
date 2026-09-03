# PUBLIC / PRIVATE / CLARIFICATION MASTER — measurement

- **Base:** `0745846`  ·  REAL `DecisionEngine.decide()` + REAL DB registry; RAG + ERP HTTP faked.
- **Code change made** — a minimal `_classify_private_state_inquiry` boundary fix (see BEFORE -> AFTER).

## BEFORE -> AFTER (PPC boundary fix in `services/decision_engine.py`)

| Dimension | BEFORE (`0745846` untouched) | AFTER |
|---|---|---|
| PUBLIC accuracy | 18/20 (90.0%) | 20/20 (100.0%) |
| PRIVATE accuracy | 14/16 (87.5%) | 16/16 (100.0%) |
| PUBLIC/PRIVATE pair accuracy | 3/4 (75.0%) | 4/4 (100.0%) |
| CLARIFICATION accuracy | 9/9 (100.0%) | 9/9 (100.0%) |
| PUBLIC identity false-positive rate | 2/20 (10.0%) | 0/20 (0.0%) |
| PRIVATE data leakage | ZERO | ZERO |
| Accidental Human CS handoff | 0/29 | 0/29 |
| Stale-history override | PASS | PASS |

BEFORE failing cases (both now PASS):

- PUB-08 `โกดังรับสินค้าอยู่ที่ไหน` — facility-location question wrongly classified private (SEM-1 over-reach on "สินค้า"+"อยู่ที่ไหน"), asked for CustCode.
- PRV-06 `เบอร์ที่ผมลงทะเบียนไว้คืออะไร` — customer's own on-file phone question fell through to public RAG.

## Scores

| Dimension | Result |
|---|---|
| PUBLIC accuracy | 20/20 (100.0%) |
| PRIVATE accuracy | 16/16 (100.0%) |
| PUBLIC/PRIVATE pair accuracy | 4/4 (100.0%) |
| CLARIFICATION accuracy (guardrails) | 9/9 (100.0%) |
| PUBLIC identity false-positive rate | 0/20 (0.0%) |
| PRIVATE data leakage | **ZERO** |
| Accidental Human CS handoff | 0/29 |
| Stale-history override | **PASS** |
| Unverified PUBLIC | **PASS** |
| Unverified PRIVATE | **PASS** |
| Verified PRIVATE | **PASS** |
| PUBLIC verified/unverified parity | **PASS** |

## PUBLIC / PRIVATE pairs

| Public | verdict | Private | verdict | pair |
|---|---|---|---|---|
| คูปองใช้ยังไง | PASS | ผมมีคูปองอะไรบ้าง | PASS | PASS |
| ทางรถใช้เวลากี่วัน | PASS | ของผมเข้าไทยหรือยัง | PASS | PASS |
| ขอเบอร์ติดต่อ | PASS | เบอร์ที่ผมลงทะเบียนไว้คืออะไร | PASS | PASS |
| เติม Wallet ยังไง | PASS | ยอด Wallet ของผมเท่าไหร่ | PASS | PASS |

## Per-case

| id | kind | verified | message | routing | src | action | id-ask | handoff | leak | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| PUB-01 | PUBLIC | True | ทางรถใช้เวลากี่วัน | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-01 | PUBLIC | False | ทางรถใช้เวลากี่วัน | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-02 | PUBLIC | True | ทางเรือกี่วัน | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-02 | PUBLIC | False | ทางเรือกี่วัน | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-03 | PUBLIC | True | ค่าขนส่งคิดยังไง | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-03 | PUBLIC | False | ค่าขนส่งคิดยังไง | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-04 | PUBLIC | True | คูปองใช้ยังไง | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-04 | PUBLIC | False | คูปองใช้ยังไง | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-05 | PUBLIC | True | ขอเบอร์ติดต่อ | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-05 | PUBLIC | False | ขอเบอร์ติดต่อ | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-06 | PUBLIC | True | มีบริการอะไรบ้าง | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-06 | PUBLIC | False | มีบริการอะไรบ้าง | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-07 | PUBLIC | True | สินค้าต้องห้ามมีอะไรบ้าง | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-07 | PUBLIC | False | สินค้าต้องห้ามมีอะไรบ้าง | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-08 | PUBLIC | True | โกดังรับสินค้าอยู่ที่ไหน | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-08 | PUBLIC | False | โกดังรับสินค้าอยู่ที่ไหน | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-09 | PUBLIC | True | ออกใบกำกับได้ไหม | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-09 | PUBLIC | False | ออกใบกำกับได้ไหม | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-10 | PUBLIC | True | เติม Wallet ยังไง | RAG | fresh_search | None | · | · | · | **PASS** |
| PUB-10 | PUBLIC | False | เติม Wallet ยังไง | RAG | fresh_search | None | · | · | · | **PASS** |
| PRV-01 | PRIVATE | True | ยอด Wallet ของผมเท่าไหร่ | WORKFLOW | fresh_search | getdatacustomer | Y | · | · | **PASS** |
| PRV-01 | PRIVATE | False | ยอด Wallet ของผมเท่าไหร่ | WORKFLOW | fresh_search | getdatacustomer | Y | · | · | **PASS** |
| PRV-02 | PRIVATE | True | ผมมีคูปองอะไรบ้าง | WORKFLOW | fresh_search | getdatacustomer | Y | · | · | **PASS** |
| PRV-02 | PRIVATE | False | ผมมีคูปองอะไรบ้าง | WORKFLOW | fresh_search | getdatacustomer | Y | · | · | **PASS** |
| PRV-03 | PRIVATE | True | ของผมเข้าไทยหรือยัง | WORKFLOW | private_state_inquiry | searchdatashipment | Y | · | · | **PASS** |
| PRV-03 | PRIVATE | False | ของผมเข้าไทยหรือยัง | WORKFLOW | private_state_inquiry | searchdatashipment | Y | · | · | **PASS** |
| PRV-04 | PRIVATE | True | ออเดอร์ของผมถึงไหนแล้ว | WORKFLOW | private_state_inquiry | searchdataorder | Y | · | · | **PASS** |
| PRV-04 | PRIVATE | False | ออเดอร์ของผมถึงไหนแล้ว | WORKFLOW | private_state_inquiry | searchdataorder | Y | · | · | **PASS** |
| PRV-05 | PRIVATE | True | เช็กพัสดุของผม | WORKFLOW | private_state_inquiry | searchdatashipment | Y | · | · | **PASS** |
| PRV-05 | PRIVATE | False | เช็กพัสดุของผม | WORKFLOW | private_state_inquiry | searchdatashipment | Y | · | · | **PASS** |
| PRV-06 | PRIVATE | True | เบอร์ที่ผมลงทะเบียนไว้คืออะไร | WORKFLOW | private_state_inquiry | getdatacustomer | Y | · | · | **PASS** |
| PRV-06 | PRIVATE | False | เบอร์ที่ผมลงทะเบียนไว้คืออะไร | WORKFLOW | private_state_inquiry | getdatacustomer | Y | · | · | **PASS** |
| PRV-07 | PRIVATE | True | ข้อมูลลูกค้าของผม | WORKFLOW | fresh_search | getdatacustomer | Y | · | · | **PASS** |
| PRV-07 | PRIVATE | False | ข้อมูลลูกค้าของผม | WORKFLOW | fresh_search | getdatacustomer | Y | · | · | **PASS** |
| PRV-08 | PRIVATE | True | ยอดค้างของผมมีไหม | WORKFLOW | fresh_search | None | Y | · | · | **PASS** |
| PRV-08 | PRIVATE | False | ยอดค้างของผมมีไหม | WORKFLOW | fresh_search | None | Y | · | · | **PASS** |
| CLR-01 | CLARIFY | False | สั่งเยอะได้ไหม | RAG | fresh_search | None | · | · | · | **PASS** |
| CLR-02 | CLARIFY | False | อันนี้ได้ไหม | RAG | fresh_search | None | · | · | · | **PASS** |
| CLR-03 | CLARIFY | False | ได้หรือเปล่าคะ | RAG | fresh_search | None | · | · | · | **PASS** |
| CLR-04 | CLARIFY | False | ราคาเท่าไหร่ | RAG | fresh_search | None | · | · | · | **PASS** |
| CLR-05 | CLARIFY | False | เช็กให้หน่อย | RAG | fresh_search | None | · | · | · | **PASS** |
| CLR-06 | CLARIFY | False | ขอรายละเอียด | RAG | fresh_search | None | · | · | · | **PASS** |
| CLR-07 | CLARIFY | False | มีไหม | RAG | fresh_search | None | · | · | · | **PASS** |
| CLR-08 | CLARIFY | False | เอาแบบเดิม | RAG | fresh_search | None | · | · | · | **PASS** |
| CLR-09 | CLARIFY | False | ไม่ใช่อันนี้ | RAG | fresh_search | None | · | · | · | **PASS** |
| CTX-01 | CTX->PUBLIC | True | คูปองใช้ยังไง | RAG | fresh_search | None | · | · | · | **PASS** |
| CTX-02 | CTX->PUBLIC | True | ทางเรือกี่วัน | RAG | fresh_search | None | · | · | · | **PASS** |
| CTX-03 | CTX->PRIVATE | True | ยอด Wallet ของผมเท่าไหร่ | WORKFLOW | fresh_search | getdatacustomer | Y | · | · | **PASS** |
| CTX-04 | CTX->PRIVATE | True | ของผมเข้าไทยหรือยัง | WORKFLOW | private_state_inquiry | searchdatashipment | Y | · | · | **PASS** |

## customer_uat_master.jsonl subset (for later RAG-GAP-0 / CONV-SELL, not solved here)

- **Public FAQ** (34): CUS-G01, CUS-G02, CUS-G04, CUS-G05, CUS-G06, CUS-G07, CUS-G08, CUS-G09, CUS-G10, CUS-G13, CUS-G14, CUS-G15, CUS-G20, CUS-G22, CUS-G23, CUS-G24, CUS-G25, CUS-G26, CUS-G27, CUS-G28, CUS-S14, CUS-F01, CUS-F02, CUS-F03, CUS-F04, CUS-F05, CUS-F06, CUS-F07, CUS-F08, CUS-F09, CUS-F10, CUS-F11, CUS-SC1, CUS-P07
- **Private ERP** (22): CUS-G03, CUS-G11, CUS-G12, CUS-G16, CUS-G17, CUS-G18, CUS-G21, CUS-S01, CUS-S02, CUS-S03, CUS-S04, CUS-S05, CUS-S07, CUS-S08, CUS-S09, CUS-S11, CUS-S12, CUS-S13, CUS-S15, CUS-S17, CUS-S18, CUS-SC2
- **Clarification** (1): CUS-SC3

---
_Generated by tests/customer_uat/ppc_boundary_eval.py — measurement only._