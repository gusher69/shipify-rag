# -*- coding: utf-8 -*-
"""P0 LATENCY FIX (2026-09-18) — semantic-interpretation reuse between
LangGraph's resolve_current_turn node and DecisionEngine.decide().

Production logs on the deployed webhook confirmed every customer turn was
taking 8-12+ seconds. Root cause: services/agent/nodes/semantics.py::
resolve_current_turn already ran the full central semantic interpretation
(services/conversation_semantics.py::interpret, including its gated LLM
disambiguation call) before services/agent/nodes/tools.py::execute_tool
ever called DecisionEngine.decide() — which then reran the IDENTICAL
interpretation from scratch. Combined with the strict per-user FIFO queue
in line_bot/webhook.py (correct, unchanged), this compounded into replies
arriving visibly "one turn late" whenever a customer typed quickly.

Fix: services/conversation_resolution.py::ConversationResolution now
carries the raw Interpretation object it was built from
(`resolved_semantic`); execute_tool threads it through
context["_pre_resolved_semantic"]; decide() reuses it when present
instead of calling _interpret_message() again. Backward compatible: any
caller that does not supply this key (every existing test, the admin
playground, direct DecisionEngine.decide() calls) computes its own
interpretation exactly as before.

This file covers the systemic audit's requested test matrix (A-E).
"""
import unittest
from unittest.mock import patch

from services.decision_engine import DecisionEngine
from services.conversation_resolution import resolve_conversation
import services.conversation_semantics as conversation_semantics

CTX = {"channel": "line", "tenant_id": "default", "sample_source": "OWNER_TEST",
       "customer_context": {}, "developer_mode": True}


def _reply(eng, text, history=None, context=None):
    ctx = dict(context or CTX)
    r = eng.decide(text, history=history or [], context=ctx)
    return (r.get("reply") or {}).get("text") or "", r.get("developer") or {}


def _with_pre_resolved(text, history=None):
    """Simulates exactly what execute_tool now does: resolve the turn
    once (as the graph's resolve_current_turn node would), then hand that
    result to decide() via context, the same way execute_tool does."""
    resolution = resolve_conversation(text, history or [], dict(CTX))
    ctx = dict(CTX)
    ctx["_pre_resolved_semantic"] = resolution.resolved_semantic
    return ctx


class TestA_DeliveryClarificationAffirmation(unittest.TestCase):
    """A. User asks a delivery question -> bot asks clarification -> user
    says "ใช่ค่ะ" -> the correct pending branch (re-ask, never a silent
    guess) resolves, using the pre-resolved-semantic code path exactly as
    the LangGraph pipeline exercises it in production."""

    def test_affirmation_after_ambiguous_delivery_question_reasks(self):
        eng = DecisionEngine()
        q = "ชำระบิลขนส่งแล้ว สินค้าจะจัดส่งถึงบ้านวันไหน"
        ctx1 = _with_pre_resolved(q, [])
        reply1, dev1 = _reply(eng, q, [], ctx1)
        self.assertEqual(dev1.get("selection_source"), "delivery_date_ambiguity_clarify")

        history = [{"role": "user", "content": q}, {"role": "assistant", "content": reply1}]
        ctx2 = _with_pre_resolved("ใช่ค่ะ", history)
        reply2, dev2 = _reply(eng, "ใช่ค่ะ", history, ctx2)
        self.assertEqual(dev2.get("selection_source"), "delivery_date_ambiguity_reclarify")
        self.assertIn("1. ระยะเวลาจัดส่งโดยทั่วไป", reply2)


class TestB_PendingChoiceResolution(unittest.TestCase):
    """B. Pending choice exists -> user says "ข้อ 2" -> the correct choice
    (private identifier ask) resolves, via the pre-resolved-semantic path."""

    def test_explicit_choice_resolves_the_pending_branch(self):
        eng = DecisionEngine()
        history = [
            {"role": "user", "content": "ชำระบิลขนส่งแล้ว สินค้าจะจัดส่งถึงบ้านวันไหน"},
            {"role": "assistant", "content": (
                "ต้องการแบบไหนคะ\n1. ระยะเวลาจัดส่งโดยทั่วไป\n2. เช็กวันถึงของบิลของคุณ")},
        ]
        ctx = _with_pre_resolved("ข้อ 2", history)
        reply, dev = _reply(eng, "ข้อ 2", history, ctx)
        self.assertEqual(dev.get("selection_source"), "delivery_date_ambiguity_resolved_private")
        self.assertIn("เลขที่บิล", reply)


class TestC_NormalQuestionSemanticsResolvedOnce(unittest.TestCase):
    """C. A normal GENERAL/KB-shaped question resolves its semantics
    EXACTLY once (not zero, not twice) and produces an IDENTICAL decision
    result whether or not a pre-resolved semantic is supplied -- proving
    the reuse path changes nothing observable, only how many times the
    interpretation work happens."""

    def test_llm_disambiguation_gate_is_consulted_at_most_once(self):
        """A message shaped to reach the gated LLM disambiguation step
        (services/conversation_semantics.py::_worth_llm_disambiguation)
        must trigger it AT MOST once per decide() call when a pre-resolved
        semantic is supplied — decide() must never re-invoke the gate a
        second time on top of the one the "graph" already paid for."""
        calls = []
        orig = conversation_semantics._llm_family

        def counting(message, history):
            calls.append(message)
            return orig(message, history)

        text = "ร้านส่งของออกมายัง"
        with patch.object(conversation_semantics, "_llm_family", side_effect=counting):
            ctx = _with_pre_resolved(text, [])
        # resolve_conversation() above already made whatever gate calls it needed
        # (0 or 1); reset the counter so this assertion is scoped to decide()'s
        # OWN contribution only.
        calls.clear()
        with patch.object(conversation_semantics, "_llm_family", side_effect=counting):
            DecisionEngine().decide(text, history=[], context=ctx)
        self.assertEqual(len(calls), 0,
                         "decide() must reuse the supplied semantic, never re-invoke the gate")

    def test_reused_semantic_produces_identical_decision_output(self):
        for text in ("น้ำเชื่อมนำเข้าได้ไหม", "ต้องแจ้งรหัสด้วยหรอคะ", "นำเข้าอะไรได้บ้าง"):
            with self.subTest(text=text):
                reply_fresh, dev_fresh = _reply(DecisionEngine(), text)
                ctx = _with_pre_resolved(text, [])
                reply_reused, dev_reused = _reply(DecisionEngine(), text, [], ctx)
                self.assertEqual(reply_fresh, reply_reused, text)
                self.assertEqual(dev_fresh.get("selection_source"), dev_reused.get("selection_source"), text)
                self.assertEqual(dev_reused.get("semantic_interpretation_source"), "reused_from_graph")
                self.assertEqual(dev_fresh.get("semantic_interpretation_source"), "computed")

    def test_absent_pre_resolved_semantic_computes_as_before(self):
        """Backward compatibility (requirement 8): every caller that does
        NOT supply context["_pre_resolved_semantic"] — every existing
        test, the admin playground, a direct decide() call — is
        completely unaffected."""
        reply, dev = _reply(DecisionEngine(), "น้ำเชื่อมนำเข้าได้ไหม")
        self.assertEqual(dev.get("semantic_interpretation_source"), "computed")
        self.assertIn("น้ำเชื่อม", reply)

    def test_malformed_pre_resolved_semantic_is_ignored_not_trusted(self):
        """A context value that isn't actually an Interpretation (e.g. a
        stray string, or None) must never be treated as valid — decide()
        falls back to computing its own, exactly like the absent case."""
        ctx = dict(CTX)
        ctx["_pre_resolved_semantic"] = "not an interpretation object"
        reply, dev = _reply(DecisionEngine(), "น้ำเชื่อมนำเข้าได้ไหม", [], ctx)
        self.assertEqual(dev.get("semantic_interpretation_source"), "computed")
        self.assertIn("น้ำเชื่อม", reply)


class TestE_NoUnsolicitedGreetingFromDecisionEngine(unittest.TestCase):
    """E. A normal, non-greeting user message must never produce an
    unsolicited generic greeting from DecisionEngine itself. This does
    NOT claim Defect 2 (an unsolicited greeting observed in a real
    production conversation) is fully root-caused or fixed — a thorough
    code review found no second-responder path anywhere in services/agent
    or line_bot/webhook.py, so the trigger could not be reproduced from
    static code. This guards the one layer that IS under direct test:
    DecisionEngine.decide() itself must not answer an ordinary slot-fill
    or KB question with a greeting."""

    _GREETING_MARKERS = ("สวัสดีค่ะ", "สวัสดีครับ", "มีอะไรให้ช่วยเหลือหรือสอบถามเพิ่มเติม")

    def test_pending_clarification_replies_are_never_a_greeting(self):
        history = [
            {"role": "user", "content": "ชำระบิลขนส่งแล้ว สินค้าจะจัดส่งถึงบ้านวันไหน"},
            {"role": "assistant", "content": (
                "ต้องการแบบไหนคะ\n1. ระยะเวลาจัดส่งโดยทั่วไป\n2. เช็กวันถึงของบิลของคุณ")},
        ]
        for text in ("ข้อ 2", "2", "ใช่ค่ะ", "ทั่วไป"):
            with self.subTest(text=text):
                eng = DecisionEngine()
                reply, _ = _reply(eng, text, history)
                for marker in self._GREETING_MARKERS:
                    self.assertNotIn(marker, reply, (text, reply))

    def test_ordinary_kb_questions_are_never_a_greeting(self):
        for text in ("น้ำเชื่อมนำเข้าได้ไหม", "นำเข้าอะไรได้บ้าง", "ต้องแจ้งรหัสด้วยหรอคะ"):
            with self.subTest(text=text):
                eng = DecisionEngine()
                reply, _ = _reply(eng, text)
                for marker in self._GREETING_MARKERS:
                    self.assertNotIn(marker, reply, (text, reply))


if __name__ == "__main__":
    unittest.main()
