# -*- coding: utf-8 -*-
"""PHASE 6 C — A MISSING BACKEND API MUST NOT LICENSE BAD CONVERSATION.

Most remaining customer requirements are blocked on ERP endpoints that
do not exist yet (AI_API_Requirement_For_Client.xlsx asks the customer's
dev team to BUILD them: tracking, order status, wallet, documents,
tracking->bill mapping, …). No code change in this repo can make those
endpoints appear — but the bot's behaviour while they are missing is
entirely our responsibility, and that IS testable.

For every such turn this suite asserts the safe-fallback contract:

  1. ANSWER-BEARING      — says something; never a bare "no information".
  2. NO FALSE COMPLETION — never claims the action already happened
                           (the hard "FALSE ACTION COMPLETION = 0" rule).
  3. NO INVENTED STATE   — when no ERP call executed, the reply carries
                           no status / date / amount / identifier the
                           customer did not supply.
  4. PROGRESSES          — either collects the identifier it needs, or
                           routes to a human; never a dead end.
  5. NO LOOP             — once the customer supplies what was asked
                           for, the same thing is not asked again.

The messages are the customers' own, taken from the UAT master set.
"""
import re
import unittest
from unittest.mock import MagicMock, patch

from services.decision_engine import DecisionEngine
from tests.test_business_action_registry import reset_real_registry
from tests.customer_uat.run_baseline import _run_one


# ── the customer turns that are blocked on a missing endpoint ────────
# (customer_uat case id -> the customer's own wording)
API_GAP_TURNS = {
    "CUS-G11": "ได้รับสินค้าไม่ครบ, เคลมสินค้ายังไงคะ",
    "CUS-G17": "ติดตามสถานะ สินค้า",
    "CUS-G18": "ยอดเงินไม่เข้า, เติมเงินแล้วรอตรวจสอบ",
    "CUS-S02": "ต้องการแก้จำนวนสินค้าในบิล",
    "CUS-S03": "สามารถเปลี่ยนเป็นจัดส่งทางรถ,ทางเรือได้ไหมคะ",
    "CUS-S04": "ลืมเลือก VAT ไปค่ะ ,ต้องการVATด้วยค่ะ",
    "CUS-S08": "บิลขนส่งนี้ หรือแทรคนี้เป็นของบิลสั่งซื้อไหน",
    "CUS-S11": "บิลขนส่ง FT ต้องการเปลี่ยนเป็นรับเอง",
    "CUS-S13": "บิลซ้ำค่ะ",
    "CUS-S15": "ใส่ที่อยู่โกดังจีนถูกไหมคะ",
    "CUS-S10": "รีเเพ็คค่ะ",
    "CUS-S16": "รวมบิลเหมารถค่ะ",
    "CUS-S06": "ต้องการสั่งผลิตตามสเปค ,สั่งสกรีนโลโก้ได้ไหมคะ",
}

# 2. a PAST-TENSE completion claim. Future intent ("แอดมินจะเช็คให้นะคะ")
#    is fine and matches the CS team's own scripts; "…ให้เรียบร้อยแล้ว"
#    asserts a thing that demonstrably did not happen.
_FALSE_COMPLETION_RE = re.compile(
    r"เรียบร้อยแล้ว|ดำเนินการให้แล้ว|ดำเนินการเรียบร้อย"
    r"|(?:แก้ไข|เปลี่ยน|ยกเลิก|ลบ|รวม|ถอน|คืนเงิน|อัปเดต|อัพเดท)\S{0,10}ให้(?:เรียบร้อย)?แล้ว"
    r"|(?:ติดต่อ|แจ้ง|สอบถาม)(?:ร้าน|ทางร้าน|โกดัง|เจ้าหน้าที่)\S{0,6}(?:ให้)?แล้ว"
    r"|ออกใบกำกับให้แล้ว|โอนเงินให้แล้ว|จัดส่งให้แล้ว")

# 3. shapes that would be INVENTED private state if no ERP call ran.
_INVENTED_STATE_RE = re.compile(
    r"\b(?:PO|POS|PA|PE|FT|FE|SA|SP)\d{3,}"          # a concrete bill no.
    r"|\b\d{9,}\b"                                    # a tracking-shaped number
    r"|\d{1,2}/\d{1,2}/\d{2,4}"                       # a concrete date
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*บาท")           # a concrete money amount

# 4. progress = asks for something, or escalates.
_ASKS_RE = re.compile(
    r"รบกวน|ขอ(?:เลข|ที่อยู่|รหัส|ข้อมูล|สลิป|รูป)|แจ้ง|ส่ง\S{0,6}มา|ระบุ|ไหนคะ|ไหมคะ|อะไรคะ|\?")
_ESCALATES_RE = re.compile(r"เจ้าหน้าที่|แอดมิน|ทีมงาน|ติดต่อกลับ")

_BARE_NO_INFO_RE = re.compile(r"^\s*(?:ตอนนี้)?ยังไม่มีข้อมูล[^\n]{0,40}$")


def _engine():
    reset_real_registry()
    return DecisionEngine()


class TestApiGapSafeFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = _engine()
        cls.results = {}
        for cid, msg in API_GAP_TURNS.items():
            r = _run_one(cls.eng, msg, history=None)
            cls.results[cid] = {
                "msg": msg,
                "reply": (r.get("reply_text") or r.get("reply") or ""),
                "erp_called": bool(r.get("erp_called")),
                "routing": r.get("routing"),
            }

    def test_1_every_turn_is_answer_bearing(self):
        bad = [f"{c}: {d['reply'][:60]!r}" for c, d in self.results.items()
               if not d["reply"].strip() or _BARE_NO_INFO_RE.match(d["reply"].strip())]
        self.assertEqual(bad, [], "dead-end / bare no-info reply:\n" + "\n".join(bad))

    def test_2_no_false_action_completion(self):
        bad = []
        for c, d in self.results.items():
            if d["erp_called"]:
                continue
            m = _FALSE_COMPLETION_RE.search(d["reply"])
            if m:
                bad.append(f"{c}: claims {m.group(0)!r} with no ERP call -> {d['reply'][:80]!r}")
        self.assertEqual(bad, [], "FALSE ACTION COMPLETION:\n" + "\n".join(bad))

    def test_3_no_invented_private_state(self):
        bad = []
        for c, d in self.results.items():
            if d["erp_called"]:
                continue
            # anything the CUSTOMER themselves put in the turn is not invented
            leftover = _INVENTED_STATE_RE.findall(d["reply"])
            leftover = [x for x in leftover if x not in d["msg"]]
            if leftover:
                bad.append(f"{c}: invented {leftover!r} -> {d['reply'][:80]!r}")
        self.assertEqual(bad, [], "INVENTED PRIVATE STATE:\n" + "\n".join(bad))

    def test_4_every_turn_progresses_the_conversation(self):
        bad = []
        for c, d in self.results.items():
            if not (_ASKS_RE.search(d["reply"]) or _ESCALATES_RE.search(d["reply"])
                    or d["routing"] == "HUMAN_HANDOFF"):
                bad.append(f"{c}: neither collects nor escalates -> {d['reply'][:80]!r}")
        self.assertEqual(bad, [], "DEAD END:\n" + "\n".join(bad))

    def test_5_supplying_the_identifier_does_not_re_ask_for_it(self):
        """NO LOOP: the exact failure the customer reported in the docx
        ('AI ขอรหัส แล้วถามรหัสใหม่อีก') must not recur for ERP flows."""
        bad = []
        for cid in ("CUS-S02", "CUS-S04", "CUS-S08"):
            msg = API_GAP_TURNS[cid]
            first = self.results[cid]["reply"]
            hist = [{"role": "user", "content": msg},
                    {"role": "assistant", "content": first}]
            follow = _run_one(self.eng, "PO123456789", history=hist)
            reply2 = (follow.get("reply_text") or follow.get("reply") or "")
            # the same identifier request, verbatim, twice in a row is a loop
            if reply2.strip() and reply2.strip() == first.strip():
                bad.append(f"{cid}: identical reply repeated after the id was supplied")
        self.assertEqual(bad, [], "LOOP:\n" + "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
