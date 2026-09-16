# -*- coding: utf-8 -*-
"""REAL LINE 2026-09-16 (final two defects, SHA b6f2de5).

DEFECT 1 — completed estimate inputs did not survive the next turn.
    ราคาเท่าไหร่ -> asks dimensions; ขนาด 40 x 30 x 20 ซม. -> 570 THB;
    ราคาเท่าไหร่ -> asked dimensions AGAIN.
Root cause: Frame.dimensions was a declared-but-never-assigned field (as
weight had been), so the price bridge seeded the calculator with weight +
method only. Fix: dimensions is a typed Frame measurement read from the
customer's own turn (via the calculator's ONE parser) and from the
calculator's acknowledgement; the price bridge seeds every known input
and, when complete, the AUTHORITATIVE calculator recomputes a fresh
estimate — an old price string is never reused.
Invariants: SATISFIED_CALCULATOR_INPUT_CANNOT_BE_REASKED,
            COMPLETED_ESTIMATE_INPUTS_SURVIVE_FOLLOWUP_TURN.

DEFECT 2 — journey filler became the product ("อยากสั่งของจากจีนอีก 50 ชิ้น"
-> product "ของอีก"). Fix in the ONE product-noun extractor: the closed-
class journey words อีก(+bare classifier) / ใหม่ / เริ่มใหม่ / ขอ+import
verb are structural filler; real unknown product names are untouched.

Same persisted session throughout, production LangGraph path.
Test tier is pinned offline by tests/__init__.py — no paid API call.
"""
import re
import unittest

from services.agent.runner import authoritative_run, SAFETY_FLAG_PREFIXES
from services.agent.adapters import existing_engine as adapter
from services.conversation_semantics import _import_noun, derive_active_frame

CTX = {"channel": "line", "tenant_id": "default", "sample_source": "OWNER_TEST",
       "customer_context": {}, "developer_mode": True}

JOURNEY = ("อยากสั่งของจากจีน 20 คู่", "เป็นรองเท้าคับ", "อยากส่งทางเรือได้ไหมคับ",
           "หนักประมาณ 30 โล", "ราคาเท่าไหร่", "ขนาด 40 x 30 x 20 ซม.",
           "ราคาเท่าไหร่", "ราคาอีกทีเท่าไหร่")
SAME_SESSION_NEW_JOURNEY = ("อยากสั่งของจากจีนอีก 50 ชิ้น", "เป็นกระเป๋า")

_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*บาท")


def _slot(d, name):
    v = (d.known_slots or {}).get(name)
    return (v.get("value"), v.get("unit")) if isinstance(v, dict) else (v, None)


def _turn(text, history):
    window = history[-20:]
    out = authoritative_run(text, history=window, context=dict(CTX),
                            engine_fallback=lambda: adapter.execute(text, window, dict(CTX)))
    return out, out.decision


class TestFillerNeverBecomesProduct(unittest.TestCase):
    """The ONE extractor: filler out, real (even unknown) products in."""

    def test_filler_forms_yield_no_product(self):
        for text in ("อยากสั่งของจากจีนอีก 50 ชิ้น", "จะสั่งของใหม่ 10 ชิ้น", "อยากนำเข้าอีกตัว",
                     "อยากสั่งของจากจีน 20 คู่", "สั่งสินค้าใหม่ 5 ชิ้น"):
            with self.subTest(text=text):
                self.assertIsNone(_import_noun(text), text)

    def test_real_products_survive_including_unknown_and_compound(self):
        for text, noun in (("ขอสั่งกระเป๋า 5 ใบจากจีน", "กระเป๋า"),
                           ("เริ่มใหม่ อยากสั่งรองเท้า 30 คู่", "รองเท้า"),
                           ("อยากสั่งชั้นวางของ 10 อัน", "ชั้นวางของ"),
                           ("อยากสั่งของเล่นจากจีน", "ของเล่น"),
                           ("อยากสั่งเสื้อผ้ามือสองอีก 20 ตัว", "เสื้อผ้ามือสอง"),
                           ("สั่งอะไหล่รถใหม่ 3 ชิ้น", "อะไหล่รถ"),
                           ("อยากนำเข้ากล่องใส่ของ", "กล่องใส่ของ"),
                           ("อยากนำเข้าเครื่องจักร", "เครื่องจักร")):
            with self.subTest(text=text):
                self.assertEqual(_import_noun(text), noun)


class TestDimensionsAreATypedFrameMeasurement(unittest.TestCase):

    def test_frame_reads_dimensions_from_the_customer_turn(self):
        hist = [
            {"role": "user", "content": "อยากสั่งรองเท้าจากจีน 20 คู่"},
            {"role": "assistant", "content": "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้า จำนวนประมาณ 20 คู่นะคะ 😊 สนใจส่งทางรถหรือทางเรือคะ"},
            {"role": "user", "content": "ขนาด 40 x 30 x 20 ซม."},
            {"role": "assistant", "content": "ประเมินเบื้องต้นสำหรับทางเรือประมาณ 570.00 บาทค่ะ โดยคิดจากน้ำหนัก 30 กก. (570 บาท)"},
        ]
        f = derive_active_frame(hist)
        self.assertEqual((f.dimensions, f.dim_unit), ("40x30x20", "cm"))
        self.assertEqual(f.product, "รองเท้า")


class TestExactRealJourney(unittest.TestCase):
    """The exact 8-turn journey, then the same-session new journey."""

    def test_journey(self):
        history, results = [], []
        for text in JOURNEY:
            out, d = _turn(text, history)
            results.append((text, out, d))
            history = history + [{"role": "user", "content": text},
                                 {"role": "assistant", "content": d.final_response}]

        d5 = results[4][2]   # first price: asks ONLY dimensions
        self.assertIn("ขนาดสินค้า", d5.final_response)
        self.assertNotIn("รบกวนแจ้งน้ำหนัก", d5.final_response)

        d6 = results[5][2]   # dimensions -> fresh estimate
        m6 = _PRICE_RE.search(d6.final_response)
        self.assertIsNotNone(m6, d6.final_response)
        self.assertAlmostEqual(float(m6.group(1)), 570.0)
        self.assertIn("30 กก.", d6.final_response)

        for i in (6, 7):     # repeated price questions
            text, out, d = results[i]
            with self.subTest(turn=text):
                self.assertNotIn("ขนาดสินค้า", d.final_response, "SATISFIED input re-asked")
                self.assertNotIn("รบกวนแจ้งน้ำหนัก", d.final_response)
                m = _PRICE_RE.search(d.final_response)
                self.assertIsNotNone(m, d.final_response)
                self.assertAlmostEqual(float(m.group(1)), 570.0, msg="must recompute from 30kg + 40x30x20 + sea")
                self.assertIn("30 กก.", d.final_response)
                self.assertEqual(_slot(d, "product")[0], "รองเท้า")
                self.assertEqual(_slot(d, "quantity"), (20, "คู่"))
                self.assertEqual(_slot(d, "shipping_method")[0], "sea")
                self.assertAlmostEqual(float(_slot(d, "weight")[0]), 30.0)
                self.assertEqual(_slot(d, "dimensions")[0], "40x30x20")
                dev = (out.engine_result or {}).get("developer") or {}
                self.assertEqual(dev.get("selection_source"), "frame_inputs_known_price_recompute")

        # SAME SESSION: explicit new journey with filler, then the product
        out9, d9 = _turn(SAME_SESSION_NEW_JOURNEY[0], history)
        self.assertEqual(_slot(d9, "quantity"), (50, "ชิ้น"))
        self.assertIsNone(_slot(d9, "product")[0], "filler must not become the product")
        self.assertIsNone(_slot(d9, "weight")[0]); self.assertIsNone(_slot(d9, "dimensions")[0])
        self.assertIsNone(_slot(d9, "shipping_method")[0])
        self.assertNotIn("ของอีก", d9.final_response)
        self.assertIn("ประเภทสินค้า", d9.final_response, "must ask the product")
        self.assertIn("50 ชิ้น", d9.final_response)
        history = history + [{"role": "user", "content": SAME_SESSION_NEW_JOURNEY[0]},
                             {"role": "assistant", "content": d9.final_response}]

        out10, d10 = _turn(SAME_SESSION_NEW_JOURNEY[1], history)
        self.assertEqual(_slot(d10, "product")[0], "กระเป๋า")
        self.assertEqual(_slot(d10, "quantity"), (50, "ชิ้น"))
        self.assertIsNone(_slot(d10, "weight")[0]); self.assertIsNone(_slot(d10, "dimensions")[0])
        self.assertIn("ทางรถหรือทางเรือ", d10.final_response, "must ask the method")
        self.assertNotIn("กก.", d10.final_response)

        for text, out, d in results + [(SAME_SESSION_NEW_JOURNEY[0], out9, d9), (SAME_SESSION_NEW_JOURNEY[1], out10, d10)]:
            flagged = [e for e in (d.errors or []) if e.split(":")[0] in SAFETY_FLAG_PREFIXES]
            self.assertEqual(flagged, [], (text, flagged))
            self.assertEqual(out.used, "langgraph", text)


if __name__ == "__main__":
    unittest.main()
