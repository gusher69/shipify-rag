"""Tests for services/hybrid_question_classifier.py (Hybrid Question
Segmentation sprint, 2026-08-02) — the deterministic, Registry-driven
classifier that lets AI Playground's Auto mode select RAG_ONLY / ERP_ONLY
/ HYBRID / CLARIFICATION_REQUIRED / UNKNOWN, and the question segmenter
used for Hybrid execution. Uses the same _FakeSupabase/BusinessActionRegistry
convention as tests/test_decision_engine.py — never a real DB/network call.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry
from services.hybrid_question_classifier import classify_question, _split_clauses, _segment_by_value


def _seed_action(reg, *, key, action_type="API", category=None, ai_description="", keywords=None,
                  enabled=True, priority=0, params=None):
    action = reg.create({
        "action_key": key, "name": key, "display_name": key, "action_type": action_type,
        "category": category, "ai_description": ai_description, "search_keywords": keywords or [],
        "enabled": enabled, "priority": priority,
    })
    if params:
        reg.replace_parameters(action["id"], params)
    return action["id"]


class TestClauseSegmentation(unittest.TestCase):
    def test_splits_on_thai_conjunction(self):
        clauses = _split_clauses("ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร")
        self.assertEqual(clauses, ["ลูกค้า C00001 มีคูปองอะไร", "คูปองใช้งานอย่างไร"])

    def test_no_conjunction_returns_single_clause(self):
        clauses = _split_clauses("ขอดูข้อมูลลูกค้ารหัส C00001")
        self.assertEqual(clauses, ["ขอดูข้อมูลลูกค้ารหัส C00001"])

    def test_segment_by_value_matches_the_spec_example(self):
        result = _segment_by_value("ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", "C00001")
        self.assertEqual(result["erp_sub_question"], "ลูกค้า C00001 มีคูปองอะไร")
        self.assertEqual(result["rag_sub_question"], "คูปองใช้งานอย่างไร")

    def test_segment_returns_none_when_not_separable(self):
        self.assertIsNone(_segment_by_value("ขอดูข้อมูลลูกค้ารหัส C00001", "C00001"))

    def test_segment_returns_none_when_value_not_in_any_clause(self):
        # Conjunction present but the matched value isn't literally in
        # either clause -> cannot safely attribute a clause to ERP.
        self.assertIsNone(_segment_by_value("ขอทราบนโยบาย และเวลาทำการ", "C00001"))


class TestClassifyQuestion(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())

    def _seed_customer_lookup(self):
        action_id = _seed_action(self.reg, key="get_customer_coupons", category="customer",
                                  keywords=["คูปอง", "ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False,
             "input_source": "customer_message", "validation_pattern": r"^C\d+$"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode"]},
        ])
        return action_id

    def test_empty_message_is_unknown(self):
        result = classify_question("", self.reg)
        self.assertEqual(result["classification"], "UNKNOWN")

    def test_greeting_only_is_unknown(self):
        result = classify_question("สวัสดีค่ะ", self.reg)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertIsNone(result["selected_action_id"])

    def test_no_business_action_match_is_rag_only(self):
        self._seed_customer_lookup()
        result = classify_question("ขอทราบนโยบายการคืนสินค้า", self.reg)
        self.assertEqual(result["classification"], "RAG_ONLY")
        self.assertIsNone(result["selected_action_id"])
        self.assertEqual(result["rag_sub_question"], "ขอทราบนโยบายการคืนสินค้า")

    def test_action_matched_no_parameter_value_is_erp_only(self):
        action_id = self._seed_customer_lookup()
        result = classify_question("สวัสดีค่ะ ช่วยดูข้อมูลลูกค้าให้หน่อย", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["selected_action_id"], action_id)
        self.assertIsNone(result["rag_sub_question"])

    def test_single_clause_with_value_is_erp_only_not_hybrid(self):
        self._seed_customer_lookup()
        result = classify_question("ขอดูข้อมูลลูกค้ารหัส C00001", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["erp_sub_question"], "ขอดูข้อมูลลูกค้ารหัส C00001")

    def test_compound_question_is_hybrid_and_segments(self):
        action_id = self._seed_customer_lookup()
        result = classify_question("ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", self.reg)
        self.assertEqual(result["classification"], "HYBRID")
        self.assertEqual(result["selected_action_id"], action_id)
        self.assertEqual(result["erp_sub_question"], "ลูกค้า C00001 มีคูปองอะไร")
        self.assertEqual(result["rag_sub_question"], "คูปองใช้งานอย่างไร")
        self.assertGreater(result["confidence"], 0.0)

    def test_ambiguous_actions_require_clarification(self):
        _seed_action(self.reg, key="action_one", category="customer", keywords=["ข้อมูลลูกค้า"])
        _seed_action(self.reg, key="action_two", category="customer", keywords=["ข้อมูลลูกค้า"])
        result = classify_question("ข้อมูลลูกค้า", self.reg)
        self.assertEqual(result["classification"], "CLARIFICATION_REQUIRED")
        self.assertEqual(len(result["candidate_action_ids"]), 2)
        self.assertIsNone(result["selected_action_id"])

    def test_forced_action_id_skips_detection_but_still_segments(self):
        action_id = self._seed_customer_lookup()
        result = classify_question("ลูกค้า C00001 มีคูปองอะไร และคูปองใช้งานอย่างไร", self.reg,
                                     forced_action_id=action_id)
        self.assertEqual(result["erp_sub_question"], "ลูกค้า C00001 มีคูปองอะไร")
        self.assertEqual(result["rag_sub_question"], "คูปองใช้งานอย่างไร")

    def _seed_tracking_vs_shipment_list(self):
        tracking_id = _seed_action(self.reg, key="search_tracking", category="shipment", keywords=["tracking"])
        self.reg.replace_parameters(tracking_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^C\d+$"},
            {"name": "Tracking", "display_name": "เลข Tracking", "required": True,
             "input_source": "customer_message"},
        ])
        list_id = _seed_action(self.reg, key="search_shipment_list", category="shipment", keywords=["tracking"])
        self.reg.replace_parameters(list_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^C\d+$"},
        ])
        return tracking_id, list_id

    def test_decisive_parameter_evidence_breaks_a_keyword_tie(self):
        """Final Conversational Correctness (2026-08-15) — a specific
        Tracking-shaped value decisively selects the tracking action even
        though both candidates tie on the bare "tracking" keyword."""
        tracking_id, list_id = self._seed_tracking_vs_shipment_list()
        result = classify_question("เช็ก tracking TRACK123456 ของลูกค้า C00001", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["selected_action_id"], tracking_id)

    def test_shared_identifier_alone_still_requires_clarification(self):
        """A bare CustCode both tied actions require identically is never
        discriminating on its own — must still ask for clarification, not
        guess based on which action happens to have a keyword edge."""
        self._seed_tracking_vs_shipment_list()
        result = classify_question("ขอดู tracking ของผม C00001", self.reg)
        self.assertEqual(result["classification"], "CLARIFICATION_REQUIRED")
        self.assertEqual(len(result["candidate_action_ids"]), 2)

    def test_registry_failure_falls_back_to_rag_only(self):
        class _BrokenRegistry:
            def enabled_actions(self):
                raise RuntimeError("db down")

            def get_full(self, *a, **k):
                return None

        result = classify_question("ขอทราบนโยบาย", _BrokenRegistry())
        self.assertEqual(result["classification"], "RAG_ONLY")


class TestShipmentListKeywordCoverage(unittest.TestCase):
    """Customer-Reported ERP Conversation Defect (2026-08-19) — a known
    customer's personal shipping-cost / arrival-status question
    (GOLDEN-059-SHIPMENT-COST-AND-ARRIVAL-NATURAL-LANGUAGE) fell through to
    RAG_ONLY and answered with a generic rate FORMULA instead of the
    customer's own real shipment record, because searchdatashipmentlist —
    the Business Action that actually has this data (its response_mapping
    already includes the latest shipment's real total cost and arrival
    date) — had only 5 narrow keywords and zero example questions, so
    _keyword_score's substring match never fired. Fixed as a Business
    Action CONFIGURATION change on the live registry (never a Decision
    Engine code change) — this test seeds a fake action with the EXACT
    keyword set now configured in production, so a regression here (the
    keyword list drifting back to something too narrow) is caught by
    `python -m unittest discover -s tests`, not just eyeballed live."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        # The exact keyword list configured on searchdatashipmentlist in
        # production, MINUS the bare "บิลขนส่ง" keyword removed 2026-08-24
        # (Shipping Address Change Intent Ambiguity fix) — that single
        # generic keyword (substring-matches inside ANY sentence merely
        # mentioning a shipping bill, Thai keywords being plain substring
        # matches with no word boundary) tied 1.0-for-1.0 against
        # requestshippingaddresschange's own keyword on the exact phrase
        # "ต้องการเปลี่ยนที่อยู่บิลขนส่ง", forcing CLARIFICATION_REQUIRED on
        # a customer's unambiguous address-change request. Every OTHER
        # keyword below already covers this action's real shipment-status
        # intents (พัสดุ/tracking/บิลขนส่งล่าสุด/เมื่อไหร่จะถึง/...) without
        # the bare generic term — see TestShipmentAddressChangeDisambiguation
        # below for the regression coverage this fix is protecting.
        self.action_id = _seed_action(
            self.reg, key="searchdatashipmentlist", category="Customer Shipment Retrieval",
            ai_description="ค้นหารายการบิลขนส่งของลูกค้า โดยค้นหาจากรหัสลูกค้า",
            keywords=[
                "พัสดุ", "tracking", "shipment list", "ติดตามพัสดุ",
                "ค่าขนส่งเท่าไหร่", "ค่าส่งเท่าไหร่", "ค่าส่งล่าสุด", "บิลขนส่งล่าสุด",
                "ถูกที่สุด", "ถูกกว่า", "เมื่อไหร่จะถึง", "ถึงไทยหรือยัง", "ถึงหรือยัง",
                "มาถึงหรือยัง", "ของถึงไหนแล้ว", "พัสดุล่าสุด", "การจัดส่งล่าสุด",
                "มาถึง", "จะมาถึง",
            ],
        )
        self.reg.replace_parameters(self.action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d+$"},
        ])

    def test_personal_cheapest_shipping_cost_question_is_erp_not_rag(self):
        result = classify_question(
            "ของมาถึงแล้วช่วยคำนวณค่าขนส่งในไทยหน่อยได้ไหมว่าอะไรถูกที่สุด", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["selected_action_id"], self.action_id)

    def test_personal_arrival_status_question_is_erp_not_rag(self):
        result = classify_question("ช่วยเช็คบิลสั่งซื้อล่าสุดหน่อยว่าเมื่อไหร่จะมาถึง", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["selected_action_id"], self.action_id)

    def test_generic_rate_formula_question_still_stays_rag_only(self):
        """Regression guard: the new personal-phrasing keywords must never
        capture the GENERIC "how is the rate calculated" FAQ question —
        that one has no personal/record framing and must keep answering
        from the static rate policy in RAG, not a specific customer's
        shipment record."""
        result = classify_question("ค่าขนส่งคิดยังไง", self.reg)
        self.assertEqual(result["classification"], "RAG_ONLY")
        self.assertIsNone(result["selected_action_id"])

    def test_unrelated_question_still_stays_rag_only(self):
        result = classify_question("โกดังจีนอยู่ที่ไหน", self.reg)
        self.assertEqual(result["classification"], "RAG_ONLY")
        self.assertIsNone(result["selected_action_id"])


class TestShipmentAddressChangeDisambiguation(unittest.TestCase):
    """Shipping Address Change Intent Ambiguity fix (2026-08-24) —
    confirmed live on production: "ต้องการเปลี่ยนที่อยู่บิลขนส่ง" (an
    unambiguous request to change a delivery address) returned
    CLARIFICATION_REQUIRED instead of decisively selecting
    requestshippingaddresschange. Root cause: searchdatashipmentlist's own
    search_keywords included the bare, generic "บิลขนส่ง" ("shipping
    bill") — Thai keywords are plain substring matches with no word
    boundary (by design, Thai has none), so that single generic keyword
    matched inside ANY sentence merely mentioning a shipping bill,
    including the address-change phrase's own tail, tying 1.0-for-1.0
    against requestshippingaddresschange's own keyword for the same
    message. Fixed as a Business Action CONFIGURATION change only (the
    bare "บิลขนส่ง" keyword removed from searchdatashipmentlist) — never a
    Decision Engine code change, never a hardcoded phrase check. Both
    actions here are seeded with their real, current production keyword
    lists (searchdatashipmentlist's post-fix) so a keyword drifting back
    to something this generic is caught by the test suite, not just
    eyeballed live."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.shipment_id = _seed_action(
            self.reg, key="searchdatashipmentlist", category="Customer Shipment Retrieval",
            ai_description="ค้นหารายการบิลขนส่งของลูกค้า โดยค้นหาจากรหัสลูกค้า",
            keywords=[
                "พัสดุ", "tracking", "shipment list", "ติดตามพัสดุ",
                "ค่าขนส่งเท่าไหร่", "ค่าส่งเท่าไหร่", "ค่าส่งล่าสุด", "บิลขนส่งล่าสุด",
                "ถูกที่สุด", "ถูกกว่า", "เมื่อไหร่จะถึง", "ถึงไทยหรือยัง", "ถึงหรือยัง",
                "มาถึงหรือยัง", "ของถึงไหนแล้ว", "พัสดุล่าสุด", "การจัดส่งล่าสุด",
                "มาถึง", "จะมาถึง",
            ],
        )
        self.reg.replace_parameters(self.shipment_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d+$"},
        ])
        self.address_change_id = _seed_action(
            self.reg, key="requestshippingaddresschange", category="Customer Support Request",
            ai_description="รับคำขอเปลี่ยนที่อยู่จัดส่ง/ที่อยู่รับสินค้าจากลูกค้า แล้วแจ้งเจ้าหน้าที่ให้ดำเนินการแก้ไขใน ERP",
            keywords=[
                "ต้องการเปลี่ยนที่อยู่บิลขนส่ง", "อยากเปลี่ยนที่อยู่จัดส่ง", "แก้ที่อยู่จัดส่งยังไง",
                "เปลี่ยนที่อยู่รับของ", "เปลี่ยนที่อยู่รับสินค้า", "ขอเปลี่ยนที่อยู่บิล",
                "แก้ไขที่อยู่จัดส่ง", "เปลี่ยนที่อยู่ของผม", "เปลี่ยนที่อยู่จัดส่งในไทย",
                # Added 2026-08-24 — a second, distinct gap found while
                # reproducing the reported ambiguity: "ช่วยเปลี่ยนที่อยู่
                # จัดส่งให้หน่อย" matched NO existing keyword at all (every
                # keyword above requires a specific prefix/suffix word —
                # "อยาก"/"แก้ไข"/"ในไทย" — this phrasing has none of them),
                # scoring 0 and falling through to RAG_ONLY. This bare core
                # phrase closes that gap without colliding with any
                # shipment-status keyword (verified: none of
                # searchdatashipmentlist's own keywords contain it).
                "เปลี่ยนที่อยู่จัดส่ง",
            ],
        )

    # The exact phrase confirmed live to fail before this fix.
    def test_exact_reported_phrase_selects_address_change_decisively(self):
        result = classify_question("ต้องการเปลี่ยนที่อยู่บิลขนส่ง", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["selected_action_id"], self.address_change_id)

    def test_alternate_address_change_phrasing_1(self):
        result = classify_question("อยากเปลี่ยนที่อยู่จัดส่ง", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["selected_action_id"], self.address_change_id)

    def test_alternate_address_change_phrasing_2(self):
        result = classify_question("ขอเปลี่ยนที่อยู่รับของ", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["selected_action_id"], self.address_change_id)

    def test_alternate_address_change_phrasing_3(self):
        result = classify_question("ช่วยเปลี่ยนที่อยู่จัดส่งให้หน่อย", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["selected_action_id"], self.address_change_id)

    # Competing shipment-status intents must still resolve to
    # searchdatashipmentlist, never accidentally shift toward
    # requestshippingaddresschange now that the tie is broken.
    def test_shipment_status_phrase_1_still_selects_shipment_list(self):
        result = classify_question("พัสดุล่าสุดถึงไหนแล้ว", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["selected_action_id"], self.shipment_id)

    def test_shipment_status_phrase_2_still_selects_shipment_list(self):
        result = classify_question("ของผมล่าสุดค่าส่งเท่าไหร่", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["selected_action_id"], self.shipment_id)

    def test_shipment_status_phrase_3_still_selects_shipment_list(self):
        result = classify_question("บิลล่าสุดจะมาถึงเมื่อไหร่", self.reg)
        self.assertEqual(result["classification"], "ERP_ONLY")
        self.assertEqual(result["selected_action_id"], self.shipment_id)

    # China warehouse question must still fall through to RAG for either
    # action — never claimed by shipment-list or address-change.
    def test_china_warehouse_question_stays_rag_only(self):
        result = classify_question("ที่อยู่โกดังจีนอยู่ที่ไหน", self.reg)
        self.assertEqual(result["classification"], "RAG_ONLY")
        self.assertIsNone(result["selected_action_id"])


class TestLooseMarkerSegmentation(unittest.TestCase):
    """Golden Application Defect Fixes (2026-08-16) — GOLDEN-026 root
    cause: bare "แล้ว" (never "และ"/"แล้วก็"/any of the other FIXED
    _SEGMENT_CONJUNCTIONS) is the single most common way a Thai customer
    joins two related questions in one message, but it's heavily
    overloaded — also an ordinary temporal/completion particle with no
    second question at all. See _LOOSE_SEGMENT_MARKERS' own docstring for
    why it's only trusted as a clause boundary when the candidate RAG
    clause independently carries its own question evidence."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())

    def _seed_customer_lookup(self, key="get_customer_coupons", keywords=None):
        action_id = _seed_action(self.reg, key=key, category="customer",
                                  keywords=keywords or ["คูปอง", "ข้อมูลลูกค้า"])
        self.reg.replace_parameters(action_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False,
             "input_source": "customer_message", "validation_pattern": r"^C\d+$"},
        ])
        self.reg.set_parameter_groups(action_id, [
            {"name": "identifier_group", "rule": "AT_LEAST_ONE", "members": ["CustCode"]},
        ])
        return action_id

    def test_bare_laew_with_question_marker_segments_as_hybrid(self):
        """The exact GOLDEN-026 pattern — 'มีคูปองอะไรบ้าง แล้วคูปองใช้งาน
        ยังไง' never contains 'และ'/'แล้วก็'/any fixed conjunction at all,
        so the ORIGINAL classifier (fixed conjunctions only) never split
        it — it stayed one clause, so segmentation returned None and the
        turn fell to ERP_ONLY instead of HYBRID."""
        action_id = self._seed_customer_lookup()
        result = classify_question("ลูกค้า C00001 มีคูปองอะไรบ้าง แล้วคูปองใช้งานยังไง", self.reg)
        self.assertEqual(result["classification"], "HYBRID")
        self.assertEqual(result["selected_action_id"], action_id)
        self.assertEqual(result["erp_sub_question"], "ลูกค้า C00001 มีคูปองอะไรบ้าง")
        self.assertEqual(result["rag_sub_question"], "คูปองใช้งานยังไง")

    def test_wallet_top_up_variant_segments_as_hybrid(self):
        action_id = self._seed_customer_lookup(key="get_wallet", keywords=["wallet", "เติมเงิน"])
        result = classify_question("ลูกค้า C00001 มี wallet เท่าไหร่ แล้วเติมเงินยังไง", self.reg)
        self.assertEqual(result["classification"], "HYBRID")
        self.assertEqual(result["selected_action_id"], action_id)
        self.assertEqual(result["rag_sub_question"], "เติมเงินยังไง")

    def test_bare_laew_without_question_marker_stays_single_intent(self):
        """'แล้ว' used purely as a completion particle ("already done"),
        no genuine second question follows it — must NOT be forced into
        HYBRID off the marker alone (the exact over-triggering the task
        brief explicitly warns against: "Do not make every sentence
        containing แล้ว Hybrid")."""
        self._seed_customer_lookup()
        result = classify_question("ลูกค้า C00001 เช็คคูปองให้แล้วนะ", self.reg)
        self.assertNotEqual(result["classification"], "HYBRID")

    def test_leading_laew_with_no_second_clause_stays_single_intent(self):
        self._seed_customer_lookup()
        result = classify_question("แล้วลูกค้า C00001 มีคูปองไหม", self.reg)
        self.assertNotEqual(result["classification"], "HYBRID")

    def test_fixed_conjunction_path_still_wins_over_loose_fallback(self):
        """A message with BOTH a fixed conjunction ("และ") and no bare
        "แล้ว" must still segment via the original, unmodified path —
        regression guard that the loose fallback never interferes with
        the existing, already-passing behavior (GOLDEN-025)."""
        action_id = self._seed_customer_lookup(keywords=["คูปอง", "ข้อมูลลูกค้า", "กระเป๋าเงิน"])
        result = classify_question("ลูกค้ารหัส C00001 กระเป๋าเงินเหลือเท่าไหร่ และ CBM คำนวณยังไง", self.reg)
        self.assertEqual(result["classification"], "HYBRID")
        self.assertEqual(result["selected_action_id"], action_id)
        self.assertEqual(result["rag_sub_question"], "CBM คำนวณยังไง")


if __name__ == "__main__":
    unittest.main()
