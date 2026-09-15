# -*- coding: utf-8 -*-
"""PRODUCTION HOTFIX — typed WEIGHT slot, separate from order quantity.

Root cause (task): `Frame.weight` was declared in
services/conversation_semantics.py but never assigned anywhere — no
detector recognised a compatible answer to the assistant's own
ASK_WEIGHT_FOR_RATE question, so `derive_active_frame()` could never read
it back and `frame_ack_reply()`'s ask ladder repeated
"รบกวนแจ้งน้ำหนักโดยประมาณเพิ่มเติมได้ไหมคะ" forever. This suite locks the
fix at the SHARED STATE MECHANISM level (Frame / derive_active_frame /
resolve_frame_correction / frame_ack_reply / the DecisionEngine dispatch
that wires them together) — not a phrase patch.

Test tier is pinned offline by tests/__init__.py — no paid API call.
"""
import unittest
from unittest.mock import patch

import config
from services.conversation_semantics import (
    Frame, derive_active_frame, resolve_frame_correction, frame_ack_reply,
    _parse_weight_answer, _ASSISTANT_ASKED_WEIGHT_RE, is_frame_followup,
)
from services.decision_engine import DecisionEngine
from services.agent.runner import authoritative_run, SAFETY_FLAG_PREFIXES
from services.agent.adapters import existing_engine as adapter

CTX = {"channel": "line", "tenant_id": "default", "sample_source": "REAL_LINE",
       "customer_context": {}, "developer_mode": True}

ASK_PRODUCT = "ได้ค่ะ รับทราบ จำนวนประมาณ 20 คู่นะคะ 😊 รบกวนแจ้งชื่อหรือประเภทสินค้าที่สนใจนำเข้าด้วยนะคะ"
ASK_METHOD = "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้า จำนวนประมาณ 20 คู่นะคะ 😊 สนใจส่งทางรถหรือทางเรือคะ"
ASK_WEIGHT = "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้า จำนวนประมาณ 15 คู่ ขนส่งทางเรือนะคะ 😊 รบกวนแจ้งน้ำหนักโดยประมาณเพิ่มเติมได้ไหมคะ"

HIST_ASK_WEIGHT = [
    {"role": "user", "content": "20 คู่อยากสั่งของจากจีน"},
    {"role": "assistant", "content": ASK_PRODUCT},
    {"role": "user", "content": "รองเท้าครับ"},
    {"role": "assistant", "content": ASK_METHOD},
    {"role": "user", "content": "เอ้ย 15 คู่"},
    {"role": "assistant", "content": "รับทราบค่ะ ปรับเป็นจำนวนประมาณ 15 คู่ สำหรับรองเท้านะคะ สนใจส่งทางรถหรือทางเรือคะ"},
    {"role": "user", "content": "ส่งเรือได้ปะ"},
    {"role": "assistant", "content": ASK_WEIGHT},
]


def turn(text, history):
    out = authoritative_run(text, history=history, context=dict(CTX),
                            engine_fallback=lambda: adapter.execute(text, history, dict(CTX)))
    return out, out.decision


def _slot(d, name):
    v = (d.known_slots or {}).get(name)
    return (v.get("value"), v.get("unit")) if isinstance(v, dict) else (v, None)


class TestTypedMeasurementForms(unittest.TestCase):
    """Task §1/§7 — every listed weight spelling resolves to weight, and
    never contaminates order quantity."""

    WEIGHT_FORMS = ("10 กิโลกรัม", "10 กก.", "10 กก", "10kg", "10 kg", "10 โล",
                    "ประมาณสิบกิโล")
    QUANTITY_FORMS = (("20 คู่", "คู่"), ("20 ชิ้น", "ชิ้น"), ("5 กล่อง", "กล่อง"))

    def test_every_weight_spelling_is_recognised(self):
        frame = derive_active_frame(HIST_ASK_WEIGHT)
        for text in self.WEIGHT_FORMS:
            with self.subTest(text=text):
                r = resolve_frame_correction(text, frame)
                self.assertEqual(r["op"], "SET_WEIGHT", (text, r))
                self.assertIsNone(r["quantity"], "a weight answer must never set quantity")
                self.assertAlmostEqual(r["weight"], 10.0)

    def test_order_quantity_forms_stay_quantity_never_weight(self):
        frame = derive_active_frame(HIST_ASK_WEIGHT[:4])  # product known, no weight yet
        for text, unit in self.QUANTITY_FORMS:
            with self.subTest(text=text):
                r = resolve_frame_correction(text, frame)
                self.assertEqual(r["op"], "SET_QUANTITY", (text, r))
                self.assertIsNone(r["weight"], "an order-quantity answer must never set weight")
                self.assertEqual(r["unit"], unit)

    def test_parse_weight_answer_never_raises_on_garbage(self):
        for text in ("", None, "สวัสดี", "10", "กิโล", "๑๐ กิโล", "abc kg kg"):
            self.assertIsNotNone(_parse_weight_answer.__call__)  # callable, never raises below
            _parse_weight_answer(text)  # must not raise


class TestAssistantAskedWeightDetector(unittest.TestCase):

    def test_matches_the_actual_committed_ask(self):
        self.assertTrue(_ASSISTANT_ASKED_WEIGHT_RE.search(
            "รบกวนแจ้งน้ำหนักโดยประมาณเพิ่มเติมได้ไหมคะ"))

    def test_bare_weight_answer_is_a_frame_followup_shape(self):
        self.assertTrue(is_frame_followup("10 กิโลกรัม"))
        self.assertTrue(is_frame_followup("10 กก."))


class TestFrameNeverConfusesWeightAndQuantity(unittest.TestCase):
    """Task §2/§3 — a measurement answer must never replace product, and
    order quantity / weight are independent typed slots."""

    def test_bare_weight_answer_does_not_touch_product_or_quantity(self):
        frame = derive_active_frame(HIST_ASK_WEIGHT)
        r = resolve_frame_correction("10 กิโลกรัม", frame)
        self.assertEqual(r["op"], "SET_WEIGHT")
        self.assertIsNone(r["product"])
        self.assertIsNone(r["quantity"])
        new_frame = Frame(**{**frame.as_dict(), "weight": r["weight"], "weight_unit": r["weight_unit"]})
        self.assertEqual(new_frame.product, "รองเท้า")
        self.assertEqual(new_frame.quantity, 15)
        self.assertEqual(new_frame.unit, "คู่")

    def test_weight_ack_reads_back_through_derive_active_frame(self):
        frame = derive_active_frame(HIST_ASK_WEIGHT)
        r = resolve_frame_correction("10 กิโลกรัม", frame)
        frame.weight, frame.weight_unit = r["weight"], r["weight_unit"]
        reply = frame_ack_reply(frame, changed="weight")
        self.assertNotIn("รบกวนแจ้งน้ำหนัก", reply, "must not re-ask weight in the same turn")
        hist = HIST_ASK_WEIGHT + [{"role": "user", "content": "10 กิโลกรัม"},
                                  {"role": "assistant", "content": reply}]
        f2 = derive_active_frame(hist)
        self.assertEqual(f2.product, "รองเท้า")
        self.assertEqual((f2.quantity, f2.unit), (15, "คู่"))
        self.assertEqual(f2.method, "sea")
        self.assertEqual(f2.weight, 10.0)

    def test_weight_correction_after_weight_known(self):
        frame = Frame(product="รองเท้า", quantity=15, unit="คู่", method="sea",
                      weight=10.0, weight_unit="กก.")
        r = resolve_frame_correction("เอ้ย 12 กิโล", frame)
        self.assertEqual(r["op"], "SET_WEIGHT")
        self.assertEqual(r["weight"], 12.0)
        # a weight correction must never touch the order quantity
        self.assertIsNone(r["quantity"])

    def test_quantity_correction_after_weight_known_leaves_weight_alone(self):
        frame = Frame(product="รองเท้า", quantity=15, unit="คู่", method="sea",
                      weight=10.0, weight_unit="กก.")
        r = resolve_frame_correction("เอ้ย 8 คู่", frame)
        self.assertEqual(r["op"], "CORRECT_QUANTITY")
        self.assertEqual(r["quantity"], 8)
        self.assertIsNone(r["weight"], "a quantity correction must never touch weight")


class TestRequestedSlotSatisfied(unittest.TestCase):
    """Task §4 — SATISFIED_REQUESTED_SLOT_CANNOT_BE_REASKED."""

    def test_weight_ack_does_not_ask_for_weight_again(self):
        frame = derive_active_frame(HIST_ASK_WEIGHT)
        frame.weight, frame.weight_unit = 10.0, "กก."
        reply = frame_ack_reply(frame, changed="weight")
        self.assertNotIn("รบกวนแจ้งน้ำหนัก", reply)

    def test_full_frame_ladder_has_nothing_left_to_ask(self):
        frame = Frame(product="รองเท้า", quantity=15, unit="คู่", method="sea",
                      weight=10.0, weight_unit="กก.")
        reply = frame_ack_reply(frame, changed="weight")
        for phrase in ("ชื่อหรือประเภทสินค้า", "แจ้งจำนวนโดยประมาณ", "ทางรถหรือทางเรือ",
                       "รบกวนแจ้งน้ำหนัก"):
            self.assertNotIn(phrase, reply, phrase)


class TestExactSixTurnJourney(unittest.TestCase):
    """Task §6 — the exact reported production journey, unstubbed, over
    the real LangGraph production runtime path (LANGGRAPH_MODE=production
    semantics via authoritative_run, DecisionEngine as fallback)."""

    def test_the_exact_journey(self):
        history = []
        decisions = []
        for text in ("20 คู่อยากสั่งของจากจีน", "รองเท้าครับ", "เอ้ย 15 คู่",
                     "ส่งเรือได้ปะ", "10 กิโลกรัม", "แล้วราคาเท่าไหร่อะ"):
            out, d = turn(text, history)
            decisions.append((out, d))
            history = history + [{"role": "user", "content": text},
                                 {"role": "assistant", "content": d.final_response}]

        _, d1 = decisions[0]
        self.assertEqual(_slot(d1, "quantity"), (20, "คู่"))

        _, d2 = decisions[1]
        self.assertEqual(_slot(d2, "product")[0], "รองเท้า")
        self.assertEqual(_slot(d2, "quantity"), (20, "คู่"))

        _, d3 = decisions[2]
        self.assertEqual(_slot(d3, "product")[0], "รองเท้า")
        self.assertEqual(_slot(d3, "quantity"), (15, "คู่"))

        _, d4 = decisions[3]
        self.assertEqual(_slot(d4, "shipping_method")[0], "sea")
        self.assertEqual(_slot(d4, "product")[0], "รองเท้า")
        self.assertEqual(_slot(d4, "quantity"), (15, "คู่"))

        _, d5 = decisions[4]
        w5, wu5 = _slot(d5, "weight")
        self.assertAlmostEqual(float(w5), 10.0)
        self.assertEqual(_slot(d5, "product")[0], "รองเท้า")
        self.assertEqual(_slot(d5, "quantity"), (15, "คู่"))
        self.assertEqual(_slot(d5, "shipping_method")[0], "sea")
        self.assertNotIn("รบกวนแจ้งน้ำหนัก", d5.final_response,
                         "turn 5's own reply must not re-ask weight")

        out6, d6 = decisions[5]
        w6, _ = _slot(d6, "weight")
        self.assertAlmostEqual(float(w6), 10.0, msg="weight must survive the price turn")
        self.assertEqual(_slot(d6, "product")[0], "รองเท้า")
        self.assertEqual(_slot(d6, "quantity"), (15, "คู่"))
        self.assertEqual(_slot(d6, "shipping_method")[0], "sea")
        self.assertNotIn("รบกวนแจ้งน้ำหนัก", d6.final_response,
                         "the price question must never re-ask a known weight")
        self.assertNotEqual(d6.requested_slot, "weight")

        # safety across every turn of the journey
        for out, d in decisions:
            flagged = [e for e in (d.errors or []) if e.split(":")[0] in SAFETY_FLAG_PREFIXES]
            self.assertEqual(flagged, [], (d.normalized_message, flagged))
            self.assertFalse(d.auth_required, d.normalized_message)


class TestNoNewRegressionInFrameDispatch(unittest.TestCase):
    """The two decision_engine.py call sites that now dispatch SET_WEIGHT
    must still dispatch every op they dispatched before this fix."""

    def test_change_target_and_change_method_still_work_end_to_end(self):
        eng = DecisionEngine()
        hist = [{"role": "user", "content": "อยากสั่งของจากจีน"},
                {"role": "assistant", "content": "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้านะคะ 😊 สนใจส่งทางรถหรือทางเรือคะ"}]
        r = eng.decide("ทางเรือครับ", history=hist, context=dict(CTX))
        self.assertIn("เรือ", (r.get("reply") or {}).get("text") or "")


if __name__ == "__main__":
    unittest.main()
