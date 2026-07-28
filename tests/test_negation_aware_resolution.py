"""Regression tests for contrastive follow-up understanding and
negation-aware retrieval (rag/query_resolution.py::resolve_conversation(),
extract_excluded_entities(), strip_negated_spans(); rag/hybrid_scoring.py::
compute_keyword_score()/apply_hybrid_ranking()).

Bug: "แล้วในประเทศไทยล่ะ มีไหมที่ไม่ใช่จีนอ่ะ" after "มีโกดังจีนไหม" treated
"จีน" as positive keyword evidence (it only appears to be EXCLUDED) and
retrieved China warehouse chunks instead of Thai ones.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.query_resolution import (
    resolve_conversation, strip_negated_spans, extract_excluded_entities, extract_entities,
)
from rag.hybrid_scoring import compute_keyword_score, apply_hybrid_ranking


def _hist(*pairs):
    history = []
    for q, a in pairs:
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": a})
    return history


class TestScenarioA(unittest.TestCase):
    """Prev "มีโกดังจีนไหม" -> Current "แล้วในประเทศไทยล่ะ มีไหมที่ไม่ใช่จีนอ่ะ"."""

    def test_resolved_location_and_exclusion(self):
        r = resolve_conversation("แล้วในประเทศไทยล่ะ มีไหมที่ไม่ใช่จีนอ่ะ",
                                  _hist(("มีโกดังจีนไหม", "มีค่ะ")))
        self.assertEqual(r["resolved_question"], "มีโกดังในประเทศไทยไหม และอยู่ที่ไหน")
        self.assertEqual(r["excluded_entities"]["location"], ["จีน"])
        self.assertEqual(r["replaced_entities"]["location"], {"from": "จีน", "to": "ไทย"})

    def test_keyword_scoring_never_credits_excluded_term(self):
        # "จีน" only appears NEGATED in the query — a chunk containing "จีน"
        # must not earn a positive keyword hit purely from that word.
        china_only_chunk = {"text": "จีน โรงงาน กวางเจา สินค้า"}
        query = "โกดังที่ไม่ใช่จีน"
        score_with_negation = compute_keyword_score(query, china_only_chunk)
        score_without_negation = compute_keyword_score("โกดังจีน", china_only_chunk)
        self.assertEqual(score_with_negation, 0.0)
        self.assertGreater(score_without_negation, 0.0)

    def test_retrieval_down_ranks_excluded_term_chunks(self):
        # Equal keyword/vector evidence for both — the ONLY difference is
        # excluded_terms=["จีน"], which must down-rank the china chunk
        # below the thai chunk despite otherwise-identical evidence.
        china_chunk = {"text": "โกดัง มีที่อยู่ จีน กวางเจา", "score": 0.9}
        thai_chunk = {"text": "โกดัง มีที่อยู่ ไทย อ่อนนุช", "score": 0.9}
        kept = apply_hybrid_ranking("โกดัง มีที่อยู่", [china_chunk, thai_chunk], excluded_terms=["จีน"])
        self.assertEqual(kept[0]["text"], thai_chunk["text"])
        self.assertTrue(kept[1]["excluded_term_penalty"])
        self.assertFalse(kept[0]["excluded_term_penalty"])

    def test_never_removes_all_candidates(self):
        china_only = [{"text": "โกดังจีน อยู่ที่กวางเจา", "score": 0.9}]
        kept = apply_hybrid_ranking("มีโกดังในประเทศไทยไหม", china_only, excluded_terms=["จีน"])
        self.assertTrue(kept)


class TestScenarioB(unittest.TestCase):
    """Prev "ขอเรททางเรือ" -> Current "แล้วทางรถล่ะ ไม่เอาเรือ"."""

    def test_transport_replaced_and_excluded(self):
        r = resolve_conversation("แล้วทางรถล่ะ ไม่เอาเรือ", _hist(("ขอเรททางเรือ", "เรทเรือคือ...")))
        self.assertEqual(r["resolved_question"], "ขอเรททางรถ")
        self.assertEqual(r["excluded_entities"]["transport"], ["เรือ"])
        self.assertEqual(r["replaced_entities"]["transport"], {"from": "เรือ", "to": "รถ"})


class TestScenarioC(unittest.TestCase):
    """Prev "ขอที่อยู่โกดังไทย" -> Current "แล้วจีนล่ะ"."""

    def test_location_replaced(self):
        r = resolve_conversation("แล้วจีนล่ะ", _hist(("ขอที่อยู่โกดังไทย", "ที่อยู่ไทยคือ...")))
        self.assertEqual(r["resolved_question"], "ขอที่อยู่โกดังจีน")
        self.assertEqual(r["replaced_entities"]["location"], {"from": "ไทย", "to": "จีน"})


class TestScenarioD(unittest.TestCase):
    """"ขอข้อมูลโกดังที่ไม่ใช่จีน" — must not return China-only answer."""

    def test_no_history_never_resolves_china_as_positive(self):
        r = resolve_conversation("ขอข้อมูลโกดังที่ไม่ใช่จีน", None)
        self.assertNotEqual(r.get("entities_carried", {}).get("location"), "จีน")
        excluded = extract_excluded_entities("ขอข้อมูลโกดังที่ไม่ใช่จีน")
        self.assertEqual(excluded["location"], ["จีน"])

    def test_positive_extraction_never_sees_negated_location(self):
        entities = extract_entities("ขอข้อมูลโกดังที่ไม่ใช่จีน")
        self.assertNotEqual(entities["location"], "จีน")


class TestScenarioE(unittest.TestCase):
    """A normal query containing "ไม่" must not be over-filtered."""

    def test_normal_mai_usage_not_stripped(self):
        excluded = extract_excluded_entities("ไม่มีข้อมูลตรงนี้เลยค่ะ")
        self.assertEqual(excluded["location"], [])
        self.assertEqual(excluded["transport"], [])

    def test_strip_negated_spans_preserves_unrelated_text(self):
        text = "ไม่มีข้อมูลตรงนี้เลยค่ะ"
        self.assertEqual(strip_negated_spans(text), text)


if __name__ == "__main__":
    unittest.main()
