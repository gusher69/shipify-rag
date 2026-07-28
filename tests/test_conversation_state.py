"""Regression tests for Conversation Intelligence Phase 1 — Conversation
Memory & State Engine (rag/conversation_state.py). Reuses rag/
query_resolution.py's Conversation Resolver 2.0 and rag/
query_understanding.py's Unified Intent Classification; adds only topic
bucket + transition tracking.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.conversation_state import build_conversation_state, detect_topic_bucket


def _run_conversation(turns):
    """Feeds each turn through build_conversation_state in order, using
    each turn's own text as the next turn's history (assistant replies
    aren't needed — history only reads USER turns)."""
    history = []
    states = []
    for turn in turns:
        state = build_conversation_state(turn, history)
        states.append(state)
        history.append({"role": "user", "content": turn})
        history.append({"role": "assistant", "content": "ok"})
    return states


class TestTopicBucketDetection(unittest.TestCase):
    def test_warehouse(self):
        self.assertEqual(detect_topic_bucket("ขอโกดังจีน"), "warehouse")

    def test_promotion(self):
        self.assertEqual(detect_topic_bucket("โปรโมชั่นมีอะไร"), "promotion")

    def test_no_keyword_returns_none(self):
        self.assertIsNone(detect_topic_bucket("มีแผนที่ไหม"))


class TestScenarioWarehouseStaysOnTopic(unittest.TestCase):
    """โกดังจีน -> มีแผนที่ไหม -> ขอเบอร์ -> กี่โมงเปิด — all Warehouse."""

    def test_all_turns_remain_warehouse(self):
        states = _run_conversation(["โกดังจีน", "มีแผนที่ไหม", "ขอเบอร์", "กี่โมงเปิด"])
        for s in states:
            self.assertEqual(s["topic"], "warehouse")
        self.assertEqual(states[1]["subtopic"], "map")
        self.assertEqual(states[2]["subtopic"], "contact")


class TestScenarioTransportSwitch(unittest.TestCase):
    """เรททางเรือ -> กี่วัน -> แล้วรถล่ะ -> กี่วัน — transport correctly switches."""

    def test_transport_replacement_across_turns(self):
        states = _run_conversation(["เรททางเรือ", "กี่วัน", "แล้วรถล่ะ", "กี่วัน"])
        for s in states:
            self.assertEqual(s["topic"], "shipping")
        self.assertEqual(states[0]["transport"], "เรือ")
        self.assertEqual(states[1]["transport"], "เรือ")
        self.assertEqual(states[2]["transport"], "รถ")
        self.assertEqual(states[3]["transport"], "รถ")


class TestScenarioPaymentStaysOnTopic(unittest.TestCase):
    """วิธีจ่ายบิล -> ใช้บัตรได้ไหม -> ขั้นต่ำเท่าไหร่ — all Payment."""

    def test_all_turns_remain_payment(self):
        states = _run_conversation(["วิธีจ่ายบิล", "ใช้บัตรได้ไหม", "ขั้นต่ำเท่าไหร่"])
        for s in states:
            self.assertEqual(s["topic"], "payment")


class TestScenarioTopicSwitch(unittest.TestCase):
    """โกดังจีน -> โปรโมชั่นมีอะไร — must switch topic."""

    def test_switches_to_promotion_and_drops_location(self):
        states = _run_conversation(["โกดังจีน", "โปรโมชั่นมีอะไร"])
        self.assertEqual(states[0]["topic"], "warehouse")
        self.assertEqual(states[1]["topic"], "promotion")
        self.assertEqual(states[1]["transition"], "switch_topic")
        self.assertIsNone(states[1]["location"])


class TestScenarioLocationReplacement(unittest.TestCase):
    """โกดังไทย -> แล้วจีนล่ะ -> มีแผนที่ไหม -> ขอเบอร์ -> เปิดกี่โมง."""

    def test_location_replaced_and_topic_stays_warehouse(self):
        states = _run_conversation(["โกดังไทย", "แล้วจีนล่ะ", "มีแผนที่ไหม", "ขอเบอร์", "เปิดกี่โมง"])
        for s in states:
            self.assertEqual(s["topic"], "warehouse")
        self.assertEqual(states[0]["location"], "ไทย")
        for s in states[1:]:
            self.assertEqual(s["location"], "จีน")
        self.assertEqual(states[1]["state_changes"].get("location"), {"from": "ไทย", "to": "จีน"})


class TestConversationConfidence(unittest.TestCase):
    def test_confidence_fields_present(self):
        state = build_conversation_state("ขอโกดังจีน", [])
        conf = state["conversation_confidence"]
        for key in ("topic_confidence", "entity_confidence", "transition_confidence", "overall"):
            self.assertIn(key, conf)
            self.assertGreaterEqual(conf[key], 0.0)
            self.assertLessEqual(conf[key], 1.0)


if __name__ == "__main__":
    unittest.main()
