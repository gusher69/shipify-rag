# -*- coding: utf-8 -*-
"""CUSTOMER-CALC-1 — shipping-cost estimate as multi-turn slot collection.

REAL LINE: "ค่านำเข้าเท่าไหร่คะ สินค้า1ชิ้น น้ำหนัก 2กิโล ขนาด 54*12*43"
-> "ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ เดี๋ยวทางเราประสานเจ้าหน้าที่…"
(Fix-2 unsupported_company_information). It is an estimate request with
one missing slot (ROAD/SEA) — not a no-info case.
"""
import json
import pathlib
import unittest
from unittest.mock import MagicMock, patch

from services.shipping_estimate_flow import (
    RATES, EstimateState, extract_estimate_fields, compute_estimate,
    derive_estimate_state,
)
from services.decision_engine import DecisionEngine
from tests.test_decision_engine import _fake_playground_result

_BASE = pathlib.Path(__file__).resolve().parent / "customer_uat" / "baseline_results.json"


class TestTrustedRates(unittest.TestCase):
    def test_rates_match_the_production_rate_faq_text(self):
        txt = _BASE.read_text(encoding="utf-8")
        # the "เรทนำเข้า" FAQ answer, verbatim, appears in the baseline pass
        self.assertIn("35 บาท/กิโลกรัม หรือ 6,900 บาท/CBM", txt)
        self.assertIn("19 บาท/กิโลกรัม หรือ 4,500 บาท/CBM", txt)
        self.assertEqual(RATES["road"], {"kg": 35.0, "cbm": 6900.0})
        self.assertEqual(RATES["sea"], {"kg": 19.0, "cbm": 4500.0})


class TestParsing(unittest.TestCase):
    def test_dimension_formats(self):
        for d in ("54*12*43", "54x12x43", "54×12×43", "54 12 43 ซม."):
            s = extract_estimate_fields(f"น้ำหนัก 2 กก. ขนาด {d}")
            self.assertEqual((s.length, s.width, s.height), (54.0, 12.0, 43.0), d)

    def test_weight_wording_variants(self):
        for w in ("2กิโล", "2 กิโล", "2กก", "2 กก.", "2 kg", "2 โล"):
            s = extract_estimate_fields(f"น้ำหนัก {w} ขนาด 54x12x43")
            self.assertEqual(s.weight, 2.0, w)

    def test_count_phrase_not_read_as_dimension(self):
        s = extract_estimate_fields("สินค้า1ชิ้น น้ำหนัก 2กิโล ขนาด 54*12*43")
        self.assertEqual((s.length, s.width, s.height), (54.0, 12.0, 43.0))
        self.assertEqual(s.quantity, 1)

    def test_retention_and_correction(self):
        s = EstimateState()
        extract_estimate_fields("น้ำหนัก 2 โล", s)
        extract_estimate_fields("54x12x43", s)
        extract_estimate_fields("ทางรถ", s)
        self.assertTrue(s.complete())
        extract_estimate_fields("ไม่ใช่ 2 กิโล เป็น 3 กิโล", s)
        self.assertEqual(s.weight, 3.0)
        self.assertEqual((s.length, s.width, s.height, s.method), (54.0, 12.0, 43.0, "road"))


class TestComputation(unittest.TestCase):
    def test_exact_customer_example(self):
        s = extract_estimate_fields("สินค้า1ชิ้น น้ำหนัก 2กิโล ขนาด 54*12*43")
        road = compute_estimate(s, "road")
        self.assertAlmostEqual(road["cbm"], 0.027864, places=6)
        self.assertAlmostEqual(road["kg_charge"], 70.0, places=2)
        self.assertAlmostEqual(road["cbm_charge"], 192.2616, places=3)
        self.assertEqual(road["estimate"], 192.26)
        self.assertEqual(road["basis"], "cbm")
        sea = compute_estimate(s, "sea")
        self.assertEqual(sea["estimate"], 125.39)

    def test_multi_box_uses_per_box_dims_and_total_weight(self):
        s = extract_estimate_fields("3 กล่อง กล่องละ 50x40x30 น้ำหนักรวม 20 กิโล")
        self.assertEqual(s.quantity, 3)
        self.assertTrue(s.per_box_dims and s.total_weight_given)
        c = compute_estimate(s, "road")
        self.assertAlmostEqual(c["cbm"], 0.18, places=4)     # 0.06 * 3
        self.assertAlmostEqual(c["weight"], 20.0, places=2)  # total, not *3
        self.assertEqual(c["estimate"], 1242.0)              # CBM basis


class TestRoutingE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = DecisionEngine()

    def _run(self, msg, hist):
        ctx = {"channel": "line", "tenant_id": "default", "external_user_id": "U_calc1",
               "developer_mode": True, "customer_context": {}}
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG rate FAQ]", confidence=0.9)):
            r = self.eng.decide(msg, history=hist, context=ctx)
        dev = r.get("developer") or {}
        return {"routing": (r.get("routing") or {}).get("type"),
                "src": dev.get("selection_source"),
                "handoff": (r.get("handoff_payload") or {}).get("reason"),
                "reply": (r.get("reply") or {}).get("text") or ""}

    def test_1_exact_real_failure_asks_only_route(self):
        r = self._run("ค่านำเข้าเท่าไหร่คะ สินค้า1ชิ้น น้ำหนัก 2กิโล ขนาด 54*12*43", [])
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertEqual(r["src"], "shipping_estimate_flow")
        self.assertIsNone(r["handoff"])                 # NOT Fix-2 / Human CS
        self.assertIn("ทางรถหรือทางเรือ", r["reply"])
        self.assertNotIn("ไม่มีข้อมูล", r["reply"])

    def test_2_weight_only_asks_dims_and_route(self):
        r = self._run("ช่วยคำนวณค่าส่งให้หน่อย น้ำหนัก 2 โล", [])
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertIn("ขนาด", r["reply"])
        self.assertNotIn("น้ำหนักสินค้า และ ขนาดสินค้า (กว้าง x ยาว x สูง) และ น้ำหนัก", r["reply"])

    def test_3_dims_only_asks_weight_and_route(self):
        r = self._run("ช่วยคำนวณค่าส่ง กล่อง 54x12x43", [])
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertIn("น้ำหนัก", r["reply"])

    def test_5_and_6_full_input_calculates(self):
        r = self._run("ทางรถ น้ำหนัก 2 กิโล ขนาด 54x12x43", [])
        self.assertEqual(r["routing"], "GENERAL")
        self.assertIn("192.26", r["reply"])
        r = self._run("ทางเรือ น้ำหนัก 2 กิโล ขนาด 54x12x43", [])
        self.assertIn("125.39", r["reply"])

    def test_9_retains_values_across_turns(self):
        h = []
        for m in ["ช่วยคำนวณค่าส่งให้หน่อย", "น้ำหนัก 2 โล", "54x12x43"]:
            o = self._run(m, h)
            h += [{"role": "user", "content": m}, {"role": "assistant", "content": o["reply"]}]
        o = self._run("ทางรถ", h)
        self.assertEqual(o["routing"], "GENERAL")
        self.assertIn("192.26", o["reply"])   # weight 2 + dims from earlier turns

    def test_11_no_accidental_human_cs(self):
        for m in ["ค่านำเข้าเท่าไหร่คะ สินค้า1ชิ้น น้ำหนัก 2กิโล ขนาด 54*12*43",
                  "ช่วยคำนวณค่าส่งให้หน่อย น้ำหนัก 2 โล", "ทางรถ น้ำหนัก 2 กิโล ขนาด 54x12x43"]:
            r = self._run(m, [])
            self.assertNotEqual(r["routing"], "HUMAN_HANDOFF", m)
            self.assertIsNone(r["handoff"], m)

    def test_12_bare_rate_faq_unchanged(self):
        for m in ["เรทเท่าไหร่คะ", "ค่านำเข้าเท่าไหร่คะ", "เรทนำเข้าเท่าไหร่"]:
            r = self._run(m, [])
            self.assertNotEqual(r["src"], "shipping_estimate_flow", m)

    def test_13_fix2_true_no_info_unchanged(self):
        r = self._run("Shipify รับประกันว่าสินค้าทุกชิ้นจะผ่านศุลกากรไหม", [])
        self.assertNotEqual(r["src"], "shipping_estimate_flow")


if __name__ == "__main__":
    unittest.main()
