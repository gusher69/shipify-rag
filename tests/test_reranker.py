import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.reranker import get_reranker, HeuristicReranker, NoneReranker, LLMReranker, CrossEncoderReranker


def _chunk(hybrid_score, classification="direct_evidence", **extra):
    c = {"hybrid_score": hybrid_score, "classification": classification, "keyword_score": 0.0,
         "heading_score": 0.0, "graph_score": 0.0, "matched_query": "q", "heading_path": [], "section_title": None}
    c.update(extra)
    return c


class TestGetReranker(unittest.TestCase):
    def test_default_is_heuristic(self):
        self.assertIsInstance(get_reranker(), HeuristicReranker)
        self.assertIsInstance(get_reranker("heuristic"), HeuristicReranker)

    def test_none_provider(self):
        self.assertIsInstance(get_reranker("none"), NoneReranker)

    def test_unknown_provider_falls_back_to_heuristic(self):
        self.assertIsInstance(get_reranker("nonexistent"), HeuristicReranker)


class TestHeuristicReranker(unittest.TestCase):
    def test_assigns_rerank_score_to_every_candidate(self):
        reranker = HeuristicReranker()
        candidates = [_chunk(0.5), _chunk(0.3)]
        result = reranker.rerank("q", candidates, top_n=2)
        self.assertTrue(all("rerank_score" in c for c in result))

    def test_never_promotes_a_lower_tier_above_a_higher_tier(self):
        """A supporting_evidence chunk must never outrank a
        direct_evidence chunk, even with a higher raw hybrid_score —
        reranking only reorders WITHIN a tier."""
        reranker = HeuristicReranker()
        weaker_tier_high_score = _chunk(0.9, classification="supporting_evidence")
        stronger_tier_lower_score = _chunk(0.3, classification="direct_evidence")
        result = reranker.rerank("q", [weaker_tier_high_score, stronger_tier_lower_score], top_n=2)
        self.assertEqual(result[0]["classification"], "direct_evidence")

    def test_exact_keyword_match_scores_higher_than_synonym_match(self):
        reranker = HeuristicReranker()
        exact = _chunk(0.5, keyword_score=0.8, matched_query="mission")
        synonym = _chunk(0.5, keyword_score=0.8, matched_query="mission statement")
        result = reranker.rerank("mission", [synonym, exact], top_n=2)
        self.assertEqual(result[0]["matched_query"], "mission")

    def test_top_n_truncates(self):
        reranker = HeuristicReranker()
        candidates = [_chunk(0.9 - i * 0.1) for i in range(5)]
        result = reranker.rerank("q", candidates, top_n=2)
        self.assertEqual(len(result), 2)


class TestNoneReranker(unittest.TestCase):
    def test_preserves_order_and_sets_rerank_score_from_hybrid(self):
        reranker = NoneReranker()
        candidates = [_chunk(0.3), _chunk(0.9)]
        result = reranker.rerank("q", candidates, top_n=2)
        self.assertEqual(result[0]["rerank_score"], 0.3)
        self.assertEqual(result[1]["rerank_score"], 0.9)


class TestUnimplementedProviders(unittest.TestCase):
    def test_llm_reranker_raises_clear_error(self):
        with self.assertRaises(NotImplementedError):
            LLMReranker().rerank("q", [], top_n=1)


class TestCrossEncoderReranker(unittest.TestCase):
    """Phase 3.6 (2026-08-05) — CrossEncoderReranker is now a real
    implementation (services/reranker.py), not a stub. Never hits the
    real sentence-transformers model/network in these tests — the model
    loader (_get_cross_encoder_model) is mocked so unit tests stay fast
    and deterministic; the real model was separately verified live
    (correctly scored a relevant Thai chunk 4.39 vs -9.70 for an
    irrelevant one) during the Phase 3 implementation pass."""

    def test_empty_candidates_returns_empty_without_loading_model(self):
        with patch("services.reranker._get_cross_encoder_model") as mock_loader:
            result = CrossEncoderReranker().rerank("q", [], top_n=3)
        self.assertEqual(result, [])
        mock_loader.assert_not_called()

    def test_scores_and_reranks_using_model_predictions(self):
        fake_model = MagicMock()
        fake_model.predict.return_value = [0.1, 0.9]  # second candidate scores higher
        candidates = [_chunk(0.5, classification="direct_evidence", content="a"),
                      _chunk(0.5, classification="direct_evidence", content="b")]
        with patch("services.reranker._get_cross_encoder_model", return_value=fake_model):
            result = CrossEncoderReranker().rerank("q", candidates, top_n=2)
        self.assertEqual(result[0]["content"], "b")
        self.assertEqual(result[0]["rerank_score"], 0.9)

    def test_never_promotes_a_lower_tier_above_a_higher_tier(self):
        """Same tier-preservation guarantee as HeuristicReranker — a
        higher cross-encoder score must never let a lower evidence tier
        outrank a higher one."""
        fake_model = MagicMock()
        fake_model.predict.return_value = [0.99, 0.01]  # weaker tier scores much higher
        weaker_tier_high_score = _chunk(0.5, classification="supporting_evidence", content="weak-tier")
        stronger_tier_lower_score = _chunk(0.5, classification="direct_evidence", content="strong-tier")
        with patch("services.reranker._get_cross_encoder_model", return_value=fake_model):
            result = CrossEncoderReranker().rerank("q", [weaker_tier_high_score, stronger_tier_lower_score], top_n=2)
        self.assertEqual(result[0]["classification"], "direct_evidence")

    def test_top_n_truncates(self):
        fake_model = MagicMock()
        fake_model.predict.return_value = [0.5, 0.4, 0.3]
        candidates = [_chunk(0.5, content=str(i)) for i in range(3)]
        with patch("services.reranker._get_cross_encoder_model", return_value=fake_model):
            result = CrossEncoderReranker().rerank("q", candidates, top_n=2)
        self.assertEqual(len(result), 2)

    def test_model_load_failure_raises_clear_actionable_error(self):
        with patch("services.reranker._get_cross_encoder_model", side_effect=OSError("no network")):
            with self.assertRaises(RuntimeError) as ctx:
                CrossEncoderReranker().rerank("q", [_chunk(0.5)], top_n=1)
        self.assertIn("could not load model", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
