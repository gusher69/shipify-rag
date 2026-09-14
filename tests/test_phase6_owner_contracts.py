# -*- coding: utf-8 -*-
"""PHASE 6 CLOSURE GATE — owner-ruled behaviour contracts (F and G).

F. CANCELLATION — two intents that must never be merged (owner ruling 1):
     POLICY     "ยกเลิกบิลสั่งซื้อได้ไหม"        -> RAG policy answer, no
                identifier demanded just to explain policy.
     OPERATION  "ช่วยยกเลิกบิล POSxxxx ให้หน่อย"  -> operational request:
                collect only the needed identifier, hand to a human, and
                NEVER say the cancellation happened.

G. INVOICE (owner ruling 3) — no new issuance flow is built, but the three
   concepts are kept separate and each has executable safe behaviour:
     how-to               -> RAG
     retrieve a document  -> only if a document capability exists, else a
                             truthful limitation / handoff
     ask staff to issue   -> never claim it was issued

The shared hard requirement in both: FALSE ACTION COMPLETION = 0.
Test tier is pinned offline by tests/__init__.py.
"""
import re
import unittest

from services.decision_engine import DecisionEngine
from services.operational_change_flow import is_operational_cancellation
from tests.test_business_action_registry import reset_real_registry
from tests.customer_uat.run_baseline import _run_one


# a PAST-TENSE completion claim about something that demonstrably did not
# execute. Future intent ("แอดมินจะดำเนินการให้นะคะ") is fine and matches
# the CS team's own scripts.
_FALSE_COMPLETION_RE = re.compile(
    r"เรียบร้อยแล้ว|ดำเนินการให้แล้ว|ดำเนินการเรียบร้อย"
    r"|(?:ยกเลิก|แก้ไข|เปลี่ยน|ลบ|คืนเงิน|ออกใบกำกับ|ออกให้)\S{0,10}(?:ให้)?(?:เรียบร้อย)?แล้ว"
    r"|ยกเลิกให้แล้ว|ออกใบกำกับให้เรียบร้อย")
_IDENTITY_ASK_RE = re.compile(r"รหัสลูกค้า|ยืนยันตัวตน|เลขสมาชิก")
_ASKS_OR_ESCALATES_RE = re.compile(
    r"รบกวน|ขอ(?:เลข|ไฟล์|ข้อมูล)|แจ้ง|ระบุ|เจ้าหน้าที่|แอดมิน|ติดต่อกลับ|\?|ไหมคะ|ไหนคะ")


def _engine():
    reset_real_registry()
    return DecisionEngine()


class TestCancellationContract(unittest.TestCase):
    """F — the two intents stay separate."""

    POLICY = ["ยกเลิกบิลสั่งซื้อได้ไหม", "ยกเลิกออเดอร์ได้ไหมคะ",
              "ยกเลิกคำสั่งซื้อได้หรือเปล่าครับ"]
    OPERATION = ["ช่วยยกเลิกบิล POS_TEST_001 ให้หน่อย",
                 "ขอให้ยกเลิกออเดอร์ POS_TEST_002 หน่อยครับ",
                 "ยกเลิกบิลสั่งซื้อ POS_TEST_003"]

    @classmethod
    def setUpClass(cls):
        cls.eng = _engine()

    def _run(self, msg):
        r = _run_one(self.eng, msg, history=None)
        return {"route": r.get("routing_type"), "src": r.get("selection_source"),
                "reply": (r.get("reply_text") or r.get("reply") or ""),
                "erp": bool(r.get("erp_called"))}

    # ── recogniser level ────────────────────────────────────────────
    def test_policy_shape_is_not_operational(self):
        for m in self.POLICY:
            self.assertFalse(is_operational_cancellation(m), m)

    def test_operation_shape_is_operational(self):
        for m in self.OPERATION:
            self.assertTrue(is_operational_cancellation(m), m)

    def test_bare_frame_cancel_is_neither(self):
        # "ไม่เอาแล้ว" / "ยกเลิก" inside an import journey is a frame
        # cancel and must not be captured by the operational collector.
        for m in ("ไม่เอาแล้ว", "ยกเลิก", "พอแล้วค่ะ"):
            self.assertFalse(is_operational_cancellation(m), m)

    # ── end-to-end ──────────────────────────────────────────────────
    def test_policy_question_stays_rag_and_asks_no_identifier(self):
        for m in self.POLICY:
            o = self._run(m)
            self.assertEqual(o["route"], "RAG", f"{m!r} -> {o['route']}")
            self.assertNotEqual(o["src"], "operational_change_collection", m)
            self.assertIsNone(_IDENTITY_ASK_RE.search(o["reply"]), m)
            self.assertIsNone(_FALSE_COMPLETION_RE.search(o["reply"]), m)

    def test_operational_request_is_recognised_and_collects(self):
        for m in self.OPERATION:
            o = self._run(m)
            self.assertEqual(o["src"], "operational_change_collection", f"{m!r} -> {o['src']}")
            self.assertTrue(_ASKS_OR_ESCALATES_RE.search(o["reply"]), m)

    def test_operational_request_never_claims_cancellation_happened(self):
        for m in self.OPERATION:
            o = self._run(m)
            self.assertFalse(o["erp"], f"{m!r} executed an ERP action unexpectedly")
            self.assertIsNone(_FALSE_COMPLETION_RE.search(o["reply"]),
                              f"{m!r} claimed completion: {o['reply'][:90]!r}")


class TestInvoiceConceptsStaySeparate(unittest.TestCase):
    """G — TRAIN-08 now has executable safe behaviour even though the
    issuance flow itself stays deferred."""

    HOWTO = "โหลดใบกำกับยังไง"
    RETRIEVE = "ขอไฟล์ใบกำกับของบิลนี้"
    ISSUE = "ช่วยออกใบกำกับให้หน่อย"

    @classmethod
    def setUpClass(cls):
        cls.eng = _engine()

    def _run(self, msg):
        r = _run_one(self.eng, msg, history=None)
        return {"route": r.get("routing_type"), "src": r.get("selection_source"),
                "reply": (r.get("reply_text") or r.get("reply") or ""),
                "erp": bool(r.get("erp_called"))}

    def test_1_howto_is_answered_by_rag(self):
        o = self._run(self.HOWTO)
        self.assertIn(o["route"], ("RAG", "GENERAL", "HYBRID"), o["route"])
        self.assertTrue(o["reply"].strip())
        self.assertIsNone(_FALSE_COMPLETION_RE.search(o["reply"]))

    def test_2_document_retrieval_is_truthful_without_the_capability(self):
        o = self._run(self.RETRIEVE)
        self.assertTrue(o["reply"].strip())
        # no document API exists -> it must not pretend one ran
        self.assertFalse(o["erp"])
        self.assertIsNone(_FALSE_COMPLETION_RE.search(o["reply"]),
                          f"claimed completion: {o['reply'][:90]!r}")

    def test_3_issuance_request_never_claims_issued(self):
        o = self._run(self.ISSUE)
        self.assertTrue(o["reply"].strip())
        self.assertFalse(o["erp"])
        self.assertIsNone(_FALSE_COMPLETION_RE.search(o["reply"]),
                          f"claimed issued: {o['reply'][:90]!r}")

    def test_the_three_concepts_are_not_collapsed_into_one_reply(self):
        """A how-to answer and an issuance request must not produce the
        identical canned reply -- that would mean the concepts merged."""
        howto = self._run(self.HOWTO)["reply"]
        issue = self._run(self.ISSUE)["reply"]
        if howto.strip() == issue.strip() and "[STUB-RAG-ANSWER]" not in howto:
            self.fail("how-to and issuance produced an identical reply")


if __name__ == "__main__":
    unittest.main()
