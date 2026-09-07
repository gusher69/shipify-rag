# -*- coding: utf-8 -*-
"""PHASE-6B — Customer "แก้ไขเคส Shipify Part 2" full multi-turn
regression.

The customer asked explicitly for ROOT-CAUSE fixes verified as *full
conversations*, not sentence-by-sentence patches ("เคสนี้ขอให้แก้ที่ root
cause และ regression เป็น full multi-turn conversation … ไม่อยากให้ patch
เฉพาะแต่ละประโยค"). So every scenario here is a running dialogue A–E, and
each family also gets PARAPHRASE variants whose wording is deliberately
different from the acceptance-doc sentences — a fix only passes if it
generalizes.

Root causes covered:
  P2-1  calculator never folded mm/m -> cm before CBM.
  P2-3  the SP/FT shipping-withdrawal reply rejected a brand-prefixed
        CustCode ("SP1008") so it fell to RAG.
  P2-4 / Page-5 / P2-B1  no conversational / service-intent layer before
        RAG: help / discovery / import-interest / money-transfer turns
        dead-ended on KB_NOT_FOUND.
  P2-B2  a pending "send me the link" prompt swallowed a WEBSITE-link
        request and every clarification -> the reported 3× answer loop.
  P2-B4/5/6  the "supplier ships to your China warehouse" journey was
        answered with a nearby FAQ / a notification action instead of an
        honest "no confirmed info + Human CS".
  P2-B7/B8  a PUBLIC contact / email / website request was pulled into an
        identity-gated ERP lookup ("กรุณาแจ้งรหัสลูกค้าค่ะ").

The pre-RAG families are all deterministic, so this suite does not depend
on the LLM tier.
"""
import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-part2")

from services.decision_engine import DecisionEngine
from services.conversation_semantics import interpret
from services.shipping_estimate_flow import extract_estimate_fields, EstimateState
from tests.test_business_action_registry import reset_real_registry
from tests.test_decision_engine import _fake_playground_result


def _t(role, c):
    return {"role": role, "content": c}


_ASK_URL = "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ"
_HELP_HEAD = "ต้องการให้ช่วยเรื่องไหนคะ"
_DISCOVERY_TAIL = "สนใจบริการไหนเป็นพิเศษคะ"
_MONEY_HEAD = "บริการฝากโอนเงินให้ร้านค้าจีน"
_WEBSITE_HEAD = "ลิงก์เว็บไซต์หลักของแต่ละแพลตฟอร์ม"
_CONTACT_MARK = "@Shipify"
_WH_FALLBACK_MARK = "ยังไม่มีข้อมูลยืนยันในระบบ"
_CUSTCODE_ASK = "กรุณาแจ้งรหัสลูกค้า"


class _Engine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_real_registry()
        cls.eng = DecisionEngine()

    def say(self, msg, history=None, *, cust_code=None, rag="[RAG-NO-DATA]"):
        binding = ({"cust_code": cust_code, "status": "verified", "channel": "line",
                    "external_user_id": "Up2", "tenant_id": "default"} if cust_code else None)
        b = MagicMock()
        b.get_verified_binding.return_value = binding
        b.get_verified_binding_for_custcode.return_value = binding
        ctx = {"channel": "line", "tenant_id": "default", "external_user_id": "Up2",
               "developer_mode": True,
               "customer_context": ({"cust_code": cust_code} if cust_code else {})}
        payload = {"data": {}}
        req = MagicMock(return_value=MagicMock(status_code=200, json=lambda: payload,
                                               text=json.dumps(payload)))
        with patch("services.customer_binding_service.get_customer_binding_service", return_value=b), \
             patch("services.credential_store.CredentialStore.resolve",
                   return_value={"ok": True, "value": "S", "error": None}), \
             patch("services.action_executor.requests.request", req), \
             patch("services.link_conversion_flow.requests.get",
                   return_value=MagicMock(status_code=200, headers={}, close=lambda: None)), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer=rag, confidence=0.2)):
            r = self.eng.decide(msg, history=history or [], context=ctx)
        d = r.get("developer") or {}
        return {
            "routing": (r.get("routing") or {}).get("type"),
            "src": d.get("selection_source"),
            "svc": d.get("service_intent_family"),
            "action": d.get("selected_business_action"),
            "reply": (r.get("reply") or {}).get("text") or "",
            "handoff": (r.get("handoff_payload") or {}).get("reason"),
            "sem": (d.get("semantic_interpretation") or {}),
        }


# ═══════════════════ Conversation A — help -> discovery -> import ═══════
class TestConversationA(_Engine):
    """สวัสดี -> ต้องการความช่วยเหลือ -> มีบริการอะไรบ้าง ->
    ต้องการนำเข้าเครื่องจักร -> (next step) -> งั้นโอนเงินให้ร้านที่จีน."""

    def test_full_journey(self):
        h = [_t("user", "สวัสดีครับ"), _t("assistant", "สวัสดีค่ะ มีอะไรให้ช่วยไหมคะ")]

        r1 = self.say("ใช่ครับ", h)
        self.assertIn(_HELP_HEAD, r1["reply"])          # T01 — not KB fallback
        self.assertNotIn("ไม่มีข้อมูล", r1["reply"])
        h += [_t("user", "ใช่ครับ"), _t("assistant", r1["reply"])]

        r2 = self.say("ต้องการความช่วยเหลือ", h)
        self.assertIn(_HELP_HEAD, r2["reply"])
        h += [_t("user", "ต้องการความช่วยเหลือ"), _t("assistant", r2["reply"])]

        r3 = self.say("มีบริการอะไรบ้าง", h)
        self.assertIn(_DISCOVERY_TAIL, r3["reply"])     # T02 — describe + next step
        h += [_t("user", "มีบริการอะไรบ้าง"), _t("assistant", r3["reply"])]

        r4 = self.say("ต้องการนำเข้าเครื่องจักร", h)
        self.assertNotIn("ไม่มีข้อมูล", r4["reply"])    # T03 — import intent, not KB dead-end
        self.assertIn("เครื่องจักร", r4["reply"])
        h += [_t("user", "ต้องการนำเข้าเครื่องจักร"), _t("assistant", r4["reply"])]

        r5 = self.say("งั้นโอนเงินให้ร้านที่จีน", h)
        self.assertIn(_MONEY_HEAD, r5["reply"])         # T05 — money-transfer, NOT withdrawal
        self.assertNotIn("ถอนเงินจากระบบ", r5["reply"])
        self.assertNotIn("ประวัติการชำระเงินขนส่ง", r5["reply"])

    def test_import_intent_is_not_a_withdrawal_howto(self):
        r = self.say("อยากนำเข้าสินค้า")
        self.assertNotIn("ไม่มีข้อมูล", r["reply"])

    def test_money_transfer_asks_next_step_not_just_describes(self):
        r = self.say("สนใจฝากโอนเงินให้ร้านจีน")
        self.assertIn(_MONEY_HEAD, r["reply"])
        self.assertIn("รบกวนแจ้ง", r["reply"])          # next-best-action question


# ═══════════════════ Conversation B — website link + rejection loop ════
class TestConversationB(_Engine):
    def test_full_journey(self):
        h = []
        r1 = self.say("การนำเข้าส่งออกของค่ะ", h)
        self.assertNotIn("ไม่มีข้อมูลยืนยัน", r1["reply"])   # P2-B1
        h += [_t("user", "การนำเข้าส่งออกของค่ะ"), _t("assistant", r1["reply"])]

        r2 = self.say("ขอลิงก์เว็บ Taobao และ Tmall", h)
        self.assertIn(_WEBSITE_HEAD, r2["reply"])            # P2-B2 — website, not "send product link"
        self.assertNotIn(_ASK_URL, r2["reply"])
        h += [_t("user", "ขอลิงก์เว็บ Taobao และ Tmall"), _t("assistant", _ASK_URL)]

        # even if the bot had shown the wrong prompt, a clarification must
        # NOT get the same reply back.
        r3 = self.say("ไม่ค่ะ ขอลิงก์ที่จะเข้าไปดูของ", h)
        self.assertNotEqual(r3["reply"].strip(), _ASK_URL)
        self.assertIn(_WEBSITE_HEAD, r3["reply"])
        h += [_t("user", "ไม่ค่ะ ขอลิงก์ที่จะเข้าไปดูของ"), _t("assistant", _ASK_URL)]

        r4 = self.say("คุณไม่เข้าใจลูกค้า", h)
        self.assertNotEqual(r4["reply"].strip(), _ASK_URL)   # no 3rd repeat

    def test_real_product_url_still_converts(self):
        # STEP 7 must not regress the 1688/Taobao/Tmall product conversion.
        r = self.say("https://detail.1688.com/offer/649256171234.html")
        self.assertEqual(interpret("https://detail.1688.com/offer/649256171234.html").intent_family,
                         "LINK_CONVERSION")
        self.assertNotIn(_WEBSITE_HEAD, r["reply"])

    def test_explicit_convert_verb_still_conversion(self):
        self.assertEqual(
            interpret("แปลงลิงก์ให้หน่อย https://item.taobao.com/item.htm?id=1").intent_family,
            "LINK_CONVERSION")


# ═══════════════════ Conversation C — warehouse inbound + contact ═════
class TestConversationC(_Engine):
    def test_full_journey(self):
        h = []
        r1 = self.say("ร้านส่งของไปคลังจีน แล้วจะรู้ได้ยังไงว่าเป็นลูกค้าคนไหน", h)
        self.assertIn(_WH_FALLBACK_MARK, r1["reply"])         # P2-B4 honest, not nearby FAQ
        self.assertNotIn("ที่อยู่โกดังจีน", r1["reply"])
        self.assertEqual(r1["routing"], "HUMAN_HANDOFF")
        h += [_t("user", "ร้านส่งของไปคลังจีน แล้วจะรู้ได้ยังไงว่าเป็นลูกค้าคนไหน"),
              _t("assistant", r1["reply"])]

        r2 = self.say("ต้องแจ้งอะไรไหมว่าจะมีของไปส่งที่คลัง", h)
        self.assertIn(_WH_FALLBACK_MARK, r2["reply"])         # P2-B5 — not MOQ
        self.assertNotIn("ขั้นต่ำ", r2["reply"])
        h += [_t("user", "ต้องแจ้งอะไรไหมว่าจะมีของไปส่งที่คลัง"), _t("assistant", r2["reply"])]

        r3 = self.say("ของถึงแล้วจะติดต่อกลับไหม", h)
        self.assertIn(_WH_FALLBACK_MARK, r3["reply"])         # P2-B6 — not SendLineNotiCS
        self.assertNotIn("ยืนยันการดำเนินการ", r3["reply"])
        h += [_t("user", "ของถึงแล้วจะติดต่อกลับไหม"), _t("assistant", r3["reply"])]

        r4 = self.say("ติดต่อช่องทางไหน", h)
        self.assertIn(_CONTACT_MARK, r4["reply"])             # P2-B7 — grounded contact
        self.assertNotIn(_CUSTCODE_ASK, r4["reply"])
        h += [_t("user", "ติดต่อช่องทางไหน"), _t("assistant", r4["reply"])]

        r5 = self.say("ขออีเมล และเว็บไซต์", h)
        self.assertIn(_CONTACT_MARK, r5["reply"])             # P2-B8 — PUBLIC, never CustCode
        self.assertNotIn(_CUSTCODE_ASK, r5["reply"])

    def test_warehouse_inbound_never_selects_a_notification_action(self):
        for m in ("ของไปถึงคลังจีนแล้วจะแจ้งกลับไหมคะ",
                  "พอของถึงโกดังจะติดต่อผมไหม"):
            r = self.say(m)
            self.assertNotIn("SendLineNotiCS", (r["action"] or ""))
            self.assertIn(_WH_FALLBACK_MARK, r["reply"])


# ═══════════════════ Conversation D — wrong auth -> recovery ══════════
class TestConversationD(_Engine):
    def test_public_contact_never_enters_auth_then_recovers(self):
        h = []
        r1 = self.say("ขออีเมล และเว็บไซต์", h)
        self.assertNotIn(_CUSTCODE_ASK, r1["reply"])          # never asks in the first place
        h += [_t("user", "ขออีเมล และเว็บไซต์"), _t("assistant", r1["reply"])]

        # even if a bogus code is volunteered, the following contact
        # question must still be answered as public info.
        h += [_t("user", "123456"), _t("assistant", "ขอบคุณค่ะ")]
        r2 = self.say("ติดต่อทางไหนคะ", h)
        self.assertIn(_CONTACT_MARK, r2["reply"])
        self.assertNotIn(_CUSTCODE_ASK, r2["reply"])

    def test_contact_request_does_not_loop_on_custcode(self):
        h = [_t("user", "ขอเบอร์ติดต่อ"), _t("assistant", "กรุณาแจ้งรหัสลูกค้าค่ะ")]
        r = self.say("ติดต่อทางไหนคะ", h)
        self.assertNotIn(_CUSTCODE_ASK, r["reply"])
        self.assertIn(_CONTACT_MARK, r["reply"])


# ═══════════════════ Conversation E — calculator mm -> kg -> correct ══
class TestConversationE(_Engine):
    def test_mm_is_converted_before_cbm(self):
        r = self.say("520mm x 220mm x 110mm ส่งทางเรือ หนัก 2 กิโลค่ะ")
        # sea: max(0.012584 CBM * 4500, 2 * 19) ~ 57 baht — NOT 56628
        self.assertNotIn("56628", r["reply"])
        self.assertNotIn("12.584", r["reply"])

    def test_mm_state_is_centimetres(self):
        for m, exp in [
            ("520mm x 220mm x 110mm หนัก 2 กิโล ทางเรือ", (52.0, 22.0, 11.0)),
            ("300มม x 200มม x 100มม 5 กก ทางรถ", (30.0, 20.0, 10.0)),
            ("0.5m x 0.4m x 0.3m 8kg ทางเรือ", (50.0, 40.0, 30.0)),
        ]:
            s = EstimateState()
            extract_estimate_fields(m, s)
            self.assertEqual((s.length, s.width, s.height), exp, m)
            self.assertEqual(s.dim_unit, "cm", m)

    def test_plain_cm_triple_unchanged(self):
        s = EstimateState()
        extract_estimate_fields("54x12x43 หนัก 10 กิโล ส่งทางเรือ", s)
        self.assertEqual((s.length, s.width, s.height), (54.0, 12.0, 43.0))

    def test_correction_then_new_cycle(self):
        h = [_t("user", "54x12x43"), _t("assistant", "รบกวนแจ้งน้ำหนักด้วยค่ะ")]
        r1 = self.say("10 โล", h)
        self.assertNotIn("ไม่มีข้อมูล", r1["reply"])
        h += [_t("user", "10 โล"), _t("assistant", r1["reply"])]
        r2 = self.say("ไม่ใช่ 10 กิโล เอา 5 กิโล", h)
        self.assertNotIn("ไม่มีข้อมูล", r2["reply"])


# ═══════════════════ paraphrase batteries (generalization) ════════════
class TestHelpParaphrases(_Engine):
    CASES = [
        "ต้องการความช่วยเหลือ", "ช่วยหน่อยครับ", "สอบถามหน่อยค่ะ",
        "มีเรื่องอยากถาม", "มีเรื่องอยากสอบถามครับ", "ปรึกษาหน่อยได้ไหม",
        "รบกวนสอบถามหน่อย", "ขอความช่วยเหลือหน่อยค่ะ",
    ]

    def test_all_open_the_conversation(self):
        for m in self.CASES:
            self.assertEqual(interpret(m).intent_family, "HELP_INTENT", m)
            r = self.say(m)
            self.assertIn(_HELP_HEAD, r["reply"], m)
            self.assertNotIn("ไม่มีข้อมูล", r["reply"], m)


class TestServiceDiscoveryParaphrases(_Engine):
    CASES = [
        "มีบริการอะไรบ้าง", "ให้บริการอะไรบ้างคะ", "รับทำอะไรบ้าง",
        "ทำอะไรได้บ้าง", "สนใจใช้บริการ", "อยากใช้บริการของ Shipify",
        "การนำเข้าส่งออกของค่ะ", "นำเข้า-ส่งออกทำยังไงบ้าง",
    ]

    def test_all_describe_plus_next_step(self):
        for m in self.CASES:
            self.assertEqual(interpret(m).intent_family, "SERVICE_DISCOVERY", m)
            r = self.say(m)
            self.assertIn(_DISCOVERY_TAIL, r["reply"], m)


class TestMoneyTransferParaphrases(_Engine):
    CASES = [
        "งั้นโอนเงินให้ร้านที่จีน", "ฝากโอนเงินให้ร้านจีนยังไง",
        "อยากโอนเงินให้ร้านค้าจีน", "ช่วยโอนเงินไปให้ร้านที่จีนได้ไหม",
        "ฝากโอนหยวนให้ร้าน", "จ่ายเงินให้ร้านค้าจีนยังไง",
    ]

    def test_all_route_to_money_transfer_service(self):
        for m in self.CASES:
            self.assertEqual(interpret(m).intent_family, "MONEY_TRANSFER_INTEREST", m)
            r = self.say(m)
            self.assertIn(_MONEY_HEAD, r["reply"], m)
            self.assertNotIn("ถอนเงินจากระบบ", r["reply"], m)

    def test_withdrawal_is_still_withdrawal(self):
        # the OPPOSITE direction must not collapse into money-transfer.
        self.assertEqual(interpret("ถอนเงินขนส่งยังไงคะ").intent_family, "SHIPPING_WITHDRAWAL")
        self.assertEqual(interpret("ถอนเงินค่าสั่งซื้อยังไง").intent_family, "PURCHASE_WITHDRAWAL")


class TestWebsiteLinkParaphrases(_Engine):
    CASES = [
        "ขอลิงก์เว็บ Taobao และ Tmall", "ขอลิงก์เว็บไซต์ Taobao",
        "ขอลิงก์ที่จะเข้าไปดูของ", "ขอ url เว็บ 1688",
        "อยากได้ลิงก์หน้าเว็บ Tmall", "ขอลิงก์เข้าเว็บเถาเป่า",
    ]

    def test_all_website_not_conversion(self):
        for m in self.CASES:
            self.assertEqual(interpret(m).intent_family, "WEBSITE_LINK_REQUEST", m)
            r = self.say(m)
            self.assertIn(_WEBSITE_HEAD, r["reply"], m)
            self.assertNotEqual(r["reply"].strip(), _ASK_URL, m)


class TestContactInfoParaphrases(_Engine):
    CASES = [
        "ติดต่อช่องทางไหน", "ติดต่อทางไหนคะ", "ขอเบอร์ติดต่อ",
        "ขออีเมล และเว็บไซต์", "ขอเบอร์โทรบริษัท", "ช่องทางติดต่อมีอะไรบ้าง",
        "ติดต่อ Shipify ยังไง", "ขอ line id",
    ]

    def test_all_public_never_custcode(self):
        for m in self.CASES:
            self.assertEqual(interpret(m).intent_family, "CONTACT_INFO", m)
            r = self.say(m)
            self.assertNotIn(_CUSTCODE_ASK, r["reply"], m)
            self.assertNotIn("ยืนยันตัวตน", r["reply"], m)


class TestWarehouseInboundParaphrases(_Engine):
    CASES = [
        "ร้านส่งของไปคลังจีน แล้วจะรู้ได้ยังไงว่าเป็นลูกค้าคนไหน",
        "โรงงานส่งของเข้าโกดังจีน จะรู้ได้ไงว่าเป็นของผม",
        "ต้องแจ้งอะไรไหมว่าจะมีของไปส่งที่คลัง",
        "ต้องบอกล่วงหน้าไหมว่าจะมีของเข้าโกดัง",
        "ของถึงแล้วจะติดต่อกลับไหม",
        "พอของถึงคลังจะแจ้งกลับไหมคะ",
    ]

    def test_all_honest_fallback_no_nearby_faq(self):
        for m in self.CASES:
            self.assertEqual(interpret(m).intent_family, "WAREHOUSE_INBOUND_JOURNEY", m)
            r = self.say(m)
            self.assertIn(_WH_FALLBACK_MARK, r["reply"], m)
            self.assertNotIn("ขั้นต่ำ", r["reply"], m)
            self.assertNotIn("ยืนยันการดำเนินการ", r["reply"], m)


class TestRejectActParaphrases(unittest.TestCase):
    def test_reject_detected_on_meaning(self):
        for m in ["ไม่ใช่อันนี้", "ไม่ได้ถามแบบนั้น", "คุณไม่เข้าใจลูกค้า",
                  "คุณเข้าใจผิดแล้ว", "ตอบไม่ตรงคำถามเลย", "ไม่ค่ะ ขอแบบอื่น",
                  "หมายถึงว่าขอเว็บไซต์"]:
            self.assertEqual(interpret(m).conversation_act, "REJECT", m)

    def test_plain_slot_answer_is_not_reject(self):
        for m in ["FT3182", "10 กิโล", "ทางเรือ", "กรุงเทพ"]:
            self.assertEqual(interpret(m).conversation_act, "NONE", m)


# ═══════════════════ P2-3 — SP/FT shipping-withdrawal brand reply ═════
class TestShippingWithdrawalBrandReply(unittest.TestCase):
    def test_brand_prefixed_custcode_is_accepted(self):
        from services.withdrawal_flow import _BRAND_ANSWER_RE
        for m, exp in [("SP1008", "SP"), ("FT1324", "FT"), ("โค้ด FT1324", "FT"),
                       ("SP", "SP"), ("เอฟที ค่ะ", "เอฟที"), ("FT3182 ครับ", "FT")]:
            mm = _BRAND_ANSWER_RE.match(m)
            self.assertIsNotNone(mm, m)
            self.assertEqual(mm.group(1), exp, m)

    def test_non_brand_replies_are_rejected(self):
        from services.withdrawal_flow import _BRAND_ANSWER_RE
        for m in ["12345678901234567", "ขอถามเรื่องอื่น", "ไม่ทราบค่ะ", "SP1008 กับ FT1324"]:
            self.assertIsNone(_BRAND_ANSWER_RE.match(m), m)

    def test_pending_brand_reply_resolves_from_custcode_prefix(self):
        from services import withdrawal_flow as wf
        h = [_t("user", "ถอนเงินขนส่งยังไงคะ"), _t("assistant", wf.ASK_BRAND_REPLY)]
        with patch.object(wf, "fetch_kb_answer", side_effect=lambda sb, tag: f"<{tag}>"):
            self.assertEqual(wf.shipping_withdrawal_pending_brand_reply(None, h, "SP1008"),
                             "<SHIPPING_WITHDRAWAL_SP>")
            self.assertEqual(wf.shipping_withdrawal_pending_brand_reply(None, h, "FT1324"),
                             "<SHIPPING_WITHDRAWAL_FT>")
        # not the SP-or-FT turn -> no interception
        self.assertIsNone(wf.shipping_withdrawal_pending_brand_reply(None, [], "SP1008"))


# ═══════════════════ regression guards — existing families ════════════
class TestExistingFamiliesUnchanged(unittest.TestCase):
    def test_core_families_still_recognised(self):
        cases = {
            "ใช้คูปองยังไง": "COUPON_USAGE",
            "บิลผมถึงไหนแล้ว": "SHIPMENT_STATUS",
            "ขอใบกำกับภาษีได้ไหม": "INVOICE",
            "เหมารถไปส่งที่ลาดกระบัง": "CHARTER_TRUCK",
            "ถอนเงินขนส่งยังไงคะ": "SHIPPING_WITHDRAWAL",
            "โกดังอยู่ที่ไหน": "PICKUP_LOCATION",
            "แบตเตอรี่นำเข้าได้ไหม": "PRODUCT_POLICY",
            "สนใจนำเข้ารองเท้า": "IMPORT_INTEREST",
        }
        for m, fam in cases.items():
            self.assertEqual(interpret(m).intent_family, fam, m)


# ═══════════════════ follow-up continuity (multi-turn) ═══════════════
class TestFollowUpContinuity(_Engine):
    """A running conversation must carry context forward turn to turn —
    each message is not re-processed in isolation (Part-2 T04)."""

    def test_help_then_discovery_then_import_is_one_journey(self):
        h = []
        r1 = self.say("มีเรื่องอยากสอบถามค่ะ", h)
        self.assertIn(_HELP_HEAD, r1["reply"])
        h += [_t("user", "มีเรื่องอยากสอบถามค่ะ"), _t("assistant", r1["reply"])]
        r2 = self.say("ให้บริการอะไรบ้างคะ", h)
        self.assertIn(_DISCOVERY_TAIL, r2["reply"])
        h += [_t("user", "ให้บริการอะไรบ้างคะ"), _t("assistant", r2["reply"])]
        r3 = self.say("อยากนำเข้าเครื่องซักผ้าอุตสาหกรรม", h)
        self.assertIn("เครื่องซักผ้าอุตสาหกรรม", r3["reply"])
        self.assertNotIn("ไม่มีข้อมูล", r3["reply"])

    def test_import_ack_then_quantity_followup_continues_frame(self):
        h = []
        r1 = self.say("ต้องการนำเข้าอะไหล่รถยนต์", h)
        self.assertIn("อะไหล่รถยนต์", r1["reply"])
        h += [_t("user", "ต้องการนำเข้าอะไหล่รถยนต์"), _t("assistant", r1["reply"])]
        r2 = self.say("ประมาณ 500 ชิ้น", h)
        # a quantity follow-up stays in the import frame, not a new intent
        self.assertNotIn("ไม่มีข้อมูล", r2["reply"])
        self.assertNotIn(_CUSTCODE_ASK, r2["reply"])

    def test_money_transfer_then_amount_detail_stays_in_flow(self):
        h = []
        r1 = self.say("อยากฝากโอนเงินให้ร้านจีน", h)
        self.assertIn(_MONEY_HEAD, r1["reply"])
        h += [_t("user", "อยากฝากโอนเงินให้ร้านจีน"), _t("assistant", r1["reply"])]
        r2 = self.say("ประมาณ 8000 หยวน", h)
        self.assertNotIn("ไม่มีข้อมูล", r2["reply"])

    def test_bare_affirmation_only_continues_a_help_offer(self):
        # "ครับ" after a help offer -> HELP; "ครับ" with no such offer is
        # NOT hijacked into HELP.
        r_yes = self.say("ครับ", [_t("user", "สวัสดี"),
                                  _t("assistant", "สวัสดีค่ะ มีอะไรให้ช่วยไหมคะ")])
        self.assertIn(_HELP_HEAD, r_yes["reply"])
        r_no = self.say("ครับ", [_t("user", "ค่าส่งกี่บาท"),
                                 _t("assistant", "ทางรถ กก. ละ 35 บาทค่ะ")])
        self.assertNotIn(_HELP_HEAD, r_no["reply"])


# ═══════════════════ explicit topic switch mid-journey ══════════════
class TestTopicSwitchMidJourney(_Engine):
    def test_switch_from_discovery_to_calculator(self):
        h = [_t("user", "มีบริการอะไรบ้าง"),
             _t("assistant", "Shipify มีบริการฝากสั่ง ฝากนำเข้า ค่ะ " + _DISCOVERY_TAIL)]
        r = self.say("ขอถามเรื่องคำนวณค่าส่งแทนค่ะ กล่อง 50x40x30 หนัก 10 กิโล ทางเรือ", h)
        # the switch lands on the estimate flow (completed or asking a
        # slot) — NOT still stuck on the discovery reply.
        self.assertIn(r["src"], ("shipping_estimate_flow", None))
        self.assertNotIn(_DISCOVERY_TAIL, r["reply"])
        self.assertTrue(any(k in r["reply"] for k in ("ประเมิน", "บาท", "CBM", "น้ำหนัก")), r["reply"])

    def test_switch_from_import_to_contact(self):
        h = [_t("user", "ต้องการนำเข้าเครื่องจักร"),
             _t("assistant", "รับทราบค่ะ (สินค้า เครื่องจักร) รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ")]
        r = self.say("เปลี่ยนไปถามเรื่องช่องทางติดต่อดีกว่า", h)
        self.assertIn(_CONTACT_MARK, r["reply"])
        self.assertNotIn("จำนวนโดยประมาณ", r["reply"])

    def test_switch_from_website_request_to_withdrawal(self):
        h = [_t("user", "ขอลิงก์เว็บ Taobao"), _t("assistant", _WEBSITE_HEAD + " ...")]
        r = self.say("ไม่เอาแล้ว ถามเรื่องถอนเงินขนส่ง", h)
        self.assertNotIn(_WEBSITE_HEAD, r["reply"])

    def test_switch_out_of_pending_shipment_to_help(self):
        h = [_t("user", "เช็คบิลหน่อย"), _t("assistant", "กรุณาแจ้งเลขที่บิลขนส่งค่ะ")]
        r = self.say("ไม่ถามเรื่องบิลแล้ว ขอความช่วยเหลือเรื่องอื่น", h)
        self.assertNotIn("เลขที่บิลขนส่ง", r["reply"])


# ═══════════════════ website-link vs product-link conversion ═════════
class TestWebsiteVsConversionBoundary(_Engine):
    def test_bare_platform_url_is_conversion_not_website(self):
        self.assertEqual(interpret("https://detail.1688.com/offer/620011111111.html").intent_family,
                         "LINK_CONVERSION")

    def test_taobao_short_url_is_conversion(self):
        self.assertEqual(interpret("https://e.tb.cn/h.abcXYZ").intent_family, "LINK_CONVERSION")

    def test_convert_verb_with_platform_word_is_conversion(self):
        self.assertEqual(interpret("แปลงลิงก์ 1688 อันนี้ให้หน่อย").intent_family, "LINK_CONVERSION")

    def test_website_words_without_url_or_verb_are_website_request(self):
        for m in ["ขอลิงก์หน้าเว็บ 1688 หน่อย", "อยากได้ url หน้าเว็บ Taobao",
                  "ขอลิงก์เข้าเว็บไซต์ Tmall", "ขอลิงก์เว็บไว้เข้าไปเลือกของ"]:
            self.assertEqual(interpret(m).intent_family, "WEBSITE_LINK_REQUEST", m)

    def test_url_pasted_after_website_context_still_converts_not_errors(self):
        # P2-B3 — the CustCode-400 error is fixed; a URL here converts.
        h = [_t("user", "ขอลิงก์เว็บ Taobao"), _t("assistant", _WEBSITE_HEAD + " ...")]
        r = self.say("https://item.taobao.com/item.htm?id=700000009999", h)
        self.assertNotIn("ไม่สามารถดำเนินการได้", r["reply"])
        self.assertNotIn("ระบบขัดข้อง", r["reply"])


# ═══════════════════ calculator — units, generalized ════════════════
class TestCalculatorUnitsGeneralized(_Engine):
    def test_grams_alone_parsed_as_kg(self):
        s = EstimateState()
        extract_estimate_fields("40x30x20 ซม หนัก 2500 กรัม ทางรถ", s)
        self.assertEqual(s.weight, 2.5)

    def test_grams_correction_replaces_kg(self):
        h = [_t("user", "54x12x43 หนัก 10 กิโล"),
             _t("assistant", "รับทราบค่ะ น้ำหนัก 10 กก. ต้องการประเมินทางรถหรือทางเรือคะ")]
        r = self.say("ไม่ใช่ 10 กิโล เอา 800 กรัม", h)
        self.assertNotIn("10 กก", r["reply"].replace("810", ""))

    def test_mm_mixed_with_explicit_cm_stays_consistent(self):
        s = EstimateState()
        extract_estimate_fields("300mm x 20cm x 100mm 5 กก ทางเรือ", s)
        # all three folded to cm; a "20cm" among "mm" is not doubled
        self.assertEqual(s.dim_unit, "cm")
        for v in (s.length, s.width, s.height):
            self.assertTrue(0 < v < 60, (s.length, s.width, s.height))

    def test_metre_dims_folded_to_cm(self):
        s = EstimateState()
        extract_estimate_fields("1.2m x 0.8m x 0.6m 15kg ทางเรือ", s)
        self.assertEqual((s.length, s.width, s.height), (120.0, 80.0, 60.0))
        self.assertEqual(s.dim_unit, "cm")

    def test_kg_to_gram_correction_within_thread(self):
        h = [_t("user", "กล่อง 30x30x30 5 กิโล"),
             _t("assistant", "รับทราบค่ะ ประเมินทางรถหรือทางเรือคะ")]
        r = self.say("ขอแก้เป็น 3500 กรัม", h)
        self.assertNotIn("ไม่มีข้อมูล", r["reply"])

    def test_new_calc_cycle_does_not_carry_old_weight(self):
        h = [_t("user", "54x12x43 หนัก 10 กิโล ทางเรือ"),
             _t("assistant", "ประเมินเบื้องต้นสำหรับทางเรือ ประมาณ 350 บาทค่ะ")]
        r = self.say("เริ่มใหม่ กล่อง 20x20x20", h)
        self.assertIn("20x20x20", r["reply"])
        self.assertNotIn("10 กก", r["reply"])


# ═══════════════════ stale state / repeated-answer prevention ═══════
class TestStaleStateAndRepeatGuard(_Engine):
    def test_old_link_conversion_does_not_steal_a_fresh_help_turn(self):
        h = [_t("user", "แปลงลิงก์ https://detail.1688.com/offer/611111111111.html"),
             _t("assistant", "แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ")]
        r = self.say("ต้องการความช่วยเหลือ", h)
        self.assertIn(_HELP_HEAD, r["reply"])

    def test_repeated_contact_question_is_not_a_custcode_loop(self):
        h = [_t("user", "ขอเบอร์ติดต่อ"), _t("assistant", "กรุณาแจ้งรหัสลูกค้าค่ะ"),
             _t("user", "123456"), _t("assistant", "ขอบคุณค่ะ")]
        r = self.say("แล้วติดต่อทางไหนได้อีก", h)
        self.assertNotIn(_CUSTCODE_ASK, r["reply"])
        self.assertIn(_CONTACT_MARK, r["reply"])

    def test_reject_does_not_resend_identical_reply(self):
        h = [_t("user", "ขอลิงก์เว็บ Tmall"), _t("assistant", _ASK_URL)]
        r = self.say("ไม่ใช่ค่ะ ที่ถามคือลิงก์หน้าเว็บ", h)
        self.assertNotEqual(r["reply"].strip(), _ASK_URL)

    def test_completed_estimate_then_new_topic_does_not_recalc(self):
        h = [_t("user", "คำนวณค่าส่ง 50x40x30 ซม 8 กก ทางรถ"),
             _t("assistant", "ประเมินเบื้องต้นสำหรับทางรถ ประมาณ 350 บาทค่ะ")]
        r = self.say("ขอถามเรื่องช่องทางติดต่อ", h)
        self.assertNotIn("ประเมิน", r["reply"])
        self.assertIn(_CONTACT_MARK, r["reply"])


# ═══════════════════ public vs private routing boundary ════════════
class TestPublicPrivateRoutingBoundary(_Engine):
    def test_public_contact_email_website_never_asks_identity(self):
        for m in ["ขออีเมลบริษัท", "ขอเว็บไซต์บริษัท", "ขอเบอร์โทรฝ่ายบริการลูกค้า",
                  "ติดต่อแอดมินยังไง"]:
            r = self.say(m)
            for bad in (_CUSTCODE_ASK, "ยืนยันตัวตน", "เบอร์โทรที่ผูก"):
                self.assertNotIn(bad, r["reply"], m)

    def test_private_account_query_still_private(self):
        # a self-record balance question must NOT be swept into CONTACT_INFO
        self.assertNotEqual(interpret("ยอดเงินในบัญชีผมเหลือเท่าไหร่").intent_family, "CONTACT_INFO")
        self.assertNotEqual(interpret("บิลผมส่งของหรือยัง").intent_family, "CONTACT_INFO")

    def test_bogus_code_after_public_turn_does_not_start_verification(self):
        h = [_t("user", "ขออีเมล และเว็บไซต์"),
             _t("assistant", "ติดต่อ Shipify ได้ทาง LINE: @Shipify ...")]
        r = self.say("123456", h)
        self.assertNotIn("ยืนยันตัวตน", r["reply"])
        self.assertNotIn("authorization", str(r).lower())

    def test_contact_request_with_identifier_token_is_not_pre_rag_intercepted(self):
        # carries a real record id -> not the public pre-RAG contact path
        r = self.say("ขอเบอร์ผู้รับของบิล FT3182001")
        self.assertNotEqual(r["src"], "phase6b_service_intent")


# ═══════════════════ service discovery / next-best-action ═══════════
class TestServiceDiscoveryNextBestAction(_Engine):
    def test_help_reply_lists_service_options_as_a_question(self):
        r = self.say("ช่วยหน่อยครับ")
        self.assertIn("เช่น", r["reply"])
        self.assertIn("คะ", r["reply"])

    def test_discovery_reply_ends_with_a_next_step_question(self):
        r = self.say("รับทำอะไรบ้าง")
        self.assertIn(_DISCOVERY_TAIL, r["reply"])

    def test_import_interest_reply_asks_for_a_detail(self):
        r = self.say("สนใจนำเข้าเครื่องปรับอากาศ")
        self.assertTrue(any(k in r["reply"] for k in ("จำนวน", "ปริมาณ", "น้ำหนัก", "ทางรถหรือทางเรือ")))

    def test_money_transfer_reply_asks_for_amount_or_shop(self):
        r = self.say("ฝากโอนเงินให้ร้านค้าจีนหน่อย")
        self.assertTrue(any(k in r["reply"] for k in ("ยอดเงิน", "หยวน", "ร้าน", "ลิงก์สินค้า")))


if __name__ == "__main__":
    unittest.main()
