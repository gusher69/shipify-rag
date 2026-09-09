# -*- coding: utf-8 -*-
"""OWNER-REAL-LINE-FIX-06 — a fresh explicit IMPORT_INTEREST opener must
re-establish the journey over stale history, the product slot must
persist, and no internal "(สินค้า X)" state token may reach the customer.

Real LINE (after many owner tests in the same chat):
  user: อยากสั่งของจากจีน        -> OLD RAG advice (FIX-01 did not fire)
  user: เป็นชั้นวางของ            -> generic import advice (slot lost)
  user: เปลี่ยนเป้นรองเท้าได้ไหม  -> correction works but shows "(สินค้า รองเท้า)"

ROOT CAUSE: the FIX-01 `_broad_china_buy` branch and the `_svc_pre_rag`
IMPORT_INTEREST branch were both guarded by
`_derive_active_frame(history) is None`. A stale frame reconstructed from
a long prior owner-test history suppressed the fresh discovery reply,
dropping "อยากสั่งของจากจีน" to generic RAG — and the cascade lost the
product slot on the next turn.

FIX: those guards now also fire when the current turn is not an in-frame
follow-up (`not is_frame_followup(message)`); a genuine follow-up was
already handled by SEM-GEN-1 above. `frame_ack_reply` was reworded to
carry the frame in natural prose with no "(สินค้า X)" parenthetical.
"""
import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-owner06")

from services.conversation_semantics import derive_active_frame, frame_ack_reply, Frame


def _engine():
    from tests.test_business_action_registry import reset_real_registry
    reset_real_registry()
    from services.decision_engine import DecisionEngine
    return DecisionEngine()


_ENGINE = _engine()

_DISCOVERY = ("ได้ค่ะ 😊 ถ้ามีลิงก์สินค้าที่สนใจจาก Taobao, 1688 หรือ Tmall ส่งมาได้เลยนะคะ "
             "ถ้ายังไม่มีลิงก์ บอกคร่าว ๆ ได้เลยว่าอยากสั่งสินค้าอะไร เดี๋ยวช่วยแนะนำขั้นตอนต่อให้ค่ะ")

# one prior owner-test round: import -> product -> correction -> link -> rate
_ROUND = [
    {"role": "user", "content": "อยากสั่งของจากจีน"},
    {"role": "assistant", "content": _DISCOVERY},
    {"role": "user", "content": "เป็นโต๊ะ"},
    {"role": "assistant", "content": "รับทราบค่ะ (สินค้า โต๊ะ) รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"},
    {"role": "user", "content": "เปลี่ยนเป็นเก้าอี้"},
    {"role": "assistant", "content": "ได้ค่ะ เปลี่ยนเป็นเก้าอี้ได้เลยค่ะ 😊 รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"},
    {"role": "user", "content": "https://detail.1688.com/offer/652702302959.html"},
    {"role": "assistant", "content": "แปลงลิงก์ให้เรียบร้อยแล้วค่ะ 😊\nhttps://fasttrade.in.th/x"},
    {"role": "user", "content": "แล้วค่าขนส่งทางเรือกิโลละเท่าไหร่"},
    {"role": "assistant", "content": "ทางเรือคิด 19 บาท/กิโลกรัม ค่ะ"},
]

_POLICY = ("คืนสินค้า", "เงื่อนไขการคืน", "return", "refund", "[RAG",
           "ไม่ได้อยู่ในรายการสินค้าที่ห้าม")
_FAKE_CS = ("ประสานเจ้าหน้าที่", "ให้เจ้าหน้าที่ตรวจสอบ", "ทีมงานจะติดต่อกลับ")
_TOKENS = ("(สินค้า", "(product", "(slot", "frame=", "journey=", "product=")


def _replay(seed, *, handoff="NONE"):
    from tests.phase6c_lab.harness import WebhookConversation
    c = WebhookConversation(engine=_ENGINE, mode="deterministic",
                            handoff_status=handoff, rag_answer="[RAG-GENERIC-IMPORT-ADVICE]",
                            rag_conf=0.55)
    if seed:
        c.seed_history(list(seed))
    out = []
    for m in ["อยากสั่งของจากจีน", "เป็นชั้นวางของ", "เปลี่ยนเป้นรองเท้าได้ไหม", "20 คู่"]:
        out.append(c.send(m))
    frame = derive_active_frame(c.history)
    return out, frame, c


class _ReplayAsserts:
    def _assert_journey(self, turns, frame, *, label):
        t1, t2, t3, t4 = turns
        # turn 1 — FIX-01 discovery, not RAG, no fake staff
        self.assertNotEqual(t1["routing"], "RAG", f"{label} t1 routing")
        self.assertIn(t1["selection_source"], ("phase6b_service_intent",), f"{label} t1 src={t1['selection_source']}")
        self.assertIsNone(t1["handoff_reason"], f"{label} t1 handoff")
        for w in _FAKE_CS:
            self.assertNotIn(w, t1["reply"], f"{label} t1 fake CS")
        # turn 2 — product slot consumed, journey continues (not RAG)
        self.assertNotEqual(t2["routing"], "RAG", f"{label} t2 routing")
        self.assertIn("ชั้นวางของ", t2["reply"], f"{label} t2 product ack")
        # turn 3 — correction against the same frame
        self.assertNotEqual(t3["routing"], "RAG", f"{label} t3 routing")
        self.assertIn("รองเท้า", t3["reply"], f"{label} t3 correction")
        # final frame
        self.assertIsNotNone(frame, f"{label} final frame")
        self.assertEqual(frame.product, "รองเท้า", f"{label} final product")
        self.assertEqual(frame.quantity, 20, f"{label} final quantity")
        # no policy misroute, no visible state token anywhere
        for i, t in enumerate(turns, 1):
            for w in _POLICY:
                self.assertNotIn(w, t["reply"], f"{label} t{i} policy leak: {t['reply']!r}")
            for w in _TOKENS:
                self.assertNotIn(w, t["reply"], f"{label} t{i} state token: {t['reply']!r}")


class TestRealWebhookReplay(_ReplayAsserts, unittest.TestCase):
    def test_empty_history(self):
        turns, frame, _ = _replay([])
        self._assert_journey(turns, frame, label="empty")

    def test_10_turn_history(self):
        turns, frame, _ = _replay(_ROUND)
        self._assert_journey(turns, frame, label="10-turn")

    def test_30_turn_history(self):
        turns, frame, _ = _replay(_ROUND * 3)
        self._assert_journey(turns, frame, label="30-turn")

    def test_50_turn_history(self):
        turns, frame, _ = _replay(_ROUND * 5)
        self._assert_journey(turns, frame, label="50-turn")

    def test_prior_link_and_rate_history(self):
        seed = _ROUND * 2 + [
            {"role": "user", "content": "อยากได้ลิงก์เว็บ Taobao"},
            {"role": "assistant", "content": "ลิงก์เว็บไซต์หลัก: Taobao https://www.taobao.com ..."},
        ]
        turns, frame, _ = _replay(seed)
        self._assert_journey(turns, frame, label="link+rate")

    def test_handoff_notified_history(self):
        turns, frame, _ = _replay(_ROUND * 2, handoff="NOTIFIED")
        self._assert_journey(turns, frame, label="handoff-NOTIFIED")


class TestFrameAckHasNoStateToken(unittest.TestCase):
    def test_all_ack_variants_are_token_free_and_round_trip(self):
        cases = [
            ("none", Frame(product="รองเท้า")),
            ("none", Frame(product="รองเท้า", quantity=20, method="sea")),
            ("quantity", Frame(product="รองเท้า", quantity=20)),
            ("method", Frame(product="รองเท้า", quantity=20, method="sea")),
            ("product", Frame(product="เก้าอี้")),
            ("product", Frame(product="เก้าอี้", quantity=10)),
        ]
        for ch, fr in cases:
            txt = frame_ack_reply(fr, changed=ch)
            for tok in _TOKENS:
                self.assertNotIn(tok, txt, f"{ch}: {txt!r}")
            g = derive_active_frame([{"role": "user", "content": "สนใจนำเข้าของ"},
                                     {"role": "assistant", "content": txt}])
            self.assertIsNotNone(g, f"{ch} recovery")
            self.assertEqual(g.product, fr.product, f"{ch} product: {txt!r}")
            if fr.quantity:
                self.assertEqual(g.quantity, fr.quantity, f"{ch} quantity: {txt!r}")
            if fr.method and ch != "product":
                self.assertEqual(g.method, fr.method, f"{ch} method: {txt!r}")


class TestOldTokenFixturesStillParse(unittest.TestCase):
    def test_legacy_paren_token_backward_compatible(self):
        # persisted history from before the reword must still reconstruct
        g = derive_active_frame([
            {"role": "user", "content": "อยากสั่งของจากจีน"},
            {"role": "assistant", "content": "รับทราบค่ะ (สินค้า เครื่องจักร) รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"}])
        self.assertEqual(g.product, "เครื่องจักร")


if __name__ == "__main__":
    unittest.main()
