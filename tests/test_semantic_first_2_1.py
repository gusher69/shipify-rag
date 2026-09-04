# -*- coding: utf-8 -*-
"""SEMANTIC-FIRST-2.1 — close the remaining semantic-routing residuals.

Three residuals from SEMANTIC-FIRST-2's own verification:

  1. WAREHOUSE / PICKUP paraphrase -> identity-gated ERP over-reach
     (getdatacustomer / searchdatashipmentlist) -> fake "ดำเนินการ
     เรียบร้อยค่ะ" / a private list dump.
  2. COUPON-USAGE paraphrase -> enters private / customer-id collection.
  3. INVOICE unseen (withholding-tax / document) paraphrase -> weak
     General-Chat drift instead of an honest company no-info.

Fix: the ONE central interpreter (services/conversation_semantics.py)
names a PUBLIC_INFO family; the Decision Engine then coerces the turn to
SHIPIFY_INFORMATION (identity-gated Business Actions excluded from the
candidate set) and the RAG orchestrator keeps it off the General-Chat
Fallback. No sentence dictionaries — a data-driven `coupon` synonym
group and a `payment` intent-lookup gate (mirroring the existing
warranty / invoice gates) handle recall / collision.

Customer test phrases appear in FIXTURES ONLY.
"""
import re
import unittest
from unittest.mock import MagicMock, patch

from services.conversation_semantics import interpret, PUBLIC_INFO_FAMILIES
from services.decision_engine import DecisionEngine, classify_turn_intent
from services.slot_filling_engine import detect_erp_intent
from services.business_action_registry import BusinessActionRegistry
from tests.test_business_action_registry import _FakeSupabase
from tests.test_decision_engine import _seed_action, _fake_playground_result


def _fake_llm_family(message, history=None):
    s = message or ""
    if re.search(r"รับของ|โหลดของ|จุดรับ|โกดัง|คลัง|สาขา|พิกัด", s) and re.search(r"ไหน|ตรงไหน|ที่ไหน|โซน|ย่าน|ที่ใด", s):
        return {"family": "PICKUP_LOCATION"}
    if re.search(r"โค้ด|คูปอง|ส่วนลด|voucher", s) and re.search(r"กรอก|ใส่|ช่อง|ใช้|ตรงไหน|ตอนไหน|ยังไง", s) \
            and not re.search(r"มีกี่|เหลือ|ในบัญชี|ของฉันมี|ค้างอยู่", s):
        return {"family": "COUPON_USAGE"}
    if re.search(r"มีคูปอง|เหลือคูปอง|คูปอง.*กี่|ในบัญชี.*ส่วนลด|ส่วนลด.*ค้างอยู่", s):
        return {"family": "MY_COUPONS", "is_private": True}
    if re.search(r"ภาษี|ใบเสร็จ|ใบกำกับ|เอกสาร|หัก ณ ที่จ่าย|withholding", s):
        return {"family": "INVOICE"}
    return {"family": "UNKNOWN"}


_BIND = {"cust_code": "FT3182", "status": "verified", "channel": "line",
         "external_user_id": "U_sf21", "tenant_id": "default"}


def _mk_engine():
    reg = BusinessActionRegistry(_FakeSupabase())
    # getdatacustomer — the identity-gated customer-data lookup that
    # over-reached. Mirrors production: its OWN search_keywords are the
    # customer-data ones only ("ข้อมูลลูกค้า"); coupons/wallet are
    # associated purely through its RESPONSE MAPPING field keywords, the
    # exact configuration that let a coupon-USAGE how-to score onto it.
    gid = _seed_action(reg, key="getdatacustomer", action_type="API",
                       category="Customer Data Retrieval", keywords=["ข้อมูลลูกค้า"])
    reg.replace_parameters(gid, [
        {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
         "input_source": "customer_message"}])
    reg.replace_response_mapping(gid, [
        {"json_path": "$.data.Coupon", "mapped_label": "คูปอง",
         "field_metadata": {"keywords": ["คูปอง", "coupon", "ส่วนลด"]}},
        {"json_path": "$.data.PurchaseWallet", "mapped_label": "ยอดเงิน Wallet",
         "field_metadata": {"keywords": ["wallet", "ยอดเงิน"]}}])
    reg.upsert_execution(gid, {"endpoint": "https://example.test/customer", "http_method": "GET"})
    # a shipment list action — the other over-reach candidate (its real
    # keywords, not warehouse/pickup words).
    lid = _seed_action(reg, key="searchdatashipmentlist", action_type="API",
                       category="logistics", keywords=["รายการพัสดุ", "บิลขนส่งทั้งหมด"])
    reg.replace_parameters(lid, [
        {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
         "input_source": "customer_message"}])
    reg.upsert_execution(lid, {"endpoint": "https://example.test/list", "http_method": "GET"})
    # a RAG action so the public path has somewhere to land — no
    # keywords, so it never "decisively wins its own turn": the turn
    # reaches RAG via the SHIPIFY_INFORMATION coercion (identity-gated
    # actions excluded), which is exactly the path under test.
    _seed_action(reg, key="kb", action_type="RAG")
    e = DecisionEngine(reg._sb)
    e.registry = reg
    return e


_CUSTOMER_CTX = {
    "cust_code": "FT3182", "identity_confirmed": True,
    "last_business_action": "getdatacustomer", "segment": "hot",
}


def _decide(engine, message, *, verified=True, llm=_fake_llm_family,
            rag_answer="PUBLIC-RAG-ANSWER", rag_conf=0.9, unsupported=False):
    bsvc = MagicMock()
    bsvc.get_verified_binding.return_value = _BIND if verified else None
    bsvc.get_verified_binding_for_custcode.return_value = _BIND if verified else None
    ctx = {"developer_mode": True, "channel": "line",
           "customer_context": dict(_CUSTOMER_CTX) if verified else {}}
    with patch("services.conversation_semantics._llm_family", side_effect=llm), \
         patch("services.customer_binding_service.get_customer_binding_service", return_value=bsvc), \
         patch("services.credential_store.CredentialStore.resolve",
               return_value={"ok": True, "value": "S", "error": None}), \
         patch("services.action_executor.requests.request",
               return_value=MagicMock(status_code=200, json=lambda: {"data": {"Coupon": []}})), \
         patch("services.playground_orchestrator.run_playground_turn",
               return_value=_fake_playground_result(answer=rag_answer, confidence=rag_conf,
                                                    unsupported_company_fact=unsupported)):
        r = engine.decide(message, history=[], context=ctx)
    dev = r.get("developer") or {}
    ics = dev.get("information_collection_status") or {}
    return {
        "routing": (r.get("routing") or {}).get("type"),
        "handoff": (r.get("handoff_payload") or {}).get("reason"),
        "src": dev.get("selection_source"),
        "sem": (dev.get("semantic_interpretation") or {}).get("intent_family"),
        "turn_intent": dev.get("turn_intent"),
        "coerced": dev.get("turn_intent_coerced"),
        "ics_action": ics.get("selected_business_action"),
        "reply": (r.get("reply") or {}).get("text") or "",
    }


_FAKE_SUCCESS_RE = re.compile(r"ดำเนินการเรียบร้อย|รายการบิลขนส่งทั้งหมด|รายการทั้งหมด:")


# ── 1. WAREHOUSE / PICKUP — must stay PUBLIC, never identity-gated ERP ──

class TestWarehousePickupStaysPublic(unittest.TestCase):
    EXISTING = "โกดังรับสินค้าอยู่ที่ไหน"
    PARAPHRASES = ["ไปรับของแถวไหนครับ", "จุดรับสินค้าของทางร้านอยู่ตรงไหน", "ต้องไปรับของที่สาขาไหน"]
    UNSEEN = "อยากไปโหลดของเองต้องเข้าไปรับที่จุดไหนของบริษัท"

    def setUp(self):
        self.eng = _mk_engine()

    def _assert_public(self, o, msg):
        self.assertNotEqual(o["routing"], "API", f"{msg!r} -> ERP API: {o}")
        self.assertNotIn(o["ics_action"], ("getdatacustomer", "searchdatashipmentlist"),
                         f"{msg!r} selected identity-gated action {o['ics_action']}")
        self.assertNotEqual(o["src"], "private_state_inquiry", f"{msg!r} routed private: {o}")
        self.assertFalse(_FAKE_SUCCESS_RE.search(o["reply"]), f"{msg!r} fake success: {o['reply']!r}")
        # both pickup-side families are PUBLIC information (in
        # PUBLIC_INFO_FAMILIES) — a loose "go pick up myself" paraphrase
        # may land on either; what matters is it stays off the ERP path.
        self.assertIn(o["sem"], ("PICKUP_LOCATION", "SELF_PICKUP"), f"{msg!r} -> {o['sem']}")

    def test_existing_wording_public(self):
        self._assert_public(_decide(self.eng, self.EXISTING), self.EXISTING)

    def test_paraphrases_public(self):
        for m in self.PARAPHRASES:
            self._assert_public(_decide(self.eng, m), m)

    def test_unseen_paraphrase_public(self):
        o = _decide(self.eng, self.UNSEEN)
        self._assert_public(o, self.UNSEEN)

    def test_turn_intent_is_coerced_by_the_central_family(self):
        o = _decide(self.eng, "ไปรับของแถวไหนครับ")
        self.assertEqual(o["turn_intent"], "SHIPIFY_INFORMATION")
        self.assertEqual(o["coerced"], "semantic_public_information_family")

    def test_no_fake_success_even_when_erp_would_have_succeeded(self):
        # ERP mock returns 200 OK — a mis-routed turn would have replied
        # "ดำเนินการเรียบร้อยค่ะ"; the public coercion prevents that.
        for m in [self.EXISTING] + self.PARAPHRASES + [self.UNSEEN]:
            o = _decide(self.eng, m)
            self.assertFalse(_FAKE_SUCCESS_RE.search(o["reply"]), f"{m!r}: {o['reply']!r}")


# ── 2. COUPON — USAGE is PUBLIC (no identity) ; MY_COUPONS is PRIVATE ──

class TestCouponUsagePublicVsMyCouponsPrivate(unittest.TestCase):
    USAGE = ["คูปองใช้ยังไง", "ใช้ส่วนลดยังไงครับ", "ต้องกดคูปองตรงไหน"]
    USAGE_UNSEEN = "เอาโค้ดคูปองไปกรอกช่องไหนตอนจ่ายเงิน"
    MINE = ["ผมมีคูปองอะไรบ้าง", "บัญชีผมเหลือคูปองไหม"]
    MINE_UNSEEN = "ในบัญชีผมตอนนี้มีคูปองใช้ได้กี่ใบ"

    def setUp(self):
        self.eng = _mk_engine()

    def _assert_usage_public(self, o, msg):
        self.assertEqual(o["sem"], "COUPON_USAGE", msg)
        self.assertNotEqual(o["routing"], "API", f"{msg!r} -> ERP: {o}")
        self.assertNotEqual(o["ics_action"], "getdatacustomer",
                            f"{msg!r} entered customer-id collection: {o}")
        self.assertNotIn("รหัสลูกค้า", o["reply"], f"{msg!r} asked for a customer id: {o['reply']!r}")
        self.assertEqual(o["turn_intent"], "SHIPIFY_INFORMATION", msg)

    def test_usage_variants_public_no_identity(self):
        for m in self.USAGE:
            self._assert_usage_public(_decide(self.eng, m), m)

    def test_usage_unseen_paraphrase_public_no_identity(self):
        self._assert_usage_public(_decide(self.eng, self.USAGE_UNSEEN), self.USAGE_UNSEEN)

    def test_my_coupons_is_private_and_needs_identity(self):
        for m in self.MINE + [self.MINE_UNSEEN]:
            o = _decide(self.eng, m, verified=True)
            self.assertEqual(o["sem"], "MY_COUPONS", m)
            self.assertIn(o["turn_intent"], ("PRIVATE_ACTION",), f"{m!r}: {o}")

    def test_my_coupons_requires_authorization(self):
        for m in self.MINE:
            o = _decide(self.eng, m, verified=False)
            self.assertNotEqual(o["routing"], "API", f"{m!r} ran ERP unverified: {o}")
            self.assertTrue(re.search(r"รหัสลูกค้า|ยืนยันตัวตน|เลขออเดอร์", o["reply"]),
                            f"{m!r} did not ask to verify: {o['reply']!r}")

    def test_public_private_distinction_is_by_meaning_not_pronoun(self):
        # a self-pronoun in a how-to is still PUBLIC
        o = _decide(self.eng, "แล้วผมต้องเอาคูปองไปใส่ตรงไหน")
        self.assertEqual(o["sem"], "COUPON_USAGE")
        self.assertNotEqual(o["ics_action"], "getdatacustomer")


# ── 3. payment intent-lookup gate (mirrors warranty / invoice gates) ──

class TestPaymentIntentGate(unittest.TestCase):
    def test_bare_checkout_step_mention_is_not_a_payment_lookup(self):
        self.assertIsNone(detect_erp_intent("เอาโค้ดคูปองไปกรอกช่องไหนตอนจ่ายเงิน"))
        self.assertIsNone(detect_erp_intent("กดชำระเงินยังไง"))

    def test_genuine_payment_status_lookup_still_detected(self):
        self.assertEqual(detect_erp_intent("ผมมียอดค้างชำระอยู่เท่าไหร่"), "payment")
        self.assertEqual(detect_erp_intent("เช็คยอดค้างชำระของผม"), "payment")

    def test_other_intents_unaffected(self):
        self.assertEqual(detect_erp_intent("ของถึงไหนแล้ว"), "tracking")
        self.assertIsNone(detect_erp_intent("สวัสดีครับ"))


# ── 4. INVOICE / DOCUMENT — recognized family, honest no-info, no drift ─

class TestInvoiceDocumentFamily(unittest.TestCase):
    def setUp(self):
        self.eng = _mk_engine()

    def test_unseen_withholding_paraphrase_is_invoice_family(self):
        # the withholding-tax phrasing resolves via the ONE gated
        # semantic call (deterministic tier alone reaches only GENERAL/
        # UNKNOWN); simulate a reachable resolver.
        with patch("services.conversation_semantics._llm_family", side_effect=_fake_llm_family):
            for m in ["อยากได้เอกสารหัก ณ ที่จ่ายของบิลนี้ทำไงคะ",
                      "ขอเอกสารรับรองการหักภาษี ณ ที่จ่ายจากทางบริษัทได้ไหม"]:
                self.assertEqual(interpret(m, []).intent_family, "INVOICE", m)

    def test_unsupported_document_fact_is_honest_no_info_not_general_drift(self):
        # forced answerability-gate flag -> honest company no-info /
        # Human CS follow-up, never a general-chat guess.
        o = _decide(self.eng, "ขอเอกสารรับรองการหักภาษี ณ ที่จ่ายจากทางบริษัทได้ไหม",
                    unsupported=True, rag_answer="ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ")
        self.assertEqual(o["routing"], "HUMAN_HANDOFF")
        self.assertEqual(o["handoff"], "unsupported_company_information")

    def test_issuance_and_download_unchanged(self):
        self.assertEqual(interpret("ออกใบกำกับภาษีให้ได้ไหมคะ", []).intent_family, "INVOICE")
        self.assertEqual(interpret("โหลดใบกำกับภาษียังไง", []).intent_family, "INVOICE")


# ── 5. the central family set + regression guards ─────────────────────

class TestPublicInfoFamilySet(unittest.TestCase):
    def test_membership(self):
        self.assertEqual(
            PUBLIC_INFO_FAMILIES,
            frozenset({"PICKUP_LOCATION", "SELF_PICKUP", "COUPON_USAGE",
                       "PRODUCT_POLICY", "CHARTER_TRUCK", "INVOICE"}))

    def test_private_families_are_not_in_the_set(self):
        for fam in ("SHIPMENT_STATUS", "MY_COUPONS", "ADDRESS_CHANGE"):
            self.assertNotIn(fam, PUBLIC_INFO_FAMILIES)


class TestProtectedRegressionSpotChecks(unittest.TestCase):
    def setUp(self):
        self.eng = _mk_engine()

    def test_my_coupons_pronoun_query_still_private(self):
        o = _decide(self.eng, "ผมมีคูปองอะไรบ้าง", verified=True)
        self.assertEqual(o["turn_intent"], "PRIVATE_ACTION")

    def test_shipment_status_paraphrase_still_asks_for_identifier(self):
        o = _decide(self.eng, "ร้านส่งของออกมายัง")
        self.assertNotEqual(o["handoff"], "unsupported_company_information")

    def test_true_no_info_company_fact_still_fix2(self):
        o = _decide(self.eng, "Shipify รับประกันว่าสินค้าทุกชิ้นจะผ่านศุลกากรไหม",
                    unsupported=True, rag_answer="ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ")
        self.assertEqual(o["routing"], "HUMAN_HANDOFF")
        self.assertEqual(o["handoff"], "unsupported_company_information")


if __name__ == "__main__":
    unittest.main()
