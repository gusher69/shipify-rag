# -*- coding: utf-8 -*-
"""REAL LINE 2026-09-16 — persistent-session journeys.

The LINE webhook keeps ONE long-lived session per LINE user (24-hour
inactivity window) and feeds the accumulated history into every turn:
journeys are NOT isolated. Every earlier harness started each journey on
an empty history, which is why the owner's manual production test
disproved "STALE_CONTEXT_TAKEOVER = 0": a previous journey's
shipping_method=sea and weight=5 กก. walked into a brand-new
"อยากสั่งของจากจีน 20 คู่" journey, and "หนักประมาณ 30 โล" was read as
quantity "30 โหล".

These tests use the SAME session lifecycle as production: a previous
journey is played first, then the explicit new opener, with NO history
reset in between — exactly what the webhook would feed the runtime.
They run the real LangGraph production path (authoritative_run with
LANGGRAPH_MODE=production, DecisionEngine as fallback) and, where the
graph is not the unit under test, the DecisionEngine directly.

Shared mechanisms locked here (never a phrase patch):
  * derive_active_frame accumulates from the journey OPEN onward, and an
    explicit fresh opener is a journey boundary for the resolver, the
    graph's state merge and the service-intent frame merge alike;
  * the vocabulary has a typed weight_unit group, so "โล"/"กก" are
    canonical weight words and are never fuzzy-corrected to a count unit;
  * the ONE bare-method-answer vocabulary accepts a desire verb
    ("อยากส่งทางเรือได้ไหมครับ") inside an active journey;
  * a first-person possession probe ("คูปองผมมีไหม") is a private
    list-mine inquiry, not the public how-to.

Test tier is pinned offline by tests/__init__.py — no paid API call.
"""
import unittest

import config
from services.agent.runner import authoritative_run, SAFETY_FLAG_PREFIXES
from services.agent.adapters import existing_engine as adapter
from services.conversation_semantics import (
    derive_active_frame, resolve_frame_correction, Frame, interpret,
)
from services.decision_engine import DecisionEngine, _classify_private_state_inquiry
from services.language.thai_normalizer import normalize

CTX = {"channel": "line", "tenant_id": "default", "sample_source": "OWNER_TEST",
       "customer_context": {}, "developer_mode": True}

# A previous journey exactly as the platform acknowledged it in the real
# production session (reply wording IS the state): product, 15 คู่, sea,
# 5 กก., then three price asks that ended on the same acknowledgement.
PREVIOUS_JOURNEY = [
    {"role": "user", "content": "20 คู่อยากสั่งของจากจีน"},
    {"role": "assistant", "content": "ได้ค่ะ รับทราบ จำนวนประมาณ 20 คู่นะคะ 😊 รบกวนแจ้งชื่อหรือประเภทสินค้าที่สนใจนำเข้าด้วยนะคะ"},
    {"role": "user", "content": "รองเท้าคับ"},
    {"role": "assistant", "content": "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้า จำนวนประมาณ 20 คู่ ขนส่งทางเรือ น้ำหนักประมาณ 10 กก.นะคะ 😊"},
    {"role": "user", "content": "เอ้ย 15 คู่"},
    {"role": "assistant", "content": "รับทราบค่ะ ปรับเป็นจำนวนประมาณ 15 คู่ สำหรับรองเท้านะคะ ขนส่งทางเรือ ตามเดิมค่ะ น้ำหนักประมาณ 10 กก. ตามเดิมค่ะ"},
    {"role": "user", "content": "ส่งเรื่อได้ปะ"},
    {"role": "assistant", "content": "รับทราบค่ะ เปลี่ยนเป็นขนส่งทางเรือ สำหรับรองเท้านะคะ จำนวนประมาณ 15 คู่ ตามเดิมค่ะ น้ำหนักประมาณ 10 กก. ตามเดิมค่ะ"},
    {"role": "user", "content": "5 กิโลกรัม"},
    {"role": "assistant", "content": "รับทราบค่ะ น้ำหนักประมาณ 5 กก. สำหรับรองเท้านะคะ จำนวนประมาณ 15 คู่ ขนส่งทางเรือ ตามเดิมค่ะ"},
    {"role": "user", "content": "แล้วราคาเท่าไหร่อะ"},
    {"role": "assistant", "content": "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้า จำนวนประมาณ 15 คู่ ขนส่งทางเรือ น้ำหนักประมาณ 5 กก.นะคะ 😊"},
    {"role": "user", "content": "แล้วราคาเท่าไหร่อะ"},
    {"role": "assistant", "content": "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้า จำนวนประมาณ 15 คู่ ขนส่งทางเรือ น้ำหนักประมาณ 5 กก.นะคะ 😊"},
    {"role": "user", "content": "แล้วราคาเท่าไหร่อะ"},
    {"role": "assistant", "content": "ได้ค่ะ รับทราบว่าต้องการนำเข้ารองเท้า จำนวนประมาณ 15 คู่ ขนส่งทางเรือ น้ำหนักประมาณ 5 กก.นะคะ 😊"},
]


def _slot(d, name):
    v = (d.known_slots or {}).get(name)
    return (v.get("value"), v.get("unit")) if isinstance(v, dict) else (v, None)


def _graph_turn(text, history):
    window = history[-20:]  # the webhook's own max_turns=20 window
    out = authoritative_run(text, history=window, context=dict(CTX),
                            engine_fallback=lambda: adapter.execute(text, window, dict(CTX)))
    return out, out.decision


class TestExplicitOpenerIsAJourneyBoundary(unittest.TestCase):
    """Task §2 — NEW_JOURNEY_STALE_SLOT_LEAK = 0, at the frame layer."""

    def test_frame_after_opener_carries_nothing_from_previous_journey(self):
        hist = PREVIOUS_JOURNEY + [
            {"role": "user", "content": "อยากสั่งของจากจีน 20 คู่"},
            {"role": "assistant", "content": "ได้ค่ะ รับทราบ จำนวนประมาณ 20 คู่นะคะ 😊 รบกวนแจ้งชื่อหรือประเภทสินค้าที่สนใจนำเข้าด้วยนะคะ"},
        ]
        f = derive_active_frame(hist)
        self.assertIsNotNone(f)
        self.assertEqual((f.quantity, f.unit), (20, "คู่"))
        self.assertIsNone(f.method, "previous journey's sea must not leak")
        self.assertIsNone(f.weight, "previous journey's 5 กก. must not leak")
        self.assertIsNone(f.product, "previous journey's product must not leak")

    def test_previous_journey_is_still_readable_before_the_opener(self):
        """Nothing legitimately known is lost: BEFORE the new opener the
        old journey is still the active frame."""
        f = derive_active_frame(PREVIOUS_JOURNEY)
        self.assertEqual((f.product, f.quantity, f.method, f.weight), ("รองเท้า", 15, "sea", 5.0))


class TestTypedMeasurementPrecedence(unittest.TestCase):
    """Task §3/§4 — WEIGHT_TO_QUANTITY_CONFUSION = 0."""

    WEIGHT_FORMS = ("หนักประมาณ 30 โล", "30 โล", "หนัก 30 โล", "ประมาณ 30โล",
                    "30 กิโล", "30 กก", "30kg", "น้ำหนัก 30 กิโลกรัม")

    def test_normaliser_never_corrects_lo_to_dozen(self):
        for text in self.WEIGHT_FORMS:
            with self.subTest(text=text):
                r = normalize(text)
                self.assertNotIn("โหล", r.normalized_text, (text, r.normalized_text))
                self.assertFalse(any(c.applied and c.replacement == "โหล" for c in r.candidates))

    def test_every_weight_form_resolves_as_weight_never_quantity(self):
        frame = Frame(product="รองเท้า", quantity=20, unit="คู่", method="sea")
        for text in self.WEIGHT_FORMS:
            with self.subTest(text=text):
                r = resolve_frame_correction(normalize(text).normalized_text, frame)
                self.assertEqual(r["op"], "SET_WEIGHT", (text, r))
                self.assertAlmostEqual(r["weight"], 30.0)
                self.assertIsNone(r["quantity"])

    def test_genuine_dozen_stays_an_order_quantity(self):
        frame = Frame(product="รองเท้า", quantity=20, unit="คู่")
        r = resolve_frame_correction("30 โหล", frame)
        self.assertEqual(r["op"], "SET_QUANTITY")
        self.assertEqual((r["quantity"], r["unit"]), (30, "โหล"))
        self.assertIsNone(r["weight"])
        self.assertEqual(normalize("30 โหล").normalized_text, "30 โหล")

    def test_count_units_still_corrected_and_still_quantities(self):
        for text, unit in (("20 คู่", "คู่"), ("20 ชิ้น", "ชิ้น"), ("5 กล่อง", "กล่อง")):
            r = resolve_frame_correction(text, Frame(product="รองเท้า", quantity=1, unit="คู่"))
            self.assertEqual(r["op"], "SET_QUANTITY", text)
            self.assertEqual(r["unit"], unit)


class TestShippingMethodSelectionInContext(unittest.TestCase):
    """Task §5 — inside an active journey, wanting a mode IS choosing it;
    a policy question about modes is not."""

    def test_selection_variants(self):
        frame = Frame(product="รองเท้า", quantity=20, unit="คู่")
        for text in ("อยากส่งทางเรือได้ไหมครับ", "อยากส่งทางเรือได้ไหมคับ", "ส่งเรือได้ไหม",
                     "เอาทางเรือ", "ขอส่งเรือ", "เรือได้ปะ", "ต้องการส่งทางเรือ", "สนใจทางเรือ"):
            with self.subTest(text=text):
                r = resolve_frame_correction(text, frame)
                self.assertEqual((r["op"], r["method"]), ("CHANGE_METHOD", "sea"), (text, r))

    def test_policy_questions_are_not_selections(self):
        frame = Frame(product="รองเท้า", quantity=20, unit="คู่")
        for text in ("ส่งทางเรือกี่วัน", "ทางเรือกับทางรถต่างกันยังไง", "ทางเรือแพงไหม"):
            with self.subTest(text=text):
                r = resolve_frame_correction(text, frame)
                self.assertNotEqual(r["op"], "CHANGE_METHOD", (text, r))


class TestPossessionProbeIsPrivateInquiry(unittest.TestCase):
    """Task §7 — ANSWERABLE_INTENT_CANNOT_DEGRADE_TO_HUMAN_CS_FALLBACK:
    "do I have coupons" is the customer's own record (private, API-gap
    path: identifier -> handoff), not the public how-to."""

    def test_family_and_private_classifier(self):
        for text in ("แล้วคูปองผมมีไหม", "คูปองผมมีไหม", "คูปองฉันเหลือมั้ย"):
            with self.subTest(text=text):
                self.assertEqual(interpret(text, []).intent_family, "MY_COUPONS")
                psi = _classify_private_state_inquiry(text)
                self.assertIsNotNone(psi, text)
                self.assertEqual(psi["domain"], "customer_data")

    def test_public_forms_stay_public(self):
        for text in ("ใช้คูปองยังไง", "คูปองใช้ไม่หมด คืนได้ไหม", "มีขั้นต่ำในการสั่งไหม",
                     "มีบริการอะไรบ้าง", "สินค้าที่ห้ามนำเข้ามีอะไรบ้าง"):
            with self.subTest(text=text):
                self.assertIsNone(_classify_private_state_inquiry(text), text)
        self.assertEqual(interpret("ใช้คูปองยังไง", []).intent_family, "COUPON_USAGE")

    def test_fresh_coupon_possession_asks_identifier_not_handoff(self):
        eng = DecisionEngine()
        r = eng.decide("แล้วคูปองผมมีไหม", history=[], context=dict(CTX))
        self.assertEqual((r.get("routing") or {}).get("type"), "WORKFLOW")
        self.assertEqual((r.get("developer") or {}).get("selection_source"), "private_state_inquiry")


class TestExactRealLineJourneyOnPersistentSession(unittest.TestCase):
    """Task §8 — the exact production sequence on top of the real
    previous journey, no reset, through the production LangGraph path."""

    def test_the_exact_live_sequence(self):
        history = list(PREVIOUS_JOURNEY)
        results = []
        for text in ("อยากสั่งของจากจีน 20 คู่", "เป็นรองเท้าคับ", "อยากส่งทางเรือได้ไหมคับ",
                     "หนักประมาณ 30 โล", "ราคาเท่าไหร่", "40x60x30 ซม."):
            out, d = _graph_turn(text, history)
            results.append((text, out, d))
            history = history + [{"role": "user", "content": text},
                                 {"role": "assistant", "content": d.final_response}]

        _, _, d1 = results[0]   # explicit new opener
        self.assertEqual(_slot(d1, "quantity"), (20, "คู่"))
        self.assertIsNone(_slot(d1, "shipping_method")[0], "old sea must be cleared")
        self.assertIsNone(_slot(d1, "weight")[0], "old weight must be cleared")
        self.assertIsNone(_slot(d1, "product")[0])
        self.assertNotIn("ขนส่งทางเรือ", d1.final_response)
        self.assertNotIn("กก.", d1.final_response)

        _, _, d2 = results[1]   # product answer
        self.assertEqual(_slot(d2, "product")[0], "รองเท้า")
        self.assertEqual(_slot(d2, "quantity"), (20, "คู่"))
        self.assertIsNone(_slot(d2, "shipping_method")[0])
        self.assertIsNone(_slot(d2, "weight")[0])
        # the ASK "สนใจส่งทางรถหรือทางเรือคะ" is correct; the leak signature
        # is the ACKNOWLEDGED "ขนส่งทางเรือ" / "น้ำหนักประมาณ".
        self.assertNotIn("ขนส่งทางเรือ", d2.final_response, "stale sea leaked into the product ack")
        self.assertNotIn("กก.", d2.final_response, "stale weight leaked into the product ack")
        self.assertIn("ทางรถหรือทางเรือ", d2.final_response, "method must now be ASKED, not assumed")

        _, _, d3 = results[2]   # method selection in context
        self.assertEqual(_slot(d3, "shipping_method")[0], "sea")
        self.assertEqual(_slot(d3, "product")[0], "รองเท้า")
        self.assertEqual(_slot(d3, "quantity"), (20, "คู่"))
        self.assertIn("ขนส่งทางเรือ", d3.final_response)
        self.assertNotIn("มีบริการขนส่งทางรถและทางเรือ", d3.final_response)

        _, _, d4 = results[3]   # หนักประมาณ 30 โล
        self.assertNotIn("โหล", d4.normalized_message)
        w4, _ = _slot(d4, "weight")
        self.assertAlmostEqual(float(w4), 30.0)
        self.assertEqual(_slot(d4, "quantity"), (20, "คู่"), "quantity must stay 20 คู่")
        self.assertEqual(_slot(d4, "shipping_method")[0], "sea")
        self.assertNotIn("โหล", d4.final_response)
        self.assertNotIn("5 กก.", d4.final_response)

        _, _, d5 = results[4]   # price
        w5, _ = _slot(d5, "weight")
        self.assertAlmostEqual(float(w5), 30.0, msg="price must use the CURRENT 30 kg")
        self.assertIn("30 กก.", d5.final_response)
        self.assertNotIn("5 กก.", d5.final_response)
        self.assertNotIn("รบกวนแจ้งน้ำหนัก", d5.final_response)
        self.assertIn("ขนาดสินค้า", d5.final_response, "ask ONLY the genuinely missing input")
        self.assertNotRegex(d5.final_response, r"\d+\s*บาท")

        _, _, d6 = results[5]   # dimensions -> real estimate from the rate table
        self.assertRegex(d6.final_response, r"\d+(\.\d+)?\s*บาท")
        self.assertIn("30 กก.", d6.final_response)

        for text, out, d in results:
            flagged = [e for e in (d.errors or []) if e.split(":")[0] in SAFETY_FLAG_PREFIXES]
            self.assertEqual(flagged, [], (text, flagged))
            self.assertEqual(out.used, "langgraph", text)


class TestOpenerCarryingAProductAlsoStartsClean(unittest.TestCase):
    """The service-intent frame merge must not inherit the previous
    journey's measurements into an opener that names its product."""

    def test_product_opener_after_previous_journey(self):
        eng = DecisionEngine()
        r = eng.decide("อยากสั่งรองเท้าจากจีน 20 คู่", history=list(PREVIOUS_JOURNEY), context=dict(CTX))
        reply = (r.get("reply") or {}).get("text") or ""
        self.assertIn("รองเท้า", reply)
        self.assertNotIn("ขนส่งทางเรือ", reply, "old sea leaked into the opener's acknowledgement")
        self.assertNotIn("กก.", reply, "old weight leaked into the opener's acknowledgement")
        self.assertIn("ทางรถหรือทางเรือ", reply, "method must be ASKED for the new journey")


if __name__ == "__main__":
    unittest.main()
