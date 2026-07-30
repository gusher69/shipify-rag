import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.conversation_analytics_store import (
    InMemoryConversationAnalyticsStore, get_analytics_store, reset_analytics_store_for_tests,
)


class TestInMemoryConversationAnalyticsStore(unittest.TestCase):
    def test_record_and_get_conversation_merges_across_turns(self):
        store = InMemoryConversationAnalyticsStore()
        store.record_turn({"conversation_id": "c1", "action_id": "a1",
                            "questions_asked": ["BillStatus"], "auto_filled_fields": []})
        store.record_turn({"conversation_id": "c1", "action_id": "a1",
                            "questions_asked": ["Latest"], "auto_filled_fields": ["Latest"],
                            "execution_result": "completed"})
        row = store.get_conversation("c1")
        self.assertEqual(row["total_turns"], 2)
        self.assertEqual(set(row["questions_asked"]), {"BillStatus", "Latest"})
        self.assertEqual(row["auto_filled_fields"], ["Latest"])
        self.assertEqual(row["execution_result"], "completed")
        self.assertIsNotNone(row["completed_at"])

    def test_get_conversation_unknown_returns_none(self):
        store = InMemoryConversationAnalyticsStore()
        self.assertIsNone(store.get_conversation("nope"))

    def test_list_conversations_filters_by_action(self):
        store = InMemoryConversationAnalyticsStore()
        store.record_turn({"conversation_id": "c1", "action_id": "a1"})
        store.record_turn({"conversation_id": "c2", "action_id": "a2"})
        rows = store.list_conversations(action_id="a1")
        self.assertEqual([r["conversation_id"] for r in rows], ["c1"])

    def test_compute_metrics_empty_store_never_crashes(self):
        store = InMemoryConversationAnalyticsStore()
        metrics = store.compute_metrics()
        self.assertEqual(metrics["conversation_count"], 0)
        self.assertIn("note", metrics)

    def test_compute_metrics_basic_shape(self):
        store = InMemoryConversationAnalyticsStore()
        store.record_turn({"conversation_id": "c1", "action_id": "a1",
                            "questions_asked": ["BillStatus"], "questions_skipped": ["Latest"],
                            "auto_filled_fields": ["Latest"], "detected_fields": ["Latest"],
                            "confirmation_result": True, "execution_result": "completed"})
        store.record_turn({"conversation_id": "c2", "action_id": "a1",
                            "questions_asked": ["BillStatus"], "execution_result": "cancelled"})
        metrics = store.compute_metrics(action_id="a1")
        self.assertEqual(metrics["conversation_count"], 2)
        self.assertEqual(metrics["cancellation_rate"], 0.5)
        self.assertEqual(metrics["confirmation_rate"], 1.0)
        self.assertIsNone(metrics["detection_accuracy"])
        self.assertTrue(metrics["most_asked_questions"])

    def test_factory_returns_singleton_and_reset_clears_it(self):
        reset_analytics_store_for_tests()
        s1 = get_analytics_store()
        s1.record_turn({"conversation_id": "cX", "action_id": "aX"})
        s2 = get_analytics_store()
        self.assertIs(s1, s2)
        self.assertIsNotNone(s2.get_conversation("cX"))
        reset_analytics_store_for_tests()
        self.assertIsNone(get_analytics_store().get_conversation("cX"))


if __name__ == "__main__":
    unittest.main()
