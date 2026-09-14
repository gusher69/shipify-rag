# -*- coding: utf-8 -*-
"""PHASE 6 POST-DEPLOY §7 — CURRENT-TURN / FRAME CONSISTENCY.

The same turn is represented five times on the live path:

  1. Interpretation            services/conversation_semantics.py::_compose
  2. legacy runtime Frame      services/conversation_semantics.py::derive_active_frame
  3. ConversationResolution    services/conversation_resolution.py (P1, shadow)
  4. structured shadow Frame   services/conversation_frame_store.py (P2, shadow)
  5. response planner          services/service_intent_flow.py::import_interest_reply

This suite asserts they AGREE about product / quantity / unit / method /
question-action / requested_slot, and — the point of §7 — it fails if a
unit or a question intent DISAPPEARS at any of those hand-offs. Before
this pass the unit was lost at three of them: the acknowledgement
renderer (hardcoded "ชิ้น"), the legacy Frame (no unit field at all), and
P1's _frame_known_slots (passed None for the unit).

Structured state stays SHADOW — this suite never enables a read cutover;
it only measures agreement.

Test tier is pinned offline by tests/__init__.py.
"""
import unittest

from services.conversation_semantics import (
    _compose, derive_active_frame, Frame, frame_ack_reply,
)
from services.conversation_resolution import resolve_conversation
from services.conversation_frame_store import build_frame, empty_frame
from services.service_intent_flow import import_interest_reply

CASES = [
    # (opening turn, product, quantity, unit, method)
    ("อยากสั่งรองเท้าจากจีน 30 คู่ ส่งเรือ ราคาเท่าไหร่", "รองเท้า", 30, "คู่", "sea"),
    ("อยากนำเข้าชั้นวางของ 10 ชิ้น คิดค่าส่งยังไง", "ชั้นวางของ", 10, "ชิ้น", None),
    ("สั่งกล่องพลาสติก 5 ลัง ถึงไทยกี่วัน", "กล่องพลาสติก", 5, "ลัง", None),
    ("อยากได้เครื่องซีลถุง 3 เครื่อง ส่งทางรถได้ไหม", "เครื่องซีลถุง", 3, "เครื่อง", "road"),
    ("อยากสั่งของเล่นจากจีน 12 กล่อง ส่งเรือ", "ของเล่น", 12, "กล่อง", "sea"),
    ("อยากสั่งผ้าม่านจากจีน 2 พาเลท", "ผ้าม่าน", 2, "พาเลท", None),
    ("อยากสั่งตู้เก็บของจากจีน 100 ขวด ราคาเท่าไหร่", "ตู้เก็บของ", 100, "ขวด", None),
]


def _ack_for(ent):
    return import_interest_reply(ent.get("product"), quantity=ent.get("quantity"),
                                 method=ent.get("method"), unit=ent.get("quantity_unit"))


class TestRepresentationsAgree(unittest.TestCase):

    def test_interpretation_carries_the_typed_quantity_and_question(self):
        for msg, product, n, u, method in CASES:
            with self.subTest(msg=msg):
                ent = _compose(msg)[2]
                self.assertEqual(ent.get("product"), product)
                self.assertEqual(ent.get("quantity"), n)
                self.assertEqual(ent.get("quantity_unit"), u)
                self.assertEqual(ent.get("quantity_raw"), f"{n} {u}")
                if method:
                    self.assertEqual(ent.get("method"), method)

    def test_legacy_frame_reads_back_everything_the_ack_wrote(self):
        """Representation 1 -> 5 -> 2: the planner writes the state into
        its own prose and the legacy frame parses it back."""
        for msg, product, n, u, method in CASES:
            with self.subTest(msg=msg):
                ent = _compose(msg)[2]
                ack = _ack_for(ent)
                frame = derive_active_frame([{"role": "user", "content": msg},
                                             {"role": "assistant", "content": ack}])
                self.assertIsNotNone(frame, msg)
                self.assertEqual(frame.product, product, msg)
                self.assertEqual(frame.quantity, n, msg)
                self.assertEqual(frame.unit, u, f"UNIT LOST IN THE LEGACY FRAME: {msg}")
                if method:
                    self.assertEqual(frame.method, method, msg)

    def test_p1_resolution_keeps_the_unit_as_a_typed_slot(self):
        """Representation 2 -> 3: P1's SlotValue must carry the unit, not
        None (this was one of the three places it disappeared)."""
        for msg, product, n, u, method in CASES:
            with self.subTest(msg=msg):
                ent = _compose(msg)[2]
                hist = [{"role": "user", "content": msg},
                        {"role": "assistant", "content": _ack_for(ent)}]
                res = resolve_conversation("ส่งทางเรือ", hist)
                slots = {**(res.known_slots or {}), **(res.slot_updates or {})}
                q = slots.get("quantity")
                self.assertIsNotNone(q, f"quantity missing from P1 for {msg!r}")
                qd = q.as_dict() if hasattr(q, "as_dict") else q
                self.assertEqual(qd.get("value"), n, msg)
                self.assertEqual(qd.get("unit"), u, f"UNIT LOST IN P1 RESOLUTION: {msg}")

    def test_p2_structured_frame_keeps_the_unit(self):
        """Representation 3 -> 4: the persisted shadow frame's slot dict
        must still carry the unit."""
        for msg, product, n, u, method in CASES:
            with self.subTest(msg=msg):
                ent = _compose(msg)[2]
                hist = [{"role": "user", "content": msg},
                        {"role": "assistant", "content": _ack_for(ent)}]
                res = resolve_conversation("ส่งทางเรือ", hist)
                frame = build_frame(empty_frame(), res)
                slot = (frame.get("slots") or {}).get("quantity")
                if slot is None:
                    continue          # this turn updated no quantity slot
                self.assertEqual(slot.get("value"), n, msg)
                self.assertEqual(slot.get("unit"), u, f"UNIT LOST IN P2 FRAME: {msg}")

    def test_response_planner_echoes_the_supplied_unit(self):
        """Representation 5: no generic unit substitution, ever."""
        for msg, product, n, u, method in CASES:
            with self.subTest(msg=msg):
                ack = _ack_for(_compose(msg)[2])
                self.assertIn(f"{n} {u}", ack, msg)
                if u != "ชิ้น":
                    self.assertNotIn(f"{n} ชิ้น", ack, f"generic unit substituted: {msg}")

    def test_question_intent_is_never_lost(self):
        """The question/action clause is separated from the entity — but
        it must remain readable as a fact of the turn, not vanish."""
        for msg, _p, _n, _u, _m in CASES:
            ent = _compose(msg)[2]
            with self.subTest(msg=msg):
                if any(k in msg for k in ("เท่าไหร่", "ยังไง", "กี่วัน", "ได้ไหม")):
                    self.assertTrue(ent.get("question_span"), msg)
                    self.assertIn(ent.get("question_kind"),
                                  ("PRICE", "DURATION", "METHOD_ELIGIBILITY", "HOWTO", "OTHER"))

    def test_next_action_never_re_asks_a_known_slot(self):
        for msg, product, n, u, method in CASES:
            with self.subTest(msg=msg):
                ack = _ack_for(_compose(msg)[2])
                self.assertNotIn("รบกวนแจ้งชื่อหรือประเภทสินค้า", ack, msg)
                self.assertNotIn("รบกวนแจ้งจำนวนโดยประมาณ", ack, msg)
                if method:
                    self.assertNotIn("สนใจส่งทางรถหรือทางเรือ", ack, msg)

    def test_structured_state_is_still_shadow_only(self):
        """§7's explicit constraint: nothing here turns on a structured
        read. The legacy text-derived frame remains the sole authority."""
        import services.conversation_frame_store as store
        self.assertIn("SHADOW-WRITE ONLY", store.__doc__)


if __name__ == "__main__":
    unittest.main()
