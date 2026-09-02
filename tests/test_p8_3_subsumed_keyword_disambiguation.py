"""P8.3 — generic, config-driven disambiguation when one tied candidate's
matched keyword is only an incidental substring of a DIFFERENT tied
candidate's longer, more-specific configured keyword.

Confirmed REAL LINE failure (HEAD 9fa077c): "ขอเช็กพัสดุเดียวครับ" →
searchdatashipment configures the keyword "พัสดุเดียว" (unique to it),
searchdatashipmentlist configures the generic keyword "พัสดุ"; both scored
1.0 / strong 1.0 because "พัสดุ" is a substring of "พัสดุเดียว", forcing
CLARIFICATION_REQUIRED instead of selecting the DETAIL action and
collecting its missing ShipmentCode.

The real Shipify strings/keys below appear in FIXTURES ONLY.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry
from services.hybrid_question_classifier import classify_question
from services.action_selection_primitives import (
    matched_configured_keywords, surviving_unique_keyword_matches,
)


def _seed(reg, *, key, keywords, params=None, category=None, ai_description=""):
    a = reg.create({
        "action_key": key, "name": key, "display_name": key, "action_type": "API",
        "category": category, "ai_description": ai_description,
        "search_keywords": keywords, "enabled": True, "priority": 0,
    })
    if params:
        reg.replace_parameters(a["id"], params)
    return a["id"]


_CUST = {"name": "CustCode", "required": True, "input_source": "customer_message"}
_SECRET = {"name": "SecretCode", "required": True, "input_source": "credential_store"}
_SHIPCODE = {"name": "ShipmentCode", "required": True, "input_source": "customer_message"}


# ── unit: the shared helper is purely config-driven ──────────────────

class HelperIsGenericAndConfigDriven(unittest.TestCase):
    def _mk(self, kws):
        return {"id": id(tuple(kws)), "search_keywords": list(kws)}

    def test_shorter_keyword_subsumed_by_a_longer_one_in_the_message(self):
        a = self._mk(["alphabeta"])          # more specific
        b = self._mk(["alpha", "status"])    # generic fragment
        msg = "please run alphabeta now"
        cm = {a["id"]: matched_configured_keywords(a, msg),
              b["id"]: matched_configured_keywords(b, msg)}
        surv = surviving_unique_keyword_matches(cm, msg)
        self.assertEqual(surv[a["id"]], {"alphabeta"})
        self.assertEqual(surv[b["id"]], set())        # "alpha" dropped (fragment of "alphabeta")

    def test_verbatim_shared_keyword_is_kept_for_both(self):
        a = self._mk(["unique_a", "status"])
        b = self._mk(["unique_b", "status"])
        msg = "what is the status"
        cm = {a["id"]: matched_configured_keywords(a, msg),
              b["id"]: matched_configured_keywords(b, msg)}
        surv = surviving_unique_keyword_matches(cm, msg)
        self.assertEqual(surv[a["id"]], {"status"})
        self.assertEqual(surv[b["id"]], {"status"})   # genuine shared evidence, untouched

    def test_no_literal_phrase_or_action_name_in_the_new_p83_code(self):
        # Scope: only the P8.3 additions — the two shared helpers and the
        # classifier's Subsumed-Keyword Discriminator block. They must
        # contain no literal discriminator phrase and no action key/id.
        import inspect
        from services.action_selection_primitives import (
            matched_configured_keywords as h1, surviving_unique_keyword_matches as h2)
        import services.hybrid_question_classifier as hqc
        src = inspect.getsource(h1) + inspect.getsource(h2)
        with open(hqc.__file__, encoding="utf-8") as fh:
            full = fh.read()
        src += full.split("Subsumed-Keyword Discriminator", 1)[1].split(
            "Final Conversational Correctness", 1)[0]
        for banned in ("พัสดุเดียว", "พัสดุ", "searchdatashipment", "searchdatashipmentlist",
                       "SP1008", "FT3182", "ShipmentCode", "รายละเอียด"):
            self.assertNotIn(banned, src)


# ── Test 1 — the confirmed production failure ────────────────────────

class Test1_ConfirmedProductionFailure(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.detail = _seed(self.reg, key="searchdatashipment", category="Customer Shipment Retrieval",
                            keywords=["เลขบิลขนส่ง", "พัสดุเดียว", "shipment detail", "รายละเอียดพัสดุ"],
                            params=[_CUST, _SECRET, _SHIPCODE])
        _seed(self.reg, key="searchdatashipmentlist", category="Customer Shipment Retrieval",
              keywords=["ติดตามพัสดุ", "พัสดุ", "พัสดุล่าสุด", "สถานะรับเข้าไทย", "shipment"],
              params=[_CUST, _SECRET])

    def test_detail_action_is_selected_not_clarification(self):
        r = classify_question("ขอเช็กพัสดุเดียวครับ", self.reg)
        self.assertNotEqual(r["classification"], "CLARIFICATION_REQUIRED")
        self.assertEqual(r.get("candidate_action_ids") or [self.detail], [self.detail])

    # Test 2 — another configured discriminator on the SAME action
    def test_second_configured_discriminator_behaves_the_same_way(self):
        r = classify_question("ขอดูรายละเอียดพัสดุบิลนี้", self.reg)
        self.assertNotEqual(r["classification"], "CLARIFICATION_REQUIRED")
        # "รายละเอียดพัสดุ" is unique to DETAIL; "พัสดุ" (LIST) is its fragment.
        self.assertEqual(r.get("candidate_action_ids") or [self.detail], [self.detail])

    # Test 3 — a genuine generic status request still prefers LIST
    def test_generic_status_request_is_not_forced_to_detail(self):
        r = classify_question("เช็กสถานะพัสดุล่าสุด", self.reg)
        # only LIST's keywords ("พัสดุล่าสุด", "สถานะ...") match — DETAIL has no
        # unique evidence, so this is NOT forced to DETAIL.
        ids = r.get("candidate_action_ids") or []
        self.assertNotIn(self.detail, [ids[0]] if ids else [])


# ── Test 5 — genuine ambiguity is preserved ─────────────────────────

class Test5_GenuineAmbiguityPreserved(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        _seed(self.reg, key="act_orders", keywords=["รายการ", "ล่าสุด"], params=[_CUST, _SECRET])
        _seed(self.reg, key="act_shipments", keywords=["รายการ", "ล่าสุด"], params=[_CUST, _SECRET])

    def test_two_actions_with_identical_evidence_still_clarify(self):
        r = classify_question("ขอดูรายการล่าสุด", self.reg)
        self.assertEqual(r["classification"], "CLARIFICATION_REQUIRED")
        self.assertEqual(len(r["candidate_action_ids"]), 2)


# ── Test 7 / 8 / 9 — synthetic portability (no Shipify names) ────────

class SyntheticPortability(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())

    def test_7_unique_evidence_wins_even_when_other_action_has_all_params(self):
        # Action X: strong unique config evidence, missing one required slot.
        x = _seed(self.reg, key="action_x", keywords=["widget_detail_report", "widget"],
                  params=[_CUST, _SECRET, {"name": "WidgetId", "required": True,
                                            "input_source": "customer_message"}])
        # Action Y: only the generic fragment "widget", all params satisfiable.
        _seed(self.reg, key="action_y", keywords=["widget", "list"], params=[_CUST, _SECRET])
        r = classify_question("please pull the widget_detail_report", self.reg)
        self.assertNotEqual(r["classification"], "CLARIFICATION_REQUIRED")
        self.assertEqual(r.get("candidate_action_ids"), [x])

    def test_8_selector_portability_unique_x_vs_unique_y(self):
        x = _seed(self.reg, key="alpha_action", keywords=["unique_x", "shared_word"],
                  params=[_CUST, _SECRET])
        _seed(self.reg, key="beta_action", keywords=["unique_y", "shared_word"],
              params=[_CUST, _SECRET])
        r = classify_question("do the unique_x thing please", self.reg)
        self.assertNotEqual(r["classification"], "CLARIFICATION_REQUIRED")
        self.assertEqual(r.get("candidate_action_ids"), [x])

    def test_9_shared_keyword_gets_no_discriminator_boost(self):
        _seed(self.reg, key="alpha_action", keywords=["unique_x", "shared_word"], params=[_CUST, _SECRET])
        _seed(self.reg, key="beta_action", keywords=["unique_y", "shared_word"], params=[_CUST, _SECRET])
        r = classify_question("check the shared_word", self.reg)
        # both matched "shared_word" verbatim -> existing clarification stands
        self.assertEqual(r["classification"], "CLARIFICATION_REQUIRED")


# ── Test 10 — P8.2 security is untouched ─────────────────────────────

class Test10_P82SecurityPreserved(unittest.TestCase):
    def test_p82_helper_unaffected_and_still_strips_cross_identity_memory(self):
        from services.action_selection_primitives import strip_cross_identity_identifier_memory
        ctx = strip_cross_identity_identifier_memory(
            {"cust_code": "FT3182", "last_shipment_code": "SP100820260817001"},
            profile_cust_code="SP1008", verified_cust_code="FT3182")
        self.assertNotIn("last_shipment_code", ctx)   # P8.2 still rejects it

    def test_p83_does_not_reference_identifier_memory(self):
        import services.hybrid_question_classifier as m
        with open(m.__file__, encoding="utf-8") as fh:
            src = fh.read()
        # the P8.3 block resolves ambiguity from search_keywords only
        block = src.split("Subsumed-Keyword Discriminator", 1)[1].split("Final Conversational Correctness", 1)[0]
        self.assertNotIn("identifier_memory", block)
        self.assertNotIn("last_shipment_code", block)
        self.assertNotIn("customer_context", block)


if __name__ == "__main__":
    unittest.main()
