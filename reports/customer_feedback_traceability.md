# Customer Feedback Traceability Matrix — Phase 6

For the 69 pre-existing `customer_uat_master.jsonl` cases, the
case-by-case FEEDBACK-ID / ROOT CAUSE / STATUS table already exists —
`docs/customer_uat_sources/CUSTOMER_MASTER_FAILURE_INVENTORY.md`'s
69-row table serves that exact purpose (Case / Category / Expected
route / Result / Classification / Notes) and is not duplicated here to
avoid two sources of truth drifting apart. This file covers the items
added or changed BY this task.

| FEEDBACK-ID | SOURCE | ORIGINAL ISSUE | ROOT CAUSE CLASS | TEST IDs | FIXED BY COMPONENT | EXACT CASE PASS | GENERALIZATION PASS | CHANNEL PARITY | STATUS |
|---|---|---|---|---|---|---|---|---|---|
| PHASE6-SLOT-01-QTY-AS-PRODUCT | Owner REAL LINE OWNER_TEST repro | "20 คู่อยากสั่งของจากจีน" re-asked quantity it already knew | ENTITY_EXTRACTION (quantity+unit span mistaken for product noun) | `TestExactOwnerRepro`, `TestEntityExtractionUnit.test_quantity_only_opener_product_is_none`, `TestOwnerRequiredMatrix.test_A_quantity_only` | `services/playground_orchestrator.py::_product_interest_noun` (strip qty span first) + `services/conversation_semantics.py::_compose` (extract qty into entities) | YES | YES (8-unit x product matrix) | DecisionEngine direct — verified; Admin Auto / LINE webhook share the same `decide()` call, no channel-specific branch touched | FIXED |
| PHASE6-SLOT-02-PRODUCT-QTY-GLUED | Generalization matrix (owner Step 4, cases C/D/E) | product+quantity in one turn produced one corrupted glued string | ENTITY_EXTRACTION (no span segmentation) | `TestEntityExtractionUnit.test_product_then_quantity_both_captured_cleanly`, `test_quantity_leading_different_unit_and_product`, `TestUnitGeneralization.*` | same as above | YES | YES | same as above | FIXED |
| PHASE6-SLOT-03-NARROW-IMPORT-VERB | Generalization matrix (owner Step 4, cases B/C/D) | "อยากสั่งรองเท้าจากจีน" (bare order-verb + product) fell through to plain RAG, not recognized as IMPORT_INTEREST at all | INTENT (recognizer verb-list too narrow, inconsistent with the filler-strip regex's own definition of "ordering verb") | `TestEntityExtractionUnit.test_product_first_recognized_as_import_interest`, `TestOwnerRequiredMatrix.test_B/C/D` | `services/playground_orchestrator.py::_FIX23_IMPORT_VERB_RE` (bare "สั่ง" added) | YES | YES | same as above | FIXED |
| PHASE6-SLOT-04-NO-PRODUCT-ASK-PRIORITY | Owner explicit expected next-question order | ask-priority cascade never considered product missing | NEXT_BEST_ACTION | `TestAskPriority.*` | `services/conversation_semantics.py::frame_ack_reply` (product checked first) | YES | YES | Frame-ack path is channel-agnostic (used from decision_engine.py regardless of channel) | FIXED |
| PHASE6-SLOT-05-METHOD-VOCAB-NARROW | Owner explicit test matrix, case D | "ส่งเรือ" not recognized as a method, glued onto product | ENTITY_EXTRACTION | `TestEntityExtractionUnit.test_bare_method_word_song_form_recognized`, `test_product_quantity_method_all_in_one_turn`, `TestOwnerRequiredMatrix.test_D` | `services/conversation_semantics.py::_METHOD_WORD_RE` (broadened) + `_product_interest_noun` (method span stripped) | YES | partial — only "ส่งเรือ/ส่งรถ" added; "ส่งทางอากาศ" already covered via existing "ทางอากาศ" substring | same as above | FIXED |
| PHASE6-SLOT-06-OF-SUFFIX-TRUNCATION | Discovered during root-cause tracing (not in the owner's original repro) | "ชั้นวางของ" -> "ชั้นวาง" (a generic filler particle at the tail of a real compound noun has no word-boundary protection in Thai script) | ENTITY_EXTRACTION | none (deliberately not asserted as passing — see final report Technical Debt) | NOT FIXED | NO | NO | N/A | **KNOWN GAP, NOT FIXED THIS TASK** |
| ARCH-P1-P2.1A | Owner-approved phase-by-phase program, this session | no canonical, shadow-verifiable conversation state existed | STATE / CONTEXT | `test_p1_conversation_resolution.py`, `test_p2_conversation_frame.py`, `p2_stab_livetier/run_livetier.py`, `test_p2_1_shadow_telemetry.py`, `test_p2_1a_owner_test_source.py` | `conversation_resolution.py`, `conversation_frame_store.py`, `conversation_intelligence_telemetry.py`, `config.py` | YES (shadow-parity labs) | YES | Wired into LINE webhook + Admin Auto shadow-write, DecisionEngine builds it once, shared everywhere | SHIPPED, SHADOW-ONLY (no read cutover, P3 not started) |
| REAL-LINE-FIX-03..06 | Owner real-LINE repros, this session | see each fix's own test docstring | LINK / SLOT_FILLING / CORRECTION / STATE | `test_owner_real_line_fix_03..06.py` | `link_conversion_flow.py`, `decision_engine.py`, `conversation_semantics.py` | YES | YES (each fix's own test matrix) | verified via the same `WebhookConversation` production-equivalent harness | FIXED (prior to this task, carried forward here) |

No customer comment in the 80-record inventory is left as UNTESTED
without an explicit reason: the 69 pre-existing cases carry their reason
in `CUSTOMER_MASTER_FAILURE_INVENTORY.md`; the 11 new/carried-forward
records above are each either FIXED-and-tested, SHIPPED-shadow-only (by
explicit owner design, not an oversight), or an explicitly-named KNOWN
GAP with a stated reason it was not fixed this pass.
