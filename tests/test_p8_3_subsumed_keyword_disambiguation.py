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


# ══════════════════════════════════════════════════════════════════════
# P8.3.1 — the SAME config-driven rule now also breaks the Decision
# Engine's OWN search_candidate_actions top-score tie, not just the
# hybrid classifier's `close` set. Confirmed REAL LINE failure (HEAD
# ed4c138): "ขอเช็กพัสดุเดียวครับ" — classify_question correctly returned
# ERP_ONLY -> searchdatashipment, but for a non-HYBRID/non-CLARIFICATION
# classification the engine re-runs its own search_candidate_actions,
# which scored searchdatashipmentlist and searchdatashipment BOTH at
# _score 1.0 (same "พัสดุ" ⊂ "พัสดุเดียว" collision, no discriminator on
# that path) and select_best_action returned candidates[0] = the LIST
# action -> ERP executed with the latest shipment, no ShipmentCode
# prompt. Real Shipify strings/keys below are FIXTURES ONLY.
# ══════════════════════════════════════════════════════════════════════

from unittest.mock import patch, MagicMock
from services.decision_engine import (
    DecisionEngine, search_candidate_actions, select_best_action,
)
from services.action_selection_primitives import resolve_subsumed_keyword_tie


def _engine(reg):
    e = DecisionEngine(reg._sb)
    e.registry = reg
    return e


def _seed_shipment_detail_and_list(reg):
    detail = _seed(reg, key="searchdatashipment", category="Customer Shipment Retrieval",
                   keywords=["เลขบิลขนส่ง", "พัสดุเดียว", "shipment detail", "รายละเอียดพัสดุ"],
                   params=[_CUST, _SECRET, _SHIPCODE])
    lst = _seed(reg, key="searchdatashipmentlist", category="Customer Shipment Retrieval",
                keywords=["ติดตามพัสดุ", "พัสดุ", "พัสดุล่าสุด", "สถานะรับเข้าไทย", "shipment"],
                params=[_CUST, _SECRET])
    reg.upsert_execution(detail, {"endpoint": "https://example.test/shipment", "http_method": "GET"})
    reg.upsert_execution(lst, {"endpoint": "https://example.test/shipment-list", "http_method": "GET"})
    return detail, lst


class P831_Test1_DecisionEngineLevelFailure(unittest.TestCase):
    """The real failure, asserted at the FULL Decision Engine output:
    DETAIL is selected, its identifier is missing, NO ERP call is made,
    and slot collection asks for the missing identifier (a pending
    workflow turn)."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.detail, self.lst = _seed_shipment_detail_and_list(self.reg)
        self.engine = _engine(self.reg)

    def test_detail_selected_identifier_missing_no_erp_call_asks(self):
        with patch("services.action_executor.requests.request") as mock_req:
            result = self.engine.decide(
                "ขอเช็กพัสดุเดียวครับ", history=[],
                context={"channel": "admin", "developer_mode": True})
        mock_req.assert_not_called()                              # ERP CALL = NO
        self.assertEqual(result["routing"]["type"], "WORKFLOW")   # slot collection turn
        info = (result.get("developer") or {}).get("information_collection_status") or {}
        self.assertEqual(info.get("selected_business_action"), "searchdatashipment")
        self.assertFalse(info.get("is_complete"))
        self.assertNotIn("ShipmentCode", info.get("collected_parameters") or {})


class P831_Test2_ClassifierAndEngineAgree(unittest.TestCase):
    """For the unique/subsumed case, the action classify_question selects
    and the action the Decision Engine's own search+select settles on are
    the SAME — the two layers can no longer disagree."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.detail, self.lst = _seed_shipment_detail_and_list(self.reg)

    def test_same_action_from_both_selection_paths(self):
        msg = "ขอเช็กพัสดุเดียวครับ"
        cls = classify_question(msg, self.reg)
        cls_ids = cls.get("candidate_action_ids") or []
        engine_pick = select_best_action(
            search_candidate_actions(self.reg, workflow=None, message=msg, collected_slots={}),
            minimum_score=0.5)
        self.assertEqual(cls_ids, [self.detail])
        self.assertEqual(engine_pick["id"], self.detail)


class P831_Test3_SyntheticPortabilityThroughEngine(unittest.TestCase):
    """No Shipify vocabulary: Action X configures the longer phrase
    `unique_long` (and is missing a required slot); Action Y configures
    only its substring `unique`, with every param satisfiable. Message
    "please fetch unique_long" -> the Decision Engine's own selection
    path settles on X. A brand-new customer's actions/keywords need no
    Python change."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.x = _seed(self.reg, key="action_x", keywords=["unique_long", "unique"],
                       params=[_CUST, _SECRET, {"name": "XId", "required": True,
                                                 "input_source": "customer_message"}])
        self.y = _seed(self.reg, key="action_y", keywords=["unique", "list"],
                       params=[_CUST, _SECRET])

    def test_engine_selection_path_picks_x(self):
        cands = search_candidate_actions(self.reg, workflow=None,
                                         message="please fetch unique_long", collected_slots={})
        self.assertEqual(cands[0]["action_key"], "action_x")
        self.assertEqual(select_best_action(cands, minimum_score=0.5)["action_key"], "action_x")

    def test_shared_helper_is_config_driven_only(self):
        # the shared rule takes only (tied candidates, message) — no
        # collected slots, no identifier memory, no customer context.
        import inspect
        sig = inspect.signature(resolve_subsumed_keyword_tie)
        self.assertEqual(list(sig.parameters), ["tied_candidates", "message"])


class P831_Test4_GenuineSharedKeywordTiePreserved(unittest.TestCase):
    """Both tied candidates matched the SAME keyword verbatim -> the
    shared rule returns None and the engine does NOT arbitrarily promote
    one; the classifier still asks for clarification."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.a = _seed(self.reg, key="act_a", keywords=["sharedkw", "ล่าสุด"], params=[_CUST, _SECRET])
        self.b = _seed(self.reg, key="act_b", keywords=["sharedkw", "ล่าสุด"], params=[_CUST, _SECRET])

    def test_no_unique_winner_and_clarification_stands(self):
        msg = "check the sharedkw please"
        cands = search_candidate_actions(self.reg, workflow=None, message=msg, collected_slots={})
        top = [c for c in cands if c["_score"] == cands[0]["_score"]]
        self.assertEqual(len(top), 2)
        self.assertIsNone(resolve_subsumed_keyword_tie(top, msg))
        self.assertEqual(classify_question(msg, self.reg)["classification"], "CLARIFICATION_REQUIRED")


class P831_Test5_GenericListRequestStaysList(unittest.TestCase):
    """A genuinely generic list request, where only the LIST action has
    matching configured evidence, still selects LIST — the discriminator
    never forces DETAIL when DETAIL has no unique evidence of its own."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.detail, self.lst = _seed_shipment_detail_and_list(self.reg)

    def test_list_wins_when_only_list_keywords_match(self):
        cands = search_candidate_actions(self.reg, workflow=None,
                                         message="ขอดูสถานะรับเข้าไทยของพัสดุล่าสุด", collected_slots={})
        self.assertEqual(cands[0]["action_key"], "searchdatashipmentlist")
        self.assertEqual(select_best_action(cands, minimum_score=0.5)["action_key"],
                         "searchdatashipmentlist")


class P831_Test6_ExplicitCurrentTurnIdentifierWins(unittest.TestCase):
    """A well-formed identifier present in THIS turn's message lifts the
    specific-record action ABOVE the keyword tie on its own
    (_identifier_pattern_score), so DETAIL wins by score — the subsumed-
    keyword rule is not even the deciding factor, and must not fight it."""

    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.detail = _seed(self.reg, key="searchdatashipment", category="Customer Shipment Retrieval",
                            keywords=["พัสดุเดียว", "shipment detail"],
                            params=[_CUST, _SECRET,
                                    {"name": "ShipmentCode", "required": True, "input_source": "customer_message",
                                     "validation_pattern": r"^[A-Za-z]{2}\d{15,}$"}])
        self.lst = _seed(self.reg, key="searchdatashipmentlist", category="Customer Shipment Retrieval",
                         keywords=["พัสดุ", "shipment"], params=[_CUST, _SECRET])

    def test_identifier_in_message_selects_detail_by_score(self):
        cands = search_candidate_actions(self.reg, workflow=None,
                                         message="ขอเช็กพัสดุ FT318220260726001", collected_slots={})
        self.assertEqual(cands[0]["action_key"], "searchdatashipment")
        self.assertGreater(cands[0]["_score"], cands[1]["_score"])   # won on score, not a tie reorder


class P831_Test7_P82SecurityPreserved(unittest.TestCase):
    """P8.2 cross-identity stale Identifier Memory rejection is untouched:
    a verified CustCode + an account-scoped identifier learned under a
    DIFFERENT cust_code is still stripped, and never prefills the missing
    required slot."""

    def test_stale_cross_identity_identifier_still_rejected(self):
        from services.action_selection_primitives import strip_cross_identity_identifier_memory
        from services.decision_engine import _apply_identifier_memory
        ctx = strip_cross_identity_identifier_memory(
            {"cust_code": "FT3182", "last_shipment_code": "SP100820260817001"},
            profile_cust_code="SP1008", verified_cust_code="FT3182")
        self.assertNotIn("last_shipment_code", ctx)
        action = {"action_key": "searchdatashipment", "parameters": [
            {"name": "CustCode", "input_source": "customer_message", "required": True},
            {"name": "ShipmentCode", "input_source": "customer_message", "required": True},
        ]}
        collected = _apply_identifier_memory(action, {"CustCode": "FT3182"}, ctx)
        self.assertNotIn("ShipmentCode", collected)


class P831_NoNewHardcode(unittest.TestCase):
    """The P8.3.1 additions — the shared helper and the two call sites —
    carry no literal Thai phrase, Business Action key, or DETAIL/LIST
    special case. A future customer's actions work with no source edit."""

    def test_shared_helper_and_call_sites_have_no_fixture_literals(self):
        import inspect
        import services.decision_engine as de
        import services.hybrid_question_classifier as hqc
        src = inspect.getsource(resolve_subsumed_keyword_tie)
        with open(de.__file__, encoding="utf-8") as fh:
            de_full = fh.read()
        # the P8.3.1 post-pass block in search_candidate_actions
        src += de_full.split("Subsumed-Keyword Discriminator (P8.3.1", 1)[1].split("return scored", 1)[0]
        with open(hqc.__file__, encoding="utf-8") as fh:
            hqc_full = fh.read()
        src += hqc_full.split("Subsumed-Keyword Discriminator", 1)[1].split(
            "Final Conversational Correctness", 1)[0]
        for banned in ("พัสดุเดียว", "พัสดุ", "searchdatashipment", "searchdatashipmentlist",
                       "SP1008", "FT3182", "ShipmentCode", "รายละเอียด"):
            self.assertNotIn(banned, src, msg=banned)


if __name__ == "__main__":
    unittest.main()
