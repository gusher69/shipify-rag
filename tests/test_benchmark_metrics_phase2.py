"""Regression tests for AI Evaluation Phase 2 metrics
(services/benchmark_metrics.py) — Query Understanding, Critical Facts,
Grounding, and Conversation Scenario aggregation. Pure functions, no DB,
no LLM.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import benchmark_metrics as metrics


class TestFieldMatch(unittest.TestCase):
    def test_no_expectation_is_vacuous_pass(self):
        r = metrics.field_match(None, "warehouse")
        self.assertTrue(r["pass"])

    def test_normalized_exact_match(self):
        r = metrics.field_match("Warehouse", " warehouse ")
        self.assertTrue(r["pass"])

    def test_mismatch_fails(self):
        r = metrics.field_match("warehouse", "payment")
        self.assertFalse(r["pass"])


class TestEntityAccuracy(unittest.TestCase):
    def test_exact_match(self):
        r = metrics.entity_accuracy({"location": "จีน"}, {"location": "จีน"})
        self.assertTrue(r["exact_match"])
        self.assertEqual(r["partial_match"], 1.0)

    def test_partial_match_fraction(self):
        r = metrics.entity_accuracy({"location": "จีน", "transport": "รถ"}, {"location": "จีน", "transport": "เรือ"})
        self.assertFalse(r["exact_match"])
        self.assertEqual(r["partial_match"], 0.5)
        self.assertEqual(r["mismatched_keys"], ["transport"])

    def test_no_expectation_is_vacuous_pass(self):
        r = metrics.entity_accuracy({}, {"location": "จีน"})
        self.assertTrue(r["exact_match"])


class TestExcludedEntityAccuracy(unittest.TestCase):
    def test_set_comparison_ignores_order(self):
        r = metrics.excluded_entity_accuracy({"location": ["จีน", "ไทย"]}, {"location": ["ไทย", "จีน"]})
        self.assertTrue(r["exact_match"])

    def test_mismatch_detected(self):
        r = metrics.excluded_entity_accuracy({"location": ["จีน"]}, {"location": []})
        self.assertFalse(r["exact_match"])


class TestCriticalFacts(unittest.TestCase):
    def test_correct_value_and_unit(self):
        facts = [{"key": "fee", "value": 500, "unit": "THB", "required": True}]
        r = metrics.evaluate_critical_facts(facts, "ค่าธรรมเนียมขั้นต่ำ 500 บาท")
        self.assertTrue(r["pass"])
        self.assertEqual(r["facts"][0]["status"], "correct")

    def test_missing_value_fails(self):
        facts = [{"key": "fee", "value": 500, "unit": "THB", "required": True}]
        r = metrics.evaluate_critical_facts(facts, "ไม่มีข้อมูลค่าธรรมเนียม")
        self.assertFalse(r["pass"])
        self.assertEqual(r["facts"][0]["status"], "missing")

    def test_wrong_value_detected(self):
        facts = [{"key": "fee", "value": 500, "unit": "THB", "required": True}]
        r = metrics.evaluate_critical_facts(facts, "ค่าธรรมเนียม 300 บาท")
        self.assertFalse(r["pass"])
        self.assertEqual(r["facts"][0]["status"], "wrong_value")

    def test_percent_unit_alias_accepted(self):
        facts = [{"key": "rate", "value": 3, "unit": "%", "required": True}]
        r = metrics.evaluate_critical_facts(facts, "อัตราค่าธรรมเนียมร้อยละ 3")
        self.assertTrue(r["pass"])

    def test_optional_fact_missing_does_not_fail_case(self):
        facts = [{"key": "note", "value": 1, "unit": "days", "required": False}]
        r = metrics.evaluate_critical_facts(facts, "no matching content at all")
        self.assertTrue(r["pass"])

    def test_baht_symbol_alias_accepted(self):
        facts = [{"key": "fee", "value": 500, "unit": "THB", "required": True}]
        r = metrics.evaluate_critical_facts(facts, "ค่าธรรมเนียม ฿500")
        self.assertTrue(r["pass"])


class TestGrounding(unittest.TestCase):
    def test_no_answer_is_not_applicable(self):
        r = metrics.evaluate_grounding(answer_text=None, citations=[], retrieved_chunks=[])
        self.assertEqual(r["status"], "not_applicable")

    def test_supported_when_citation_retrieved_and_facts_match(self):
        chunks = [{"file_name": "a.md", "text": "ค่าธรรมเนียม 500 บาท"}]
        citations = [{"file_name": "a.md"}]
        facts = [{"key": "fee", "value": 500, "unit": "THB"}]
        r = metrics.evaluate_grounding(answer_text="ค่าธรรมเนียม 500 บาท", citations=citations,
                                        retrieved_chunks=chunks, critical_facts=facts)
        self.assertEqual(r["status"], "supported")

    def test_unsupported_when_no_citation(self):
        r = metrics.evaluate_grounding(answer_text="some answer", citations=[], retrieved_chunks=[{"file_name": "a.md"}])
        self.assertEqual(r["status"], "unsupported")

    def test_contradicted_when_prohibited_source_cited(self):
        chunks = [{"file_name": "china.md", "text": "x"}]
        citations = [{"file_name": "china.md"}]
        r = metrics.evaluate_grounding(answer_text="answer", citations=citations, retrieved_chunks=chunks,
                                        prohibited_files=["china.md"])
        self.assertEqual(r["status"], "contradicted")

    def test_citation_not_actually_retrieved_is_unsupported(self):
        r = metrics.evaluate_grounding(answer_text="answer", citations=[{"file_name": "phantom.md"}],
                                        retrieved_chunks=[{"file_name": "a.md"}])
        self.assertFalse(r["citation_sources_retrieved"])
        self.assertEqual(r["status"], "unsupported")

    def test_partially_supported_when_some_facts_unsupported(self):
        chunks = [{"file_name": "a.md", "text": "ค่าธรรมเนียม 500 บาท"}]
        citations = [{"file_name": "a.md"}]
        facts = [{"key": "fee", "value": 500, "unit": "THB"}, {"key": "rate", "value": 3, "unit": "%"}]
        r = metrics.evaluate_grounding(answer_text="ค่าธรรมเนียม 500 บาท", citations=citations,
                                        retrieved_chunks=chunks, critical_facts=facts)
        self.assertEqual(r["status"], "partially_supported")
        self.assertIn("rate", r["unsupported_critical_facts"])

    def test_correct_abstention_for_no_information_case_is_supported_not_unsupported(self):
        """Grounding Failure Audit fix: a case that EXPECTS no_information
        and correctly abstains must never be marked unsupported merely
        for having nothing to cite."""
        r = metrics.evaluate_grounding(
            answer_text="ขออภัยค่ะ ทีมงานจะติดต่อกลับเพื่อช่วยเหลือเพิ่มเติมนะคะ",
            citations=[], retrieved_chunks=[{"file_name": "a.xlsx"}],
            expected_answerability="no_information",
        )
        self.assertEqual(r["status"], "supported")

    def test_correct_abstention_english_phrasing_also_supported(self):
        r = metrics.evaluate_grounding(
            answer_text="I'm sorry, but there is no information available about that in the provided context.",
            citations=[], retrieved_chunks=[{"file_name": "a.xlsx"}],
            expected_answerability="no_information",
        )
        self.assertEqual(r["status"], "supported")

    def test_no_information_expected_but_answer_is_not_an_abstention_still_scored_normally(self):
        """The fix must not blanket-exempt every no_information case —
        only ones where the answer itself is a genuine abstention."""
        r = metrics.evaluate_grounding(
            answer_text="The China warehouse is in Guangzhou.",
            citations=[], retrieved_chunks=[{"file_name": "a.xlsx"}],
            expected_answerability="no_information",
        )
        self.assertEqual(r["status"], "unsupported")

    def test_answerable_case_with_no_citation_still_unsupported(self):
        """Fix must not affect answerable cases — only ones expecting
        no_information."""
        r = metrics.evaluate_grounding(
            answer_text="The fee is 500 THB.", citations=[], retrieved_chunks=[{"file_name": "a.xlsx"}],
            expected_answerability="direct_answer",
        )
        self.assertEqual(r["status"], "unsupported")


class TestConversationScenarioMetrics(unittest.TestCase):
    def test_all_turns_pass_gives_full_scenario_pass(self):
        turns = [{"turn_pass": True, "expected_transition": "same_topic", "topic_ok": True} for _ in range(3)]
        m = metrics.conversation_scenario_metrics(turns)
        self.assertEqual(m["scenario_pass"], True)
        self.assertEqual(m["turn_pass_rate"], 1.0)

    def test_one_failed_turn_fails_whole_scenario(self):
        turns = [{"turn_pass": True}, {"turn_pass": False}, {"turn_pass": True}]
        m = metrics.conversation_scenario_metrics(turns)
        self.assertFalse(m["scenario_pass"])
        self.assertAlmostEqual(m["turn_pass_rate"], 2 / 3, places=3)

    def test_topic_switch_accuracy(self):
        turns = [{"turn_pass": True, "expected_transition": "switch_topic", "topic_ok": True}]
        m = metrics.conversation_scenario_metrics(turns)
        self.assertEqual(m["topic_switch_accuracy"], 1.0)

    def test_empty_scenario_is_vacuous_pass(self):
        m = metrics.conversation_scenario_metrics([])
        self.assertTrue(m["scenario_pass"])


class TestQueryUnderstandingScore(unittest.TestCase):
    def test_all_pass_scores_one(self):
        qu = {"topic": {"pass": True}, "intent": {"pass": True}, "entities": {"exact_match": True}}
        self.assertEqual(metrics.query_understanding_score(qu), 1.0)

    def test_partial_pass(self):
        qu = {"topic": {"pass": True}, "intent": {"pass": False}}
        self.assertEqual(metrics.query_understanding_score(qu), 0.5)


if __name__ == "__main__":
    unittest.main()
