"""UAT / Regression Test Suite — reusable test case data (Part 1/4 of the
2026-07-29 reliability sprint).

Plain Python data structures ONLY — no DB table, no execution logic.
services/uat_runner.py imports these lists and actually runs them against
the existing, frozen RAG/ERP/Hybrid entry points
(services/playground_orchestrator.py::run_playground_turn,
services/erp_test_harness.py::run_erp_test,
admin/routes.py::hybrid_playground_ask).

Every RAG case is checked STRUCTURALLY (chunks retrieved, citation
present, confidence in a sensible range, correct "no information" /
"partial answer" behavior) rather than against a hand-written expected
answer string — this is a live LLM, an exact wording match would be
brittle and would encourage fabricating "expected" text nobody actually
verified.

Confirmed live against the real knowledge base (2 files, ~195 chunks,
Thai-language shipping/import-forwarding business content) before these
cases were written — see uat_runner.py's docstring for the exploratory
queries used to design them.
"""

# ── Part 1 — RAG test cases ─────────────────────────────────────────────
# expected_outcome: "answer_found" | "no_answer" | "low_confidence"
# checks: list of structural assertions the runner evaluates generically.

RAG_TEST_CASES = [
    {
        "id": "rag_th_answer_found",
        "category": "Thai question with answer",
        "question": "บริษัทนี้ทำธุรกิจอะไร",
        "language": "th",
        "expected_outcome": "answer_found",
        "checks": ["chunks_retrieved", "citation_present", "confidence_min:0.5", "answerability_in:direct_answer,partial_answer"],
    },
    {
        "id": "rag_en_answer_found",
        "category": "English question with answer",
        "question": "What services does this company offer?",
        "language": "en",
        "expected_outcome": "answer_found",
        "checks": ["chunks_retrieved", "answerability_in:direct_answer,partial_answer"],
    },
    {
        "id": "rag_cross_language_retrieval",
        "category": "Cross-language retrieval",
        "question": "What business is this company in?",
        "language": "en",
        "expected_outcome": "answer_found",
        "checks": ["chunks_retrieved", "answerability_in:direct_answer,partial_answer"],
        "note": "Thai-language knowledge base, English question — verifies embedding/retrieval crosses the language boundary.",
    },
    {
        "id": "rag_followup_history",
        "category": "Follow-up using conversation history",
        "question": "แล้วค่าบริการล่ะ",
        "language": "th",
        "history": [
            {"role": "user", "content": "บริษัทนี้ทำธุรกิจอะไร"},
            {"role": "assistant", "content": "บริษัทนี้มีธุรกิจหลักในการให้บริการฝากสั่ง ฝากนำเข้า และฝากโอนเงินกับร้านค้าจีนค่ะ"},
        ],
        "expected_outcome": "answer_found",
        "checks": ["chunks_retrieved"],
        "note": "Follow-up should resolve via rag/query_resolution.py's Conversation Resolver 2.0 — checked structurally (did retrieval run at all), not for exact wording.",
    },
    {
        "id": "rag_no_answer_unrelated",
        "category": "Question with no answer (out of domain)",
        "question": "วิธีทำอาหารไทยที่อร่อยที่สุดคืออะไร",
        "language": "th",
        "expected_outcome": "no_answer",
        "checks": ["answerability_is:no_information", "confidence_max:0.2"],
    },
    {
        "id": "rag_no_answer_gold_price",
        "category": "Question with no answer (unrelated domain fact)",
        "question": "ราคาทองคำวันนี้เท่าไหร่",
        "language": "th",
        "expected_outcome": "no_answer",
        "checks": ["answerability_is:no_information"],
    },
    {
        "id": "rag_low_confidence_broad",
        "category": "Low confidence (broad/ambiguous question)",
        "question": "What services does this company offer?",
        "language": "en",
        "expected_outcome": "low_confidence",
        "checks": ["confidence_range:0.3,0.75", "answerability_in:partial_answer,direct_answer"],
        "note": "Broad question observed live at confidence ~0.55/partial_answer — checked as a range, not an exact number, since a live LLM/retrieval score is not perfectly reproducible run to run.",
    },
    {
        "id": "rag_citation_correct",
        "category": "Correct citation",
        "question": "บริษัทนี้ทำธุรกิจอะไร",
        "language": "th",
        "expected_outcome": "answer_found",
        "checks": ["citation_present", "cited_source_nonempty"],
    },
    {
        "id": "rag_multiple_source_citation",
        "category": "Multiple source citation",
        "question": "บริษัทนี้ทำธุรกิจอะไร",
        "language": "th",
        "expected_outcome": "answer_found",
        "checks": ["chunks_retrieved_min:2"],
        "note": ("Live run returned 3 retrieved chunks from 2 distinct files (AI Knowledge Master (1).xlsx + "
                 "logs_live_1784000969351.txt) for this question, confirming multi-source retrieval is possible "
                 "with the current KB — checked as >=2 chunks retrieved, not a fixed count."),
    },
    {
        "id": "rag_wrong_source_detection",
        "category": "Wrong source detection",
        "question": "ราคาทองคำวันนี้เท่าไหร่",
        "language": "th",
        "expected_outcome": "no_answer",
        "checks": ["chunks_retrieved_max:0", "answerability_is:no_information"],
        "note": ("Design rationale: the live KB is shipping/import-forwarding content only. A gold-price question "
                 "has zero legitimately relevant chunks, so 'wrong source' is checked as 'the pipeline must not "
                 "retrieve/cite ANY chunk for a question with no genuine source in this KB' rather than testing "
                 "against a specific wrong document we cannot honestly construct without fabricating KB content."),
    },
    {
        "id": "rag_hallucination_prevention",
        "category": "Hallucination prevention",
        "question": "บริษัทนี้มีสาขาที่ดาวอังคารไหม",
        "language": "th",
        "expected_outcome": "no_answer",
        "checks": ["answerability_is:no_information", "no_information_phrase_present"],
        "note": ("Absurd question with no possible grounding ('does the company have a branch on Mars') — the "
                 "correct behavior is a clean 'no information' answer, never an invented answer. Checked "
                 "structurally: answerability must be no_information and the answer must not claim a fact."),
    },
]

# ── Part 2 — ERP test data ──────────────────────────────────────────────
# Actual per-action test generation happens in uat_runner.py (it needs
# the live registry to enumerate actions and their configured
# parameters/groups/conversation-behavior) — this module only holds the
# fixed sample VALUES/messages used to exercise each generic check
# category, so the same wording isn't duplicated across the runner.

ERP_SAMPLE_MESSAGES = {
    # A per-action-key deliberately-plausible "success" message. Falls
    # back to a generic templated message (built from the action's own
    # required parameter names) when a key isn't listed here.
    "customer_data_lookup": "ขอเช็คข้อมูลลูกค้ารหัส C00001",
    "search_po": "ค้นหาออเดอร์ PO12345 ของลูกค้า C00001",
    "fixture_order_lookup": "เช็คสถานะพัสดุเลขที่ ORD-1001",
    "fixture_product_lookup": "เช็คสินค้ารหัส SKU-1001",
    "fixture_invoice_lookup": "ขอดูใบแจ้งหนี้เลขที่ INV-1001",
    "fixture_cancel_order": "ขอยกเลิกออเดอร์ ORD-1001",
}

ERP_MISSING_PARAM_MESSAGE = "สวัสดีค่ะ"  # a generic greeting — should never bind any required parameter
ERP_EMPTY_MESSAGE = ""

# ── Part 3 — Hybrid Playground scenarios (spec's 3 exact scenarios) ─────

HYBRID_TEST_CASES = [
    {
        "id": "hybrid_faq_only_routes_rag",
        "category": "FAQ-only question routes to RAG",
        "question": "บริษัทนี้ทำธุรกิจอะไร",
        "mode": "auto",
        "expected_route": "rag",
    },
    {
        "id": "hybrid_shipment_only_routes_erp",
        "category": "Shipment-status-only question routes to ERP",
        "question": "เช็คสถานะพัสดุเลขที่ ORD-1001",
        "mode": "auto",
        "action_id_key": "fixture_order_lookup",
        "expected_route": "erp",
    },
    {
        "id": "hybrid_mixed_question_both_contribute",
        "category": "Mixed question in explicit Hybrid mode — both ERP and RAG contribute",
        "question": "ของผมถึงไหนแล้ว แล้วปกติใช้เวลากี่วัน",
        "mode": "hybrid",
        "action_id_key": "fixture_order_lookup",
        "expected_route": "hybrid",
    },
]
