"""P4 — Cold / Warm / Hot lead analysis (services/lead_stage_service.py).

Deterministic signal model, analytical side-channel only. No LLM, no
network, no change to any AI response.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from services.lead_stage_service import (
    evaluate_turn_signals, recompute_lead_stage, update_lead_stage_from_turn,
    stage_reason_labels, is_real_line_user_id, _band, _WEIGHTS,
)

_UA = "U" + "a" * 32
_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _sig(**kw):
    kw.setdefault("question", "")
    return evaluate_turn_signals(**kw)


def _run(prev_score, signals, *, prev_updated_at=None, had_operational=False, now=_NOW):
    return recompute_lead_stage(prev_score=prev_score, prev_updated_at=prev_updated_at,
                                turn_signals=signals, had_operational=had_operational, now=now)


class Bands(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(_band(0), "COLD")
        self.assertEqual(_band(29), "COLD")
        self.assertEqual(_band(30), "WARM")
        self.assertEqual(_band(69), "WARM")
        self.assertEqual(_band(70), "HOT")
        self.assertEqual(_band(100), "HOT")


class SignalExtraction(unittest.TestCase):
    def test_greeting_and_import_interest_are_cold_signals(self):
        s = _sig(question="สวัสดีครับ อยากทราบว่ารับนำเข้าสินค้าจากจีนไหม",
                 actionable_intent="unknown")
        self.assertIn("import_interest", s)
        self.assertIn("general_inquiry", s)
        self.assertNotIn("product_identified", s)
        self.assertFalse(any(x in s for x in ("active_order_intent", "active_shipment")))

    def test_product_identified_from_requestspec(self):
        s = _sig(question="น้ำหอมครับ", product_identified=True)
        self.assertEqual(s, ["product_identified"])

    def test_rate_and_duration_from_facets_or_intent(self):
        self.assertIn("rate_interest", _sig(question="ค่าขนส่งทางรถเท่าไหร่",
                                            actionable_intent="shipping_rate", facets=["rate"]))
        self.assertIn("duration_interest", _sig(question="ใช้เวลากี่วัน", facets=["duration"]))

    def test_transport_comparison(self):
        s = _sig(question="รถกับเรืออันไหนถูกกว่า", transport_modes=["รถ", "เรือ"], comparison="cheaper")
        self.assertIn("transport_comparison", s)
        self.assertIn("transport_interest", s)

    def test_operational_order_phrase(self):
        self.assertIn("active_order_intent", _sig(question="มีรายการที่ต้องเปิดบิลครับ"))
        self.assertIn("active_order_intent", _sig(question="พร้อมสั่งแล้วครับ"))

    def test_operational_business_action(self):
        s = _sig(question="ขอเช็ครายการบิลของผม", routing_type="API",
                 selected_business_action="searchdatashipmentlist")
        self.assertIn("active_shipment", s)

    def test_goods_at_warehouse(self):
        self.assertIn("goods_at_warehouse", _sig(question="ตอนนี้ของอยู่โกดังจีนแล้วครับ"))

    def test_private_identity_lookup_is_not_operational(self):
        s = _sig(question="เบอร์ที่ผมลงทะเบียนไว้คืออะไร", routing_type="API",
                 selected_business_action="getdatacustomer", is_verified=True)
        self.assertEqual(s, ["verified_customer_lookup"])
        self.assertNotIn("active_shipment", s)
        self.assertNotIn("active_order_intent", s)

    def test_static_coupon_faq_is_not_operational(self):
        s = _sig(question="ใช้คูปองยังไง", actionable_intent="coupon_policy")
        self.assertNotIn("payment_for_item", s)
        self.assertFalse(any(x in s for x in
                             ("active_order_intent", "active_shipment", "goods_at_warehouse")))

    def test_thank_you_produces_no_signal(self):
        self.assertEqual(_sig(question="ขอบคุณครับ"), [])


class AcceptanceStages(unittest.TestCase):
    # A
    def test_A_new_general_user_is_cold(self):
        out = _run(0, ["general_inquiry", "import_interest"])
        self.assertEqual(out["stage"], "COLD")

    # B
    def test_B_product_known_reaches_warm(self):
        out = _run(15, ["product_identified"])
        self.assertEqual(out["stage"], "WARM")

    # C
    def test_C_rate_then_duration_is_warm(self):
        a = _run(10, ["rate_interest", "transport_interest"])
        self.assertEqual(a["stage"], "WARM")
        b = _run(a["score"], ["duration_interest"])
        self.assertEqual(b["stage"], "WARM")

    # D
    def test_D_multiple_evaluation_signals_strong_warm(self):
        t1 = _run(0, ["product_identified", "rate_interest"])
        t2 = _run(t1["score"], ["duration_interest", "transport_comparison", "transport_interest"])
        self.assertEqual(t2["stage"], "WARM")
        self.assertGreaterEqual(t2["score"], 45)
        self.assertLess(t2["score"], 70)

    # E
    def test_E_operational_action_is_hot(self):
        out = _run(0, ["active_order_intent"])
        self.assertEqual(out["stage"], "HOT")
        self.assertGreaterEqual(out["score"], 70)

    # F
    def test_F_active_shipment_is_hot(self):
        out = _run(0, ["active_shipment"])
        self.assertEqual(out["stage"], "HOT")

    # G
    def test_G_private_info_only_not_hot(self):
        out = _run(0, ["verified_customer_lookup"])
        self.assertNotEqual(out["stage"], "HOT")
        # and it must not drag a WARM customer up to HOT
        warm = _run(50, ["verified_customer_lookup"])
        self.assertEqual(warm["stage"], "WARM")

    # H
    def test_H_static_faq_not_hot(self):
        self.assertNotEqual(_run(0, [])["stage"], "HOT")

    # I
    def test_I_no_downgrade_from_small_talk(self):
        out = _run(45, [], prev_updated_at=(_NOW - timedelta(hours=2)).isoformat())
        self.assertEqual(out["stage"], "WARM")
        self.assertGreaterEqual(out["score"], 45)

    # J — a customer who already went HOT (had an operational signal) then asks an FAQ
    def test_J_hot_then_faq_stays_hot(self):
        out = _run(78, ["warehouse_interest"], had_operational=True,
                   prev_updated_at=(_NOW - timedelta(hours=1)).isoformat())
        self.assertEqual(out["stage"], "HOT")


class ScoreDynamics(unittest.TestCase):
    def test_per_turn_delta_is_capped(self):
        out = _run(0, ["product_identified", "rate_interest", "duration_interest",
                       "transport_comparison", "warehouse_interest", "quantity_known"])
        self.assertLessEqual(out["score"], 45)   # _PER_TURN_DELTA_CAP

    def test_score_capped_at_100(self):
        out = _run(95, ["active_order_intent"])
        self.assertLessEqual(out["score"], 100)

    def test_lazy_decay_after_long_inactivity(self):
        stale = (_NOW - timedelta(days=40)).isoformat()
        out = _run(80, [], prev_updated_at=stale)
        self.assertLess(out["score"], 80)
        self.assertEqual(out["stage"], "WARM")   # decayed 80*0.7 = 56

    def test_no_decay_when_recently_active(self):
        fresh = (_NOW - timedelta(days=2)).isoformat()
        self.assertEqual(_run(80, [], had_operational=True, prev_updated_at=fresh)["score"], 80)

    def test_small_talk_never_subtracts(self):
        self.assertEqual(_run(60, [])["score"], 60)

    def test_warm_ceiling_until_operational_signal(self):
        # evaluation questions alone never reach HOT
        out = _run(65, ["product_identified", "rate_interest", "duration_interest"])
        self.assertEqual(out["stage"], "WARM")
        self.assertLessEqual(out["score"], 69)


class RealUsersOnly(unittest.TestCase):
    def test_shape_guard(self):
        self.assertTrue(is_real_line_user_id("U" + "0123456789abcdef" * 2))
        self.assertFalse(is_real_line_user_id("playground:UAT-01"))
        self.assertFalse(is_real_line_user_id("Uprobe0000000000000000000000000A"))
        self.assertFalse(is_real_line_user_id("TEST_HANDOFF_UA"))

    def test_update_skips_synthetic_user(self):
        with patch("profiles.manager.get_profile") as gp:
            out = update_lead_stage_from_turn("playground:UAT-01", question="น้ำหอมครับ")
        self.assertIsNone(out)
        gp.assert_not_called()


class PersistenceIsSideChannelOnly(unittest.TestCase):
    def _fake_sb(self):
        captured = {}
        upd = MagicMock()
        def _update(row):
            captured["row"] = row
            return MagicMock(eq=lambda *a, **k: MagicMock(execute=lambda: MagicMock(data=[])))
        tbl = MagicMock()
        tbl.update.side_effect = _update
        sb = MagicMock()
        sb.table.return_value = tbl
        return sb, captured

    def test_only_lead_columns_are_written(self):
        sb, captured = self._fake_sb()
        prof = {"lead_score": 10, "lead_stage": "COLD", "lead_reasons": ["import_interest"],
                "lead_stage_updated_at": (_NOW - timedelta(hours=1)).isoformat()}
        with patch("profiles.manager.get_profile", return_value=prof), \
             patch("profiles.manager.supabase", sb), \
             patch("profiles.manager._profile_cache_clear"):
            out = update_lead_stage_from_turn(
                _UA, question="มีรายการที่ต้องเปิดบิล",
                conversation_fields={"routing_type": "RAG"}, now=_NOW)
        self.assertEqual(set(captured["row"].keys()),
                         {"lead_stage", "lead_score", "lead_reasons", "lead_stage_updated_at"})
        self.assertNotIn("conversation_tier", captured["row"])
        self.assertNotIn("tier_score", captured["row"])
        self.assertEqual(out["lead_stage"], "HOT")

    def test_reasons_are_bounded_machine_keys(self):
        sb, captured = self._fake_sb()
        prof = {"lead_score": 20, "lead_reasons": ["import_interest", "general_inquiry"]}
        with patch("profiles.manager.get_profile", return_value=prof), \
             patch("profiles.manager.supabase", sb), \
             patch("profiles.manager._profile_cache_clear"):
            update_lead_stage_from_turn(
                _UA, question="ค่าขนส่งทางรถเท่าไหร่กับใช้เวลากี่วัน",
                conversation_fields={"routing_type": "RAG"}, now=_NOW)
        reasons = captured["row"]["lead_reasons"]
        self.assertLessEqual(len(reasons), 6)
        for r in reasons:
            self.assertIn(r, _WEIGHTS)

    def test_module_has_no_prompt_or_response_path_import(self):
        import services.lead_stage_service as m
        with open(m.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("import services.prompt_builder", src)
        self.assertNotIn("from services.prompt_builder", src)
        self.assertNotIn("import services.decision_engine", src)


class ReasonLabels(unittest.TestCase):
    def test_labels_are_thai_and_fallback_to_key(self):
        out = stage_reason_labels(["product_identified", "rate_interest", "made_up_key"])
        self.assertEqual(out[0], "ระบุสินค้าแล้ว")
        self.assertEqual(out[2], "made_up_key")


if __name__ == "__main__":
    unittest.main()
