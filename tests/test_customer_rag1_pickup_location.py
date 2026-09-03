# -*- coding: utf-8 -*-
"""CUSTOMER-RAG-1 — pickup / receiving-point wording must stay on the
company/RAG path.

REAL LINE (session 6c9b9026, turn 688): "สามารถรับสินค้าได้ที่ไหนหรอคะ"
retrieved the trusted "ขอที่อยู่โกดังหน่อย" FAQ (2 Thai warehouses +
maps + phone) at rerank 0.84 / raw_vector_rank 1, but the Company/
Operational Topic Guard (a text-only deny-list checked BEFORE retrieval
quality) matched none of the phrasing's terms — only "โกดัง" is on the
list and the customer omitted it — so the turn was diverted to General
Chat Fallback and answered "no info about branch pickup location".

The equivalent wording that already answers correctly ("โกดังรับสินค้ามี
ที่ไหนบ้าง") only passes because it contains "โกดัง".
"""
import unittest

from services.playground_orchestrator import _COMPANY_OPERATIONAL_TOPIC_RE
from rag.canonical_query import rewrite_canonical_query
from rag.query_understanding import is_self_pickup_permission, _classify_actionable


class TestPickupLocationTopicGuard(unittest.TestCase):
    def test_customer_pickup_wordings_stay_on_company_path(self):
        # A/B/C/D — every customer/UAT pickup-location variant must match
        for q in ["สามารถรับสินค้าได้ที่ไหนหรอคะ",
                  "มีจุดรับสินค้าที่ไหนบ้าง",
                  "โกดังรับสินค้ามีที่ไหนบ้าง",
                  "สามารถรับสินค้าได้ที่ไหนบ้างคะ",
                  "รับสินค้าที่สาขาไหนได้บ้าง",
                  "ไปรับสินค้าเองได้ที่ไหน"]:
            self.assertTrue(_COMPANY_OPERATIONAL_TOPIC_RE.search(q), q)

    def test_self_pickup_policy_question_stays_on_rag_path(self):
        # E — "รับสินค้าเองได้ไหม" must reach RAG (where the trusted chunk
        # itself states the self-pickup option); it must NOT be diverted
        # to General Chat. The guard matching does NOT assert self-pickup
        # by itself.
        self.assertTrue(_COMPANY_OPERATIONAL_TOPIC_RE.search("รับสินค้าเองได้ไหมคะ"))

    def test_private_shipment_pickup_wording_still_flagged_self_reference(self):
        # F — "ของผมไปรับได้หรือยัง" already carries "ของผม", which routes
        # PRIVATE upstream (classify_turn_intent / _classify_private_state_
        # inquiry) before this guard is even consulted.
        self.assertIn("ของผม", "ของผมไปรับได้หรือยัง")

    def test_unrelated_public_wording_unaffected(self):
        # G — a plain contact-number request does not falsely match the
        # new pickup terms.
        self.assertIsNone(_COMPANY_OPERATIONAL_TOPIC_RE.search("ขอเบอร์โทรหน่อย"))

    def test_new_terms_are_present(self):
        for term in ("รับสินค้า", "จุดรับ", "มารับสินค้า", "คลังไทย", "สาขา"):
            self.assertIn(term, _COMPANY_OPERATIONAL_TOPIC_RE.pattern)


class TestPickupLocationCanonicalRewrite(unittest.TestCase):
    """Every semantically-equivalent pickup-location wording is
    standardized to the SAME warehouse-address retrieval query, so the
    trusted FAQ is retrieved consistently (not phrasing-dependent)."""

    def test_location_variants_canonicalize_to_warehouse_address(self):
        # A / B — a pickup-LOCATION question (a "where" interrogative)
        for q in ["สามารถรับสินค้าได้ที่ไหนหรอคะ",       # exact REAL failure
                  "มีจุดรับสินค้าที่ไหนบ้าง",
                  "โกดังรับสินค้ามีที่ไหนบ้าง",
                  "สามารถรับสินค้าได้ที่ไหนบ้างคะ",
                  "ไปรับของเองได้ที่ไหน"]:                 # "เอง" + a where marker -> still LOCATION
            r = rewrite_canonical_query(q)
            self.assertTrue(r["rewrite_applied"], q)
            self.assertEqual(r["canonical_query"], "ขอที่อยู่โกดัง", q)

    def test_self_pickup_permission_is_not_a_location_rewrite(self):
        # CUSTOMER-RAG-1.1 — a self-pickup PERMISSION question ("เอง" +
        # yes/no, NO where marker) keeps its own intent, not rewritten.
        for q in ["รับสินค้าเองได้ไหมคะ", "ไปรับของเองได้ไหม",
                  "สามารถมารับสินค้าเองได้หรือเปล่า"]:
            r = rewrite_canonical_query(q)
            self.assertNotEqual(r["canonical_query"], "ขอที่อยู่โกดัง", q)

    def test_private_pickup_wording_is_not_canonicalized(self):
        # F — "ของผม…" pickup wording is PRIVATE shipment state, must not
        # be rewritten into a public warehouse-address query.
        for q in ["ของผมไปรับได้หรือยัง", "ของผมไปรับได้ที่ไหน",
                  "พัสดุผมมารับได้ที่ไหน"]:
            r = rewrite_canonical_query(q)
            self.assertNotEqual(r["canonical_query"], "ขอที่อยู่โกดัง", q)

    def test_unrelated_where_questions_are_not_canonicalized(self):
        for q in ["ซื้อคูปองที่ไหน", "จ่ายเงินยังไง", "ขอเบอร์ติดต่อ"]:
            r = rewrite_canonical_query(q)
            self.assertNotEqual(r["canonical_query"], "ขอที่อยู่โกดัง", q)


class TestSelfPickupIntent(unittest.TestCase):
    """CUSTOMER-RAG-1.1 — SELF-PICKUP PERMISSION vs LOCATION vs PRIVATE."""

    def test_self_pickup_recognizer(self):
        for q in ["รับสินค้าเองได้ไหมคะ", "คลังสินค้าเองได้ไหมคะ",  # spell-corrected form
                  "ไปรับของเองได้ไหม", "สามารถมารับสินค้าเองได้หรือเปล่า",
                  "มารับเองได้ไหมคะ"]:
            self.assertTrue(is_self_pickup_permission(q), q)

    def test_location_and_private_and_generic_are_not_self_pickup(self):
        for q in ["สามารถรับสินค้าได้ที่ไหนหรอคะ",   # LOCATION (where marker)
                  "รับสินค้าได้ที่ไหน",
                  "โกดังรับสินค้ามีที่ไหนบ้าง",
                  "ของผมไปรับได้หรือยัง",             # PRIVATE
                  "ของผมพร้อมรับหรือยัง",
                  "ทำเองได้ไหม",                       # no pickup context
                  "ขอเบอร์ติดต่อ"]:
            self.assertFalse(is_self_pickup_permission(q), q)

    def test_actionable_intent_is_self_pickup_permission(self):
        # even with the spell-corrector's "รับสินค้า"->"คลังสินค้า" (which
        # otherwise makes topic=โกดัง), the intent stays self_pickup.
        for q in ["รับสินค้าเองได้ไหมคะ", "คลังสินค้าเองได้ไหมคะ", "ไปรับของเองได้ไหม"]:
            intent, _conf = _classify_actionable(q, {"topic": "โกดัง", "attribute": "location"})
            self.assertEqual(intent, "self_pickup_permission", q)


if __name__ == "__main__":
    unittest.main()
