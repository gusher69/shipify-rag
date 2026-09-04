# -*- coding: utf-8 -*-
"""SEMANTIC-FIRST-2 — novel language / ambiguity / missing-input robustness.

A NEW or slightly different natural-language wording whose MEANING is
understandable must not fall into UNKNOWN / no-information / Fix-2 /
Human CS just because it is unseen or short. The ONE central semantic
interpreter (services/conversation_semantics.py::interpret) names the
intent; the Decision Engine then:

  * meaning understood + input missing   -> ask ONLY for the missing
    identifier (WORKFLOW), never Human CS;
  * meaning genuinely unclear            -> clarify, never Human CS;
  * trusted info genuinely unavailable   -> existing Fix-2 path,
    UNCHANGED.

These tests use SEMANTIC generalisation, not phrase enumeration: the
gated LLM family call is simulated (in production it is reachable for
any conversational Thai turn the deterministic tier could not resolve —
SEMANTIC-FIRST-2 widened `_worth_llm_disambiguation`). The deterministic
tier and the Fix-2 boundary guards are tested without any LLM.
"""
import re
import unittest
from unittest.mock import MagicMock, patch

from services.conversation_semantics import interpret, _worth_llm_disambiguation
from services.decision_engine import DecisionEngine, _classify_private_state_inquiry
from services.business_action_registry import BusinessActionRegistry
from tests.test_business_action_registry import _FakeSupabase
from tests.test_decision_engine import _seed_action, _engine_with_registry, _fake_playground_result


# ── a tiny fake for the ONE gated LLM family call ──────────────────────
# In production the widened gate makes this call reachable for any
# conversational Thai turn; here we simulate what a competent resolver
# returns for the MEANING of each novel wording. No sentence table:
# the fake keys on generic dispatch / document / location / etc. cues,
# the same abstract features a real model would use.
def _fake_llm_family(message, history=None):
    s = message or ""
    if re.search(r"ร้าน|ต้นทาง|โรงงาน|ซัพพลาย|ฝั่งจีน|shop", s) and re.search(r"ส่ง|ปล่อย|จัดส่ง|ออก", s):
        return {"family": "SHIPMENT_STATUS", "is_private": True}
    if re.search(r"ส่ง|ถึงไหน|คืบหน้า|สถานะ|เดินทาง", s) and re.search(r"ยัง|หรือยัง|รึยัง|ไหน|ยังไง", s):
        return {"family": "SHIPMENT_STATUS", "is_private": True}
    if re.search(r"ภาษี|ใบเสร็จ|ใบกำกับ|เอกสาร.*หัก|หัก ณ ที่จ่าย|withholding", s):
        return {"family": "INVOICE"}
    if re.search(r"จุดรับ|รับพัสดุ|โกดัง|คลัง|สาขา", s) and re.search(r"ไหน|โซน|ที่ไหน|ตรงไหน|อยู่", s):
        return {"family": "PICKUP_LOCATION"}
    if re.search(r"โค้ด|คูปอง|ส่วนลด|voucher", s) and re.search(r"กรอก|ใส่|ช่อง|ใช้|ตรงไหน", s):
        return {"family": "COUPON_USAGE"}
    if re.search(r"หูฟัง|ลำโพง|สินค้า|ของ|พวก", s) and re.search(r"ส่งเข้ามา|นำเข้า|ส่งเข้า|ได้ไหม|ได้ป่าว|ได้มั้ย", s):
        return {"family": "PRODUCT_POLICY"}
    if re.search(r"กะราคา|ตีราคา|คิดราคา|ประเมิน|ค่าส่ง.*เท่าไหร่|กี่บาท", s):
        return {"family": "SHIPPING_ESTIMATE"}
    if re.search(r"เหมารถ|จ้างรถ|เช่ารถ|รถหกล้อ|รถสิบล้อ|รถกระบะวิ่ง", s):
        return {"family": "CHARTER_TRUCK"}
    if re.search(r"เปลี่ยน|สลับ|ย้าย|แก้", s) and re.search(r"ที่อยู่|ปลายทาง|จุดหมาย|ผู้รับ", s):
        return {"family": "ADDRESS_CHANGE"}
    return {"family": "UNKNOWN"}


_BIND = {"cust_code": "FT3182", "status": "verified", "channel": "line",
         "external_user_id": "U_sf2", "tenant_id": "default"}


def _mk_engine():
    reg = BusinessActionRegistry(_FakeSupabase())
    # the production-shaped read/status + address-change actions the
    # customer workflow actually supports
    _seed_action(reg, key="kb", action_type="RAG",
                 keywords=["นโยบาย", "บริษัท", "ใบกำกับ", "คูปอง", "โกดัง", "สินค้า", "เหมารถ"])
    _seed_action(reg, key="searchdatashipment", action_type="API", category="logistics",
                 keywords=["พัสดุ", "สถานะ", "ติดตาม"],
                 params=[{"name": "ShipmentCode", "required": True, "param_type": "string",
                          "validation_regex": r"^[A-Za-z]{2}\d{10,}$"}])
    _seed_action(reg, key="searchdataorder", action_type="API", category="customer",
                 keywords=["ออเดอร์", "คำสั่งซื้อ", "ร้าน"],
                 params=[{"name": "OrderCode", "required": True, "param_type": "string",
                          "validation_regex": r"^POS?\d+$"}])
    return _engine_with_registry(reg)


def _decide(engine, message, history=None, *, llm=_fake_llm_family, unsupported=False,
            answer="ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ"):
    bsvc = MagicMock()
    bsvc.get_verified_binding.return_value = _BIND
    bsvc.get_verified_binding_for_custcode.return_value = _BIND
    ctx = {"channel": "line", "tenant_id": "default", "external_user_id": "U_sf2",
           "developer_mode": True, "customer_context": {"cust_code": "FT3182", "identity_confirmed": True}}
    with patch("services.conversation_semantics._llm_family", side_effect=llm), \
         patch("services.customer_binding_service.get_customer_binding_service", return_value=bsvc), \
         patch("services.credential_store.CredentialStore.resolve",
               return_value={"ok": True, "value": "S", "error": None}), \
         patch("services.action_executor.requests.request",
               return_value=MagicMock(status_code=200, json=lambda: {"data": {}})), \
         patch("services.playground_orchestrator.run_playground_turn",
               return_value=_fake_playground_result(answer=answer, confidence=0.2,
                                                    unsupported_company_fact=unsupported)):
        r = engine.decide(message, history=history or [], context=ctx)
    dev = r.get("developer") or {}
    return {
        "routing": (r.get("routing") or {}).get("type"),
        "handoff": (r.get("handoff_payload") or {}).get("reason"),
        "src": dev.get("selection_source"),
        "sem": (dev.get("semantic_interpretation") or {}).get("intent_family"),
        "synth": bool(dev.get("private_state_inquiry_semantic_synth")),
        "fix2_suppressed": dev.get("fix2_suppressed"),
        "reply": (r.get("reply") or {}).get("text") or "",
    }


_ASK_ID_RE = re.compile(r"เลข(บิล|พัสดุ|ออเดอร์|คำสั่งซื้อ|แทร็ก|ติดตาม|ใบสั่งซื้อ)|"
                        r"หมายเลข|order|shipment|tracking|FT/FE", re.IGNORECASE)


# ── 1. the reported REAL failure family — seller / origin dispatch ──────

class TestSellerDispatchParaphrases(unittest.TestCase):
    """`ร้านส่งหรือยังคะ` and its paraphrases are an understood
    shipment/order STATUS inquiry that is only missing the record id —
    ask for the id, never auto-Human-CS."""

    LISTED = [
        "ร้านส่งหรือยังคะ",
        "ของออกจากร้านหรือยัง",
        "ต้นทางส่งมาหรือยังครับ",
        "ร้านค้าส่งของออกมายัง",
        "ฝั่งร้านปล่อยของหรือยัง",
        "เช็กให้หน่อยว่าร้านส่งหรือยัง",
    ]
    UNSEEN = [
        "ทางร้านจีนจัดส่งของออกมาแล้วหรือเปล่าคะ",
        "โรงงานที่จีนปล่อยพัสดุออกมาแล้วรึยังคะ",
    ]

    def setUp(self):
        self.eng = _mk_engine()

    def test_listed_paraphrases_ask_for_identifier_not_human_cs(self):
        bad = []
        for m in self.LISTED:
            o = _decide(self.eng, m)
            if o["routing"] == "HUMAN_HANDOFF" or o["handoff"] == "unsupported_company_information":
                bad.append(f"{m!r} -> HUMAN_HANDOFF")
            elif not _ASK_ID_RE.search(o["reply"]):
                bad.append(f"{m!r} -> {o['routing']} / {o['reply'][:60]!r}")
        self.assertEqual(bad, [], "seller-dispatch paraphrase misrouted:\n" + "\n".join(bad))

    def test_unseen_paraphrases_generalise_the_same_way(self):
        for m in self.UNSEEN:
            o = _decide(self.eng, m)
            self.assertNotEqual(o["routing"], "HUMAN_HANDOFF", m)
            self.assertTrue(_ASK_ID_RE.search(o["reply"]), f"{m!r} -> {o['reply'][:80]!r}")

    def test_core_case_works_without_any_llm(self):
        # the deterministic private-state recognizer already carries the
        # exact reported wording (regression floor if the LLM is offline)
        self.assertIsNotNone(_classify_private_state_inquiry("ร้านส่งหรือยังคะ"))
        o = _decide(self.eng, "ร้านส่งหรือยังคะ", llm=lambda *a, **k: {"family": "UNKNOWN"})
        self.assertNotEqual(o["routing"], "HUMAN_HANDOFF")
        self.assertTrue(_ASK_ID_RE.search(o["reply"]))

    def test_identifier_in_the_same_turn_is_accepted(self):
        o = _decide(self.eng, "ร้านส่งของออเดอร์ POS123456 มายัง")
        self.assertNotEqual(o["routing"], "HUMAN_HANDOFF")


# ── 2. one unseen paraphrase per conversational family ─────────────────

class TestUnseenParaphrasePerFamily(unittest.TestCase):
    CASES = {
        "SHIPMENT_STATUS": "ล็อตที่สั่งไปเดินทางถึงขั้นไหนแล้วอะครับ",
        "INVOICE":         "อยากได้เอกสารหัก ณ ที่จ่ายของบิลนี้ทำไงคะ",
        "PICKUP_LOCATION": "จุดรับพัสดุฝั่งไทยอยู่โซนไหนของกรุงเทพครับ",
        "COUPON_USAGE":    "โค้ดลดราคาเอาไปกรอกตรงช่องไหนตอนจ่ายเงินอะ",
        "PRODUCT_POLICY":  "พวกหูฟังบลูทูธส่งเข้ามาได้ป่าวครับ",
        "SHIPPING_ESTIMATE": "ช่วยกะราคาส่งของกล่องนี้ให้หน่อยดิ",
        "CHARTER_TRUCK":   "อยากจ้างรถหกล้อวิ่งส่งของต่อในไทยได้มั้ย",
        "ADDRESS_CHANGE":  "ขอสลับจุดหมายปลายทางของบิลขนส่งเป็นบ้านอีกหลังได้ไหม",
    }

    def setUp(self):
        self.eng = _mk_engine()

    def test_family_is_named_by_the_central_interpreter(self):
        bad = []
        for fam, m in self.CASES.items():
            with patch("services.conversation_semantics._llm_family", side_effect=_fake_llm_family):
                got = interpret(m, []).intent_family
            if got != fam:
                bad.append(f"{fam}: {m!r} -> {got}")
        self.assertEqual(bad, [], "\n".join(bad))

    def test_no_family_becomes_human_cs_or_fix2(self):
        bad = []
        for fam, m in self.CASES.items():
            o = _decide(self.eng, m)
            if o["routing"] == "HUMAN_HANDOFF" and o["handoff"] == "unsupported_company_information":
                bad.append(f"{fam}: {m!r} -> Fix-2 Human CS ({o['reply'][:50]!r})")
        self.assertEqual(bad, [], "\n".join(bad))


# ── 3. AMBIGUITY — genuinely unclear -> clarify, never Human CS ─────────

class TestAmbiguityClarifies(unittest.TestCase):
    def setUp(self):
        self.eng = _mk_engine()

    def test_referentless_fragments_do_not_auto_escalate(self):
        # the natural RAG path for a contentless fragment does not set
        # unsupported_company_fact — it returns a low-confidence /
        # clarification result; the turn must not land on Human CS.
        for m in ("อันนั้นเป็นไงบ้าง", "แล้วยังไงต่อ", "มันได้ไหม"):
            o = _decide(self.eng, m, answer="ไม่แน่ใจว่าหมายถึงเรื่องไหนคะ")
            self.assertNotEqual(o["handoff"], "unsupported_company_information",
                                f"{m!r} -> Fix-2 Human CS: {o['reply'][:60]!r}")

    def test_elliptical_possessive_is_suppressed_even_if_gate_fires(self):
        # even if the answerability gate DID flag it, a bare possessive
        # follow-up with no referent is unclear language -> clarify.
        o = _decide(self.eng, "ของผมล่ะ", unsupported=True)
        self.assertEqual(o["fix2_suppressed"], "elliptical_no_referent_clarify")
        self.assertEqual(o["routing"], "WORKFLOW")
        self.assertIn("ไม่แน่ใจว่าหมายถึงรายการไหน", o["reply"])


# ── 4. MISSING-INPUT — understood change request -> ask, not Fix-2 ─────

class TestMissingInputBoundary(unittest.TestCase):
    def setUp(self):
        self.eng = _mk_engine()

    def test_address_change_paraphrase_asks_for_id_not_human_cs(self):
        o = _decide(self.eng, "ขอสลับจุดหมายปลายทางของบิลขนส่งเป็นบ้านอีกหลังได้ไหม",
                    unsupported=True)
        self.assertNotEqual(o["handoff"], "unsupported_company_information")
        self.assertNotEqual(o["routing"], "HUMAN_HANDOFF")
        self.assertTrue(re.search(r"เลขบิล|FT/FE|ที่อยู่ปลายทางใหม่|ผู้รับ", o["reply"]),
                        o["reply"][:120])

    def test_understood_change_request_is_not_missing_company_knowledge(self):
        # forced answerability-gate flag on an understood ADDRESS_CHANGE
        # -> the boundary helper converts it to a collect prompt.
        o = _decide(self.eng, "ขอสลับที่อยู่ปลายทางของบิลขนส่งหน่อยค่ะ", unsupported=True)
        self.assertEqual(o["fix2_suppressed"], "understood_change_request_missing_input")
        self.assertNotEqual(o["routing"], "HUMAN_HANDOFF")


# ── 5. TRUE NO-INFO CONTROL — existing Fix-2 path unchanged ────────────

class TestTrueNoInfoControlUnchanged(unittest.TestCase):
    def setUp(self):
        self.eng = _mk_engine()

    def test_understood_company_question_with_no_trusted_info_still_fix2(self):
        o = _decide(self.eng, "Shipify รับประกันว่าสินค้าทุกชิ้นจะผ่านศุลกากรไหม",
                    unsupported=True)
        self.assertEqual(o["routing"], "HUMAN_HANDOFF")
        self.assertEqual(o["handoff"], "unsupported_company_information")
        self.assertIsNone(o["fix2_suppressed"])

    def test_second_true_no_info_question_still_fix2(self):
        o = _decide(self.eng, "Shipify มีสาขาที่ประเทศเวียดนามไหมคะ", unsupported=True)
        self.assertEqual(o["handoff"], "unsupported_company_information")


# ── 6. the LLM gate really is widened (not pre-gated on a topic marker) ─

class TestLlmGateWidened(unittest.TestCase):
    def test_novel_conversational_thai_is_worth_a_semantic_call(self):
        for m in ("ร้านส่งหรือยังคะ", "ต้นทางส่งมาหรือยัง", "อันนี้ยังไงต่อดีคะ",
                  "ของที่สั่งไปมันเงียบไปเลยอะ"):
            self.assertTrue(_worth_llm_disambiguation(m), m)

    def test_greetings_values_and_noise_are_not(self):
        for m in ("สวัสดีครับ", "ขอบคุณค่ะ", "โอเคเลย", "2 กิโล", "54x12x43", "SP100045"):
            self.assertFalse(_worth_llm_disambiguation(m), m)


if __name__ == "__main__":
    unittest.main()
