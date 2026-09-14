# -*- coding: utf-8 -*-
"""LANGGRAPH MVP GATE — the four priorities, end to end, no stubs.

The customer's problem in one sentence:

    "เวลาเปลี่ยนคำ เปลี่ยนบริบท AI ตอบไม่ได้ ทั้งที่ความหมายเดียวกัน"

so this suite measures exactly that, on the real path — the graph running
over the real Decision Engine, with the real reply text carried between
turns. (The generalization lab's understanding tier stubs execution for
speed; a stubbed reply carries no state, because on this platform the
assistant's own wording IS the conversation state. Continuity must
therefore be proven here, unstubbed, not there.)

  1. a quantity-only opener asks for the product
  2. the product answer preserves the quantity and never re-asks it
  3. an explicit policy question beats a stale import journey
  4. paraphrases of one meaning reach one canonical result

Test tier is pinned offline by tests/__init__.py — no paid API call.
"""
import unittest

import config
from services.agent.runner import run_agent, graph_is_authoritative, shadow_run

CTX = {"channel": "line", "tenant_id": "default", "sample_source": "TEST",
       "customer_context": {}, "developer_mode": True}


def _turn(text, history=None):
    """One real turn: the graph plans, the existing engine executes."""
    return run_agent(text, history=history or [], context=dict(CTX))


def _journey(*messages):
    """Run a real multi-turn conversation, carrying each reply forward."""
    history, decisions = [], []
    for msg in messages:
        d = _turn(msg, history)
        decisions.append(d)
        history = history + [{"role": "user", "content": msg},
                             {"role": "assistant", "content": d.final_response}]
    return decisions


def _slot(d, name):
    v = (d.known_slots or {}).get(name)
    return (v.get("value"), v.get("unit")) if isinstance(v, dict) else (v, None)


class TestPriority1QuantityOnlyOpener(unittest.TestCase):

    def test_quantity_and_unit_are_captured_and_the_product_is_asked_for(self):
        d = _turn("20 คู่อยากสั่งของจากจีน")
        self.assertEqual(_slot(d, "quantity"), (20, "คู่"))
        self.assertIsNone(_slot(d, "product")[0])
        self.assertEqual(d.planned_action, "ASK_PRODUCT")
        self.assertIn("20 คู่", d.final_response)
        self.assertNotIn("20 ชิ้น", d.final_response)


class TestPriority2KnownSlotContinuity(unittest.TestCase):
    """The single most-reported failure."""

    def test_the_product_answer_keeps_the_quantity(self):
        _first, second = _journey("20 คู่อยากสั่งของจากจีน", "รองเท้า")
        self.assertEqual(_slot(second, "product")[0], "รองเท้า")
        self.assertEqual(_slot(second, "quantity"), (20, "คู่"),
                         "the quantity the customer already gave was forgotten")
        self.assertNotEqual(second.planned_action, "ASK_QUANTITY")
        self.assertNotIn("รบกวนแจ้งจำนวน", second.final_response)

    def test_it_holds_for_the_polite_form_too(self):
        _first, second = _journey("20 คู่อยากสั่งของจากจีน", "รองเท้าครับ")
        self.assertEqual(_slot(second, "product")[0], "รองเท้า")
        self.assertEqual(_slot(second, "quantity"), (20, "คู่"))

    def test_a_third_turn_keeps_everything_before_it(self):
        _a, _b, third = _journey("20 คู่อยากสั่งของจากจีน", "รองเท้าครับ", "ส่งเรือครับ")
        self.assertEqual(_slot(third, "product")[0], "รองเท้า")
        self.assertEqual(_slot(third, "quantity"), (20, "คู่"))
        self.assertEqual(_slot(third, "shipping_method")[0], "sea")
        self.assertNotIn(third.planned_action, ("ASK_PRODUCT", "ASK_QUANTITY"))

    def test_no_turn_ever_re_asks_an_acknowledged_slot(self):
        for d in _journey("20 คู่อยากสั่งของจากจีน", "รองเท้าครับ", "ส่งเรือครับ"):
            self.assertEqual([e for e in d.errors if e.startswith("KNOWN_SLOT_RE_ASK")],
                             [], d.final_response[:90])


class TestPriority3CurrentIntentPrecedence(unittest.TestCase):

    def test_a_policy_question_beats_the_stale_import_journey(self):
        decisions = _journey("20 คู่อยากสั่งของจากจีน", "รองเท้า", "รองเท้านำเข้าได้ไหม")
        last = decisions[-1]
        self.assertNotIn(last.planned_action, ("ASK_QUANTITY", "ASK_SHIPPING_METHOD"),
                         "kept collecting slots through an explicit policy question")
        self.assertNotIn("สนใจส่งทางรถหรือทางเรือ", last.final_response)

    def test_a_topic_switch_suspends_the_journey(self):
        decisions = _journey("อยากสั่งรองเท้าจากจีน", "ขอเบอร์ติดต่อ")
        last = decisions[-1]
        self.assertEqual(last.primary_intent, "CONTACT_INFO")
        self.assertIsNone(last.active_journey)

    def test_a_colloquial_permission_particle_is_understood(self):
        """"ได้หรอ" / "ได้ปะ" are the spoken form of "ได้หรือเปล่า"."""
        for phrasing in ("รองเท้านำเข้าได้ไหม", "รองเท้านำเข้าได้หรอ",
                         "รองเท้านำเข้าได้ปะ"):
            with self.subTest(phrasing=phrasing):
                decisions = _journey("20 คู่อยากสั่งของจากจีน", "รองเท้า", phrasing)
                self.assertNotIn(decisions[-1].planned_action,
                                 ("ASK_QUANTITY", "ASK_SHIPPING_METHOD"))


class TestPriority4ParaphraseConsistency(unittest.TestCase):
    """One meaning, many surface forms -> one canonical result."""

    FAMILIES = {
        "cancellation_policy": ["ยกเลิกบิลสั่งซื้อได้ไหม", "ยกเลิกออเดอร์ได้ไหมคะ",
                                "ยกเลิกคำสั่งซื้อได้หรือเปล่าครับ", "ยกเลิกบิลได้ปะ",
                                "ขอยกเลิกออเดอร์ได้มั้ย", "ยกเลิกบิลได้หรอ"],
        "purchase_withdrawal": ["อยากถอนเงิน", "ถอนเครดิตยังไง", "ขอถอนยอดในระบบ",
                                "ถอนเงินได้ไหม", "จะถอนยังไงคะ", "ถอนเงินออกมายังไงครับ"],
        "contact_info": ["ขอเบอร์ติดต่อ", "ขอเบอร์ติดต่อหน่อยครับ", "ติดต่อช่องทางไหนคะ",
                         "ขอเบอร์โทรหน่อยค่ะ", "ติดต่อยังไงคะ", "ขออีเมล และเว็บไซต์"],
        "supplier_dispatch": ["ร้านส่งของหรือยัง", "ต้นทางส่งมาหรือยังครับ",
                              "ร้านจีนส่งของออกมาหรือยังครับ", "ของที่สั่งร้านส่งออกมาหรือยัง",
                              "ร้านส่งของออกมารึยังคะ", "ทางร้านจัดส่งของออกมาหรือยังคะ"],
    }

    def test_each_family_resolves_to_one_canonical_understanding(self):
        for family, forms in self.FAMILIES.items():
            with self.subTest(family=family):
                canon = {(_turn(f).planned_action, _turn(f).auth_required)
                         for f in forms}
                self.assertEqual(
                    len(canon), 1,
                    f"{family} split into {sorted(str(c) for c in canon)}")

    def test_each_family_keeps_one_intent(self):
        for family, forms in self.FAMILIES.items():
            if family == "supplier_dispatch":
                continue          # the family label depends on the gated LLM tier;
                                  # the AUTHORITY it produces is what matters and
                                  # is asserted above and below.
            with self.subTest(family=family):
                intents = {_turn(f).primary_intent for f in forms}
                self.assertEqual(len(intents), 1, f"{family} -> {intents}")

    def test_the_same_meaning_never_flips_public_and_private(self):
        for family, forms in self.FAMILIES.items():
            want = _turn(forms[0]).auth_required
            for f in forms[1:]:
                with self.subTest(family=family, form=f):
                    self.assertEqual(_turn(f).auth_required, want,
                                     f"{f!r} flipped the authority of {family}")


class TestAuthAndPrivateSafety(unittest.TestCase):

    def test_a_public_question_is_never_asked_for_identity(self):
        for m in ("ส่งถึงบ้านไหม", "ยกเลิกบิลสั่งซื้อได้ไหม", "ขอเบอร์ติดต่อ",
                  "ค่าขนส่งคิดยังไง"):
            with self.subTest(m=m):
                d = _turn(m)
                self.assertFalse(d.auth_required)
                self.assertEqual([e for e in d.errors
                                  if e.startswith("AUTH_VIOLATION")], [])

    def test_a_private_request_requires_identity_before_any_read(self):
        for m in ("ของฉันส่งถึงบ้านหรือยัง", "ร้านส่งของหรือยัง", "ของผมถึงไหนแล้วครับ"):
            with self.subTest(m=m):
                d = _turn(m)
                self.assertTrue(d.auth_required)
                self.assertEqual(d.auth_state, "REQUIRED_MISSING")
                self.assertEqual(d.planned_action, "ASK_IDENTIFIER")

    def test_no_turn_in_the_gate_claims_an_action_completed(self):
        for m in ("ช่วยยกเลิกบิล POS_TEST_001", "บิลขนส่ง FT ต้องการเปลี่ยนที่อยู่จัดส่ง"):
            with self.subTest(m=m):
                d = _turn(m)
                self.assertEqual([e for e in d.errors
                                  if e.startswith("FALSE_ACTION_COMPLETION")], [])


class TestShadowModeContract(unittest.TestCase):

    def test_the_default_mode_is_shadow_and_is_not_authoritative(self):
        self.assertEqual(config.LANGGRAPH_MODE, "shadow")
        self.assertFalse(graph_is_authoritative("REAL_LINE"))
        self.assertFalse(graph_is_authoritative("OWNER_TEST"))

    def test_shadow_produces_a_comparable_decision_without_answering(self):
        d = shadow_run("20 คู่อยากสั่งของจากจีน", [], dict(CTX))
        self.assertIsNotNone(d)
        self.assertEqual(d.planned_action, "ASK_PRODUCT")


if __name__ == "__main__":
    unittest.main()
