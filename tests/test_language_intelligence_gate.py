# -*- coding: utf-8 -*-
"""THAI HUMAN-LANGUAGE INTELLIGENCE GATE — LangGraph path, real engine.

The owner's cases (task §15) and the production-authority contract
(task §12/§13):

  A/B/C  "20 คู่อยากสั่งของจากจีน" in three spellings -> quantity 20 คู่
  D      "เป้นรองเท้าคับ" in requested-product context -> product รองเท้า
  E      "ส่งเรื่อได้ปะ" -> sea shipping understood
  F      "กระต่ายนำเข่าได้หรอ" -> product policy, product กระต่าย, no re-ask
  G      "ขอเช็คออเดอของผมหนอย" -> private status, auth path, no private data

  plus: the real conversation (§10), the corpus thresholds on the
  single-turn tiers (§13), identifier / unknown-product invariants,
  LangGraph production fallback (§12), and the Langfuse trace fields (§14).

Test tier is pinned offline by tests/__init__.py — no paid API call.
"""
import unittest
from unittest.mock import patch

import config
from services.agent import runner as agent_runner
from services.agent.runner import run_agent, authoritative_run, validate_decision
from services.agent.state import AgentDecision
from services.observability import langfuse_client as lf
from tests.language_lab import corpus as C
from tests.language_lab import run_lab as L

CTX = {"channel": "line", "tenant_id": "default", "sample_source": "REAL_LINE",
       "customer_context": {}, "developer_mode": True}


def _turn(text, history=None):
    return run_agent(text, history=history or [], context=dict(CTX))


def _slot(d, name):
    v = (d.known_slots or {}).get(name)
    return (v.get("value"), v.get("unit")) if isinstance(v, dict) else (v, None)


class TestOwnerCases(unittest.TestCase):

    def test_A_B_C_quantity_and_unit_survive_every_spelling(self):
        for text in ("20 คู่อยากสั่งของจากจีน", "20คู่อยากสั่งของจากจีน", "20คุ่อยากสั่งขงจากจีน"):
            d = _turn(text)
            self.assertEqual(_slot(d, "quantity"), (20, "คู่"), text)
            self.assertIsNone(_slot(d, "product")[0], text)
            self.assertEqual(d.planned_action, "ASK_PRODUCT", text)
            self.assertIn("20 คู่", d.final_response, text)
            self.assertNotIn("20 ชิ้น", d.final_response, text)

    def test_D_typo_product_answer_in_requested_product_context(self):
        d = _turn("เป้นรองเท้าคับ", C.HISTORIES["ASK_PRODUCT"])
        self.assertEqual(d.normalized_message, "เป็นรองเท้าครับ")
        self.assertEqual(_slot(d, "product"), ("รองเท้า", None))
        self.assertEqual(_slot(d, "quantity"), (20, "คู่"))
        self.assertNotIn(d.planned_action, ("ASK_PRODUCT", "ASK_QUANTITY"))

    def test_E_sea_shipping_question_is_understood(self):
        d = _turn("ส่งเรื่อได้ปะ", C.HISTORIES["ASK_METHOD"])
        self.assertEqual(d.normalized_message, "ส่งเรือได้ปะ")
        self.assertEqual(_slot(d, "shipping_method")[0], "sea")
        self.assertNotEqual(d.planned_action, "ASK_SHIPPING_METHOD")
        self.assertIn("ทางเรือ", d.final_response)

    def test_F_unknown_product_policy_question(self):
        d = _turn("กระต่ายนำเข่าได้หรอ", C.HISTORIES["JOURNEY"])
        self.assertEqual(d.primary_intent, "PRODUCT_POLICY")
        self.assertEqual(_slot(d, "product"), ("กระต่าย", None))
        self.assertNotIn(d.planned_action, ("ASK_PRODUCT", "ASK_QUANTITY", "ASK_SHIPPING_METHOD"))
        self.assertFalse(d.auth_required)
        self.assertEqual([e for e in d.errors if "RE_ASK" in e], [])

    def test_G_private_status_typo_takes_the_auth_path(self):
        d = _turn("ขอเช็คออเดอของผมหนอย")
        self.assertEqual(d.normalized_message, "ขอเช็คออเดอร์ของผมหน่อย")
        self.assertTrue(d.auth_required)
        self.assertEqual(d.auth_state, "REQUIRED_MISSING")
        self.assertEqual(d.planned_action, "ASK_IDENTIFIER")
        self.assertEqual(d.tool_class, "PRIVATE_READ")
        self.assertFalse((d.engine_result or {}).get("developer", {}).get("erp_http_status"))
        self.assertEqual([e for e in d.errors if e.startswith(("PRIVATE_LEAK", "AUTH"))], [])


class TestRealHumanConversation(unittest.TestCase):
    """Task §10 — the owner's conversation, with the customer's real
    spelling, carried turn by turn through the real engine."""

    def test_the_journey_keeps_its_context(self):
        history, seen = [], []
        turns = ["20 คู่อยากสั่งของจากจีน", "รองเท้า คับ", "ส่งเรือได้ปะ", "แล้วราคาเท่าไหร่อะ",
                 "เอ้ย 10 คู่", "ของแบบนี้นำเข้าได้หรอ", "งั้นขอเบอร์ติดต่อ"]
        for text in turns:
            d = _turn(text, history)
            seen.append(d)
            history = history + [{"role": "user", "content": text},
                                 {"role": "assistant", "content": d.final_response}]
        q = [_slot(d, "quantity") for d in seen]
        p = [_slot(d, "product")[0] for d in seen]
        self.assertEqual(q[0], (20, "คู่"))
        self.assertEqual(p[1], "รองเท้า");             self.assertEqual(q[1], (20, "คู่"))
        self.assertEqual(_slot(seen[2], "shipping_method")[0], "sea")
        self.assertEqual(p[3], "รองเท้า");             self.assertEqual(q[3], (20, "คู่"))
        self.assertEqual(q[4], (10, "คู่"));            self.assertEqual(p[4], "รองเท้า")
        self.assertEqual(seen[5].primary_intent, "PRODUCT_POLICY")
        self.assertEqual(p[5], "รองเท้า", "demonstrative must not overwrite the known product")
        self.assertEqual(seen[6].primary_intent, "CONTACT_INFO")
        self.assertIn("02-026-6426", seen[6].final_response)
        for d in seen:
            self.assertNotIn(d.planned_action, ("ASK_PRODUCT",) if _slot(d, "product")[0] else ())
            self.assertNotIn(d.planned_action, ("ASK_QUANTITY",) if _slot(d, "quantity")[0] else ())
            self.assertFalse(d.auth_required, d.normalized_message)
            self.assertEqual([e for e in d.errors if e.split(":")[0] in agent_runner.SAFETY_FLAG_PREFIXES], [])


class TestCorpusThresholds(unittest.TestCase):
    """Task §13 on the single-turn tiers (stubbed execution — understanding
    only). The 100-journey tier runs in tests/language_lab/run_lab.py and
    is reported in reports/thai_language_intelligence.md."""

    def test_base_ground_truth_is_100_percent(self):
        b = L.evaluate_base()
        self.assertEqual(b["failures"], [])

    def test_typo_semantic_recovery_at_least_95_percent(self):
        cases = C.typo_cases()
        self.assertGreaterEqual(len(cases), 300)
        t = L.evaluate_typos(cases)
        self.assertGreaterEqual(t["recovery_pct"], 95.0,
                                [(f["noisy"], f["fails"]) for f in t["failures"]])

    def test_paraphrase_consistency_at_least_98_percent(self):
        p = L.evaluate_paraphrases()
        self.assertGreaterEqual(p["total"], 300)
        self.assertGreaterEqual(p["consistency_pct"], 98.0,
                                [(f["text"], f["fails"]) for f in p["failures"]])

    def test_identifier_mutation_is_zero(self):
        self.assertEqual(L.evaluate_identifiers()["mutations"], 0)

    def test_over_correction_is_zero(self):
        o = L.evaluate_over_correction()
        self.assertEqual(o["over_corrections"], 0, o["failures"])

    def test_unknown_products_are_preserved(self):
        u = L.evaluate_unknown_products()
        self.assertEqual(u["failures"], [])

    def test_journey_sample_has_no_safety_or_continuity_violation(self):
        j = L.evaluate_journeys(C.journeys()[:10])
        for key in ("known_slot_reask", "stale_context_takeover", "auth_violation",
                    "private_leak", "false_completion", "hallucinated_fact"):
            self.assertEqual(j[key], 0, (key, [f for f in j["failures"]][:5]))
        self.assertEqual(j["failures"], [])


class TestProductionAuthorityAndFallback(unittest.TestCase):
    """Task §12 — LANGGRAPH_MODE=production: the graph is primary for
    every source; the engine is the automatic technical/safety fallback;
    a semantic disagreement is never a fallback reason."""

    def test_production_is_authoritative_for_every_real_source(self):
        with patch.object(config, "LANGGRAPH_MODE", "production"):
            for src in ("REAL_LINE", "OWNER_TEST", "ADMIN_AUTO", "OTHER", ""):
                self.assertTrue(agent_runner.graph_is_authoritative(src), src)

    def test_the_default_mode_is_still_shadow(self):
        self.assertEqual(config.LANGGRAPH_MODE, "shadow")
        self.assertFalse(agent_runner.graph_is_authoritative("REAL_LINE"))

    def test_healthy_graph_answers_and_the_engine_is_not_called(self):
        with patch.object(config, "LANGGRAPH_MODE", "production"):
            out = authoritative_run("20คุ่อยากสั่งขงจากจีน", [], dict(CTX),
                                    engine_fallback=lambda: self.fail("engine must not run"))
        self.assertEqual(out.used, "langgraph")
        self.assertIn("20 คู่", out.decision.final_response)
        self.assertEqual(out.engine_result["reply"]["text"], out.decision.final_response)
        self.assertIsNone(out.fallback_reason)

    def test_a_raising_graph_falls_back_to_the_engine(self):
        sentinel = {"reply": {"text": "engine"}, "routing": {"type": "GENERAL"}, "developer": {}}
        with patch.object(config, "LANGGRAPH_MODE", "production"), \
             patch("services.agent.runner.run_agent", side_effect=RuntimeError("boom")):
            out = authoritative_run("สวัสดี", [], dict(CTX), engine_fallback=lambda: sentinel)
        self.assertEqual(out.used, "engine_fallback")
        self.assertEqual(out.fallback_kind, "technical")
        self.assertIs(out.engine_result, sentinel)

    def test_a_timed_out_graph_falls_back_to_the_engine(self):
        import time as _t
        sentinel = {"reply": {"text": "engine"}, "routing": {"type": "GENERAL"}, "developer": {}}

        def _slow(*_a, **_k):
            _t.sleep(1.0)
            return AgentDecision(final_response="late")

        with patch.object(config, "LANGGRAPH_MODE", "production"), \
             patch("services.agent.runner.run_agent", side_effect=_slow):
            out = authoritative_run("สวัสดี", [], dict(CTX), engine_fallback=lambda: sentinel, timeout=0.05)
        self.assertEqual(out.used, "engine_fallback")
        self.assertIn("exceeded", out.fallback_reason)

    def test_an_invalid_state_falls_back_to_the_engine(self):
        sentinel = {"reply": {"text": "engine"}, "routing": {"type": "GENERAL"}, "developer": {}}
        empty = AgentDecision(final_response="", engine_result={"reply": {"text": ""}})
        with patch.object(config, "LANGGRAPH_MODE", "production"), \
             patch("services.agent.runner.run_agent", return_value=empty):
            out = authoritative_run("สวัสดี", [], dict(CTX), engine_fallback=lambda: sentinel)
        self.assertEqual(out.used, "engine_fallback")
        self.assertEqual(out.fallback_kind, "technical")

    def test_a_safety_flagged_turn_falls_back_to_the_engine(self):
        flagged = AgentDecision(final_response="x", errors=["AUTH_VIOLATION: public turn demanded identity"],
                                engine_result={"reply": {"text": "x"}})
        self.assertEqual(validate_decision(flagged)[0], "safety")
        sentinel = {"reply": {"text": "engine"}, "routing": {"type": "GENERAL"}, "developer": {}}
        with patch.object(config, "LANGGRAPH_MODE", "production"), \
             patch("services.agent.runner.run_agent", return_value=flagged):
            out = authoritative_run("สวัสดี", [], dict(CTX), engine_fallback=lambda: sentinel)
        self.assertEqual(out.used, "engine_fallback")
        self.assertEqual(out.fallback_kind, "safety")

    def test_a_semantic_disagreement_is_not_a_fallback(self):
        """A valid decision with notes/divergence but no error keeps the
        graph's answer — the two engines may read a turn differently."""
        d = AgentDecision(final_response="graph answer", notes=["route divergence: planned X, engine routed Y"],
                          engine_result={"reply": {"text": "graph answer"}})
        self.assertEqual(validate_decision(d), (None, None))

    def test_the_webhook_wires_production_authority_and_fallback(self):
        import inspect
        from line_bot import webhook
        src = inspect.getsource(webhook)
        self.assertIn("authoritative_run(", src)
        self.assertIn("engine_fallback=lambda: engine.decide(", src)
        self.assertIn('result["developer"]["langgraph"] = _agent_telemetry', src)


class TestLangfuseNormalizationTrace(unittest.TestCase):
    """Task §14 — the trace can answer "what did this word become before
    it reached intent?" without carrying a private identifier."""

    def test_trace_fields(self):
        captured = {}

        class _Span:
            def update(self, **kw):
                captured.update(kw)

            def update_trace(self, **kw):
                pass

            def create_event(self, **kw):
                pass

            def end(self):
                pass

        class _Client:
            def start_span(self, **_kw):
                return _Span()

        with patch.object(config, "LANGFUSE_ENABLED", True), \
             patch.object(config, "LANGFUSE_PUBLIC_KEY", "pk"), \
             patch.object(config, "LANGFUSE_SECRET_KEY", "sk"), \
             patch("langfuse.Langfuse", return_value=_Client()):
            lf._reset_for_tests()
            try:
                _turn("ขอเช็คออเดอของผม POS123456 หนอย 0812345678")
            finally:
                lf._reset_for_tests()
        out = captured.get("output") or {}
        norm = out.get("normalization") or {}
        self.assertTrue(norm.get("raw_text_present"))
        self.assertTrue(norm.get("normalization_applied"))
        self.assertIn("rapidfuzz_vocab", norm.get("normalization_method") or [])
        self.assertIn("candidate_count", norm)
        self.assertIn("ออเดอร์", norm.get("normalized_text") or "")
        for key in ("primary_intent", "confidence", "planned_action", "selected_tool", "auth_state", "fallback"):
            self.assertIn(key, out)
        blob = str(captured)
        self.assertNotIn("POS123456", blob)
        self.assertNotIn("0812345678", blob)


if __name__ == "__main__":
    unittest.main()
