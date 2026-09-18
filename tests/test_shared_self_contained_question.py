# -*- coding: utf-8 -*-
"""SYSTEMIC AUDIT FIX (2026-09-18) -- rag.query_resolution.is_self_
contained_question() is the ONE shared detector for "does this Thai
message stand on its own as a complete question, or is it a bare value/
continuation fragment", replacing two previously-separate, independently
incomplete particle regexes (rag/query_resolution.py's own
_LIST_CONT_STOP_RE and rag/clarification_state.py's own
_SELF_CONTAINED_QUESTION_RE). Each was missing a DIFFERENT Thai question
particle ("ไหน" vs "หรอ"), producing the same customer-visible defect
(a genuine new question misread as a bare continuation of stale context)
twice, in two different files. This test file locks in the shared
detector's own behavior directly, independent of either call site."""
import unittest

from rag.query_resolution import is_self_contained_question


class SelfContainedQuestions(unittest.TestCase):
    """Every example from the systemic audit's own PHASE 1 + CONTRAST
    TESTS list must be recognised as a complete, self-contained question."""

    def test_audit_phase1_examples(self):
        for text in (
            "ต้องแจ้งรหัสด้วยหรอคะ", "นำเข้าอะไรได้บ้าง", "ของถึงวันไหน",
            "ส่งทางไหน", "อยู่ที่ไหน", "ทำไมต้องขอรหัส", "ราคาเท่าไหร่",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_self_contained_question(text), text)

    def test_audit_contrast_questions(self):
        for text in ("ตรงไหน", "ที่ไหน", "วันไหน", "ทางไหน", "ทำไม",
                    "ต้องแจ้งรหัสด้วยหรอคะ"):
            with self.subTest(text=text):
                self.assertTrue(is_self_contained_question(text), text)

    def test_meta_auth_phrasings(self):
        for text in ("ทำไมต้องขอรหัส", "ไม่แจ้งรหัสได้ไหม",
                    "ถามทั่วไปต้องบอกรหัสไหม", "ต้องใช้รหัสลูกค้าด้วยเหรอ",
                    "นำเข้าอะไรได้บ้างอะ"):
            with self.subTest(text=text):
                self.assertTrue(is_self_contained_question(text), text)

    def test_question_mark_alone_is_sufficient(self):
        self.assertTrue(is_self_contained_question("แล้วอันนี้?"))

    def test_empty_or_none_is_not_a_question(self):
        self.assertFalse(is_self_contained_question(""))
        self.assertFalse(is_self_contained_question(None))


class BareContinuationsStillWork(unittest.TestCase):
    """Every example from the systemic audit's own CONTRAST TESTS
    "genuine continuation" list must NOT be flagged as a question."""

    def test_audit_contrast_continuations(self):
        for text in ("น้ำปลา", "ซีอิ๊ว", "20 ชิ้น", "SP1008", "ทางเรือ",
                    "40 x 30 x 20 ซม.", "เครื่องครัว"):
            with self.subTest(text=text):
                self.assertFalse(is_self_contained_question(text), text)

    def test_short_verb_object_slot_replies_are_not_misread_as_questions(self):
        """A verb-bearing 2-token micro-reply answering a pending method/
        identifier ask ("ส่งทางเรือ", "เอาทางเรือ") must stay a bare
        continuation -- the verb-bearing-clause structural fallback only
        applies to a genuine multi-word clause (>= 3 content tokens), and
        excludes common value-supplying lead-in verbs, specifically so it
        can never regress an ordinary short slot-filling reply."""
        for text in ("ส่งทางเรือ", "เอาทางเรือ", "เอา SP1008"):
            with self.subTest(text=text):
                self.assertFalse(is_self_contained_question(text), text)


if __name__ == "__main__":
    unittest.main()
