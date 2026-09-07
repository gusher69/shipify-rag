# -*- coding: utf-8 -*-
"""CORE-CONVERSATION-ROOT-CAUSE-HARDENING — generalized multi-turn
orchestration coverage. Paraphrases deliberately DIFFERENT from the
customer UAT wording, so a fix only passes if the behaviour generalizes.

Three root causes fixed and protected here:

  RC1  A pending Business-Action / bespoke flow swallowed an EXPLICIT
       topic switch ("...ขอถามเรื่อง...แทน", "เปลี่ยนไปถามเรื่อง...") as
       another failed slot answer, because _current_intent_breaks_pending_flow
       treated EVERY follow_up_op != "NONE" as "continue this flow".
       Now a TOPIC_CHANGE op to a different decisive family breaks it.

  RC2  A message that is essentially JUST a box-dimensions triple
       ("54x12x43", "กล่อง 50x40x30") did not open the shipping-cost
       estimate flow, so it fell to RAG / the bare-identifier guard.
       Now is_bare_dims_triple() opens it and it asks for the weight.

  RC3  Weight given in GRAMS ("5000 กรัม", "800g") was not parsed at
       all. Now _WEIGHT_G_RE converts it to kg (÷1000).

All three fixes are deterministic (regex / follow-up-op), so this suite
does not depend on the LLM tier being available.
"""
import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid")

from services.decision_engine import DecisionEngine
from services.conversation_semantics import interpret, _followup_op
from services.shipping_estimate_flow import (
    is_bare_dims_triple, opens_estimate_flow, extract_estimate_fields,
    derive_estimate_state,
)
from tests.test_business_action_registry import reset_real_registry
from tests.test_decision_engine import _fake_playground_result


def _t(role, c):
    return {"role": role, "content": c}


_ASK_URL = "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ"
_ASK_BILL = "กรุณาแจ้งเลขที่บิลขนส่งค่ะ"


class _Engine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_real_registry()
        cls.eng = DecisionEngine()

    def say(self, msg, history=None, *, cust_code=None, rag="[RAG]", erp=None):
        binding = ({"cust_code": cust_code, "status": "verified", "channel": "line",
                    "external_user_id": "Ucore", "tenant_id": "default"} if cust_code else None)
        b = MagicMock()
        b.get_verified_binding.return_value = binding
        b.get_verified_binding_for_custcode.return_value = binding
        ctx = {"channel": "line", "tenant_id": "default", "external_user_id": "Ucore",
               "developer_mode": True,
               "customer_context": ({"cust_code": cust_code} if cust_code else {})}
        payload = erp if erp is not None else {"data": {}}
        req = MagicMock(return_value=MagicMock(status_code=200, json=lambda: payload,
                                               text=json.dumps(payload)))
        with patch("services.customer_binding_service.get_customer_binding_service", return_value=b), \
             patch("services.credential_store.CredentialStore.resolve",
                   return_value={"ok": True, "value": "S", "error": None}), \
             patch("services.action_executor.requests.request", req), \
             patch("services.link_conversion_flow.requests.get",
                   return_value=MagicMock(status_code=200, headers={}, close=lambda: None)), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer=rag, confidence=0.88)):
            r = self.eng.decide(msg, history=history or [], context=ctx)
        d = r.get("developer") or {}
        return {
            "routing": (r.get("routing") or {}).get("type"),
            "src": d.get("selection_source"),
            "action": d.get("selected_business_action"),
            "reply": (r.get("reply") or {}).get("text") or "",
            "handoff": (r.get("handoff_payload") or {}).get("reason"),
            "broke": d.get("pending_flow_broken_by_current_intent"),
        }


# ═══════════════ RC1 — explicit topic switch out of a pending flow ═══════
class TestExplicitTopicSwitch(_Engine):
    """A stated switch wins immediately; the prior flow's slot prompt must
    NOT be re-shown."""

    def _pending_shipment(self):
        return [_t("user", "ช่วยตามพัสดุหน่อย"), _t("assistant", _ASK_BILL)]

    def test_switch_shipment_to_shipping_cost(self):
        r = self.say("ไม่ถามเรื่องบิลแล้ว ขอถามเรื่องค่าส่ง", self._pending_shipment())
        self.assertNotIn("เลขที่บิลขนส่ง", r["reply"])
        self.assertNotEqual(r["src"], "conversation_continuation")

    def test_switch_shipment_to_coupon(self):
        r = self.say("พอแล้วเรื่องบิล เปลี่ยนไปถามเรื่องคูปองดีกว่า", self._pending_shipment())
        self.assertNotIn("เลขที่บิลขนส่ง", r["reply"])
        self.assertNotEqual(r["src"], "conversation_continuation")

    def test_switch_shipment_to_warehouse(self):
        r = self.say("ขอถามเรื่องที่อยู่โกดังแทนละกัน", self._pending_shipment())
        self.assertNotIn("เลขที่บิลขนส่ง", r["reply"])

    def test_switch_shipment_to_invoice(self):
        r = self.say("เปลี่ยนไปถามเรื่องใบกำกับดีกว่าค่ะ", self._pending_shipment())
        self.assertNotIn("เลขที่บิลขนส่ง", r["reply"])

    def test_switch_shipment_to_product_policy(self):
        r = self.say("งั้นขอถามเรื่องของต้องห้ามแทน", self._pending_shipment())
        self.assertNotIn("เลขที่บิลขนส่ง", r["reply"])

    def test_topic_change_op_is_deterministic(self):
        for m in ("ขอถามเรื่องค่าส่งแทน", "เปลี่ยนไปถามเรื่องคูปอง",
                  "งั้นถามเรื่องโกดังดีกว่า", "ไม่เอาแล้ว ถามเรื่องใบกำกับ"):
            self.assertEqual(_followup_op(m), "TOPIC_CHANGE", m)

    def test_plain_correction_is_not_a_topic_switch(self):
        # a value correction must NOT read as a topic change
        for m in ("ไม่ใช่ 10 กิโล เป็น 5 กิโล", "ไม่ใช่ FT111 เป็น FT222"):
            self.assertNotEqual(_followup_op(m), "TOPIC_CHANGE", m)

    def test_switch_out_of_link_conversion_to_rate(self):
        h = [_t("user", "ช่วยแปลงลิงก์หน่อย"), _t("assistant", _ASK_URL)]
        r = self.say("เปลี่ยนไปถามเรื่องค่าส่งดีกว่า", h)
        self.assertNotEqual(r["action"], "geturlproductdetail")

    def test_switch_keeps_working_when_prior_flow_had_no_pending(self):
        r = self.say("ขอถามเรื่องคูปองแทนค่ะ", [])
        self.assertTrue(r["reply"].strip())

    def test_non_switch_short_reply_still_continues_pending(self):
        # a bare identifier IS the pending slot answer — must continue
        r = self.say("FT318220260726001", self._pending_shipment(), cust_code="FT3182",
                     erp={"data": {"Shipment": {"Code": "FT318220260726001",
                                                "Status": "รับเข้าที่จีน", "TotalSum": "0"}}})
        self.assertNotIn("เปลี่ยนเรื่อง", r["reply"])


# ═══════════════ RC2 — bare box-dimensions triple opens the calc ════════
class TestBareDimensionsOpensCalc(_Engine):
    def test_bare_dims_x_separated(self):
        r = self.say("54x12x43")
        self.assertEqual(r["src"], "shipping_estimate_flow")
        self.assertIn("น้ำหนัก", r["reply"])          # asks for weight

    def test_bare_dims_with_box_word(self):
        r = self.say("กล่อง 50x40x30")
        self.assertEqual(r["src"], "shipping_estimate_flow")
        self.assertIn("น้ำหนัก", r["reply"])

    def test_bare_dims_with_unit_suffix(self):
        r = self.say("40x30x20 ซม")
        self.assertEqual(r["src"], "shipping_estimate_flow")

    def test_bare_dims_spaced_x(self):
        r = self.say("60 x 40 x 30")
        self.assertEqual(r["src"], "shipping_estimate_flow")

    def test_is_bare_dims_triple_positive(self):
        for m in ("54x12x43", "กล่อง 50x40x30", "40 × 30 × 20", "ขนาด 50x40x30 ซม",
                  "12x8x6 นิ้ว", "0.5x0.4x0.3 เมตร", "60x40x30 ครับ"):
            self.assertTrue(is_bare_dims_triple(m), m)

    def test_is_bare_dims_triple_negative(self):
        for m in ("2566-05-01", "FT318220260726001", "79017107089341",
                  "54x12", "ราคาเท่าไหร่", "โกดังอยู่ไหน",
                  "ขอแปลงลิงก์ https://detail.1688.com/offer/1.html",
                  "สั่ง 3x ได้ไหม", " order 12345"):
            self.assertFalse(is_bare_dims_triple(m), m)

    def test_bare_dims_then_weight_kg(self):
        r1 = self.say("54x12x43")
        h = [_t("user", "54x12x43"), _t("assistant", r1["reply"])]
        r2 = self.say("8 โล", h)
        self.assertEqual(r2["src"], "shipping_estimate_flow")
        self.assertIn("8 กก", r2["reply"])
        self.assertIn("54x12x43", r2["reply"])         # dims retained

    def test_bare_dims_does_not_hijack_a_real_identifier_turn(self):
        # a message that also names a real business intent is NOT a bare triple
        r = self.say("เช็กพัสดุ 54x12x43 ให้หน่อย")
        self.assertNotEqual(r["src"], "shipping_estimate_flow")


# ═══════════════ RC3 — grams → kg ══════════════════════════════════════
class TestGramWeight(_Engine):
    def test_extract_5000_grams_is_5_kg(self):
        st = extract_estimate_fields("54x12x43 5000 กรัม")
        self.assertEqual(st.weight, 5.0)

    def test_extract_800g_is_0_8_kg(self):
        st = extract_estimate_fields("30x20x10 800g")
        self.assertAlmostEqual(st.weight, 0.8)

    def test_grams_follow_up_after_dims(self):
        r1 = self.say("กล่อง 50x40x30")
        h = [_t("user", "กล่อง 50x40x30"), _t("assistant", r1["reply"])]
        r2 = self.say("5000 กรัม", h)
        self.assertEqual(r2["src"], "shipping_estimate_flow")
        self.assertIn("5 กก", r2["reply"])

    def test_kg_still_wins_over_gram_pattern(self):
        st = extract_estimate_fields("54x12x43 7 กก")
        self.assertEqual(st.weight, 7.0)

    def test_gram_correction_replaces_weight(self):
        h = [_t("user", "54x12x43"), _t("assistant", "รับทราบค่ะ (ขนาด 54x12x43 ซม.) ยังขาดข้อมูลสำหรับประเมิน")]
        h += [_t("user", "10 โล"), _t("assistant", "รับทราบค่ะ (น้ำหนัก 10 กก. • ขนาด 54x12x43 ซม.) ต้องการประเมินทางรถหรือทางเรือคะ")]
        r = self.say("ไม่ใช่ 10 กิโล เอา 5500 กรัม", h)
        self.assertEqual(r["src"], "shipping_estimate_flow")
        self.assertIn("5.5", r["reply"])
        self.assertNotIn("10 กก", r["reply"])


# ═══════════════ calculator lifecycle (corrections / new episode) ══════
class TestCalculatorLifecycle(_Engine):
    def _thread(self):
        r1 = self.say("54x12x43")
        h = [_t("user", "54x12x43"), _t("assistant", r1["reply"])]
        r2 = self.say("10 โล", h)
        h += [_t("user", "10 โล"), _t("assistant", r2["reply"])]
        return h

    def test_correction_replaces_not_appends(self):
        r = self.say("ไม่ใช่ 10 กิโล เป็น 5 กิโล", self._thread())
        self.assertIn("5 กก", r["reply"])
        self.assertNotIn("10 กก", r["reply"])

    def test_new_episode_clears_prior_values(self):
        h = self._thread()
        h += [_t("user", "ไม่ใช่ 10 กิโล เป็น 5 กิโล"),
              _t("assistant", "รับทราบค่ะ (น้ำหนัก 5 กก. • ขนาด 54x12x43 ซม.) ต้องการประเมินทางรถหรือทางเรือคะ")]
        r = self.say("เริ่มใหม่ กล่อง 40x30x20", h)
        self.assertEqual(r["src"], "shipping_estimate_flow")
        self.assertIn("40x30x20", r["reply"])
        self.assertNotIn("5 กก", r["reply"])           # weight not carried over

    def test_unit_preserved_metres(self):
        r = self.say("คำนวณค่าส่ง กล่อง 0.5x0.4x0.3 เมตร หนัก 8 กิโล")
        self.assertIn(" m", r["reply"])
        self.assertNotIn("cm", r["reply"])

    def test_unit_preserved_cm(self):
        r = self.say("คำนวณค่าส่ง 50 40 30 ซม 8 กก")
        self.assertIn("cm", r["reply"])

    def test_completed_estimate_then_new_topic_does_not_recalc(self):
        h = [_t("user", "คำนวณค่าส่ง 50x40x30 ซม 8 กก ทางรถ"),
             _t("assistant", "ประเมินเบื้องต้นสำหรับทางรถ ประมาณ 350 บาทค่ะ")]
        r = self.say("ขอถามเรื่องคูปองแทนค่ะ", h)
        self.assertNotIn("ประเมิน", r["reply"])

    def test_route_answer_completes_the_thread(self):
        h = self._thread()
        r = self.say("เอาทางเรือ", h)
        self.assertEqual(r["src"], "shipping_estimate_flow")


# ═══════════════ pending link-conversion follow-ups (no phrase list) ════
class TestLinkConversionFollowUps(_Engine):
    def _pending(self):
        return [_t("user", "ช่วยแปลงลิงก์หน่อย"), _t("assistant", _ASK_URL)]

    def test_confirmation_style_followup_stays_link(self):
        # "เอาลิงก์มาวางได้เลยหรอ" — no ไหม/ยังไง shape; must stay link-conv
        r = self.say("เอาลิงก์มาวางได้เลยหรอ", self._pending())
        self.assertEqual(r["action"], "geturlproductdetail")

    def test_url_after_ask_converts(self):
        r = self.say("https://detail.1688.com/offer/682345678901.html", self._pending())
        self.assertEqual(r["action"], "geturlproductdetail")

    def test_explicit_switch_out_of_link_wins(self):
        r = self.say("ขอถามเรื่องค่าส่งแทนดีกว่า", self._pending())
        self.assertNotEqual(r["action"], "geturlproductdetail")

    def test_direct_url_no_context(self):
        r = self.say("https://detail.1688.com/offer/682345678901.html", [])
        self.assertEqual(r["action"], "geturlproductdetail")


# ═══════════════ stale / completed-workflow context ═══════════════════
class TestStaleContext(_Engine):
    def test_old_shipment_answer_does_not_hijack_a_fresh_public_question(self):
        h = [_t("user", "เช็คสถานะบิล FT318220260726001"),
             _t("assistant", "สถานะบิลขนส่ง: รับเข้าที่จีนค่ะ")]
        r = self.say("แล้วค่าตีลังไม้คิดยังไง", h)
        self.assertNotIn("รับเข้าที่จีน", r["reply"])
        self.assertIn(r["routing"], ("RAG", "GENERAL", "WORKFLOW"))

    def test_completed_calc_then_bare_dims_starts_new(self):
        h = [_t("user", "คำนวณค่าส่ง 50x40x30 ซม 8 กก ทางรถ"),
             _t("assistant", "ประเมินเบื้องต้นสำหรับทางรถ ประมาณ 350 บาทค่ะ")]
        r = self.say("เริ่มใหม่ 20x20x20", h)
        self.assertEqual(r["src"], "shipping_estimate_flow")
        self.assertNotIn("8 กก", r["reply"])

    def test_old_link_conversion_in_history_does_not_leak_into_urlless_ask(self):
        h = [_t("user", "https://detail.1688.com/offer/111.html"),
             _t("assistant", "แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ\nhttps://fasttrade.in.th/x/111")]
        r = self.say("ขอแปลงอีกลิงก์ค่ะ", h)
        self.assertEqual(r["reply"], _ASK_URL)
        self.assertNotIn("111", r["reply"])


# ═══════════════ intent decided before RAG (RAG must not hijack) ══════
class TestIntentBeforeRag(_Engine):
    def test_bare_dims_never_becomes_a_rag_doc_answer(self):
        r = self.say("54x12x43", rag="Question: อะไรก็ได้\nAnswer: บลาๆ")
        self.assertNotEqual(r["routing"], "RAG")

    def test_topic_switch_target_is_routed_not_the_pending_flow(self):
        h = [_t("user", "ช่วยตามพัสดุ"), _t("assistant", _ASK_BILL)]
        r = self.say("เปลี่ยนไปถามเรื่องคูปองดีกว่า", h, rag="คูปองใช้ได้ตอนชำระเงิน")
        self.assertNotIn("เลขที่บิลขนส่ง", r["reply"])

    def test_calc_verb_beats_rate_faq(self):
        r = self.say("ช่วยคำนวณค่าส่งให้หน่อย 50x40x30 ซม 8 กก")
        self.assertEqual(r["src"], "shipping_estimate_flow")


# ═══════════════ auth / private-data safety unchanged ════════════════
class TestAuthUnchanged(_Engine):
    def test_bare_dims_is_public_no_verification(self):
        r = self.say("54x12x43")
        for bad in ("ยืนยันตัวตน", "รหัสลูกค้า", "เบอร์โทรที่ผูก"):
            self.assertNotIn(bad, r["reply"])

    def test_topic_switch_to_private_still_gates(self):
        h = [_t("user", "ช่วยแปลงลิงก์"), _t("assistant", _ASK_URL)]
        r = self.say("เปลี่ยนไปถาม ยอด Wallet ของผมเหลือเท่าไหร่", h,
                     erp={"data": {"PurchaseWallet": "9999"}})
        self.assertNotIn("9999", r["reply"])

    def test_unverified_private_after_switch_no_leak(self):
        h = [_t("user", "54x12x43"), _t("assistant", "รับทราบค่ะ (ขนาด 54x12x43 ซม.) ยังขาดข้อมูลสำหรับประเมิน")]
        r = self.say("เปลี่ยนไปถาม ออเดอร์ผมถึงไหนแล้ว", h,
                     erp={"data": {"Order": {"Status": "รับเข้าที่จีน"}}})
        self.assertNotIn("{", r["reply"])


# ═══════════════ semantic follow-up classification (deterministic) ════
class TestFollowUpOpClassification(unittest.TestCase):
    def test_topic_change_paraphrases(self):
        for m in ("งั้นถามเรื่องค่าส่งดีกว่า", "เปลี่ยนไปถามเรื่องโกดัง",
                  "ขอถามเรื่องคูปองแทน", "เอาเป็นว่าถามเรื่องใบกำกับ",
                  "ไม่เอาแล้ว ถามเรื่องเรท"):
            self.assertEqual(_followup_op(m), "TOPIC_CHANGE", m)

    def test_correction_paraphrases(self):
        for m in ("ไม่ใช่ 3 กิโล เป็น 4 กิโล", "ไม่ใช่ ทางรถ เอา ทางเรือ"):
            self.assertEqual(_followup_op(m), "CORRECTION", m)

    def test_neutral_short_replies_are_not_switches(self):
        for m in ("โอเคค่ะ", "ครับ", "5 กิโล", "ทางเรือ", "https://x.1688.com/y"):
            self.assertNotEqual(_followup_op(m), "TOPIC_CHANGE", m)


# ═══════════════ RAG answer -> follow-up must not lose the thread ══════
class TestRagThenFollowUp(_Engine):
    def _after_rate_faq(self):
        return [_t("user", "เรทเท่าไหร่"),
                _t("assistant", "เรทฝากสั่ง 5.11 บาท/หยวน ทางรถ 35 บาท/กก. ทางเรือ 19 บาท/กก.ค่ะ")]

    def test_rate_faq_then_calc_verb_opens_flow(self):
        r = self.say("งั้นช่วยคำนวณให้หน่อย กล่อง 50x40x30 ซม หนัก 6 กก", self._after_rate_faq())
        self.assertEqual(r["src"], "shipping_estimate_flow")

    def test_rate_faq_then_bare_dims_opens_flow(self):
        r = self.say("60x40x30", self._after_rate_faq())
        self.assertEqual(r["src"], "shipping_estimate_flow")

    def test_rate_faq_then_unrelated_switch(self):
        r = self.say("ขอถามเรื่องคูปองแทนค่ะ", self._after_rate_faq())
        self.assertIn(r["routing"], ("RAG", "GENERAL", "WORKFLOW"))

    def test_rag_answer_not_contradicted_by_a_stale_calc(self):
        h = [_t("user", "คำนวณค่าส่ง 50x40x30 ซม 8 กก ทางรถ"),
             _t("assistant", "ประเมินเบื้องต้นสำหรับทางรถ ประมาณ 350 บาทค่ะ")]
        r = self.say("แล้วนำเข้าจากจีนเสียภาษีไหม", h, rag="การนำเข้าอาจมีภาษีตามพิกัดศุลกากรค่ะ")
        self.assertNotIn("350", r["reply"])


# ═══════════════ ERP read -> topic switch ════════════════════════════
class TestErpThenSwitch(_Engine):
    def _after_shipment(self):
        return [_t("user", "เช็คสถานะบิล FT318220260726001"),
                _t("assistant", "สถานะบิลขนส่ง: รับเข้าที่จีนค่ะ")]

    def test_switch_from_shipment_result_to_rate_faq(self):
        r = self.say("เปลี่ยนไปถามเรื่องเรทดีกว่า", self._after_shipment(), rag="เรท 5.11 บาท/หยวนค่ะ")
        self.assertNotIn("รับเข้าที่จีน", r["reply"])

    def test_switch_from_shipment_result_to_calc(self):
        r = self.say("งั้นช่วยคำนวณค่าส่งให้หน่อย 40x30x20 ซม 3 กก", self._after_shipment())
        self.assertEqual(r["src"], "shipping_estimate_flow")

    def test_bare_dims_after_shipment_result_starts_calc(self):
        r = self.say("30x20x10", self._after_shipment())
        self.assertEqual(r["src"], "shipping_estimate_flow")


# ═══════════════ ambiguous short replies use compatible context only ═══
class TestAmbiguousShortReplies(_Engine):
    def test_short_reply_after_link_ask_is_not_a_random_faq(self):
        h = [_t("user", "ช่วยแปลงลิงก์"), _t("assistant", _ASK_URL)]
        r = self.say("อันไหนคะ", h)
        # stays in link conversion (re-asks) — never an unrelated RAG doc
        self.assertNotIn("Question:", r["reply"])

    def test_bare_ok_does_not_start_a_flow(self):
        r = self.say("โอเคค่ะ", [])
        self.assertNotEqual(r["src"], "shipping_estimate_flow")

    def test_bare_number_alone_is_not_a_calc_start(self):
        r = self.say("500", [])
        self.assertNotEqual(r["src"], "shipping_estimate_flow")

    def test_two_numbers_alone_is_not_a_calc_start(self):
        r = self.say("50 40", [])
        self.assertNotEqual(r["src"], "shipping_estimate_flow")


# ═══════════════ more topic-switch paraphrase coverage (E2E) ══════════
class TestTopicSwitchParaphrasesE2E(_Engine):
    def _pending_calc(self):
        return [_t("user", "54x12x43"),
                _t("assistant", "รับทราบค่ะ (ขนาด 54x12x43 ซม.) ยังขาดข้อมูลสำหรับประเมิน")]

    def test_switch_out_of_calc_to_coupon(self):
        r = self.say("ขอถามเรื่องคูปองแทนค่ะ", self._pending_calc())
        self.assertNotIn("ยังขาดข้อมูลสำหรับประเมิน", r["reply"])

    def test_switch_out_of_calc_to_warehouse(self):
        r = self.say("เปลี่ยนไปถามเรื่องที่อยู่โกดังดีกว่า", self._pending_calc())
        self.assertNotIn("ยังขาดข้อมูลสำหรับประเมิน", r["reply"])

    def test_switch_out_of_calc_to_invoice(self):
        r = self.say("งั้นถามเรื่องใบกำกับดีกว่า", self._pending_calc())
        self.assertNotIn("ยังขาดข้อมูลสำหรับประเมิน", r["reply"])

    def test_weight_answer_is_not_a_switch(self):
        r = self.say("7 โล", self._pending_calc())
        self.assertEqual(r["src"], "shipping_estimate_flow")
        self.assertIn("7 กก", r["reply"])

    def test_route_answer_is_not_a_switch(self):
        h = self._pending_calc()
        h += [_t("user", "7 โล"),
              _t("assistant", "รับทราบค่ะ (น้ำหนัก 7 กก. • ขนาด 54x12x43 ซม.) ต้องการประเมินทางรถหรือทางเรือคะ")]
        r = self.say("ทางเรือค่ะ", h)
        self.assertEqual(r["src"], "shipping_estimate_flow")


# ═══════════════ CBM formula sanity (calculator truth) ═══════════════
class TestCbmFormula(_Engine):
    def test_full_estimate_road_uses_higher_of_weight_or_cbm(self):
        # 100x100x100 cm = 1 CBM; 5 kg. Road: max(5*35, 1*6900) = 6900.
        r = self.say("คำนวณค่าส่ง 100x100x100 ซม 5 กก ทางรถ")
        self.assertIn("6900", r["reply"].replace(",", "").replace(" ", ""))

    def test_full_estimate_road_weight_dominates_when_heavy(self):
        # 20x20x20 cm = 0.008 CBM -> 55.2 baht; 100 kg -> 3500 baht.
        r = self.say("คำนวณค่าส่ง 20x20x20 ซม 100 กก ทางรถ")
        self.assertIn("3500", r["reply"].replace(",", "").replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
