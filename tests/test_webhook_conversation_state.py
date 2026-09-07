# -*- coding: utf-8 -*-
"""PHASE-6B-REAL — production-equivalent CONVERSATION-STATE test path.

The PHASE-6B acceptance harness was incomplete: it drove
`DecisionEngine.decide()` directly and never exercised the pre-`decide()`
state machine in `line_bot/webhook.py` (pending-confirmation lookup,
"ใช่/ยืนยัน" classification, expired-confirmation handling, handoff-status
passthrough, mid-collection pending persistence). Four real-LINE defects
(session 04769238, "ตอบมั่วเลย") slipped through because of that.

`_WebhookSim` below is a faithful, compact reimplementation of exactly
that pre-decide + post-decide-pending sequence from
`_handle_message_via_decision_engine`, wrapped around a REAL
`DecisionEngine`, with an in-memory pending-confirmation store and an
in-memory conversation history. It is the "LINE message -> webhook
pre-decide logic -> DecisionEngine -> response" path the acceptance
criteria now require; DecisionEngine-only probes are no longer accepted
as final evidence for conversation-state behaviour.

Defects covered:
  A  a COMPLETE current-turn calculator payload
     ("520mm x 220mm x 110mm ส่งทางเรือ หนัก 2 กิโล") must reach the
     calculator even after a long stale history / a prior SAFE_FALLBACK /
     handoff_status=NOTIFIED / hot tier.
  B  after "ถอนเงินขนส่งยังไง" -> "SP1008" -> SP answer, a second brand
     token "FT1325" is a brand correction (SP -> FT), not a fresh KB /
     private lookup.
  C  "ใช่ค่ะ" after a greeting ("มีอะไรให้ช่วยไหมคะ?") must NOT be
     consumed by a stale/expired pending confirmation.
  D  "ค่าขนส่งแพงไหมคะถ้าสั่งชั้นวางของ" is a shipping-cost intent — it
     must not dead-end to SAFE_FALLBACK/handoff; it explains it needs
     weight + dimensions and asks for them.
"""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-webhooksim")

from services.decision_engine import DecisionEngine
from services.pending_confirmation_service import classify_confirmation_reply
from tests.test_business_action_registry import reset_real_registry
from tests.test_decision_engine import _fake_playground_result

_EXPIRED_TEXT = "คำขอก่อนหน้าหมดเวลายืนยันแล้วค่ะ รบกวนแจ้งคำขอใหม่อีกครั้งนะคะ"
_CANCELLED_TEXT = "รับทราบค่ะ ยกเลิกการดำเนินการแล้ว"
_HELP_HEAD = "ต้องการให้ช่วยเรื่องไหนคะ"


class _WebhookSim:
    """Reproduces line_bot/webhook.py::_handle_message_via_decision_engine
    pre-decide + post-decide-pending logic around a real DecisionEngine.
    One instance == one LINE conversation (history + pending store persist
    across .send() calls)."""

    def __init__(self, *, engine, handoff_status="NONE", tenant_id="default",
                 channel="line", user_id="Uwebhooksim", customer_context=None,
                 rag_answer="[RAG]", rag_conf=0.2):
        self.engine = engine
        self.handoff_status = handoff_status
        self.tenant_id, self.channel, self.user_id = tenant_id, channel, user_id
        self.customer_context = customer_context or {}
        self.rag_answer, self.rag_conf = rag_answer, rag_conf
        self.history = []            # [{"role","content"}]
        self._pending = []           # list of pending-confirmation dicts (most recent last)

    # ---- pending store (mirrors PendingConfirmationService semantics) ----
    def _get_active(self):
        now = datetime.now(timezone.utc)
        for row in reversed(self._pending):
            if row["status"] != "pending":
                continue
            if row["expires_at"] and now > row["expires_at"]:
                row["status"] = "expired"
                return None
            return row
        return None

    def _get_most_recent(self):
        return self._pending[-1] if self._pending else None

    def _create_pending(self, *, action_id, action_name, parameters, question_text,
                        confirmation_required=True, ttl=300):
        for r in self._pending:
            if r["status"] == "pending":
                r["status"] = "cancelled"
        now = datetime.now(timezone.utc)
        self._pending.append({
            "id": f"p{len(self._pending)}", "status": "pending",
            "pending_action_id": action_id, "pending_action_name": action_name,
            "pending_parameters": parameters or {}, "question_text": question_text,
            "confirmation_required": confirmation_required,
            "created_at": now, "expires_at": now + timedelta(seconds=ttl),
        })

    # ---- the turn ----
    def send(self, question, *, expire_pending_age_minutes=None):
        """Process ONE inbound LINE text. Returns the same dict shape as
        _WebhookSim tests need: {routing, src, reply, action, handoff}."""
        if expire_pending_age_minutes is not None and self._pending:
            # helper for tests: force the most-recent pending row to have
            # expired `expire_pending_age_minutes` ago
            p = self._pending[-1]
            p["status"] = "expired"
            p["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=expire_pending_age_minutes)

        recent_history = list(self.history)
        decide_context = {
            "channel": self.channel, "developer_mode": True,
            "customer_context": dict(self.customer_context),
            "tenant_id": self.tenant_id, "external_user_id": self.user_id,
            "handoff_status": self.handoff_status,
        }

        b = MagicMock()
        b.get_verified_binding.return_value = None
        b.get_verified_binding_for_custcode.return_value = None
        payload = {"data": {}}
        req = MagicMock(return_value=MagicMock(status_code=200, json=lambda: payload,
                                               text=json.dumps(payload)))
        result = None
        with patch("services.customer_binding_service.get_customer_binding_service", return_value=b), \
             patch("services.credential_store.CredentialStore.resolve",
                   return_value={"ok": True, "value": "S", "error": None}), \
             patch("services.action_executor.requests.request", req), \
             patch("services.link_conversion_flow.requests.get",
                   return_value=MagicMock(status_code=200, headers={}, close=lambda: None)), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer=self.rag_answer, confidence=self.rag_conf)):

            pending = self._get_active()
            if pending:
                kind = classify_confirmation_reply(question)
                if kind == "confirm":
                    result = self.engine.decide(question, history=recent_history, context={
                        **decide_context, "confirmed": True,
                        "confirmed_action_id": pending["pending_action_id"],
                        "confirmed_parameters": pending.get("pending_parameters") or {}})
                    pending["status"] = "executed"
                elif kind == "cancel":
                    pending["status"] = "cancelled"
                    result = {"reply": {"text": _CANCELLED_TEXT}, "routing": {"type": "GENERAL"}, "developer": {}}
                else:
                    result = self.engine.decide(question, history=recent_history, context={
                        **decide_context,
                        "pending_action_id": pending["pending_action_id"],
                        "pending_parameters": pending.get("pending_parameters") or {}})
            else:
                kind = classify_confirmation_reply(question)
                if kind in ("confirm", "cancel"):
                    mr = self._get_most_recent()
                    if mr and mr.get("status") == "expired":
                        # ── this is the PHASE-6B-REAL webhook.py fix ──
                        import datetime as _dt
                        _exp = mr.get("expires_at")
                        _fresh = bool(_exp and (_dt.datetime.now(_dt.timezone.utc) - _exp)
                                      < _dt.timedelta(minutes=10))
                        _accepting_help = False
                        try:
                            from services.service_intent_flow import is_help_affirmation as _iha
                            _accepting_help = _iha(question, recent_history)
                        except Exception:
                            pass
                        if _fresh and not _accepting_help:
                            result = {"reply": {"text": _EXPIRED_TEXT},
                                      "routing": {"type": "WORKFLOW"}, "developer": {}}
                if result is None:
                    result = self.engine.decide(question, history=recent_history, context=decide_context)

        dev = result.get("developer") or {}
        reply_text = (result.get("reply") or {}).get("text") or ""
        routing = (result.get("routing") or {}).get("type")

        # ---- post-decide pending persistence (the bits that affect the
        #      NEXT turn's state) ----
        gate = dev.get("confirmation_gate") or {}
        ics = dev.get("information_collection_status") or {}
        if gate.get("required") and not gate.get("confirmed") and gate.get("action_id"):
            self._create_pending(action_id=gate["action_id"],
                                 action_name=gate.get("action_key"),
                                 parameters=ics.get("collected_parameters") or {},
                                 question_text=reply_text, confirmation_required=True)
        elif routing == "WORKFLOW" and ics.get("selected_action_id") and not ics.get("is_complete"):
            self._create_pending(action_id=ics["selected_action_id"],
                                 action_name=ics.get("selected_business_action"),
                                 parameters=ics.get("collected_parameters") or {},
                                 question_text=reply_text, confirmation_required=False)
        elif routing in ("API", "WEBHOOK", "TOOL", "NOTIFICATION", "HUMAN_HANDOFF"):
            act = self._get_active()
            if act and act.get("pending_action_name") == dev.get("selected_business_action"):
                act["status"] = "cancelled"

        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": reply_text})
        return {"routing": routing, "src": dev.get("selection_source"),
                "svc": dev.get("service_intent_family"),
                "action": dev.get("selected_business_action"),
                "reply": reply_text,
                "sem": (dev.get("semantic_interpretation") or {}).get("intent_family")}


def _t(role, c):
    return {"role": role, "content": c}


# real prior history from LINE session 04769238 (the turns before the
# reported failures — trimmed to the shape that matters).
_REAL_PRIOR = [
    _t("user", "อยากสั่งพวกเครื่องประดับมาขายในไทย"),
    _t("assistant", "ระบบรองรับการวางลิงก์สินค้าจาก Taobao, 1688 และ Tmall ค่ะ"),
    _t("user", "52 x 24 x 12 cm หนัก1 โล"),
    _t("assistant", "รับทราบค่ะ (น้ำหนัก 1 กก. • ขนาด 52x24x12 cm) ต้องการประเมินทางรถหรือทางเรือคะ"),
    _t("user", "ทางรถ"),
    _t("assistant", "ประเมินเบื้องต้นสำหรับทางรถประมาณ 103.33 บาทค่ะ"),
    _t("user", "https://qr.1688.com/s/AcByukF7"),
    _t("assistant", "ขอโทษด้วยค่ะ ไม่สามารถดำเนินการได้ในขณะนี้ รบกวนลองใหม่อีกครั้งนะคะ"),
    _t("user", "แปลงลิงก์ได้ไหม"),
    _t("assistant", "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ"),
    _t("user", "ถอนเงินขนส่งยังไงคะ"),
    _t("assistant", "เรื่องนี้คำตอบจะแตกต่างกันไปตามแบรนด์ค่ะ รบกวนแจ้งด้วยนะคะว่าเป็นแบรนด์ SP หรือ FT คะ"),
    _t("user", "SP1008"),
    _t("assistant", "ตอนนี้ยังไม่มีข้อมูลยืนยันเกี่ยวกับวิธีการถอนเงินขนส่งในระบบค่ะ"),
    _t("user", "FT1324"),
    _t("assistant", "ตอนนี้ยังไม่มีข้อมูลเกี่ยวกับ FT1324 ค่ะ"),
    _t("user", "นำเข้า gadget ได้ไหม"),
    _t("assistant", "สินค้าประเภท gadget ยังไม่มีข้อมูลยืนยันในระบบว่าห้ามนำเข้าหรือไม่ค่ะ"),
    _t("user", "แปลงลิงก์ให้หน่อยค่ะ https://m.1688.com/offer/860351622421.html?ptow=x"),
    _t("assistant", "แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ คุณลูกค้าเปิดบิลเข้ามาได้เลยนะคะ\n"
                    "https://fasttrade.in.th/PageProductDetailGuest/1688/860351622421/home/guest/index"),
]

_CALC_PAYLOAD = "520mm x 220mm x 110mm ส่งทางเรือ หนัก 2 กิโล"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_real_registry()
        cls.engine = DecisionEngine()

    def sim(self, **kw):
        return _WebhookSim(engine=self.engine, **kw)


# ═══════════════ A — complete calculator payload overrides stale state ═══
class TestA_CalculatorOverride(_Base):
    def _assert_calc(self, r):
        self.assertEqual(r["src"], "shipping_estimate_flow", r)
        self.assertNotIn("ไม่พบคำตอบที่ชัดเจน", r["reply"])
        self.assertNotIn("ประสานเจ้าหน้าที่", r["reply"])
        # 52 x 22 x 11 cm -> CBM 0.012584 -> sea 56.63
        self.assertIn("56", r["reply"])
        self.assertNotIn("56628", r["reply"])

    def test_A_long_stale_history_hot_tier_notified_handoff(self):
        w = self.sim(handoff_status="NOTIFIED",
                     customer_context={"conversation_tier": "hot"})
        w.history = list(_REAL_PRIOR)
        # a prior SAFE_FALLBACK turn just happened
        w.history += [_t("user", "ค่าขนส่งแพงไหมคะถ้าสั่งชั้นวางของ"),
                      _t("assistant", "ขอโทษด้วยค่ะ ตอนนี้ยังไม่พบคำตอบที่ชัดเจนสำหรับคำถามนี้")]
        self._assert_calc(w.send(_CALC_PAYLOAD))

    def test_A_short_history(self):
        w = self.sim()
        w.history = [_t("user", "อยากคำนวณค่าส่ง"),
                     _t("assistant", "รบกวนแจ้งน้ำหนักและขนาดด้วยค่ะ")]
        self._assert_calc(w.send(_CALC_PAYLOAD))

    def test_A_empty_history(self):
        self._assert_calc(self.sim().send(_CALC_PAYLOAD))

    def test_A_after_completed_link_conversion(self):
        w = self.sim()
        w.history = [_t("user", "แปลงลิงก์ https://m.1688.com/offer/860351622421.html"),
                     _t("assistant", "แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ https://fasttrade.in.th/x")]
        self._assert_calc(w.send(_CALC_PAYLOAD))

    def test_A_paraphrases(self):
        for payload in ["300mm x 200mm x 100mm ส่งทางรถ หนัก 5 กก",
                        "กล่อง 52cm x 22cm x 11cm 2 โล ทางเรือ",
                        "ขนาด 0.5m x 0.4m x 0.3m หนัก 8 กิโล ทางเรือ",
                        "54x12x43 หนัก 10 กิโล ทางเรือ"]:
            w = self.sim(handoff_status="NOTIFIED")
            w.history = list(_REAL_PRIOR)
            r = w.send(payload)
            self.assertEqual(r["src"], "shipping_estimate_flow", payload)
            self.assertNotIn("ไม่พบคำตอบ", r["reply"], payload)


# ═══════════════ B — withdrawal brand correction (SP -> FT) ═══════════
class TestB_WithdrawalBrandCorrection(_Base):
    _SP_SIG = "แบบฟอร์มรูปภาพ"
    _FT_SIG = "ประวัติการชำระเงินขนส่ง"

    def test_B_second_brand_token_is_a_correction(self):
        w = self.sim()
        r1 = w.send("ถอนเงินขนส่งยังไง")
        self.assertIn("SP หรือ FT", r1["reply"])
        r2 = w.send("SP1008")
        self.assertTrue(self._SP_SIG in r2["reply"] or "ยังไม่พร้อมในระบบ" in r2["reply"], r2)
        r3 = w.send("FT1325")
        # FT answer (or the honest KB-missing fallback) — NOT a fresh
        # "ไม่มีข้อมูลเกี่ยวกับ FT1325" lookup.
        self.assertNotIn("ไม่มีข้อมูลเกี่ยวกับ FT1325", r3["reply"])
        self.assertNotIn("ไม่มีข้อมูลเกี่ยวกับ FT", r3["reply"])
        self.assertNotEqual(r3["src"], "fresh_search", r3)

    def test_B_switch_back_to_SP(self):
        w = self.sim()
        w.send("ถอนเงินขนส่งยังไง")
        w.send("SP1008")
        w.send("FT1325")
        r = w.send("SP1008")
        self.assertNotIn("ไม่มีข้อมูลเกี่ยวกับ SP1008", r["reply"])
        self.assertNotEqual(r["src"], "fresh_search")

    def test_B_brand_token_out_of_context_is_not_hijacked(self):
        w = self.sim()
        w.send("สวัสดี")
        r = w.send("FT1325")
        # no active withdrawal exchange -> ordinary handling, not the
        # withdrawal KB answer
        self.assertNotIn("ประวัติการชำระเงินขนส่ง", r["reply"])


# ═══════════════ C — stale confirmation must not eat "ใช่ค่ะ" ══════════
class TestC_WebhookConfirmationScope(_Base):
    def test_C_help_affirmation_after_greeting_not_expired(self):
        w = self.sim()
        # an old expired pending row exists (>10 min ago)
        w._create_pending(action_id="a1", action_name="SendLineNotiCS",
                          parameters={}, question_text="ยืนยันไหมคะ")
        w._pending[-1]["status"] = "expired"
        w._pending[-1]["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=420)
        # the customer greeted and the bot offered help (real session shape)
        w.history = [_t("user", "สวัสดีค่ะ"),
                     _t("assistant", "สวัสดีค่ะ! มีอะไรให้ช่วยไหมคะ? 😊")]
        r = w.send("ใช่ค่ะ")
        self.assertNotIn("หมดเวลายืนยัน", r["reply"])
        self.assertIn(_HELP_HEAD, r["reply"])

    def test_C_recent_expired_confirmation_still_says_expired(self):
        w = self.sim()
        w._create_pending(action_id="a1", action_name="SendLineNotiCS",
                          parameters={}, question_text="ยืนยันการดำเนินการ 'SendLineNotiCS' ไหมคะ")
        # last assistant turn is a real confirmation question, expired 2 min ago
        w.history = [_t("user", "แจ้งเตือน CS"),
                     _t("assistant", "ยืนยันการดำเนินการ 'SendLineNotiCS' ไหมคะ")]
        r = w.send("ใช่ค่ะ", expire_pending_age_minutes=2)
        self.assertIn("หมดเวลายืนยัน", r["reply"])

    def test_C_genuine_confirmation_still_executes(self):
        # a real, UNEXPIRED pending confirmation + "ยืนยัน" -> decide(confirmed=True)
        w = self.sim()
        w._create_pending(action_id="a1", action_name="SendLineNotiCS",
                          parameters={"CustCode": "FT0000"},
                          question_text="ยืนยันการดำเนินการ 'SendLineNotiCS' ไหมคะ")
        w.history = [_t("user", "แจ้งเตือน CS"),
                     _t("assistant", "ยืนยันการดำเนินการ 'SendLineNotiCS' ไหมคะ")]
        r = w.send("ยืนยัน")
        # the pending row is consumed (executed), not left pending
        self.assertEqual(w._pending[-1]["status"], "executed")
        self.assertNotIn("หมดเวลายืนยัน", r["reply"])

    def test_C_cancel_still_cancels(self):
        w = self.sim()
        w._create_pending(action_id="a1", action_name="SendLineNotiCS",
                          parameters={}, question_text="ยืนยันไหมคะ")
        r = w.send("ยกเลิก")
        self.assertIn("ยกเลิก", r["reply"])
        self.assertEqual(w._pending[-1]["status"], "cancelled")


# ═══════════════ D — shipping-cost discovery, no dead-end ═════════════
class TestD_ShippingCostDiscovery(_Base):
    def _assert_cost_discovery(self, r):
        self.assertNotEqual(r["routing"], "HUMAN_HANDOFF", r)
        self.assertNotEqual(r["routing"], "SAFE_FALLBACK", r)
        self.assertNotIn("ไม่พบคำตอบที่ชัดเจน", r["reply"])
        # asks for the inputs it needs to calculate
        self.assertTrue(any(k in r["reply"] for k in ("น้ำหนัก", "ขนาด", "กว้าง", "CBM", "ปริมาตร")), r["reply"])

    def test_D_long_stale_history(self):
        w = self.sim(handoff_status="NOTIFIED", customer_context={"conversation_tier": "hot"})
        w.history = list(_REAL_PRIOR)
        self._assert_cost_discovery(w.send("ค่าขนส่งแพงไหมคะถ้าสั่งชั้นวางของ"))

    def test_D_empty_history(self):
        self._assert_cost_discovery(self.sim().send("ค่าขนส่งแพงไหมคะถ้าสั่งชั้นวางของ"))

    def test_D_paraphrases(self):
        # the fix targets the EVALUATIVE "is it expensive" phrasing
        for m in ["ค่าส่งแพงมั้ยคะ", "ส่งของแบบนี้ค่าส่งแพงไหม",
                  "ค่าขนส่งแพงหรือเปล่าคะ", "ค่าส่งราคาสูงไหม"]:
            w = self.sim(handoff_status="NOTIFIED")
            w.history = list(_REAL_PRIOR)
            self._assert_cost_discovery(w.send(m))

    def test_D_how_calculated_stays_rag_faq(self):
        # a bare "how is it priced" question (no explicit คำนวณ/ประเมิน
        # verb) is a FAQ, not the estimate slot-collection flow — the D
        # fix must not widen into it.
        from services.conversation_semantics import _compose
        # (phrasings with an explicit calc verb — คำนวณ / คิดราคา — are
        # SHIPPING_ESTIMATE by pre-existing design and are not asserted
        # here.)
        for m in ["ค่าขนส่งคิดยังไง", "ค่านำเข้าเท่าไหร่", "เรทเท่าไหร่"]:
            self.assertNotEqual(_compose(m)[0], "SHIPPING_ESTIMATE", m)

    def test_D_bare_rate_question_still_faq_not_estimate_flow(self):
        # CUSTOMER-CALC-1 must not regress: a bare "เท่าไหร่" rate question
        # is a FAQ, not the estimate slot-collection flow.
        from services.conversation_semantics import interpret
        for m in ["ค่านำเข้าเท่าไหร่", "เรทเท่าไหร่", "ค่าส่งกี่บาท"]:
            self.assertNotEqual(interpret(m, []).intent_family, "SHIPPING_ESTIMATE", m)


# ═══════════════ handoff / hot-tier does not suppress new intents ═════
class TestHandoffTierNonInterference(_Base):
    def test_notified_handoff_does_not_block_calculator(self):
        for hs in ("NONE", "PENDING", "NOTIFIED"):
            w = self.sim(handoff_status=hs, customer_context={"conversation_tier": "hot"})
            w.history = list(_REAL_PRIOR)
            r = w.send(_CALC_PAYLOAD)
            self.assertEqual(r["src"], "shipping_estimate_flow", hs)

    def test_notified_handoff_does_not_block_a_fresh_public_question(self):
        for hs in ("NONE", "NOTIFIED"):
            w = self.sim(handoff_status=hs, rag_answer="ติดต่อได้ทาง LINE @Shipify ค่ะ", rag_conf=0.9)
            w.history = list(_REAL_PRIOR)
            r = w.send("ขอเบอร์ติดต่อ")
            self.assertNotEqual(r["routing"], "WORKFLOW")
            self.assertNotIn("กรุณาแจ้งรหัสลูกค้า", r["reply"])

    def test_explicit_human_request_still_escalates(self):
        w = self.sim()
        w.history = list(_REAL_PRIOR)
        r = w.send("ขอคุยกับเจ้าหน้าที่")
        self.assertEqual(r["routing"], "HUMAN_HANDOFF")


if __name__ == "__main__":
    unittest.main()
