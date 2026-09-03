# -*- coding: utf-8 -*-
"""SEM-1 — private-record status-inquiry semantic routing.

A HUMAN question about the STATE of the customer's OWN record / account
must enter the matching Business Action's collection flow (acknowledge →
ask for the missing identifier), never fall through to RAG and dead-end
on "ไม่มีข้อมูลยืนยัน".

These tests exercise the pure concept recognizer plus the REAL
DecisionEngine against a seeded registry that mirrors the production
Status-Inquiry actions. They deliberately use UNSEEN paraphrases — the
production code must recognize the *concept*, not a phrase list.
"""
import unittest
from unittest.mock import MagicMock, patch

from tests.test_business_action_registry import _FakeSupabase
from tests.test_decision_engine import _engine_with_registry, _seed_action, _fake_playground_result
from services.business_action_registry import BusinessActionRegistry
from services.decision_engine import _classify_private_state_inquiry


def _seed_status_registry(reg):
    # list actions need only the customer identifier; detail actions need
    # a per-record identifier — SEM-1.1 record-scope routing depends on
    # both variants existing.
    specs = [
        ("searchdatashipmentlist", "Customer Shipment Retrieval", ["พัสดุล่าสุด", "รายการพัสดุ"], None),
        ("searchdatashipment", "Customer Shipment Retrieval", ["เลขบิลขนส่ง", "พัสดุเดียว"], "ShipmentCode"),
        ("searchdataorderlist", "Customer Order Retrieval", ["รายการสั่งซื้อ", "ออเดอร์ล่าสุด"], None),
        ("searchdataorder", "Customer Order Retrieval", ["เลขคำสั่งซื้อ", "ออเดอร์เดียว"], "OrderCode"),
        ("searchdatatracking", "Customer Shipment Retrieval", ["แทร็กจีน", "tracking"], "Tracking"),
        ("getdatacustomer", "Customer Data Retrieval", ["ข้อมูลลูกค้า", "ยอดเงิน"], None),
    ]
    for key, cat, kws, rec_param in specs:
        aid = _seed_action(reg, key=key, action_type="API", category=cat, keywords=kws)
        params = [{"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
                   "input_source": "customer_message"}]
        if rec_param:
            p = {"name": rec_param, "display_name": "เลขที่บิลขนส่ง" if rec_param == "ShipmentCode" else rec_param,
                 "required": True, "input_source": "customer_message"}
            if rec_param == "ShipmentCode":
                p["validation_pattern"] = r"^[A-Za-z]{2}[0-9]{6,}$"
            params.append(p)
        reg.replace_parameters(aid, params)
        reg.upsert_execution(aid, {"endpoint": f"https://erp.invalid/{key}", "http_method": "POST"})


class TestPrivateStateConcept(unittest.TestCase):
    """The recognizer is compositional, not a phrase table — unseen
    wordings in each domain must resolve to the right domain, and public
    / how-to / operational-write wordings must NOT fire."""

    def _dom(self, msg):
        r = _classify_private_state_inquiry(msg)
        return r["domain"] if r else None

    def test_shipment_paraphrases(self):
        for m in ["ของที่ส่งมาถึงไหนแล้ว", "พัสดุผมเป็นไงบ้าง", "ของผมเข้าไทยยัง",
                  "สินค้าถึงโกดังหรือยัง", "เช็กของผมให้หน่อย", "วันนี้มีของเข้าไทยไหมคะ"]:
            self.assertEqual(self._dom(m), "shipment", m)

    def test_order_paraphrases(self):
        for m in ["ออเดอร์ที่ผมสั่งไปถึงไหน", "ร้านส่งของให้ผมหรือยัง", "เช็กคำสั่งซื้อของผมที",
                  "ร้านส่งหรือยังคะ"]:
            self.assertEqual(self._dom(m), "order", m)

    def test_wallet_and_coupon_paraphrases(self):
        for m in ["เงินในระบบผมเหลือเท่าไหร่", "ยอดของผมมีเท่าไร", "เงินที่เติมเข้าไปมาหรือยัง",
                  "ในบัญชีผมมีคูปองอะไร", "ตอนนี้ผมเหลือคูปองไหม"]:
            self.assertEqual(self._dom(m), "customer_data", m)

    def test_tracking_paraphrases(self):
        for m in ["ขอแทรคไทยค่ะ", "เลขแทร็กจีนของผมสถานะอะไร"]:
            self.assertEqual(self._dom(m), "tracking", m)

    def test_public_and_howto_never_fire(self):
        for m in ["คูปองใช้ยังไง", "ใช้คูปองยังไง", "ค่าขนส่งคิดยังไง", "ขอเบอร์ติดต่อ",
                  "สินค้าที่ห้ามนำเข้ามีอะไรบ้าง", "มีบริการอะไรบ้าง", "ขนส่งเอกชนมีอะไรบ้าง",
                  "คูปองใช้ไม่หมด คืนได้ไหมคะ", "มีขั้นต่ำในการสั่งไหม",
                  "ถอนเงินสั่งซื้อยังไง", "โหลดใบกำกับยังไง", "ขอที่อยู่โกดังจีน",
                  "ระยะเวลาการส่งจากร้านจีน -โกดังจีน"]:
            self.assertIsNone(_classify_private_state_inquiry(m), m)

    def test_operational_write_never_fires(self):
        # SEM-1 point 6 — a WRITE / change request is not a status inquiry
        # and must be left for the Human Handoff phase, not this fix.
        for m in ["ต้องการแก้จำนวนสินค้าในบิล", "สามารถเปลี่ยนเป็นจัดส่งทางรถได้ไหมคะ",
                  "ยกเลิกบิลสั่งซื้อได้ไหม", "เปลี่ยนที่อยู่พัสดุให้หน่อย", "บิลซ้ำค่ะ"]:
            self.assertIsNone(_classify_private_state_inquiry(m), m)


class TestPpcBoundary(unittest.TestCase):
    """PUBLIC / PRIVATE / CLARIFICATION MASTER (2026-09-03) — two
    boundary cases the recognizer previously got wrong."""

    def _dom(self, msg):
        r = _classify_private_state_inquiry(msg)
        return r["domain"] if r else None

    def test_facility_location_or_hours_is_public_never_private(self):
        # a warehouse / branch LOCATION or opening-hours question is
        # public FAQ even though it names "สินค้า" and asks "อยู่ที่ไหน" —
        # it must NOT be coerced into a private shipment-status inquiry
        # that asks the customer for a CustCode.
        for m in ["โกดังรับสินค้าอยู่ที่ไหน", "โกดังไทยอยู่ตรงไหน",
                  "จุดรับสินค้าอยู่ที่ไหนคะ", "สาขานนทบุรีเปิดกี่โมง",
                  "คลังสินค้าเปิดทำการวันไหนบ้าง", "ออฟฟิศบริษัทอยู่ที่ไหน"]:
            self.assertIsNone(_classify_private_state_inquiry(m), m)

    def test_own_on_file_contact_value_question_is_private(self):
        # "what is the phone/email I registered?" is a question about the
        # customer's OWN account datum — private, account-scoped, must not
        # fall through to public RAG.
        for m in ["เบอร์ที่ผมลงทะเบียนไว้คืออะไร", "อีเมลที่ผมผูกไว้ในระบบคืออะไร",
                  "เบอร์ที่ลงทะเบียนของผมเป็นอะไร", "ข้อมูลลูกค้าของผมในระบบคืออะไร"]:
            self.assertEqual(self._dom(m), "customer_data", m)

    def test_public_value_question_without_owner_still_never_fires(self):
        # the value-question branch is owner-gated, so a public glossary
        # question ("CBM คืออะไร") is untouched.
        for m in ["CBM คืออะไร", "ค่า CO คืออะไร", "พรีออเดอร์คืออะไร",
                  "ค่าธรรมเนียมศุลกากรคืออะไร"]:
            self.assertIsNone(_classify_private_state_inquiry(m), m)


class TestPrivateStateRouting(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        _seed_status_registry(self.reg)
        self.engine = _engine_with_registry(self.reg)

    def _route(self, msg):
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG]", confidence=0.9)) as rag:
            res = self.engine.decide(msg, history=[], context={"channel": "line", "developer_mode": True,
                                                              "customer_context": {}})
        return res, rag

    def test_private_state_inquiry_enters_erp_collection_not_rag(self):
        for m in ["สินค้าถึงโกดังหรือยัง", "ร้านส่งของให้ผมหรือยัง", "ของผมเข้าไทยยัง",
                  "ยอดของผมเหลือเท่าไหร่", "ขอแทรคไทยค่ะ", "วันนี้มีของเข้าไทยไหมคะ"]:
            res, rag = self._route(m)
            self.assertEqual(res["routing"]["type"], "WORKFLOW", m)
            dev = res.get("developer") or {}
            self.assertEqual(dev.get("private_state_inquiry", {}).get("intent"), "status_inquiry", m)
            self.assertEqual(dev.get("selection_source"), "private_state_inquiry", m)
            reply = (res.get("reply") or {}).get("text") or ""
            self.assertNotIn("ยังไม่มีข้อมูล", reply)
            self.assertNotIn("ไม่มีข้อมูลยืนยัน", reply)

    def test_public_question_still_reaches_rag_and_no_identity_prompt(self):
        for m in ["คูปองใช้ยังไง", "ค่าขนส่งคิดยังไง", "ขอเบอร์ติดต่อ"]:
            res, rag = self._route(m)
            self.assertEqual(res["routing"]["type"], "RAG", m)
            reply = (res.get("reply") or {}).get("text") or ""
            for bad in ("ยืนยันตัวตน", "เบอร์โทรที่ผูก", "รหัสลูกค้า"):
                self.assertNotIn(bad, reply, m)

    def _sel(self, res):
        dev = res["developer"] or {}
        ics = dev.get("information_collection_status") or {}
        return dev.get("selected_business_action") or ics.get("selected_business_action")

    def test_domain_routes_to_matching_capability(self):
        self.assertEqual(self._sel(self._route("ยอดของผมเหลือเท่าไหร่")[0]), "getdatacustomer")
        # UNSPECIFIED single-record scope -> per-record detail action
        self.assertEqual(self._sel(self._route("ออเดอร์ที่ผมสั่งไปถึงไหน")[0]), "searchdataorder")
        self.assertEqual(self._sel(self._route("ของผมเข้าไทยยัง")[0]), "searchdatashipment")


class TestSem11RecordScope(unittest.TestCase):
    """SEM-1.1 — an UNSPECIFIED single-record status inquiry (no
    identifier, no explicit latest/list scope) must route to the DETAIL
    action and ask for the record identifier, never silently return a
    latest / historical record via a customer-scoped list action."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        _seed_status_registry(self.reg)
        self.engine = _engine_with_registry(self.reg)

    def _run(self, msg, cc=None):
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200,
                                          json=lambda: {"data": {"Shipment": {"Code": "FT318220260726001"}}})) as erp, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG]", confidence=0.9)):
            res = self.engine.decide(msg, history=[], context={
                "channel": "line", "developer_mode": True,
                "customer_context": dict(cc or {"cust_code": "FT3182", "identity_confirmed": True})})
        dev = res.get("developer") or {}
        ics = dev.get("information_collection_status") or {}
        return {
            "routing": res["routing"]["type"],
            "sel": dev.get("selected_business_action") or ics.get("selected_business_action"),
            "scope": (dev.get("private_state_inquiry") or {}).get("record_scope"),
            "complete": bool(ics.get("is_complete")),
            "erp": erp.called,
            "reply": (res.get("reply") or {}).get("text") or "",
        }

    def test_A_unspecified_asks_for_identifier_no_latest(self):
        r = self._run("ของผมเข้าไทยหรือยัง")
        self.assertEqual(r["scope"], "UNSPECIFIED")
        self.assertEqual(r["sel"], "searchdatashipment")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertFalse(r["complete"])
        self.assertFalse(r["erp"])
        self.assertIn("บิลขนส่ง", r["reply"])
        self.assertNotIn("FT318220260726001", r["reply"])

    def test_B_check_my_parcel_no_silent_latest(self):
        r = self._run("เช็กพัสดุของผมให้หน่อย")
        self.assertEqual(r["sel"], "searchdatashipment")
        self.assertFalse(r["complete"])

    def test_C_explicit_latest_allowed(self):
        r = self._run("พัสดุล่าสุดของผมถึงไหนแล้ว")
        self.assertEqual(r["scope"], "LATEST")
        self.assertEqual(r["sel"], "searchdatashipmentlist")

    def test_D_explicit_identifier_uses_detail_with_id(self):
        r = self._run("เช็ก FT318220260726001 ให้หน่อย")
        self.assertEqual(r["sel"], "searchdatashipment")

    def test_list_all_scope_uses_list_action(self):
        self.assertEqual(self._run("พัสดุของผมมีอะไรบ้าง")["sel"], "searchdatashipmentlist")
        self.assertEqual(self._run("วันนี้มีของเข้าไทยไหมคะ")["sel"], "searchdatashipmentlist")

    def test_order_unspecified_asks_for_order_code(self):
        r = self._run("ร้านส่งหรือยังคะ")
        self.assertEqual(r["sel"], "searchdataorder")
        self.assertFalse(r["complete"])

    def test_wallet_is_account_scoped_not_per_record(self):
        r = self._run("ยอดของผมเหลือเท่าไหร่")
        self.assertEqual(r["sel"], "getdatacustomer")

    def test_public_transport_question_untouched(self):
        r = self._run("ทางรถใช้เวลากี่วัน")
        self.assertEqual(r["routing"], "RAG")

    def test_redirect_when_keyword_search_picks_the_list_action(self):
        """SEM-1.1b — on a production-shaped registry the LIST action
        matches these phrasings by keyword and (for a verified user) would
        auto-execute and return the latest record. An UNSPECIFIED-scope
        inquiry must be REDIRECTED to the per-record detail action."""
        lst = self.reg.get_by_key("searchdatashipmentlist")
        self.reg.update(lst["id"], {"search_keywords": [
            "พัสดุล่าสุด", "รายการพัสดุ", "ของผม", "เข้าไทย", "ถึงโกดัง", "ของที่ส่ง"]})
        r = self._run("ของผมเข้าไทยหรือยัง")
        self.assertEqual(r["sel"], "searchdatashipment")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertFalse(r["complete"])
        self.assertFalse(r["erp"])
        self.assertIn("บิลขนส่ง", r["reply"])
        # explicit latest scope keeps the list action even with the same keywords
        r2 = self._run("พัสดุล่าสุดของผมถึงไหนแล้ว")
        self.assertEqual(r2["sel"], "searchdatashipmentlist")


class TestSem12PendingWorkflowBoundary(unittest.TestCase):
    """SEM-1.2 — a pending workflow is context, not ownership of every
    later message. A greeting / cancel / self-contained new request /
    self-contained UNSPECIFIED private-state inquiry must NOT be consumed
    as a pending-parameter value; a genuine value/continuation still is.
    """

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        _seed_status_registry(self.reg)
        self.reg.get_by_key("searchdatashipmentlist")
        self.reg.update(self.reg.get_by_key("searchdatashipmentlist")["id"],
                        {"search_keywords": ["พัสดุล่าสุด", "ของผม", "เข้าไทย", "ถึงโกดัง"]})
        _seed_action(self.reg, key="contact_faq", action_type="RAG", category="faq",
                     keywords=["เบอร์ติดต่อ", "เบอร์โทร", "ติดต่อ"])
        self.engine = _engine_with_registry(self.reg)
        self.pending = self.reg.get_by_key("searchdatashipment")["id"]
        self.hist = [{"role": "user", "content": "ขอเช็กพัสดุเดียวครับ"},
                     {"role": "assistant", "content": "กรุณาแจ้งเลขที่บิลขนส่งค่ะ"}]

    def _run(self, msg, hist=None, pending=True):
        ctx = {"channel": "line", "developer_mode": True,
               "customer_context": {"cust_code": "FT3182", "identity_confirmed": True}}
        if pending:
            ctx["pending_action_id"] = self.pending
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})) as erp, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAGANS]", confidence=0.9)):
            res = self.engine.decide(msg, history=self.hist if hist is None else hist, context=ctx)
        dev = res.get("developer") or {}
        ics = dev.get("information_collection_status") or {}
        return {"routing": res["routing"]["type"],
                "sel": dev.get("selected_business_action") or ics.get("selected_business_action"),
                "src": dev.get("selection_source"), "op": dev.get("conversation_operation"),
                "sup": dev.get("stale_workflow_suppressed"), "erp": erp.called,
                "reply": (res.get("reply") or {}).get("text") or ""}

    def test_greeting_during_pending_is_not_consumed(self):
        r = self._run("สวัสดีครับ")
        self.assertEqual(r["sup"], "social_greeting")
        self.assertNotIn("เลขที่บิลขนส่ง", r["reply"])
        self.assertFalse(r["erp"])
        self.assertNotEqual(r["src"], "conversation_continuation")

    def test_pending_resumes_with_a_real_identifier(self):
        r = self._run("FT318220260726001")
        self.assertEqual(r["sel"], "searchdatashipment")
        self.assertEqual(r["src"], "conversation_continuation")

    def test_self_contained_new_public_request_during_pending(self):
        r = self._run("ขอเบอร์ติดต่อ")
        self.assertNotEqual(r["src"], "conversation_continuation")
        self.assertNotIn("เลขที่บิลขนส่ง", r["reply"])

    def test_cancel_during_pending(self):
        for m in ("ไม่เอาแล้ว", "ไม่เช็กแล้ว", "ยกเลิก"):
            r = self._run(m)
            self.assertEqual(r["op"], "CANCEL", m)
            self.assertNotIn("เลขที่บิลขนส่ง", r["reply"], m)

    def test_unspecified_private_status_during_pending_asks_identifier_not_latest(self):
        r = self._run("ของผมเข้าไทยหรือยัง")
        self.assertEqual(r["sup"], "private_state_self_contained")
        self.assertEqual(r["sel"], "searchdatashipment")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertFalse(r["erp"])
        self.assertIn("บิลขนส่ง", r["reply"])
        self.assertNotIn("ล่าสุด", r["reply"])

    def test_explicit_latest_during_pending_still_allowed(self):
        self.assertEqual(self._run("พัสดุล่าสุดของผมถึงไหนแล้ว")["sel"], "searchdatashipmentlist")

    def test_explicit_identifier_during_pending_is_detail(self):
        self.assertEqual(self._run("เช็ก FT318220260726001")["sel"], "searchdatashipment")

    def test_greeting_with_no_pending_still_greets(self):
        r = self._run("สวัสดีครับ", hist=[], pending=False)
        self.assertFalse(r["erp"])
        self.assertNotIn("เลขที่บิลขนส่ง", r["reply"])


if __name__ == "__main__":
    unittest.main()
