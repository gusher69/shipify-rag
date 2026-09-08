# -*- coding: utf-8 -*-
"""OWNER-REAL-LINE-FIX-01 — broad IMPORT_INTEREST response + LINE chunking.

Real LINE reproduction:
    "สวัสดีค่ะ"  -> greeting
    "อยากสั่งของจากจีน"
      OLD: a RAG "ฝากสั่ง" answer that assumed the customer already had a
           product link, mentioned "ส่งลิงก์มาให้เจ้าหน้าที่ตรวจสอบ", and was
           split into 3 LINE bubbles (one fragment was just "...คุณ").

Expected now: ONE coherent reply that offers BOTH paths (send a link, or
say what you want) — no auth, no fake Human CS — and never a mid-sentence
bubble split.
"""
import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-owner01")

from tests.test_business_action_registry import reset_real_registry
from services.message_segmenter import segment_message
from services.link_conversion_flow import is_link_conversion_signal

_GREET_HISTORY = [
    {"role": "user", "content": "สวัสดีค่ะ"},
    {"role": "assistant", "content": "สวัสดีค่ะ 😊 มีอะไรให้ช่วยไหมคะ?"},
]

_AUTH_MARKERS = ("รหัสลูกค้า", "ยืนยันตัวตน", "เบอร์ที่ผูก")
_FAKE_CS_MARKERS = ("ให้เจ้าหน้าที่ตรวจสอบ", "แอดมินช่วยตรวจสอบ", "ทีมงานจะติดต่อกลับ",
                    "ส่งเรื่องให้เจ้าหน้าที่")


def _decide(msg, history):
    reset_real_registry()
    from services.decision_engine import DecisionEngine
    return DecisionEngine().decide(msg, history=list(history), context={"developer_mode": True})


class TestBroadImportInterestResponse(unittest.TestCase):
    BROAD = ["อยากสั่งของจากจีน", "อยากนำเข้าของจากจีน", "สนใจสั่งของจีน",
             "อยากซื้อของจาก 1688 แต่ยังไม่มีลิงก์"]

    def test_broad_requests_get_one_coherent_two_path_reply(self):
        for m in self.BROAD:
            r = _decide(m, _GREET_HISTORY)
            reply = r["reply"]["text"]
            dev = r.get("developer") or {}
            with self.subTest(msg=m):
                # not routed to a link-only / KB dead-end
                self.assertEqual(r["routing"]["type"], "GENERAL", reply)
                self.assertEqual(dev.get("selection_source"), "phase6b_service_intent")
                # BOTH paths present
                self.assertIn("ลิงก์", reply)
                self.assertTrue("ยังไม่มีลิงก์" in reply or "ไม่มีลิงก์" in reply, reply)
                self.assertTrue("อยากสั่งสินค้าอะไร" in reply or "สินค้าอะไร" in reply, reply)
                # no auth, no fake Human CS
                for a in _AUTH_MARKERS:
                    self.assertNotIn(a, reply)
                for f in _FAKE_CS_MARKERS:
                    self.assertNotIn(f, reply)
                # never split into multiple bubbles
                seg = segment_message(reply, reply_mode="auto")
                self.assertEqual(seg.message_count, 1, seg.message_parts)

    def test_no_business_action_selected_and_not_a_continuation(self):
        for m in self.BROAD:
            r = _decide(m, _GREET_HISTORY)
            dev = r.get("developer") or {}
            ics = dev.get("information_collection_status") or {}
            with self.subTest(msg=m):
                self.assertIn(ics.get("selected_business_action"), (None, "", "None"))
                self.assertNotEqual(dev.get("selection_source"), "conversation_continuation")


class TestPreservedRouting(unittest.TestCase):
    def test_generic_import_interest_without_china_still_reaches_rag(self):
        from unittest.mock import patch
        from tests.test_decision_engine import _fake_playground_result
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="ฝากนำเข้าใช้บริการยังไง...", confidence=0.9)):
            r = _decide("สนใจนำเข้าสินค้าครับ", [])
        self.assertEqual(r["routing"]["type"], "RAG")

    def test_interest_plus_own_question_still_reaches_rag(self):
        from unittest.mock import patch
        from tests.test_decision_engine import _fake_playground_result
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="สินค้าห้ามนำเข้า ได้แก่ ...", confidence=0.9)):
            r = _decide("สนใจนำเข้าสินค้าจากจีน ห้ามนำเข้าอะไรบ้าง", [])
        self.assertEqual(r["routing"]["type"], "RAG")

    def test_real_link_conversion_still_recognised(self):
        # correct-spelling forms; the "ลิ้ง" typo form is normalised
        # upstream by PHASE-6D and covered there.
        for m in ("แปลงลิงก์ 1688 ให้หน่อย", "ช่วยแปลงลิงก์ Taobao",
                  "ลิงก์นี้ https://item.taobao.com/i.htm?id=1 แปลงหน่อย",
                  "ขอลิงก์ Taobao หน่อย"):
            self.assertTrue(is_link_conversion_signal(m), m)

    def test_no_link_negation_is_not_a_conversion_signal(self):
        for m in ("อยากซื้อของจาก 1688 แต่ยังไม่มีลิงก์", "ยังไม่มีลิงก์สินค้าเลยค่ะ",
                  "ผมยังไม่ได้ลิงก์จาก taobao"):
            self.assertFalse(is_link_conversion_signal(m), m)


class TestChunkingNeverSplitsMidSentence(unittest.TestCase):
    def test_repro_reply_is_one_bubble(self):
        repro = ("หากต้องการฝากสั่งของจากจีน คุณสามารถวางลิงก์สินค้าจากเว็บไซต์ Taobao, 1688 "
                 "และ Tmall ได้ค่ะ ถ้าไม่แน่ใจว่าสินค้าหรือร้านที่คุณสนใจรองรับหรือไม่ "
                 "สามารถส่งลิงก์มาให้เจ้าหน้าที่ตรวจสอบก่อนได้ค่ะ")
        seg = segment_message(repro, reply_mode="auto")
        self.assertEqual(seg.message_count, 1, seg.message_parts)
        for p in seg.message_parts:
            self.assertNotEqual(p.strip(), "คุณ")
            self.assertFalse(p.strip().endswith("คุณ"), p)
            self.assertFalse(p.strip().startswith("สามารถ"), p)

    def test_a_genuine_paragraph_break_still_splits(self):
        two_para = ("ทางเรือคิด 19 บาท/กิโลกรัม หรือ 4,500 บาท/CBM ค่ะ\n\n"
                    "ทางรถคิด 35 บาท/กิโลกรัม หรือ 6,900 บาท/CBM ค่ะ")
        seg = segment_message(two_para, reply_mode="auto")
        self.assertGreaterEqual(seg.message_count, 2)

    def test_prose_split_only_at_a_sentence_end(self):
        # a "หาก" that follows a complete sentence -> a valid split point
        ok = ("ประเมินเบื้องต้นทางเรือประมาณ 270 บาทค่ะ "
              "หากต้องการให้คิดทางรถด้วย แจ้งได้เลยนะคะ")
        seg = segment_message(ok, reply_mode="auto")
        for p in seg.message_parts:
            self.assertNotIn("ค่ะ หาก", p)  # the boundary landed between them


if __name__ == "__main__":
    unittest.main()
