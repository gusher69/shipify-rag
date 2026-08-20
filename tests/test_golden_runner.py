import json
import unittest
from pathlib import Path

from tests.golden.golden_runner import (
    NotificationSafetyError, _substitute_captured, assert_notification_safety,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = REPO_ROOT / "tests" / "golden" / "golden_cases.json"
ASSERTIONS_MODULE_PATH = REPO_ROOT / "tests" / "golden" / "golden_assertions.py"


class TestNotificationSafetyGuard(unittest.TestCase):
    def test_passes_against_the_real_current_repo(self):
        # This IS the live guard the runner calls before sending any HTTP
        # request -- run it against the actual checked-out admin/routes.py.
        assert_notification_safety(REPO_ROOT)

    def test_aborts_when_the_call_is_present(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "admin").mkdir()
            (root / "admin" / "routes.py").write_text(
                "def hybrid_playground_ask():\n    send_handoff_notification(reason='x')\n", encoding="utf-8")
            with self.assertRaises(NotificationSafetyError):
                assert_notification_safety(root)

    def test_aborts_when_routes_file_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(NotificationSafetyError):
                assert_notification_safety(Path(td))


class TestPlaceholderSubstitution(unittest.TestCase):
    def test_substitutes_captured_value(self):
        out = _substitute_captured("ขอรายละเอียด order {{captured.OrderCode}}", {"OrderCode": "PO12345"})
        self.assertEqual(out, "ขอรายละเอียด order PO12345")

    def test_no_placeholder_passthrough(self):
        out = _substitute_captured("plain text", {})
        self.assertEqual(out, "plain text")

    def test_missing_capture_raises_rather_than_fabricating(self):
        with self.assertRaises(RuntimeError):
            _substitute_captured("{{captured.OrderCode}}", {})


class TestDatasetIntegrity(unittest.TestCase):
    """Golden Runner Integrity (Phase 8) -- structural checks on the
    committed dataset/runner/assertion-engine files themselves, so a
    regression here (a duplicate ID, a mutated expectation, a hardcoded
    special case) is caught by `python -m unittest discover -s tests`,
    the same command every other change in this repo is verified with."""

    @classmethod
    def setUpClass(cls):
        cls.dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
        cls.cases = cls.dataset["cases"]

    def test_dataset_version_present(self):
        self.assertIn("dataset_version", self.dataset)
        self.assertRegex(self.dataset["dataset_version"], r"^\d+\.\d+\.\d+$")

    def test_fifty_canonical_cases_plus_one_supplement(self):
        canonical = [c for c in self.cases
                     if not c["golden_id"].startswith("GOLDEN-038B") and c.get("source") != "customer_uat"]
        self.assertEqual(len(canonical), 50)

    def test_customer_uat_cases_present_and_never_touch_the_original_fifty_one(self):
        """Dataset version 1.1.0 (2026-08-17) — Customer-Reported ERP
        Conversation Defects added 6 new regression cases derived from
        real customer UAT feedback, without modifying any of the
        original 50 cases or GOLDEN-038B. Version 1.2.0 (same day) added
        a 7th (GOLDEN-057), found during this same fix round's own
        mandatory server UAT re-test — see its own `description`. Version
        1.3.0 (2026-08-19) added an 8th (GOLDEN-058), the GetUrlProductDetail
        link-conversion defect — see its own `description`. Version 1.4.0
        (2026-08-19) added a 9th (GOLDEN-059), the searchdatashipmentlist
        keyword/example-coverage gap — see its own `description`."""
        customer_uat = [c for c in self.cases if c.get("source") == "customer_uat"]
        self.assertEqual(len(customer_uat), 9)
        for c in customer_uat:
            self.assertIn(c.get("reported_at"), ("2026-08-17", "2026-08-19"))
            self.assertIn(c.get("severity"), ("P0", "P1", "P2"))
        self.assertEqual(len(self.cases), 60)

    def test_golden_ids_unique(self):
        ids = [c["golden_id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_case_has_required_fields(self):
        required = {"golden_id", "category", "description", "turns", "expected_route",
                    "expected_action", "expected_behavior", "assertions"}
        for c in self.cases:
            missing = required - set(c.keys())
            self.assertFalse(missing, f"{c['golden_id']} missing fields: {missing}")

    def test_every_case_has_at_least_one_assertion(self):
        for c in self.cases:
            self.assertGreater(len(c["assertions"]), 0, f"{c['golden_id']} has no assertions")

    def test_multi_turn_cases_have_all_turns_in_order(self):
        # Spot-check a known multi-turn case retains every turn verbatim.
        by_id = {c["golden_id"]: c for c in self.cases}
        case = by_id["GOLDEN-013-ORDER-STATUS-CONTEXT"]
        self.assertEqual(len(case["turns"]), 4)
        self.assertEqual(case["turns"][0], "ผม FT3182")
        self.assertEqual(case["turns"][-1], "ตอนนี้สถานะอะไร")

    def test_ambiguous_cases_have_null_expected_route(self):
        ambiguous_ids = {"GOLDEN-030-CONTEXT-NATURAL-REFERENCE", "GOLDEN-033-STAGE-HOT",
                          "GOLDEN-034-STAGE-NEGATIVE", "GOLDEN-050-NATURAL-FOLLOWUP"}
        by_id = {c["golden_id"]: c for c in self.cases}
        for gid in ambiguous_ids:
            self.assertIsNone(by_id[gid]["expected_route"], f"{gid} should be expected_route=null")

    def test_all_assertion_types_are_known_to_the_engine(self):
        from tests.golden.golden_assertions import _EVALUATORS
        for c in self.cases:
            for a in c["assertions"]:
                self.assertIn(a["type"], _EVALUATORS, f"{c['golden_id']} uses unknown assertion type {a['type']!r}")

    def test_golden_038b_exercises_the_real_dedup_state_machine(self):
        by_id = {c["golden_id"]: c for c in self.cases}
        case = by_id["GOLDEN-038B-HANDOFF-DUPLICATE-PROTECTION"]
        self.assertEqual(len(case["turns"]), 2)
        types = [a["type"] for a in case["assertions"]]
        self.assertIn("notification_simulated_sent_equals", types)

    def test_runner_never_queries_the_golden_test_registry_table(self):
        # The docstring is allowed to MENTION golden_test_registry (to
        # document that it's deliberately untouched) -- what must never
        # appear is an actual Supabase call against that table.
        runner_src = (REPO_ROOT / "tests" / "golden" / "golden_runner.py").read_text(encoding="utf-8")
        self.assertNotIn('.table("golden_test_registry")', runner_src,
                          "the runner must never read or write the original audited registry table")

    def test_assertion_engine_makes_no_network_or_db_calls(self):
        src = ASSERTIONS_MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in ("import requests", "supabase", "create_client", "psycopg2"):
            self.assertNotIn(forbidden, src, f"golden_assertions.py must stay pure -- found {forbidden!r}")


if __name__ == "__main__":
    unittest.main()
