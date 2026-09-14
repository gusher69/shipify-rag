# Customer UAT Case Map — all 69 cases (Phase 6, Phase B)

Generated from `tests/customer_uat/.gate_scratch/baseline_results.json` after this pass's fixes. Every failing case is explained individually; none is summarised only by aggregate category.

**PASS 50 / 69**

| CASE_ID | PASS/FAIL | FEEDBACK_ID | Root cause class | Expected route | Current route | Next action | Blocks release? |
|---|---|---|---|---|---|---|---|
| CUS-G01 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G02 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G03 | PASS | — | — | ERP | WORKFLOW | — | NO |
| CUS-G04 | PASS | — | — | RAG | GENERAL | — | NO |
| CUS-G05 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G06 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G07 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G08 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G09 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G10 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G11 | FAIL | AI-API-S1/S2 (see inventory) | API | ERP | WORKFLOW | build the endpoint (customer dev team); conversation already safe — see tests/test_phase6_api_gap_fallback.py | NO (not a code defect) |
| CUS-G12 | PASS | — | — | ERP | WORKFLOW | — | NO |
| CUS-G13 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G14 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G15 | PASS | — | — | RAG | GENERAL | — | NO |
| CUS-G16 | PASS | — | — | ERP | WORKFLOW | — | NO |
| CUS-G17 | FAIL | AI-API-S1/S2 (see inventory) | API | ERP | WORKFLOW | build the endpoint (customer dev team); conversation already safe — see tests/test_phase6_api_gap_fallback.py | NO (not a code defect) |
| CUS-G18 | FAIL | AI-API-S1/S2 (see inventory) | API | ERP | WORKFLOW | build the endpoint (customer dev team); conversation already safe — see tests/test_phase6_api_gap_fallback.py | NO (not a code defect) |
| CUS-G19 | FAIL | AI-API-S2 (Human CS rows) | HUMAN_CS | HUMAN_CS | RAG | collect-then-escalate is the customer's own specified behaviour; harness scores turn 1 only | NO (not a code defect) |
| CUS-G20 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G21 | FAIL | — | SOURCE_CONFLICT | ERP | RAG | code fix required | YES |
| CUS-G22 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G23 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G24 | PASS | — | — | RAG | GENERAL | — | NO |
| CUS-G25 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G26 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G27 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G28 | PASS | — | — | RAG | RAG | — | NO |
| CUS-G29 | PASS | — | — | NA | WORKFLOW | — | NO |
| CUS-S01 | PASS | — | — | ERP | WORKFLOW | — | NO |
| CUS-S02 | FAIL | AI-API-S1/S2 (see inventory) | API | ERP | WORKFLOW | build the endpoint (customer dev team); conversation already safe — see tests/test_phase6_api_gap_fallback.py | NO (not a code defect) |
| CUS-S03 | FAIL | AI-API-S1/S2 (see inventory) | API | ERP | WORKFLOW | build the endpoint (customer dev team); conversation already safe — see tests/test_phase6_api_gap_fallback.py | NO (not a code defect) |
| CUS-S04 | FAIL | AI-API-S1/S2 (see inventory) | API | ERP | WORKFLOW | build the endpoint (customer dev team); conversation already safe — see tests/test_phase6_api_gap_fallback.py | NO (not a code defect) |
| CUS-S05 | FAIL | AI-API-S1/S2 (see inventory) | API | ERP | WORKFLOW | build the endpoint (customer dev team); conversation already safe — see tests/test_phase6_api_gap_fallback.py | NO (not a code defect) |
| CUS-S06 | FAIL | AI-API-S2 (Human CS rows) | HUMAN_CS | HUMAN_CS | WORKFLOW | collect-then-escalate is the customer's own specified behaviour; harness scores turn 1 only | NO (not a code defect) |
| CUS-S07 | FAIL | PHASE6 | CODE | ERP | RAG | code fix required | YES |
| CUS-S08 | FAIL | AI-API-S1/S2 (see inventory) | API | ERP | WORKFLOW | build the endpoint (customer dev team); conversation already safe — see tests/test_phase6_api_gap_fallback.py | NO (not a code defect) |
| CUS-S09 | PASS | — | — | ERP | WORKFLOW | — | NO |
| CUS-S10 | FAIL | AI-API-S2 (Human CS rows) | HUMAN_CS | HUMAN_CS | WORKFLOW | collect-then-escalate is the customer's own specified behaviour; harness scores turn 1 only | NO (not a code defect) |
| CUS-S11 | FAIL | AI-API-S1/S2 (see inventory) | API | ERP | WORKFLOW | build the endpoint (customer dev team); conversation already safe — see tests/test_phase6_api_gap_fallback.py | NO (not a code defect) |
| CUS-S12 | FAIL | AI-API-S1/S2 (see inventory) | API | ERP | WORKFLOW | build the endpoint (customer dev team); conversation already safe — see tests/test_phase6_api_gap_fallback.py | NO (not a code defect) |
| CUS-S13 | FAIL | AI-API-S1/S2 (see inventory) | API | ERP | WORKFLOW | build the endpoint (customer dev team); conversation already safe — see tests/test_phase6_api_gap_fallback.py | NO (not a code defect) |
| CUS-S14 | PASS | — | — | RAG | RAG | — | NO |
| CUS-S15 | FAIL | AI-API-S1/S2 (see inventory) | API | ERP | WORKFLOW | build the endpoint (customer dev team); conversation already safe — see tests/test_phase6_api_gap_fallback.py | NO (not a code defect) |
| CUS-S16 | FAIL | AI-API-S2 (Human CS rows) | HUMAN_CS | HUMAN_CS | WORKFLOW | collect-then-escalate is the customer's own specified behaviour; harness scores turn 1 only | NO (not a code defect) |
| CUS-S17 | PASS | — | — | ERP | WORKFLOW | — | NO |
| CUS-S18 | PASS | — | — | ERP | WORKFLOW | — | NO |
| CUS-S20a | PASS | — | — | NA | WORKFLOW | — | NO |
| CUS-S20b | PASS | — | — | NA | WORKFLOW | — | NO |
| CUS-F01 | PASS | — | — | RAG | RAG | — | NO |
| CUS-F02 | PASS | — | — | RAG | RAG | — | NO |
| CUS-F03 | PASS | — | — | RAG | RAG | — | NO |
| CUS-F04 | PASS | — | — | RAG | RAG | — | NO |
| CUS-F05 | PASS | — | — | RAG | RAG | — | NO |
| CUS-F06 | PASS | — | — | RAG | RAG | — | NO |
| CUS-F07 | PASS | — | — | RAG | RAG | — | NO |
| CUS-F08 | PASS | — | — | RAG | RAG | — | NO |
| CUS-F09 | PASS | — | — | RAG | RAG | — | NO |
| CUS-F10 | PASS | — | — | RAG | RAG | — | NO |
| CUS-F11 | PASS | — | — | RAG | RAG | — | NO |
| CUS-SC1 | PASS | — | — | RAG | RAG | — | NO |
| CUS-SC2 | PASS | — | — | ERP | WORKFLOW | — | NO |
| CUS-SC3 | PASS | — | — | CLARIFY | WORKFLOW | — | NO |
| CUS-P07 | PASS | — | — | RAG | GENERAL | — | NO |
| CUS-P06 | FAIL | — | TEST_ENV | HUMAN_CS | RAG | harness limitation; verify on the live tier | NO (not a code defect) |
| CUS-P20 | PASS | — | — | WORKFLOW | WORKFLOW | — | NO |
| CUS-RL-genuine_continuation | PASS | — | — | ERP | API | — | NO |
| CUS-RL-p0_01_repeat_after_completed_cycle | PASS | — | — | WORKFLOW | API | — | NO |
| CUS-RL-p0_01_stale_cycle | PASS | — | — | WORKFLOW | API | — | NO |

## Why the failing cases are not all code defects

The harness's `erp_action` dimension asserts that a real ERP action executed. For 12 of the failures the endpoint the customer's own requirements document asks their dev team to BUILD does not exist yet, so no code change in this repository can satisfy that dimension. What IS in our control — the conversation while the endpoint is missing — is asserted separately and passes for every one of them (`tests/test_phase6_api_gap_fallback.py`: answer-bearing, no false action completion, no invented private state, always progresses, no loop).
