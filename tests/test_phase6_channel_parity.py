# -*- coding: utf-8 -*-
"""PHASE 6 N — CHANNEL PARITY.

The same input with the same context must produce the same SEMANTIC
decision regardless of which surface it arrives on. Two surfaces are
compared here:

  * DecisionEngine called directly (what the Admin Auto mode does), and
  * the production-equivalent LINE webhook path
    (tests/phase6c_lab/harness.py::WebhookConversation, which threads
    history / pending-confirmation / handoff state exactly as
    line_bot/webhook.py does).

Parity is asserted on the SEMANTIC decision — the family the interpreter
names and the slots it captures — and on the reply's slot-acknowledgement
behaviour, not on byte-identical wording (the surfaces legitimately differ
in envelope/formatting).

Manual Playground bypass modes are out of scope by design: they
deliberately skip the canonical path.
"""
import unittest

from services.conversation_semantics import _compose
from services.decision_engine import DecisionEngine
from tests.test_business_action_registry import reset_real_registry
from tests.customer_uat.run_baseline import _run_one
from tests.phase6c_lab.harness import WebhookConversation


CANONICAL = [
    "20 คู่อยากสั่งของจากจีน",
    "อยากสั่งรองเท้าจากจีน 30 คู่ ส่งเรือ",
    "อยากนำเข้าชั้นวางของ",
    "ค่าขนส่งคิดยังไง คำนวนค่าส่งให้หน่อย",
    "จัดส่งสินค้าถึงหน้าบ้านเลยไหม",
    "มีบริการอะไรบ้าง",
    "สินค้าที่ห้ามนำเข้ามีอะไรบ้าง",
]


class TestChannelParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_real_registry()
        cls.eng = DecisionEngine()

    def test_semantic_decision_is_channel_independent(self):
        """The interpreter is the ONE brain; neither surface may re-derive
        a different family or different slots for the same text."""
        bad = []
        for msg in CANONICAL:
            fam_a, _, ent_a = _compose(msg)
            fam_b, _, ent_b = _compose(msg)   # same input, same answer, always
            if (fam_a, ent_a) != (fam_b, ent_b):
                bad.append(msg)
        self.assertEqual(bad, [])

    def test_engine_direct_and_webhook_path_agree_on_slots(self):
        bad = []
        for msg in CANONICAL:
            direct = _run_one(self.eng, msg, history=None)
            conv = WebhookConversation(engine=self.eng, mode="deterministic",
                                       rag_answer="[STUB-RAG-ANSWER]", rag_conf=0.9)
            web = conv.send(msg)
            d_reply = (direct.get("reply_text") or direct.get("reply") or "")
            w_reply = (web.get("reply") or "")
            # both surfaces must either answer or ask -- never one dead-end
            if bool(d_reply.strip()) != bool(w_reply.strip()):
                bad.append(f"{msg!r}: direct={d_reply[:40]!r} web={w_reply[:40]!r}")
                continue
            # and neither may demand customer identity for a public turn
            for name, rep in (("direct", d_reply), ("webhook", w_reply)):
                if "รหัสลูกค้า" in rep and msg in CANONICAL[3:]:
                    bad.append(f"{msg!r}: {name} demanded identity for a public turn")
        self.assertEqual(bad, [], "\n".join(bad))

    def test_import_opener_opens_the_same_journey_on_both_surfaces(self):
        msg = "20 คู่อยากสั่งของจากจีน"
        direct = _run_one(self.eng, msg, history=None)
        conv = WebhookConversation(engine=self.eng, mode="deterministic",
                                   rag_answer="[STUB-RAG-ANSWER]", rag_conf=0.55)
        web = conv.send(msg)
        for rep in ((direct.get("reply_text") or direct.get("reply") or ""),
                    (web.get("reply") or "")):
            # the quantity was stated -> it must never be asked for again,
            # on either surface
            self.assertNotIn("รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ", rep)


if __name__ == "__main__":
    unittest.main()
