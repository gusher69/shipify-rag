# -*- coding: utf-8 -*-
"""REAL LINE 2026-09-16 (P0) — ONE journey boundary for EVERY state system.

Production (SHA 3226348) answered the explicit fresh opener

    USER: อยากสั่งของจากจีน 20 คู่
    BOT : รับทราบค่ะ (น้ำหนัก 0.03 กก. • ขนาด 40x30x20 cm) ต้องการประเมินทางรถหรือทางเรือคะ

because the previous turns had left an OPEN shipping-cost calculator
thread (a delivered estimate for 40x30x20 cm / 30 กรัม) and the
calculator — an independently maintained state system — had no journey
boundary of its own: it read the opener's bare "20" as a dimension value
and continued the old thread. The conversation Frame had been reset at
the boundary one commit earlier; the calculator, the Business-Action
pending collection, the charter / operational flows and the RAG
slot-filling flow had not. Two state authorities, one reset, one stale.

WHY THE PREVIOUS TEST MISSED IT: tests/test_real_line_persistent_session_
journeys.py seeded the previous journey's FRAME state (acknowledgement
turns) but never a pending EstimateState — its history contained no
estimate prompt / estimate result, so the calculator branch (which runs
BEFORE the import-journey branch in DecisionEngine.decide) was never
open when the opener arrived. A Frame-only reset test cannot prove a
journey boundary.

This module seeds the REAL production calculator thread verbatim, in the
same persisted session, and runs the opener through the same routing
order as the LINE webhook (authoritative_run -> LangGraph -> the
DecisionEngine executes). The single boundary is
services/conversation_semantics.py::is_new_journey_opener; every store
below is asserted against it.

Test tier is pinned offline by tests/__init__.py — no paid API call.
"""
import unittest

from services.agent.runner import authoritative_run, SAFETY_FLAG_PREFIXES
from services.agent.adapters import existing_engine as adapter
from services.conversation_semantics import is_new_journey_opener, derive_active_frame
from services.shipping_estimate_flow import derive_estimate_state
from services.decision_engine import DecisionEngine
from rag.slot_filling_flow import resolve_slot_filling_turn

CTX = {"channel": "line", "tenant_id": "default", "sample_source": "OWNER_TEST",
       "customer_context": {}, "developer_mode": True}

# The real production session, turns 76-87, verbatim: an import journey
# that drifted into the calculator and ENDED ON A DELIVERED ESTIMATE
# (0.03 กก. / 40x30x20 cm / sea). This is the exact pending state the
# opener was played into.
REAL_CALCULATOR_SESSION = [
    {"role": "user", "content": "อยากส่งทางเรือได้ไหมคับ"},
    {"role": "assistant", "content": "ตอนนี้ทางเรามีบริการขนส่งทางรถและทางเรือเท่านั้นค่ะ ยังไม่มีบริการขนส่งทางเครื่องบินนะคะ ถ้าคุณลูกค้าต้องการฝากสั่งและขนส่งทางเรือ สามารถแจ้งได้เลยค่ะ"},
    {"role": "user", "content": "หนักประมาณ 30 โล"},
    {"role": "assistant", "content": "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้า จำนวนประมาณ 30 โหล ขนส่งทางเรือนะคะ 😊 รบกวนแจ้งน้ำหนักโดยประมาณเพิ่มเติมได้ไหมคะ"},
    {"role": "user", "content": "หนักประมาณ 30 โล"},
    {"role": "assistant", "content": "ค่าขนส่งคิดจากน้ำหนักและปริมาตรค่ะ ค่าไหนมากกว่าจะถูกใช้เป็นค่าขนส่ง โดยทางรถ 35 บาท/กก. หรือ 6900 บาท/คิว และทางเรือ 19 บาท/กก. หรือ 4500 บาท/คิว ค่ะ"},
    {"role": "user", "content": "ขนาด 40 x 30 x 20 ซม."},
    {"role": "assistant", "content": "รับทราบค่ะ (ขนาด 40x30x20 cm • ทางเรือ) ยังขาดข้อมูลสำหรับประเมินค่ะ รบกวนแจ้งน้ำหนักสินค้าเพิ่มเติมด้วยนะคะ"},
    {"role": "user", "content": "หนักประมาณ 30 โล"},
    {"role": "assistant", "content": "รับทราบค่ะ (ขนาด 40x30x20 cm • ทางเรือ) ยังขาดข้อมูลสำหรับประเมินค่ะ รบกวนแจ้งน้ำหนักสินค้าเพิ่มเติมด้วยนะคะ"},
    {"role": "user", "content": "หนักประมาณ 30 กรัม"},
    {"role": "assistant", "content": "ประเมินเบื้องต้นสำหรับทางเรือประมาณ 108.00 บาทค่ะ โดยคิดจากปริมาตร 0.024 CBM (108 บาท) ซึ่งมีค่ามากกว่าการคิดตามน้ำหนัก (0.57 บาท) (เป็นการประเมินเบื้องต้นจากข้อมูลที่แจ้งมา ค่าจริงจะคิดจากการวัดขนาดและน้ำหนักที่โกดังอีกครั้งค่ะ)"},
]

# A pending Business-Action identifier collection (the other pending-
# workflow store), exactly as production asks it.
PENDING_COLLECTION_SESSION = [
    {"role": "user", "content": "ขอเช็คออเดอของผมหน่อย"},
    {"role": "assistant", "content": "กรุณาแจ้งรหัสลูกค้าค่ะ"},
]

NEW_JOURNEY_VARIANTS = (
    "อยากสั่งของจากจีน 20 คู่",
    "จะสั่งของใหม่ 10 ชิ้น",
    "อยากนำเข้าอีกตัว",
    "ขอสั่งกระเป๋า 5 ใบจากจีน",
    "เริ่มใหม่ อยากสั่งรองเท้า 30 คู่",
)
NOT_OPENERS = ("ทางรถ", "ถ้าเป็นทางรถล่ะ", "หนัก 2 กิโล", "เอ้ย 15 คู่", "ราคาเท่าไหร่",
               "ขนาด 40 x 30 x 20 ซม.", "คำนวณใหม่")


def _slot(d, name):
    v = (d.known_slots or {}).get(name)
    return (v.get("value"), v.get("unit")) if isinstance(v, dict) else (v, None)


def _graph_turn(text, history):
    window = history[-20:]  # the webhook's own max_turns=20 window
    out = authoritative_run(text, history=window, context=dict(CTX),
                            engine_fallback=lambda: adapter.execute(text, window, dict(CTX)))
    return out, out.decision


def _assert_no_stale_calculator_state(tc, text, d, reply, dev):
    tc.assertNotIn("0.03", reply, (text, reply))
    tc.assertNotIn("40x30x20", reply, (text, reply))
    tc.assertNotIn("ประเมินทางรถหรือทางเรือ", reply, (text, reply))
    tc.assertNotEqual(dev.get("selection_source"), "shipping_estimate_flow", (text, dev.get("selection_source")))
    if d is not None:
        tc.assertIsNone(_slot(d, "weight")[0], (text, "old weight leaked"))
        tc.assertIsNone(_slot(d, "dimensions")[0], (text, "old dimensions leaked"))
        tc.assertIsNone(_slot(d, "shipping_method")[0], (text, "old method leaked"))


class TestTheOneBoundary(unittest.TestCase):
    """The semantic NEW_JOURNEY class — never a sentence list."""

    def test_openers(self):
        for text in NEW_JOURNEY_VARIANTS:
            self.assertTrue(is_new_journey_opener(text), text)

    def test_continuations_are_not_openers(self):
        for text in NOT_OPENERS:
            self.assertFalse(is_new_journey_opener(text), text)


class TestCalculatorStateObeysTheBoundary(unittest.TestCase):
    """CALCULATOR STATE RESET — the EstimateState store."""

    def test_open_calculator_thread_is_closed_by_every_opener(self):
        for text in NEW_JOURNEY_VARIANTS:
            with self.subTest(text=text):
                self.assertIsNone(derive_estimate_state(REAL_CALCULATOR_SESSION, text), text)

    def test_genuine_calculator_turns_still_continue_the_thread(self):
        st = derive_estimate_state(REAL_CALCULATOR_SESSION, "ทางรถ")
        self.assertIsNotNone(st)
        self.assertEqual(st.method, "road")
        self.assertEqual((st.length, st.width, st.height), (40.0, 30.0, 20.0))
        st2 = derive_estimate_state(REAL_CALCULATOR_SESSION, "หนัก 2 กิโล")
        self.assertEqual(st2.weight, 2.0)


class TestRagSlotFillingObeysTheBoundary(unittest.TestCase):

    def test_slot_filling_flow_never_continues_over_an_opener(self):
        hist = [{"role": "user", "content": "ช่วยคำนวณค่าส่งหน่อย"},
                {"role": "assistant", "content": "ได้ค่ะ รบกวนแจ้งน้ำหนักและขนาดสินค้า (กว้าง x ยาว x สูง) เพื่อประเมินค่าขนส่งนะคะ"}]
        for text in NEW_JOURNEY_VARIANTS:
            with self.subTest(text=text):
                self.assertIsNone(resolve_slot_filling_turn(text, hist), text)


class TestFrameObeysTheBoundary(unittest.TestCase):
    """FRAME RESET (already fixed one commit earlier; locked here with the
    real calculator session as the previous state)."""

    def test_frame_after_opener_is_only_what_the_opener_said(self):
        hist = REAL_CALCULATOR_SESSION + [
            {"role": "user", "content": "อยากสั่งของจากจีน 20 คู่"},
            {"role": "assistant", "content": "ได้ค่ะ รับทราบ จำนวนประมาณ 20 คู่นะคะ 😊 รบกวนแจ้งชื่อหรือประเภทสินค้าที่สนใจนำเข้าด้วยนะคะ"}]
        f = derive_active_frame(hist)
        self.assertEqual((f.product, f.quantity, f.unit, f.method, f.weight), (None, 20, "คู่", None, None))


class TestPendingCollectionObeysTheBoundary(unittest.TestCase):
    """PRECEDENCE — a pending Business-Action identifier collection never
    continues over an explicit new-journey opener."""

    def test_opener_beats_pending_identifier_collection(self):
        eng = DecisionEngine()
        r = eng.decide("อยากสั่งของจากจีน 20 คู่", history=list(PENDING_COLLECTION_SESSION), context=dict(CTX))
        dev = r.get("developer") or {}
        reply = (r.get("reply") or {}).get("text") or ""
        self.assertEqual(dev.get("stale_workflow_suppressed"), "new_journey_opener")
        self.assertNotIn("รหัสลูกค้า", reply, reply)
        self.assertIn("20 คู่", reply)
        self.assertIn("ประเภทสินค้า", reply, "must ask the product for the NEW journey")


class TestExactPersistedSessionRepro(unittest.TestCase):
    """PERSISTED SAME-SESSION REPRO — the exact production turn, same
    routing order as the LINE webhook, real pending calculator state."""

    def test_exact_production_turn_then_product_answer(self):
        history = list(REAL_CALCULATOR_SESSION)

        out1, d1 = _graph_turn("อยากสั่งของจากจีน 20 คู่", history)
        reply1 = d1.final_response
        dev1 = (out1.engine_result or {}).get("developer") or {}
        _assert_no_stale_calculator_state(self, "opener", d1, reply1, dev1)
        self.assertEqual(_slot(d1, "quantity"), (20, "คู่"))
        self.assertIsNone(_slot(d1, "product")[0])
        self.assertIn("20 คู่", reply1)
        self.assertIn("ประเภทสินค้า", reply1, "expected: acknowledge 20 คู่ and ask the product")
        self.assertEqual(out1.used, "langgraph")
        history = history + [{"role": "user", "content": "อยากสั่งของจากจีน 20 คู่"},
                             {"role": "assistant", "content": reply1}]

        out2, d2 = _graph_turn("เป็นรองเท้าคับ", history)
        reply2 = d2.final_response
        dev2 = (out2.engine_result or {}).get("developer") or {}
        _assert_no_stale_calculator_state(self, "product answer", d2, reply2, dev2)
        self.assertEqual(_slot(d2, "product")[0], "รองเท้า")
        self.assertEqual(_slot(d2, "quantity"), (20, "คู่"))
        self.assertIn("ทางรถหรือทางเรือ", reply2, "method must be ASKED, not remembered")

        for d in (d1, d2):
            flagged = [e for e in (d.errors or []) if e.split(":")[0] in SAFETY_FLAG_PREFIXES]
            self.assertEqual(flagged, [])


class TestEveryVariantDefeatsThePendingCalculator(unittest.TestCase):
    """NEW_JOURNEY_STALE_CALCULATOR_TAKEOVER = 0 for the whole semantic
    class, in the same stale persisted session."""

    def test_variants(self):
        for text in NEW_JOURNEY_VARIANTS:
            with self.subTest(text=text):
                out, d = _graph_turn(text, list(REAL_CALCULATOR_SESSION))
                dev = (out.engine_result or {}).get("developer") or {}
                _assert_no_stale_calculator_state(self, text, d, d.final_response, dev)
                self.assertNotIn("ตอนนี้ยังไม่มีข้อมูลยืนยัน", d.final_response, (text, d.final_response))


if __name__ == "__main__":
    unittest.main()
