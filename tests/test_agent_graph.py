# -*- coding: utf-8 -*-
"""LANGGRAPH AGENT — structure, authority and the owner's test cases.

Three things this suite exists to hold:

1. ARCHITECTURE. The graph orchestrates; it does not re-implement. Every
   reach into the rest of the platform goes through
   services/agent/adapters/existing_engine.py, and no node carries a
   Thai pattern, a routing table or a policy verdict of its own. Tested
   mechanically by reading the node sources, so "do not build a second
   decision engine" cannot rot into a comment.

2. AUTHORITY. The graph becomes customer-facing only when
   config.LANGGRAPH_MODE says so, and never for a turn its own safety
   gate flagged.

3. BEHAVIOUR. The owner's cases A-J (task §20), including the
   conversation-context cases that are the actual customer complaint.

Test tier is pinned offline by tests/__init__.py — no paid API call.
"""
import pathlib
import re
import unittest
from unittest.mock import patch

import config
from services.agent import runner as agent_runner
from services.agent.graph import GRAPH_NODES, build_graph, get_graph
from services.agent.state import AgentDecision, NEXT_ACTIONS, TOOL_CLASSES, new_state
from services.agent.adapters import existing_engine as adapter
from services.conversation_semantics import ASK_PRODUCT_SLOT

CTX = {"channel": "line", "tenant_id": "default", "sample_source": "TEST",
       "customer_context": {}, "developer_mode": True}


def _run(message, history=None):
    return agent_runner.run_agent(message, history=history or [], context=dict(CTX))


def _reply_of(decision):
    return decision.final_response or ""


class TestGraphStructure(unittest.TestCase):

    def test_graph_compiles_and_declares_its_nodes(self):
        self.assertIsNotNone(build_graph())
        self.assertEqual(len(GRAPH_NODES), 13)

    def test_every_declared_node_actually_runs(self):
        d = _run("20 คู่อยากสั่งของจากจีน")
        ran = [n.split("(")[0] for n in d.node_path]
        for node in GRAPH_NODES:
            self.assertIn(node, ran, f"{node} never ran")

    def test_the_graph_is_bounded_no_node_runs_twice(self):
        d = _run("20 คู่อยากสั่งของจากจีน")
        ran = [n.split("(")[0] for n in d.node_path]
        self.assertEqual(len(ran), len(set(ran)), f"a node repeated: {ran}")

    def test_planned_action_and_tool_class_stay_in_their_vocabularies(self):
        for m in ["20 คู่อยากสั่งของจากจีน", "ยกเลิกบิลสั่งซื้อได้ไหม",
                  "ของฉันส่งถึงบ้านหรือยัง", "ขอเบอร์ติดต่อ", "อยากถอนเงิน"]:
            with self.subTest(m=m):
                d = _run(m)
                self.assertIn(d.planned_action, NEXT_ACTIONS)
                if d.tool_class:
                    self.assertIn(d.tool_class, TOOL_CLASSES)

    def test_every_tool_declares_a_complete_contract(self):
        required = ("description", "tool_class", "requires_auth", "side_effect",
                    "grounding_source", "failure_behavior")
        for name, c in adapter.TOOL_CONTRACTS.items():
            with self.subTest(tool=name):
                for key in required:
                    self.assertIn(key, c, f"{name} is missing {key}")
                self.assertIn(c["tool_class"], TOOL_CLASSES)


class TestArchitectureInvariants(unittest.TestCase):
    """The "do not build a second decision engine" rule, enforced."""

    NODE_DIR = pathlib.Path("services/agent/nodes")

    def _node_sources(self):
        for p in sorted(self.NODE_DIR.glob("*.py")):
            yield p, p.read_text(encoding="utf-8")

    def test_nodes_reach_the_platform_only_through_the_adapter(self):
        allowed = re.compile(
            r"^from services\.agent(\.|\s)|^from services\.agent\.adapters import")
        bad = []
        for path, src in self._node_sources():
            for line in src.splitlines():
                line = line.strip()
                if not line.startswith(("import services", "from services")):
                    continue
                if allowed.match(line):
                    continue
                bad.append(f"{path.name}: {line}")
        self.assertEqual(bad, [], "nodes must import platform services only "
                                  "through services/agent/adapters:\n" + "\n".join(bad))

    def test_no_node_carries_its_own_thai_language_rule(self):
        """A Thai character class inside a node would mean the graph had
        started re-implementing the interpreter."""
        thai = re.compile(r"[฀-๿]")
        bad = []
        for path, src in self._node_sources():
            for i, line in enumerate(src.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or not thai.search(line):
                    continue
                if "re.compile" in line or "re.search" in line or "re.match" in line:
                    bad.append(f"{path.name}:{i}: {stripped[:70]}")
        self.assertEqual(bad, [], "a node defined its own Thai pattern:\n" + "\n".join(bad))

    def test_the_adapter_names_the_primitives_it_reuses(self):
        src = pathlib.Path("services/agent/adapters/existing_engine.py").read_text(
            encoding="utf-8")
        for module in ("conversation_semantics", "conversation_resolution",
                       "decision_engine", "operational_change_flow"):
            self.assertIn(module, src)


class TestAuthorityGates(unittest.TestCase):

    def test_shadow_mode_is_never_authoritative(self):
        with patch.object(config, "LANGGRAPH_MODE", "shadow"):
            self.assertFalse(agent_runner.graph_is_authoritative("REAL_LINE"))
            self.assertFalse(agent_runner.graph_is_authoritative("OWNER_TEST"))
            self.assertTrue(agent_runner.graph_should_run("REAL_LINE"))

    def test_owner_test_mode_is_authoritative_only_for_owner_traffic(self):
        with patch.object(config, "LANGGRAPH_MODE", "owner_test"):
            self.assertTrue(agent_runner.graph_is_authoritative("OWNER_TEST"))
            self.assertFalse(agent_runner.graph_is_authoritative("REAL_LINE"))

    def test_off_mode_does_not_run_the_graph_at_all(self):
        with patch.object(config, "LANGGRAPH_MODE", "off"):
            self.assertFalse(agent_runner.graph_should_run("REAL_LINE"))
            self.assertIsNone(agent_runner.shadow_run("สวัสดี", [], dict(CTX)))

    def test_production_mode_is_never_the_default(self):
        self.assertNotEqual(config.LANGGRAPH_MODE, "production")
        self.assertIn(config.LANGGRAPH_MODE, config.LANGGRAPH_MODES)

    def test_a_failing_graph_never_breaks_the_turn(self):
        with patch("services.agent.runner.run_agent", side_effect=RuntimeError("boom")):
            self.assertIsNone(agent_runner.shadow_run("สวัสดี", [], dict(CTX)))

    def test_shadow_run_never_executes_the_turn_twice(self):
        """Reusing the caller's already-computed result is what keeps an
        observation-only run from doubling every ERP call."""
        precomputed = {"reply": {"text": "x"}, "routing": {"type": "GENERAL"},
                       "developer": {"selection_source": "test"}}
        with patch.object(adapter, "execute",
                          side_effect=AssertionError("executed twice")) as ex:
            d = agent_runner.run_agent(
                "สวัสดี", [], {**CTX, "_precomputed_engine_result": precomputed})
            ex.assert_not_called()
        self.assertIn("execute_tool(reused)", d.node_path)


class TestOwnerCases(unittest.TestCase):
    """Task §20, A-J."""

    def test_A_quantity_only_opener_asks_for_the_product(self):
        d = _run("20 คู่อยากสั่งของจากจีน")
        q = (d.known_slots or {}).get("quantity") or {}
        self.assertEqual(q.get("value"), 20)
        self.assertEqual(q.get("unit"), "คู่")
        self.assertFalse((d.known_slots or {}).get("product"))
        self.assertEqual(d.planned_action, "ASK_PRODUCT")

    def test_B_the_answer_keeps_the_quantity_and_never_re_asks_it(self):
        """THE customer complaint: same meaning, new context, still understood."""
        first = _run("20 คู่อยากสั่งของจากจีน")
        hist = [{"role": "user", "content": "20 คู่อยากสั่งของจากจีน"},
                {"role": "assistant", "content": _reply_of(first)}]
        d = _run("รองเท้าครับ", hist)
        product = (d.known_slots or {}).get("product") or {}
        quantity = (d.known_slots or {}).get("quantity") or {}
        self.assertEqual(product.get("value"), "รองเท้า")
        self.assertEqual(quantity.get("value"), 20, "the quantity was forgotten")
        self.assertEqual(quantity.get("unit"), "คู่")
        self.assertNotEqual(d.planned_action, "ASK_QUANTITY",
                            "re-asked a quantity the customer already gave")
        self.assertNotIn("รบกวนแจ้งจำนวน", _reply_of(d))

    def test_C_an_explicit_policy_question_beats_the_running_journey(self):
        first = _run("20 คู่อยากสั่งของจากจีน")
        hist = [{"role": "user", "content": "20 คู่อยากสั่งของจากจีน"},
                {"role": "assistant", "content": _reply_of(first)}]
        second = _run("กระต่าย", hist)
        hist += [{"role": "user", "content": "กระต่าย"},
                 {"role": "assistant", "content": _reply_of(second)}]
        d = _run("กระต่ายนำเข้าได้หรอ", hist)
        self.assertNotIn(d.planned_action, ("ASK_QUANTITY", "ASK_SHIPPING_METHOD"),
                         "kept collecting slots through an explicit policy question")

    def test_D_multi_intent_opener_keeps_every_fact(self):
        d = _run("อยากสั่งรองเท้าจากจีน 30 คู่ ส่งเรือ ราคาเท่าไหร่")
        ks = d.known_slots or {}
        self.assertEqual((ks.get("product") or {}).get("value"), "รองเท้า")
        self.assertEqual((ks.get("quantity") or {}).get("value"), 30)
        self.assertEqual((ks.get("quantity") or {}).get("unit"), "คู่")
        self.assertEqual((ks.get("shipping_method") or {}).get("value"), "sea")
        self.assertEqual((d.entities or {}).get("question_kind"), "PRICE")

    def test_E_and_F_supplier_dispatch_wordings_share_one_handling(self):
        a = _run("ร้านส่งของหรือยัง")
        b = _run("ร้านจีนส่งของออกมาหรือยังครับ")
        for d in (a, b):
            self.assertTrue(d.auth_required, "supplier dispatch status must be private")
            self.assertEqual(d.auth_state, "REQUIRED_MISSING")
            self.assertEqual(d.planned_action, "ASK_IDENTIFIER")
        self.assertEqual(a.selected_tool, b.selected_tool)
        self.assertEqual(a.planned_action, b.planned_action)

    def test_G_cancellation_policy_is_public_and_asks_no_identifier(self):
        d = _run("ยกเลิกบิลสั่งซื้อได้ไหม")
        self.assertEqual(d.primary_intent, "CANCELLATION_POLICY")
        self.assertEqual(d.planned_action, "ANSWER_POLICY")
        self.assertFalse(d.auth_required)
        self.assertNotIn("ถอนเงิน", _reply_of(d))

    def test_H_cancellation_operation_prepares_and_never_claims_completion(self):
        d = _run("ช่วยยกเลิกบิล POS_TEST_001")
        self.assertEqual(d.primary_intent, "CANCELLATION_OPERATION")
        self.assertIn(d.selected_tool, ("WORKFLOW", "HUMAN_CS"))
        self.assertNotRegex(_reply_of(d), r"ยกเลิกเรียบร้อย|ยกเลิกให้แล้ว|ยกเลิกสำเร็จ")

    def test_I_withdrawal_stays_a_withdrawal(self):
        d = _run("อยากถอนเงิน")
        self.assertEqual(d.primary_intent, "PURCHASE_WITHDRAWAL")

    def test_J_topic_switch_out_of_a_journey_is_honoured(self):
        first = _run("อยากสั่งรองเท้าจากจีน")
        hist = [{"role": "user", "content": "อยากสั่งรองเท้าจากจีน"},
                {"role": "assistant", "content": _reply_of(first)}]
        d = _run("ขอเบอร์ติดต่อ", hist)
        self.assertEqual(d.primary_intent, "CONTACT_INFO")
        self.assertEqual(d.selected_tool, "CONTACT_INFO")
        self.assertIsNone(d.active_journey, "the journey should be suspended, not driving")


class TestSafetyGate(unittest.TestCase):

    def test_a_public_turn_is_never_asked_for_identity(self):
        d = _run("ส่งถึงบ้านไหม")
        self.assertFalse(d.auth_required)
        self.assertEqual([e for e in d.errors if e.startswith("AUTH_VIOLATION")], [])

    def test_a_private_turn_requires_identity_before_any_read(self):
        d = _run("ของฉันส่งถึงบ้านหรือยัง")
        self.assertTrue(d.auth_required)
        self.assertEqual(d.auth_state, "REQUIRED_MISSING")
        self.assertEqual(d.planned_action, "ASK_IDENTIFIER")

    def test_the_response_plan_can_never_ask_for_an_acknowledged_slot(self):
        first = _run("20 คู่อยากสั่งของจากจีน")
        hist = [{"role": "user", "content": "20 คู่อยากสั่งของจากจีน"},
                {"role": "assistant", "content": _reply_of(first)}]
        for msg in ("รองเท้าครับ", "เป็นชั้นวางของ"):
            with self.subTest(msg=msg):
                d = _run(msg, hist)
                self.assertEqual(
                    [e for e in d.errors if e.startswith("KNOWN_SLOT_RE_ASK")], [])

    def test_the_product_ask_and_its_detector_cannot_drift(self):
        """The renderer's own sentence must be recognised as a product ask
        — they had silently diverged, which is what made a plain
        "รองเท้าครับ" fall through to a no-information reply."""
        from services.conversation_semantics import _ASSISTANT_ASKED_PRODUCT_RE
        self.assertRegex(ASK_PRODUCT_SLOT, r"สินค้า")
        self.assertTrue(_ASSISTANT_ASKED_PRODUCT_RE.search(ASK_PRODUCT_SLOT))


if __name__ == "__main__":
    unittest.main()
