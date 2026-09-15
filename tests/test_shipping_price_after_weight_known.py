# -*- coding: utf-8 -*-
"""CUSTOMER 6-SOURCE CLOSURE — the price question after weight is known
maps to the SAME authoritative shipping-cost rule as AI-API-S1-4.0 /
AI-API-S1-22.0 / TRAIN-03 ("ค่าขนส่งคิดยังไง", "เรทนำเข้าเท่าไหร่") — the
rate is charged on whichever of weight-kg or CBM-volume is higher
(RATES / EstimateState.missing() in services/shipping_estimate_flow.py,
the ONE calculator this repo has). No new requirement is invented here:
once the import-interest frame already knows the weight, a price
question must ask ONLY for the other genuinely-required input
(dimensions, or method) via that SAME calculator — never re-ask weight,
never invent a number, never lose product/quantity/method/weight.

Test tier is pinned offline by tests/__init__.py — no paid API call.
"""
import unittest

from services.decision_engine import DecisionEngine
from services.shipping_estimate_flow import _method_of

CTX = {"channel": "line", "developer_mode": True, "customer_context": {},
       "tenant_id": "default", "sample_source": "REAL_LINE"}


class TestRouteAnswerRecognisesSendVerb(unittest.TestCase):
    """The multi-turn calculator's own route parser was missing "ส่ง<mode>"
    ("ส่งเรือได้ปะ") — conversation_semantics.py's sibling detector
    (_BARE_METHOD_ANSWER_RE) already recognised it; this closes the gap
    so a method the customer already gave is never lost the moment a
    price question routes the turn into the calculator."""

    def test_send_verb_forms(self):
        for text, expected in (("ส่งเรือได้ปะ", "sea"), ("ส่งเรือครับ", "sea"),
                               ("ส่งรถได้ไหม", "road"), ("ทางเรือ", "sea"), ("เรือ", "sea")):
            self.assertEqual(_method_of(text), expected, text)


class TestPriceAfterWeightKnownAsksOnlyDimensions(unittest.TestCase):
    """The exact reported production journey, one turn further: once
    price is asked with weight+method known, the bot must ask ONLY for
    dimensions (the genuinely missing input) — never weight, never
    invent a number — and completing dimensions must then produce a
    REAL computed estimate from the existing rate table."""

    def _journey(self, eng, turns):
        history, replies = [], []
        for t in turns:
            r = eng.decide(t, history=history, context=dict(CTX))
            reply = (r.get("reply") or {}).get("text") or ""
            replies.append((r, reply))
            history = history + [{"role": "user", "content": t},
                                 {"role": "assistant", "content": reply}]
        return replies

    def test_price_question_asks_only_dimensions_not_weight(self):
        eng = DecisionEngine()
        turns = ["20 คู่อยากสั่งของจากจีน", "รองเท้าครับ", "เอ้ย 15 คู่",
                "ส่งเรือได้ปะ", "10 กิโลกรัม", "แล้วราคาเท่าไหร่อะ"]
        replies = self._journey(eng, turns)
        _, price_reply = replies[-1]
        self.assertNotIn("รบกวนแจ้งน้ำหนัก", price_reply, "must never re-ask weight")
        self.assertIn("ขนาดสินค้า", price_reply, "must ask for the one genuinely missing input")
        self.assertNotRegex(price_reply, r"\d+\s*บาท", "must never invent a price")
        dev = replies[-1][0].get("developer") or {}
        self.assertEqual(dev.get("selection_source"), "frame_weight_known_price_ask")
        self.assertIn("weight", dev.get("shipping_estimate_state") or {})
        self.assertEqual((dev.get("shipping_estimate_state") or {}).get("method"), "sea")

    def test_completing_dimensions_produces_a_real_computed_estimate(self):
        eng = DecisionEngine()
        turns = ["20 คู่อยากสั่งของจากจีน", "รองเท้าครับ", "เอ้ย 15 คู่",
                "ส่งเรือได้ปะ", "10 กิโลกรัม", "แล้วราคาเท่าไหร่อะ", "40x60x30 ซม."]
        replies = self._journey(eng, turns)
        _, final_reply = replies[-1]
        self.assertRegex(final_reply, r"\d+(\.\d+)?\s*บาท", "a real number, from the real calculator")
        self.assertIn("ประเมินเบื้องต้น", final_reply)
        dev = replies[-1][0].get("developer") or {}
        self.assertEqual(dev.get("selection_source"), "shipping_estimate_flow")

    def test_no_weight_no_bridge_fires(self):
        """The bridge must never fire before weight is actually known —
        it is not a generic price-question handler."""
        eng = DecisionEngine()
        turns = ["20 คู่อยากสั่งของจากจีน", "รองเท้าครับ", "แล้วราคาเท่าไหร่อะ"]
        replies = self._journey(eng, turns)
        dev = replies[-1][0].get("developer") or {}
        self.assertNotEqual(dev.get("selection_source"), "frame_weight_known_price_ask")


if __name__ == "__main__":
    unittest.main()
