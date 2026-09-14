# Customer UAT Case Map — all 69 cases (Phase 6 closure gate A)

One **primary** terminal classification per case. Secondary causes live in their own column and are never counted again.

**PASS 50 / 69 — FAIL 19**

| Primary class | Count |
|---|---|
| API_GAP | 13 |
| HUMAN_CS_ONLY | 4 |
| EXPECTATION_SUPERSEDED | 1 |
| TEST_ENV | 1 |
| **TOTAL FAIL** | **19** |

PASS 50 + FAIL 19 = 69

| CASE_ID | PASS/FAIL | Primary class | Blocking capability / reason | Secondary causes | Expected route | Current route | Blocks release? |
|---|---|---|---|---|---|---|---|
| CUS-G01 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G02 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G03 | PASS | — | — | — | ERP | WORKFLOW | NO |
| CUS-G04 | PASS | — | — | — | RAG | GENERAL | NO |
| CUS-G05 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G06 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G07 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G08 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G09 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G10 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G11 | FAIL | API_GAP | GET /orders/{bill}/items + POST /claims (claims intake) | — | ERP | WORKFLOW | NO |
| CUS-G12 | PASS | — | — | — | ERP | WORKFLOW | NO |
| CUS-G13 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G14 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G15 | PASS | — | — | — | RAG | GENERAL | NO |
| CUS-G16 | PASS | — | — | — | ERP | WORKFLOW | NO |
| CUS-G17 | FAIL | API_GAP | GET /shipments/{bill}/tracking_th | — | ERP | WORKFLOW | NO |
| CUS-G18 | FAIL | API_GAP | GET /wallet/{customer_id}/transactions | — | ERP | WORKFLOW | NO |
| CUS-G19 | FAIL | HUMAN_CS_ONLY | charter quote is priced by a person | — | HUMAN_CS | RAG | NO |
| CUS-G20 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G21 | FAIL | EXPECTATION_SUPERSEDED | owner ruling: a cancel POLICY question stays RAG | this suite's expected_route=ERP predates that ruling; behaviour is correct | ERP | RAG | NO |
| CUS-G22 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G23 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G24 | PASS | — | — | — | RAG | GENERAL | NO |
| CUS-G25 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G26 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G27 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G28 | PASS | — | — | — | RAG | RAG | NO |
| CUS-G29 | PASS | — | — | — | NA | WORKFLOW | NO |
| CUS-S01 | PASS | — | — | — | ERP | WORKFLOW | NO |
| CUS-S02 | FAIL | API_GAP | PUT /orders/{bill}/items | — | ERP | WORKFLOW | NO |
| CUS-S03 | FAIL | API_GAP | PUT /orders/{bill}/shipping_type | — | ERP | WORKFLOW | NO |
| CUS-S04 | FAIL | API_GAP | PUT /orders/{bill}/vat | — | ERP | WORKFLOW | NO |
| CUS-S05 | FAIL | API_GAP | GET /wallet/{customer_id} (purchase credit) | — | ERP | WORKFLOW | NO |
| CUS-S06 | FAIL | HUMAN_CS_ONLY | OEM/custom production coordinated by purchasing | — | HUMAN_CS | WORKFLOW | NO |
| CUS-S07 | FAIL | API_GAP | GET /documents/{bill}/invoice (document retrieval) | multi-intent turn: the how-to half is already answered via RAG | ERP | RAG | NO |
| CUS-S08 | FAIL | API_GAP | GET /shipments/map?tracking_cn= (tracking->bill) | — | ERP | WORKFLOW | NO |
| CUS-S09 | PASS | — | — | — | ERP | WORKFLOW | NO |
| CUS-S10 | FAIL | HUMAN_CS_ONLY | repack is a multi-bill warehouse operation | — | HUMAN_CS | WORKFLOW | NO |
| CUS-S11 | FAIL | API_GAP | PUT /shipments/{bill}/carrier | — | ERP | WORKFLOW | NO |
| CUS-S12 | FAIL | API_GAP | GET+POST /wallet/{cust}/withdraw (shipment) | — | ERP | WORKFLOW | NO |
| CUS-S13 | FAIL | API_GAP | DELETE /shipments/{bill} (+ human confirm) | — | ERP | WORKFLOW | NO |
| CUS-S14 | PASS | — | — | — | RAG | RAG | NO |
| CUS-S15 | FAIL | API_GAP | GET /warehouse/cn/address?customer_id= | — | ERP | WORKFLOW | NO |
| CUS-S16 | FAIL | HUMAN_CS_ONLY | charter consolidation is a logistics operation | — | HUMAN_CS | WORKFLOW | NO |
| CUS-S17 | PASS | — | — | — | ERP | WORKFLOW | NO |
| CUS-S18 | PASS | — | — | — | ERP | WORKFLOW | NO |
| CUS-S20a | PASS | — | — | — | NA | WORKFLOW | NO |
| CUS-S20b | PASS | — | — | — | NA | WORKFLOW | NO |
| CUS-F01 | PASS | — | — | — | RAG | RAG | NO |
| CUS-F02 | PASS | — | — | — | RAG | RAG | NO |
| CUS-F03 | PASS | — | — | — | RAG | RAG | NO |
| CUS-F04 | PASS | — | — | — | RAG | RAG | NO |
| CUS-F05 | PASS | — | — | — | RAG | RAG | NO |
| CUS-F06 | PASS | — | — | — | RAG | RAG | NO |
| CUS-F07 | PASS | — | — | — | RAG | RAG | NO |
| CUS-F08 | PASS | — | — | — | RAG | RAG | NO |
| CUS-F09 | PASS | — | — | — | RAG | RAG | NO |
| CUS-F10 | PASS | — | — | — | RAG | RAG | NO |
| CUS-F11 | PASS | — | — | — | RAG | RAG | NO |
| CUS-SC1 | PASS | — | — | — | RAG | RAG | NO |
| CUS-SC2 | PASS | — | — | — | ERP | WORKFLOW | NO |
| CUS-SC3 | PASS | — | — | — | CLARIFY | WORKFLOW | NO |
| CUS-P07 | PASS | — | — | — | RAG | GENERAL | NO |
| CUS-P06 | FAIL | TEST_ENV | RAG stubbed at confidence 0.9 so the no-info gate cannot fire | verify on the live tier | HUMAN_CS | RAG | NO |
| CUS-P20 | PASS | — | — | — | WORKFLOW | WORKFLOW | NO |
| CUS-RL-genuine_continuation | PASS | — | — | — | ERP | API | NO |
| CUS-RL-p0_01_repeat_after_completed_cycle | PASS | — | — | — | WORKFLOW | API | NO |
| CUS-RL-p0_01_stale_cycle | PASS | — | — | — | WORKFLOW | API | NO |

## Note on the earlier arithmetic error

A previous report printed `API 13 / HUMAN_CS 4 / SOURCE_CONFLICT 1 / CODE 1 / TEST_ENV 1` = 20 against 19 failures. The generator's own counter said API **12**; the prose mis-transcribed it. The table above is generated, not transcribed, and sums exactly. CUS-S07 is now API_GAP (owner ruling 3: document retrieval is an API/document capability, not a code defect), which is why API is 13 here for a principled reason rather than an accidental one.
