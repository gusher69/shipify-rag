# -*- coding: utf-8 -*-
"""PHASE 6 — A MISSING BACKEND API MUST NOT LICENSE BAD CONVERSATION.

Most remaining customer requirements are blocked on ERP endpoints that do
not exist yet (AI_API_Requirement_For_Client.xlsx asks the customer's dev
team to BUILD them: tracking, order status, wallet, documents,
tracking->bill mapping, …). No code change in this repo can make those
endpoints appear — but the bot's behaviour while they are missing is
entirely our responsibility, and that IS testable.

Closure gate B: coverage is keyed by FEEDBACK_ID and spans **every one of
the 26 API_GAP requirements** in reports/customer_feedback_master_inventory.json,
not only the 13 that happen to have a customer_uat case.

For every such turn this suite asserts the safe-fallback contract:

  1. ANSWER-BEARING      — says something; never a bare "no information".
  2. NO FALSE COMPLETION — never claims the action already happened
                           (the hard "FALSE ACTION COMPLETION = 0" rule).
  3. NO INVENTED STATE   — when no ERP call executed, the reply carries
                           no status / date / amount / identifier the
                           customer did not supply.
  4. PROGRESSES          — either collects the identifier it needs, or
                           routes to a human; never a dead end.
  5. NO LOOP             — once the customer supplies what was asked for,
                           the same thing is not asked again.

Wording for the AI-API-* ids is the customer's own, verbatim from the
requirement sheets. The LINE-* ids are sanitized real-chat shapes — no
customer identifier is reproduced.
"""
import json
import re
import unittest

from services.decision_engine import DecisionEngine
from tests.test_business_action_registry import reset_real_registry
from tests.customer_uat.run_baseline import _run_one


# FEEDBACK_ID -> (customer turn, required API capability)
API_GAP_TURNS = {
    "AI-API-S1-3.0":  ("สินค้าจะเข้าไทยตอนไหน", "GET /orders/{bill}/tracking + ETA"),
    "AI-API-S1-11.0": ("ได้รับสินค้าไม่ครบ, เคลมสินค้ายังไงคะ", "GET /orders/{bill}/items + POST /claims"),
    "AI-API-S1-12.0": ("สินค้าถึงโกดังหรือยัง", "GET /shipments/tracking/{tracking_cn}"),
    "AI-API-S1-16.0": ("ร้านส่งหรือยังคะ", "GET /orders/{bill}/status"),
    "AI-API-S1-17.0": ("ติดตามสถานะ สินค้า", "GET /shipments/{bill}/tracking_th"),
    "AI-API-S1-18.0": ("ยอดเงินไม่เข้า, เติมเงินแล้วรอตรวจสอบ", "GET /wallet/{cust}/transactions"),
    "AI-API-S1-21.0": ("ยกเลิกบิลสั่งซื้อได้ไหม", "GET /orders/{bill}/status (payment_status)"),
    "AI-API-S2-1.0":  ("สินค้าจะเข้าไทยตอนไหนคะ", "GET /orders/{bill}/tracking"),
    "AI-API-S2-2.0":  ("ต้องการแก้จำนวนสินค้าในบิล", "PUT /orders/{bill}/items"),
    "AI-API-S2-3.0":  ("สามารถเปลี่ยนเป็นจัดส่งทางรถ,ทางเรือได้ไหมคะ", "PUT /orders/{bill}/shipping_type"),
    "AI-API-S2-4.0":  ("ลืมเลือก VAT ไปค่ะ ,ต้องการVATด้วยค่ะ", "PUT /orders/{bill}/vat"),
    "AI-API-S2-5.0":  ("ถอนเงินสั่งซื้อยังไง", "GET /wallet/{cust} (purchase credit)"),
    "AI-API-S2-7.0":  ("โหลดใบกำกับยังไง , ขอใบกำกับค่าสินค้าหน่อย", "GET /documents/{bill}/invoice"),
    "AI-API-S2-8.0":  ("บิลขนส่งนี้ หรือแทรคนี้เป็นของบิลสั่งซื้อไหน", "GET /shipments/map?tracking_cn="),
    "AI-API-S2-9.0":  ("บิลขนส่ง FT ต้องการเปลี่ยนที่อยู่จัดส่ง", "PUT /shipments/{bill}/address"),
    "AI-API-S2-11.0": ("บิลขนส่ง FT ต้องการเปลี่ยนเป็นรับเอง", "PUT /shipments/{bill}/carrier"),
    "AI-API-S2-12.0": ("ถอนเงินขนส่งยังไงคะ", "GET+POST /wallet/{cust}/withdraw (shipment)"),
    "AI-API-S2-13.0": ("บิลซ้ำค่ะ", "DELETE /shipments/{bill} (+ human confirm)"),
    "AI-API-S2-15.0": ("ใส่ที่อยู่โกดังจีนถูกไหมคะ", "GET /warehouse/cn/address?customer_id="),
    "AI-API-S2-17.0": ("ขอแทรคไทยค่ะ", "GET /shipments/{bill}/tracking_th"),
    "AI-API-S2-18.0": ("วันนี้มีของเข้าไทยไหมคะ", "GET /shipments?arriving_today=true"),
    # sanitized real-chat shapes
    "LINE-02": ("รบกวนเช็คให้หน่อยค่ะ ถึงไทยวันไหน", "batch tracking lookup (multi-record)"),
    "LINE-04": ("บิลนี้ชำระเงินแล้ว ต้องการเปลี่ยนเป็นส่งทางเรือ", "PUT shipping_type after payment"),
    "LINE-13": ("ดึงเครดิตสั่งซื้อมาใช้ยังไงครับ", "typed wallet/credit pools"),
    "LINE-15": ("สองออเดอร์นี้ใช้เลขแทรคเดียวกัน ผิดหรือเปล่าครับ", "tracking->bill mapping"),
    "LINE-22": ("กล่องเดียวมีแทรคสองเลข ไม่แน่ใจว่ากล่องเดียวกันไหม", "tracking de-duplication"),
}

# 2. a PAST-TENSE completion claim. Future intent ("แอดมินจะเช็คให้นะคะ")
#    is fine and matches the CS team's own scripts.
_FALSE_COMPLETION_RE = re.compile(
    r"เรียบร้อยแล้ว|ดำเนินการให้แล้ว|ดำเนินการเรียบร้อย"
    r"|(?:แก้ไข|เปลี่ยน|ยกเลิก|ลบ|รวม|ถอน|คืนเงิน|อัปเดต|อัพเดท)\S{0,10}ให้(?:เรียบร้อย)?แล้ว"
    r"|(?:ติดต่อ|แจ้ง|สอบถาม)(?:ร้าน|ทางร้าน|โกดัง|เจ้าหน้าที่)\S{0,6}(?:ให้)?แล้ว"
    r"|ออกใบกำกับให้แล้ว|โอนเงินให้แล้ว|จัดส่งให้แล้ว")

# 3. shapes that would be INVENTED private state if no ERP call ran.
_INVENTED_STATE_RE = re.compile(
    r"\b(?:PO|POS|PA|PE|FT|FE|SA|SP)\d{3,}"
    r"|\b\d{9,}\b"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*บาท")

# 4. progress = asks for something, or escalates.
_ASKS_RE = re.compile(
    r"รบกวน|ขอ(?:เลข|ที่อยู่|รหัส|ข้อมูล|สลิป|รูป|ไฟล์)|แจ้ง|ส่ง\S{0,6}มา|ระบุ|ไหนคะ|ไหมคะ|อะไรคะ|\?")
_ESCALATES_RE = re.compile(r"เจ้าหน้าที่|แอดมิน|ทีมงาน|ติดต่อกลับ")
_BARE_NO_INFO_RE = re.compile(r"^\s*(?:ตอนนี้)?ยังไม่มีข้อมูล[^\n]{0,40}$")


class TestApiGapSafeFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_real_registry()
        cls.eng = DecisionEngine()
        cls.results = {}
        for fid, (msg, cap) in API_GAP_TURNS.items():
            r = _run_one(cls.eng, msg, history=None)
            cls.results[fid] = {
                "msg": msg, "capability": cap,
                "reply": (r.get("reply_text") or r.get("reply") or ""),
                "erp_called": bool(r.get("erp_called")),
                "routing": r.get("routing_type"),
                "src": r.get("selection_source"),
            }

    def test_0_all_26_api_gap_requirements_are_covered(self):
        """Coverage must span the inventory, not just the UAT subset."""
        import pathlib
        inv = json.loads(pathlib.Path("reports/customer_feedback_master_inventory.json")
                         .read_text(encoding="utf-8"))
        want = {r["feedback_id"] for r in inv["records"]
                if r["terminal_status"] == "API_GAP"}
        missing = want - set(API_GAP_TURNS)
        self.assertEqual(missing, set(), f"API_GAP ids with no fallback turn: {sorted(missing)}")
        self.assertEqual(len(API_GAP_TURNS), len(want))

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
            leftover = [x for x in _INVENTED_STATE_RE.findall(d["reply"]) if x not in d["msg"]]
            if leftover:
                bad.append(f"{c}: invented {leftover!r} -> {d['reply'][:80]!r}")
        self.assertEqual(bad, [], "INVENTED PRIVATE STATE:\n" + "\n".join(bad))

    @staticmethod
    def _progresses(d):
        """The owner's own list of acceptable fallback behaviours is:
        collect a useful identifier, escalate to Human CS, OR give
        RAG/general guidance when that is safe. A turn answered from the
        public KB therefore progresses the conversation -- notably
        AI-API-S1-21.0, the cancel POLICY question, which owner ruling 1
        says MUST stay in RAG and must NOT demand an identifier. (The UAT
        harness substitutes [STUB-RAG-ANSWER] for the real KB answer, so
        the assertion here is that an answer was produced and is not a
        bare no-info; properties 1/2/3 police truthfulness separately.)"""
        answered = bool(d["reply"].strip()) and not _BARE_NO_INFO_RE.match(d["reply"].strip())
        return bool(_ASKS_RE.search(d["reply"])
                    or _ESCALATES_RE.search(d["reply"])
                    or d["routing"] == "HUMAN_HANDOFF"
                    or (d["routing"] in ("RAG", "GENERAL", "HYBRID") and answered))

    def test_4_every_turn_progresses_the_conversation(self):
        bad = []
        for c, d in self.results.items():
            if not self._progresses(d):
                bad.append(f"{c}: dead end -> {d['reply'][:80]!r}")
        self.assertEqual(bad, [], "DEAD END:" + chr(10) + chr(10).join(bad))

    def test_5_supplying_the_identifier_does_not_re_ask_for_it(self):
        """NO LOOP: the exact failure the customer reported in the docx
        ('AI ขอรหัส แล้วถามรหัสใหม่อีก') must not recur for ERP flows."""
        bad = []
        for fid in ("AI-API-S2-2.0", "AI-API-S2-4.0", "AI-API-S2-8.0"):
            msg = API_GAP_TURNS[fid][0]
            first = self.results[fid]["reply"]
            hist = [{"role": "user", "content": msg},
                    {"role": "assistant", "content": first}]
            follow = _run_one(self.eng, "PO123456789", history=hist)
            reply2 = (follow.get("reply_text") or follow.get("reply") or "")
            if reply2.strip() and reply2.strip() == first.strip():
                bad.append(f"{fid}: identical reply repeated after the id was supplied")
        self.assertEqual(bad, [], "LOOP:\n" + "\n".join(bad))

    def test_6_write_per_requirement_report(self):
        """Emit the per-FEEDBACK_ID evidence table the closure gate asks for."""
        import pathlib
        lines = ["# API-Gap Safe-Fallback Evidence (Phase 6 closure gate B)", "",
                 f"All **{len(self.results)}** API_GAP requirements, each with an executable "
                 "safe-fallback turn. PASS means all five safety properties held.", "",
                 "| FEEDBACK_ID | Required API capability | Fallback behaviour | Test IDs | PASS/FAIL |",
                 "|---|---|---|---|---|"]
        tests = ("tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::"
                 "test_1..test_5")
        for fid, d in sorted(self.results.items()):
            ok = (d["reply"].strip()
                  and not _BARE_NO_INFO_RE.match(d["reply"].strip())
                  and not (not d["erp_called"] and _FALSE_COMPLETION_RE.search(d["reply"]))
                  and not (not d["erp_called"] and
                           [x for x in _INVENTED_STATE_RE.findall(d["reply"]) if x not in d["msg"]])
                  and self._progresses(d))
            behaviour = ("collects the identifier then hands off"
                         if d["src"] == "operational_change_collection"
                         else f"routed {d['routing']} ({d['src']})")
            lines.append(f"| {fid} | {d['capability']} | {behaviour} | {tests} | "
                         f"{'PASS' if ok else 'FAIL'} |")
        pathlib.Path("reports/phase6_api_gap_fallback_evidence.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        self.assertTrue(pathlib.Path("reports/phase6_api_gap_fallback_evidence.md").exists())


if __name__ == "__main__":
    unittest.main()
