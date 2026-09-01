"""P7.1 — an unsupported company GUARANTEE / RESPONSIBILITY yes-no
question must not get a synthesized YES or NO company policy.

Unit-level: the _COMPANY_GUARANTEE_Q_RE classifier + the exact-FAQ gate
used by the orchestrator branch. The full answer shape (grounded
no-information vs synthesized) is verified end-to-end separately.
"""
import unittest

from services.playground_orchestrator import _COMPANY_GUARANTEE_Q_RE


def _is_guarantee_q(q):
    return bool(_COMPANY_GUARANTEE_Q_RE.search(q))


class GuaranteeQuestionClassifier(unittest.TestCase):
    GUARANTEE = [
        "Shipify รับประกันว่าสินค้าทุกชิ้นจะผ่านศุลกากรไหม",
        "Shipify รับผิดชอบเรื่องภาษีศุลกากรและการผ่านด่านไหม",
        "Shipify การันตีว่าของจะไม่เสียหายระหว่างขนส่งไหม",
        "ทางบริษัทรับประกันของหายไหม",
        "Shipify รับผิดชอบถ้าของถึงช้าหรือเปล่า",
    ]
    NOT_GUARANTEE = [
        "ทางรถใช้เวลากี่วัน",
        "มีขนส่งทางเครื่องบินไหม",
        "น้ำหอมนำเข้าได้ไหม",
        "จีนอยู่ทวีปอะไร",
        "Shipify ช่วยเคลมสินค้าไหม",
        "Shipify เกี่ยวข้องกับ Shopify ไหม",
        "โกดังอ่อนนุชเปิดกี่โมง",
        "ใช้คูปองยังไง",
        "ค่าขนส่งทางรถเท่าไหร่",
    ]

    def test_guarantee_questions_match(self):
        for q in self.GUARANTEE:
            self.assertTrue(_is_guarantee_q(q), msg=q)

    def test_non_guarantee_questions_do_not_match(self):
        for q in self.NOT_GUARANTEE:
            self.assertFalse(_is_guarantee_q(q), msg=q)


class BranchGate(unittest.TestCase):
    """The orchestrator uses: guarantee-question AND no chunk is_faq_exact
    -> grounded no-information (no LLM call)."""

    def _unsupported(self, q, chunks):
        return _is_guarantee_q(q) and not any(c.get("is_faq_exact") for c in chunks)

    def test_unsupported_when_no_exact_faq(self):
        self.assertTrue(self._unsupported(
            "Shipify รับประกันว่าสินค้าทุกชิ้นจะผ่านศุลกากรไหม",
            [{"is_faq_exact": None, "classification": "direct_evidence"},
             {"is_faq_exact": None}]))

    def test_supported_when_an_exact_faq_row_backs_it(self):
        self.assertFalse(self._unsupported(
            "Shipify รับผิดชอบถ้าของเสียหายไหม",
            [{"is_faq_exact": True, "text": "Question: … Answer: …"}]))

    def test_non_guarantee_question_never_gated(self):
        self.assertFalse(self._unsupported(
            "มีขนส่งทางเครื่องบินไหม", [{"is_faq_exact": None}]))
        self.assertFalse(self._unsupported(
            "น้ำหอมนำเข้าได้ไหม", [{"is_faq_exact": None}]))


if __name__ == "__main__":
    unittest.main()
