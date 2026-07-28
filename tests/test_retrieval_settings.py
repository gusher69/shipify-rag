import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.retrieval_settings import RetrievalSettings, get_preset, PRESETS, DEFAULT_PRESET


class TestPresets(unittest.TestCase):
    def test_default_preset_is_balanced(self):
        self.assertEqual(DEFAULT_PRESET, "balanced")
        self.assertEqual(get_preset("balanced").candidate_top_k, 12)
        self.assertEqual(get_preset("balanced").final_context_top_k, 5)

    def test_strict_preset_has_higher_thresholds(self):
        strict = get_preset("strict")
        balanced = get_preset("balanced")
        self.assertGreater(strict.absolute_minimum_score, balanced.absolute_minimum_score)
        self.assertGreater(strict.relative_to_best_ratio, balanced.relative_to_best_ratio)
        self.assertLess(strict.candidate_top_k, balanced.candidate_top_k)

    def test_high_recall_preset_has_lower_thresholds(self):
        high_recall = get_preset("high_recall")
        balanced = get_preset("balanced")
        self.assertLess(high_recall.absolute_minimum_score, balanced.absolute_minimum_score)
        self.assertGreater(high_recall.candidate_top_k, balanced.candidate_top_k)

    def test_unknown_preset_falls_back_to_balanced(self):
        self.assertEqual(get_preset("nonexistent").preset, "balanced")


class TestValidation(unittest.TestCase):
    def test_default_settings_have_no_validation_errors(self):
        self.assertEqual(RetrievalSettings().validation_errors(), [])

    def test_weights_must_sum_to_one(self):
        bad = RetrievalSettings(semantic_weight=0.9, keyword_weight=0.5, heading_weight=0.2, graph_weight=0.1)
        errors = bad.validation_errors()
        self.assertTrue(any("must equal" in e for e in errors))

    def test_invalid_search_strategy_rejected(self):
        bad = RetrievalSettings(search_strategy="nonsense")
        self.assertTrue(any("search_strategy" in e for e in bad.validation_errors()))

    def test_invalid_reranker_provider_rejected(self):
        bad = RetrievalSettings(reranker_provider="nonsense")
        self.assertTrue(any("reranker_provider" in e for e in bad.validation_errors()))

    def test_candidate_top_k_must_be_at_least_final_context_top_k(self):
        bad = RetrievalSettings(candidate_top_k=3, final_context_top_k=5)
        self.assertTrue(any("candidate_top_k" in e for e in bad.validation_errors()))


class TestEffectiveWeights(unittest.TestCase):
    def test_graph_disabled_redistributes_weight(self):
        settings = RetrievalSettings(graph_search_enabled=False)
        weights = settings.effective_weights()
        self.assertEqual(weights["graph"], 0.0)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)

    def test_graph_enabled_keeps_configured_weights(self):
        settings = RetrievalSettings(graph_search_enabled=True)
        weights = settings.effective_weights()
        self.assertAlmostEqual(weights["graph"], 0.10, places=6)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
