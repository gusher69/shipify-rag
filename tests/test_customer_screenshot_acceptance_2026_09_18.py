# -*- coding: utf-8 -*-
"""CUSTOMER SCREENSHOT ACCEPTANCE SUITE (2026-09-18) — SYSTEMIC AUDIT.

A read-only audit (this same day) traced a live "stale product
continuation" defect to the SAME class of root cause already fixed once
in rag/query_resolution.py (a Thai question-particle stop-list missing
one particle, "ไหน"): rag/clarification_state.py kept its OWN, separately
incomplete copy of the same kind of list, missing a DIFFERENT particle,
"หรอ". The systemic fix (this suite) is:

1. ONE shared self-contained-question detector
   (rag.query_resolution.is_self_contained_question) now backs BOTH
   call sites — see tests/test_shared_self_contained_question.py for the
   detector's own direct test coverage, and
   tests/test_clarification_state_engine.py::
   TestMetaAuthQuestionIsNotAClarificationReply for the exact reported
   scenario at the clarification-state level.

2. A new canonical public intent, META_AUTH_REQUIREMENT — a question
   about WHETHER/WHY an identifier is required is answered directly
   (public questions never need one; private/customer-specific lookups
   do) and never mistaken for a value supplied TO a pending identifier
   ask. Verified here end-to-end through the real production execution
   path, DecisionEngine.decide().

Test tier is pinned offline by tests/__init__.py -- no paid API call.
"""
import unittest

from services.decision_engine import DecisionEngine
from services.conversation_semantics import _compose

CTX = {"channel": "line", "tenant_id": "default", "sample_source": "OWNER_TEST",
       "customer_context": {}, "developer_mode": True}


def _reply(eng, text, history=None):
    r = eng.decide(text, history=history or [], context=dict(CTX))
    return (r.get("reply") or {}).get("text") or "", (r.get("developer") or {}).get("selection_source")


class TestMetaAuthRequirement(unittest.TestCase):
    """A question about needing to GIVE an identifier is public and must
    never demand the identifier itself just to explain the policy."""

    PHRASINGS = ("ต้องแจ้งรหัสด้วยหรอคะ", "ทำไมต้องขอรหัส", "ไม่แจ้งรหัสได้ไหม",
                "ถามทั่วไปต้องบอกรหัสไหม", "ต้องใช้รหัสลูกค้าด้วยเหรอ")

    def test_every_phrasing_resolves_to_the_family(self):
        for text in self.PHRASINGS:
            with self.subTest(text=text):
                self.assertEqual(_compose(text)[0], "META_AUTH_REQUIREMENT", text)

    def test_end_to_end_never_asks_for_the_identifier(self):
        eng = DecisionEngine()
        for text in self.PHRASINGS:
            with self.subTest(text=text):
                reply, src = _reply(eng, text)
                self.assertEqual(src, "phase6b_service_intent", (text, reply))
                self.assertNotIn("กรุณาแจ้งรหัส", reply, (text, reply))
                self.assertIn("ไม่จำเป็นต้องแจ้งรหัส", reply, (text, reply))

    def test_supplying_an_actual_identifier_is_never_caught_by_this_family(self):
        """A genuine identifier VALUE ("SP1008") must never be misread as
        a META_AUTH question -- the family is about ASKING whether one is
        needed, never about giving one."""
        self.assertNotEqual(_compose("SP1008")[0], "META_AUTH_REQUIREMENT")

    def test_survives_a_stale_import_interest_frame(self):
        """CUSTOMER SCREENSHOT 2026-09-18 -- the exact reported bug: right
        after an ack + transport-mode elicitation for an earlier product
        ("น้ำเชื่อม"), the customer's real meta-auth question must win,
        never resurface the stale product."""
        eng = DecisionEngine()
        history = [
            {"role": "user", "content": "น้ำเชื่อมนำเข้าได้ไหม"},
            {"role": "assistant", "content": (
                "รับทราบค่ะ เป็นน้ำเชื่อมนะคะ 😊 หากต้องการนำเข้ากับ Shipify "
                "มีบริการขนส่งทั้งทางรถและทางเรือค่ะ ต้องการขนส่งทางรถหรือทางเรือคะ")},
        ]
        for text in self.PHRASINGS:
            with self.subTest(text=text):
                reply, _ = _reply(eng, text, history)
                self.assertNotIn("น้ำเชื่อม", reply, (text, reply))
                self.assertNotIn("กรุณาแจ้งรหัส", reply, (text, reply))

    def test_survives_a_pending_different_collection(self):
        """Same P0 class as PURCHASE_BILL_PAYMENT's own regression test
        (tests/test_customer_screenshot_acceptance_2026_09_17.py) — a
        newly added family must be registered in EVERY shared 'decisive
        intent' set, or it is silently swallowed once some OTHER
        collection is already pending. Exact repro: three private-
        tracking questions exhaust the identifier-retry budget and
        escalate; the customer's NEXT, unrelated meta-auth question must
        still get its own answer."""
        eng = DecisionEngine()
        history = []
        for text in ("มีเลขแทรคไทยมั้ย", "อยากได้เลขแทรคไทย", "เชคของเข้าไทยตรงไหน"):
            reply, _ = _reply(eng, text, history)
            history = history + [{"role": "user", "content": text},
                                 {"role": "assistant", "content": reply}]
        reply, _ = _reply(eng, "ทำไมต้องขอรหัส", history)
        self.assertIn("ไม่จำเป็นต้องแจ้งรหัส", reply, reply)


class TestAmbiguousDeliveryDate(unittest.TestCase):
    """CUSTOMER SCREENSHOT 2026-09-18 -- "ชำระบิลขนส่งแล้ว สินค้าจะจัดส่ง
    ถึงบ้านวันไหน" is genuinely ambiguous between a GENERAL delivery-
    timeframe policy question and a PRIVATE "when will MY parcel arrive"
    status question. It previously reached neither the private-state
    machinery nor a grounded public answer -- it fell to Hybrid RAG's
    general LLM synthesis, an ungrounded guess with no source and no
    identity check. The fix asks ONE clarification question instead of
    guessing, then dispatches to a real KB lookup (general) or an
    identifier ask (private) -- never an invented specific date."""

    AMBIGUOUS_QUESTION = "ชำระบิลขนส่งแล้ว สินค้าจะจัดส่งถึงบ้านวันไหน"
    CLARIFY_QUESTION = "หมายถึงสอบถามระยะเวลาจัดส่งโดยทั่วไป หรือให้เช็กวันถึงของบิลของคุณคะ"

    def test_ambiguous_question_asks_one_clarification_never_guesses_a_date(self):
        eng = DecisionEngine()
        reply, src = _reply(eng, self.AMBIGUOUS_QUESTION)
        self.assertEqual(src, "delivery_date_ambiguity_clarify")
        self.assertEqual(reply, self.CLARIFY_QUESTION)

    def test_choosing_general_never_invents_a_specific_date(self):
        eng = DecisionEngine()
        history = [{"role": "user", "content": self.AMBIGUOUS_QUESTION},
                  {"role": "assistant", "content": self.CLARIFY_QUESTION}]
        reply, src = _reply(eng, "ทั่วไปค่ะ", history)
        self.assertEqual(src, "delivery_date_ambiguity_resolved_general")
        # honest no-info (no real Supabase content in this offline test
        # tier) or a real KB answer -- either way, never a fabricated date.
        self.assertNotRegex(reply, r"\d+\s*วัน", "must never invent a specific day count")

    def test_choosing_private_asks_for_the_identifier_never_guesses(self):
        eng = DecisionEngine()
        history = [{"role": "user", "content": self.AMBIGUOUS_QUESTION},
                  {"role": "assistant", "content": self.CLARIFY_QUESTION}]
        reply, src = _reply(eng, "เช็กของฉัน", history)
        self.assertEqual(src, "delivery_date_ambiguity_resolved_private")
        self.assertIn("เลขที่บิล", reply)

    def test_explicit_general_wording_is_not_ambiguous(self):
        """A question that already states it's general never triggers the
        clarification -- it is unambiguous on its own."""
        eng = DecisionEngine()
        reply, src = _reply(eng, "โดยทั่วไปจัดส่งถึงบ้านใช้เวลากี่วัน")
        self.assertNotEqual(src, "delivery_date_ambiguity_clarify")

    def test_a_message_with_its_own_identifier_stays_on_the_private_path(self):
        """A message that already carries private-state evidence of its
        own must be untouched by this new check."""
        from services.decision_engine import _classify_private_state_inquiry
        self.assertIsNotNone(_classify_private_state_inquiry("ของผมจะถึงไทยยัง"))

    def test_check_verb_request_is_unambiguously_private_not_ambiguous(self):
        """REGRESSION (found by the bounded suite, LINE-02 in
        tests/test_phase6_api_gap_fallback.py) -- an explicit check-verb
        request ("รบกวนเช็คให้หน่อยค่ะ ถึงไทยวันไหน") must never be treated
        as the general-vs-private ambiguity; it is unambiguously private
        even on phrasings _classify_private_state_inquiry itself does not
        yet recognise."""
        eng = DecisionEngine()
        reply, src = _reply(eng, "รบกวนเช็คให้หน่อยค่ะ ถึงไทยวันไหน")
        self.assertNotEqual(src, "delivery_date_ambiguity_clarify", reply)


class TestAmbiguousDeliveryDateGenericAffirmation(unittest.TestCase):
    """OWNER REAL-LINE RETEST (2026-09-18) -- a bare acknowledgement
    ("ใช่ค่ะ", "ค่ะ", "โอเค", "ได้", "อืม", "ถูก", "ประมาณนั้น") answers
    NOTHING about which of the two ambiguity readings the customer means.
    Before this fix, the FIRST clarification question ("...หรือ...คะ")
    happened to also match rag/clarification_state.py's OWN generic
    "A หรือ B" detector, so "ใช่ค่ะ" was accepted as a valid answer by
    THAT engine and fed into RAG's resolution machinery, which silently
    picked some answer by embedding similarity to the ORIGINAL question
    without ever confirming which reading the customer meant. The fix
    re-asks with explicit numbered choices instead of guessing."""

    AMBIGUOUS_QUESTION = "ชำระบิลขนส่งแล้ว สินค้าจะจัดส่งถึงบ้านวันไหน"
    CLARIFY_QUESTION = "หมายถึงสอบถามระยะเวลาจัดส่งโดยทั่วไป หรือให้เช็กวันถึงของบิลของคุณคะ"
    SHORT_CLARIFY = "ต้องการแบบไหนคะ\n1. ระยะเวลาจัดส่งโดยทั่วไป\n2. เช็กวันถึงของบิลของคุณ"

    def test_generic_affirmations_never_select_a_branch(self):
        eng = DecisionEngine()
        history = [{"role": "user", "content": self.AMBIGUOUS_QUESTION},
                  {"role": "assistant", "content": self.CLARIFY_QUESTION}]
        for text in ("ใช่", "ใช่ค่ะ", "ค่ะ", "ครับ", "โอเค", "ได้", "อืม", "ถูก", "ประมาณนั้น"):
            with self.subTest(text=text):
                reply, src = _reply(eng, text, history)
                self.assertEqual(src, "delivery_date_ambiguity_reclarify", (text, reply))
                self.assertEqual(reply, self.SHORT_CLARIFY, (text, reply))

    def test_persisted_session_affirmation_then_explicit_choice_general(self):
        eng = DecisionEngine()
        history = []
        reply1, _ = _reply(eng, self.AMBIGUOUS_QUESTION, history)
        history += [{"role": "user", "content": self.AMBIGUOUS_QUESTION},
                   {"role": "assistant", "content": reply1}]
        reply2, src2 = _reply(eng, "ใช่ค่ะ", history)
        self.assertEqual(src2, "delivery_date_ambiguity_reclarify")
        history += [{"role": "user", "content": "ใช่ค่ะ"},
                   {"role": "assistant", "content": reply2}]
        reply3, src3 = _reply(eng, "ข้อ 1", history)
        self.assertEqual(src3, "delivery_date_ambiguity_resolved_general")
        self.assertNotRegex(reply3, r"\d+\s*วัน")

    def test_persisted_session_fresh_explicit_choice_private_asks_identifier(self):
        eng = DecisionEngine()
        history = []
        reply1, _ = _reply(eng, self.AMBIGUOUS_QUESTION, history)
        history += [{"role": "user", "content": self.AMBIGUOUS_QUESTION},
                   {"role": "assistant", "content": reply1}]
        reply2, src2 = _reply(eng, "ข้อ 2", history)
        self.assertEqual(src2, "delivery_date_ambiguity_resolved_private")
        self.assertIn("เลขที่บิล", reply2)

    def test_all_valid_general_and_private_phrasings(self):
        eng_hist = [{"role": "user", "content": self.AMBIGUOUS_QUESTION},
                   {"role": "assistant", "content": self.CLARIFY_QUESTION}]
        for text in ("ทั่วไป", "ระยะเวลาทั่วไป", "ถามโดยทั่วไป", "ข้อ 1", "อันแรก"):
            with self.subTest(text=text):
                eng = DecisionEngine()
                _, src = _reply(eng, text, eng_hist)
                self.assertEqual(src, "delivery_date_ambiguity_resolved_general", text)
        for text in ("เช็กของฉัน", "ของบิลฉัน", "เช็กวันถึงของ", "ข้อ 2", "อันหลัง"):
            with self.subTest(text=text):
                eng = DecisionEngine()
                _, src = _reply(eng, text, eng_hist)
                self.assertEqual(src, "delivery_date_ambiguity_resolved_private", text)


class TestPersistedSessionFiveTurnAcceptance(unittest.TestCase):
    """The systemic audit's own PERSISTED-SESSION ACCEPTANCE script, run
    in ONE session with no reset, exactly as specified."""

    def test_exact_five_turn_script(self):
        eng = DecisionEngine()
        history = []

        reply1, _ = _reply(eng, "น้ำเชื่อมนำเข้าได้ไหม", history)
        history += [{"role": "user", "content": "น้ำเชื่อมนำเข้าได้ไหม"},
                   {"role": "assistant", "content": reply1}]
        self.assertIn("น้ำเชื่อม", reply1)

        reply2, _ = _reply(eng, "นำเข้าอะไรได้บ้าง", history)
        history += [{"role": "user", "content": "นำเข้าอะไรได้บ้าง"},
                   {"role": "assistant", "content": reply2}]

        reply3, src3 = _reply(eng, "ต้องแจ้งรหัสด้วยหรอคะ", history)
        history += [{"role": "user", "content": "ต้องแจ้งรหัสด้วยหรอคะ"},
                   {"role": "assistant", "content": reply3}]
        self.assertEqual(src3, "phase6b_service_intent")
        self.assertNotIn("น้ำเชื่อม", reply3, "no stale product continuation")
        self.assertNotIn("กรุณาแจ้งรหัส", reply3, "no identity request on meta-auth")

        reply4, _ = _reply(eng, "นำเข้าอะไรได้บ้างอะ", history)
        history += [{"role": "user", "content": "นำเข้าอะไรได้บ้างอะ"},
                   {"role": "assistant", "content": reply4}]

        reply5, src5 = _reply(eng, "ชำระบิลขนส่งแล้ว สินค้าจะจัดส่งถึงบ้านวันไหน", history)
        self.assertEqual(src5, "delivery_date_ambiguity_clarify")
        self.assertNotRegex(reply5, r"\d+\s*วัน", "no ungrounded delivery-date guess")


class TestGenuineContinuationsStillWork(unittest.TestCase):
    """CONTRAST TESTS from the systemic audit — genuine bare-value
    continuations must be unaffected by every change in this suite."""

    def test_bare_continuations_are_not_self_contained_questions(self):
        from rag.query_resolution import is_self_contained_question
        for text in ("น้ำปลา", "ซีอิ๊ว", "เครื่องครัว", "20 ชิ้น", "ทางเรือ", "SP1008"):
            with self.subTest(text=text):
                self.assertFalse(is_self_contained_question(text), text)

    def test_genuine_questions_are_not_continuations(self):
        from rag.query_resolution import is_self_contained_question
        for text in ("ตรงไหน", "ที่ไหน", "วันไหน", "ทางไหน", "ทำไม",
                    "ต้องแจ้งรหัสด้วยหรอคะ"):
            with self.subTest(text=text):
                self.assertTrue(is_self_contained_question(text), text)


if __name__ == "__main__":
    unittest.main()
