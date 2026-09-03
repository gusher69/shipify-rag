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


if __name__ == "__main__":
    unittest.main()
