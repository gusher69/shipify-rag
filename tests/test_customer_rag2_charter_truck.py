# -*- coding: utf-8 -*-
"""CUSTOMER-RAG-2 — the charter-truck (เหมารถ / TC19) FAQ added to
Production RAG must stay faithful to the source-confirmed customer
answer. (Retrieval / answer behaviour is verified against the live RAG
after ingestion — see the phase report; this guards provenance drift.)
"""
import json
import pathlib
import unittest

from tools.seed_charter_truck_faq_tc19 import (
    QUESTION,
    ANSWER,
    ALT_QUESTIONS,
    ROW_INDEX,
    FILE_ID,
)

_MASTER = pathlib.Path(__file__).resolve().parent / "customer_uat" / "customer_uat_master.jsonl"


class TestCharterTruckFaqProvenance(unittest.TestCase):
    def test_answer_is_verbatim_from_the_source_confirmed_uat_case(self):
        rows = [json.loads(l) for l in _MASTER.read_text(encoding="utf-8").splitlines() if l.strip()]
        g19 = next(r for r in rows if r["case_id"] == "CUS-G19")
        self.assertEqual(g19["status"], "SOURCE_CONFIRMED")
        approved = g19["customer_provided_expected_answer"].strip()
        # the seeded answer must be exactly the customer-approved text
        self.assertEqual(ANSWER, approved)
        # provenance: Ai.xlsx sheet '1.thameuangton' row 19 (TC19)
        self.assertIn("row 19", g19["source"]["location"])
        self.assertEqual(ROW_INDEX, 19)

    def test_no_no_info_or_human_cs_language_in_the_answer(self):
        for bad in ("ไม่มีข้อมูล", "ยังไม่มีข้อมูล", "ไม่มีในระบบ", "ประสานเจ้าหน้าที่"):
            self.assertNotIn(bad, ANSWER)
        # it confirms the service exists and collects the 4 required inputs
        self.assertIn("มีบริการเหมารถ", ANSWER)
        for field in ("เลขบิล", "โลเคชั่นปลายทาง", "ชื่อผู้รับ", "เบอร์โทรผู้รับ"):
            self.assertIn(field, ANSWER)

    def test_paraphrases_cover_the_customer_wordings(self):
        for q in ("มีบริการเหมารถไหมคะ", "เหมารถให้ได้ไหม", "เรียกรถให้ได้ไหม",
                  "สามารถเหมารถได้ไหม"):
            self.assertIn(q, ALT_QUESTIONS)
        self.assertEqual(QUESTION, "มีบริการเหมารถไหม")
        # goes into the existing Quick_FAQ_Patch file, not a new mechanism
        self.assertEqual(FILE_ID, "40519066-2685-4d12-a7e1-92686eeeb0d7")


if __name__ == "__main__":
    unittest.main()
