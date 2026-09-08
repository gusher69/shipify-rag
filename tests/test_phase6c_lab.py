# -*- coding: utf-8 -*-
"""PHASE-6C — CI gate for the production conversation lab.

Runs the DETERMINISTIC corpus (real DecisionEngine + real semantic layer
+ real classifiers + real prompt composition; RAG synthesis LLM and the
ERP HTTP call mocked) and asserts:

  * no FAIL_ROUTING / FAIL_AUTH / FAIL_HALLUCINATION / FAIL_STATE
    regressions beyond a small, explicitly-listed known set,
  * the overall deterministic pass rate stays at/above a floor,
  * every "protected" family (real-LINE A/B/C/D, calculator, withdrawal,
    link conversion, contact, warehouse-inbound, help/discovery) has
    zero hard failures.

The live grounding/hallucination pass is run separately (not in CI —
needs the real LLM + embeddings): `python -m tests.phase6c_lab.run_lab --live`.
"""
import os
import unittest
from collections import Counter

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-phase6c-ci")

from tests.phase6c_lab.corpus import CORPUS
from tests.phase6c_lab.harness import WebhookConversation
from tests.phase6c_lab.validator import validate, observed_source
from tests.phase6c_lab.run_lab import _resolve_history
from tests.test_business_action_registry import reset_real_registry
from services.decision_engine import DecisionEngine

# hard-failure categories the CI gate never tolerates for a deterministic case
_HARD = {"FAIL_ROUTING", "FAIL_AUTH", "FAIL_HALLUCINATION", "FAIL_STATE"}

# families whose deterministic behaviour must be 100% clean of hard failures
_PROTECTED_FAMILIES = {
    "calculator_payload", "calculator_units", "shipping_cost_discovery",
    "shipping_withdrawal", "link_conversion", "contact_info",
    "warehouse_inbound_journey", "help_intent", "service_discovery",
    "money_transfer_intent", "website_link_request", "private_erp_required",
    "journey_contradiction", "journey_topic_switch", "journey_reject_loop",
    "stale_confirmation", "genuine_confirmation", "genuine_cancel",
}

# known-acceptable soft misses in deterministic mode (mocked RAG can't
# produce the graded prose some FAIL_NEXT_ACTION / FAIL_GROUNDING checks
# want). Keep this list SHORT and explicit; a new id here needs a reason.
_KNOWN_SOFT: dict = {}


class TestPhase6CDeterministicGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_real_registry()
        cls.engine = DecisionEngine()
        cls.results = []
        for case in [c for c in CORPUS if c["mode"] != "live"]:
            ctx = case.get("context") or {}
            conv = WebhookConversation(engine=cls.engine, mode="deterministic",
                                       handoff_status=ctx.get("handoff_status", "NONE"),
                                       cust_code=ctx.get("cust_code"),
                                       conversation_tier=ctx.get("conversation_tier"))
            conv.seed_history(_resolve_history(ctx))
            seed = ctx.get("seed")
            if seed == "expired_confirmation":
                conv.seed_expired_confirmation()
            elif seed == "active_confirmation":
                conv.seed_active_confirmation()
            outs = []
            try:
                for turn in case["turns"]:
                    outs.append(conv.send(turn if isinstance(turn, str) else turn["user"]))
                verdict = validate(case, outs)
            except Exception as e:
                verdict = {"status": "FAIL_STATE", "reason": f"harness exception: {e!r}"}
            cls.results.append((case, outs, verdict))

    def test_no_hard_failures(self):
        hard = [(c["case_id"], c["family"], v["status"], v["reason"])
                for (c, _o, v) in self.results if v["status"] in _HARD]
        self.assertEqual(hard, [], f"{len(hard)} hard failures:\n" +
                         "\n".join(f"  {x[0]} [{x[1]}] {x[2]} — {x[3]}" for x in hard))

    def test_protected_families_have_no_failures_at_all(self):
        bad = [(c["case_id"], c["family"], v["status"], v["reason"])
               for (c, _o, v) in self.results
               if c["family"] in _PROTECTED_FAMILIES and v["status"] not in ("PASS", "EXPECTED_LIMITATION")
               and c["case_id"] not in _KNOWN_SOFT]
        self.assertEqual(bad, [], f"{len(bad)} protected-family failures:\n" +
                         "\n".join(f"  {x[0]} [{x[1]}] {x[2]} — {x[3]}" for x in bad))

    def test_overall_deterministic_pass_floor(self):
        c = Counter(v["status"] for (_c, _o, v) in self.results)
        n = len(self.results)
        passed = c.get("PASS", 0) + c.get("EXPECTED_LIMITATION", 0)
        pct = 100.0 * passed / n if n else 0.0
        self.assertGreaterEqual(pct, 90.0,
                                f"deterministic pass rate {pct:.1f}% ({passed}/{n}); breakdown {dict(c)}")


if __name__ == "__main__":
    unittest.main()
