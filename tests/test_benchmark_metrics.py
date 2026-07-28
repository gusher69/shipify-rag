import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import benchmark_metrics as m


class TestRetrievalMetrics(unittest.TestCase):
    def test_recall_at_k_hit_and_miss(self):
        files = ["a.md", "b.md", "c.md"]
        self.assertTrue(m.recall_at_k(files, "b.md", 3))
        self.assertFalse(m.recall_at_k(files, "b.md", 1))
        self.assertTrue(m.recall_at_k(files, "b.md", 2))

    def test_recall_at_k_no_expected_file_is_vacuous_pass(self):
        self.assertTrue(m.recall_at_k(["a.md"], None, 1))

    def test_reciprocal_rank(self):
        self.assertEqual(m.reciprocal_rank(["a", "b", "c"], "a"), 1.0)
        self.assertEqual(m.reciprocal_rank(["a", "b", "c"], "b"), 0.5)
        self.assertEqual(m.reciprocal_rank(["a", "b", "c"], "z"), 0.0)

    def test_precision_at_k(self):
        self.assertAlmostEqual(m.precision_at_k(["a", "b"], "a", 2), 0.5)
        self.assertAlmostEqual(m.precision_at_k(["a", "b"], "z", 2), 0.0)

    def test_expected_section_hit(self):
        sections = ["Our Mission", "Contact Us"]
        self.assertTrue(m.expected_section_hit(sections, "Our Mission", 2))
        self.assertFalse(m.expected_section_hit(sections, "Our Services", 2))
        self.assertTrue(m.expected_section_hit(sections, None, 2))

    def test_prohibited_file_hit(self):
        hits = m.prohibited_file_hit(["a.xlsx", "b.md"], ["a.xlsx"], 2)
        self.assertEqual(hits, ["a.xlsx"])
        self.assertEqual(m.prohibited_file_hit(["c.md"], ["a.xlsx"], 2), [])


class TestAnswerMetrics(unittest.TestCase):
    def test_must_include_pass_and_fail(self):
        r = m.must_include_check("Shipify offers warehouse and customs clearance.", ["warehouse", "customs"])
        self.assertTrue(r["pass"])
        r2 = m.must_include_check("Shipify offers warehouse services.", ["warehouse", "delivery"])
        self.assertFalse(r2["pass"])
        self.assertEqual(r2["missing"], ["delivery"])

    def test_must_not_include(self):
        r = m.must_not_include_check("We do not offer refunds.", ["guaranteed refund"])
        self.assertTrue(r["pass"])
        r2 = m.must_not_include_check("We offer a guaranteed refund.", ["guaranteed refund"])
        self.assertFalse(r2["pass"])

    def test_detect_answer_language(self):
        self.assertEqual(m.detect_answer_language("This is a test in English."), "English")
        self.assertEqual(m.detect_answer_language("นี่คือคำตอบภาษาไทยทั้งหมด"), "Thai")
        self.assertEqual(m.detect_answer_language("Please contact Shipify support ที่ support@shipify-example.com"),
                         "Mixed Thai-English")

    def test_language_correct(self):
        self.assertTrue(m.language_correct("นี่คือคำตอบภาษาไทย", "Thai"))
        self.assertFalse(m.language_correct("This is English", "Thai"))
        self.assertTrue(m.language_correct("anything", None))

    def test_citation_correct(self):
        self.assertTrue(m.citation_correct(["company-profile-test.md"], "company-profile-test.md"))
        self.assertFalse(m.citation_correct(["graph-test.md"], "company-profile-test.md"))

    def test_answerability_correct(self):
        self.assertTrue(m.answerability_correct("direct_answer", "direct_answer"))
        self.assertFalse(m.answerability_correct("partial_answer", "direct_answer"))
        self.assertTrue(m.answerability_correct("anything", None))

    def test_p95(self):
        self.assertEqual(m.p95([]), 0.0)
        self.assertEqual(m.p95([100]), 100)
        vals = list(range(1, 101))  # 1..100
        self.assertGreaterEqual(m.p95(vals), 95)


class TestFailureClassification(unittest.TestCase):
    def test_system_error_wins_over_everything(self):
        ft = m.classify_failure(retrieval_r1=True, retrieval_r5=True, must_include_ok=True,
                                 must_not_include_ok=True, citation_ok=True, language_ok=True,
                                 answerability_ok=True, system_error=True)
        self.assertEqual(ft, "system_error")

    def test_retrieval_miss_before_ranking_failure(self):
        ft = m.classify_failure(retrieval_r1=False, retrieval_r5=False, must_include_ok=True,
                                 must_not_include_ok=True, citation_ok=True, language_ok=True,
                                 answerability_ok=True, system_error=False)
        self.assertEqual(ft, "retrieval_miss")

    def test_ranking_failure_when_r5_hit_but_not_r1(self):
        ft = m.classify_failure(retrieval_r1=False, retrieval_r5=True, must_include_ok=True,
                                 must_not_include_ok=True, citation_ok=True, language_ok=True,
                                 answerability_ok=True, system_error=False)
        self.assertEqual(ft, "ranking_failure")

    def test_no_failure_when_everything_ok(self):
        ft = m.classify_failure(retrieval_r1=True, retrieval_r5=True, must_include_ok=True,
                                 must_not_include_ok=True, citation_ok=True, language_ok=True,
                                 answerability_ok=True, system_error=False)
        self.assertIsNone(ft)

    def test_unsupported_claim_before_wrong_citation(self):
        ft = m.classify_failure(retrieval_r1=True, retrieval_r5=True, must_include_ok=True,
                                 must_not_include_ok=False, citation_ok=False, language_ok=True,
                                 answerability_ok=True, system_error=False)
        self.assertEqual(ft, "unsupported_claim")


if __name__ == "__main__":
    unittest.main()
