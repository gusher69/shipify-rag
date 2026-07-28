"""Regression tests from the "Synonym Engine doesn't work for
ขอโลเคชั่นโกดัง" audit.

Audit finding: the CODE was already correct — the real running server
process had simply never been restarted since rag/synonym_service.py,
rag/searcher.py, rag/faq_matcher.py, and rag/hybrid_scoring.py were
written (confirmed by comparing `Get-Process` start times against file
mtimes; the fix was operational — restart the server — not a code
change). These tests lock in the actual, already-correct behavior so a
future regression is caught by the test suite instead of relying on
manually noticing a stale process again.

Uses two FAQ rows shaped exactly like the real knowledge_items rows
found in the DB during the audit (Thai warehouse vs. China warehouse),
so a query about one must never match the other.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.faq_matcher import find_best_faq_match
from rag.hybrid_scoring import compute_keyword_score
from rag.synonym_service import expand_query_with_synonyms

THAI_WAREHOUSE_ROW = {
    "id": "thai-warehouse",
    "question": "ขอที่อยู่โกดังหน่อย",
    "alt_questions": ["โกดังไทยอยู่ที่ไหน", "ขอพิกัดโกดังไทย", "ขอโลเคชั่นรับสินค้า", "รับสินค้าเองได้ที่ไหน"],
}
CHINA_WAREHOUSE_ROW = {
    "id": "china-warehouse",
    "question": "ขอที่อยู่โกดังจีน",
    "alt_questions": ["โกดังจีนอยู่ที่ไหน", "ขอที่อยู่ส่งของที่จีน", "ต้องส่งสินค้าไปโกดังไหน", "ขอพิกัดโกดังจีน"],
}
ALL_ROWS = [THAI_WAREHOUSE_ROW, CHINA_WAREHOUSE_ROW]

# Stand-in for the actual knowledge_chunk text ingested alongside this FAQ
# row (contains the real map links and Thai-warehouse-specific wording) —
# used to prove the ordinary hybrid retrieval fallback path (not just the
# FAQ exact-match short-circuit) also finds the right evidence.
THAI_WAREHOUSE_CHUNK = {
    "text": ("มีโกดังไทย 2 ที่นะคะ\nพิกัดจุดรับสินค้าใหม่ อ่อนนุช 46\n"
              "https://maps.app.goo.gl/yRz6MpZot1337b3w6\n"
              "จุดรับสินค้า Location : https://maps.app.goo.gl/bkqFVxT2uTcUcjJh8"),
    "heading_path": [], "section_title": "ขอที่อยู่โกดังหน่อย", "file_name": "RAG_Knowledge",
}


def _best_faq_match(question):
    variants = expand_query_with_synonyms(question)["variants"]
    best = None
    for v in variants:
        result = find_best_faq_match(v, ALL_ROWS)
        if not result:
            continue
        if result["match_type"] == "exact":
            return result
        if best is None or result["score"] > best["score"]:
            best = result
    return best


class TestWarehouseLocationRegressionScenarios(unittest.TestCase):
    def test_1_location_warehouse_query_retrieves_thai_warehouse_row(self):
        match = _best_faq_match("ขอโลเคชั่นโกดัง")
        self.assertIsNotNone(match)
        self.assertEqual(match["row"]["id"], "thai-warehouse")

    def test_2_coordinates_thai_warehouse_query_retrieves_same_row(self):
        match = _best_faq_match("ขอพิกัดโกดังไทย")
        self.assertIsNotNone(match)
        self.assertEqual(match["row"]["id"], "thai-warehouse")
        self.assertEqual(match["match_type"], "exact")

    def test_3_map_question_recovers_via_hybrid_fallback_evidence(self):
        """This phrasing ("มีแผนที่โกดังไหม") doesn't clear the FAQ exact-
        match near-exact threshold (structurally different sentence), so
        the FAQ short-circuit correctly does NOT fire here — but the
        ordinary hybrid retrieval path (keyword scoring against the
        synonym-expanded variants) still finds strong evidence in the
        actual chunk text, which is what "only enrich, never replace
        existing logic" guarantees."""
        match = _best_faq_match("มีแผนที่โกดังไหม")
        self.assertIsNone(match)  # FAQ exact-match correctly abstains
        variants = expand_query_with_synonyms("มีแผนที่โกดังไหม")["variants"]
        score = compute_keyword_score("มีแผนที่โกดังไหม", THAI_WAREHOUSE_CHUNK, variants)
        self.assertEqual(score, 1.0)

    def test_4_china_warehouse_query_retrieves_china_row_not_thai(self):
        match = _best_faq_match("ขอโลเคชั่นโกดังจีน")
        self.assertIsNotNone(match)
        self.assertEqual(match["row"]["id"], "china-warehouse")

    def test_5_unrelated_location_query_never_falsely_matches_warehouse(self):
        for q in ("ขอโลเคชั่นสำนักงานใหญ่ที่กรุงเทพ", "location ของบริษัทคู่ค้าอยู่ไหน",
                  "ราคาส่งของไปเชียงใหม่เท่าไหร่"):
            with self.subTest(question=q):
                match = _best_faq_match(q)
                self.assertIsNone(match)


class TestSynonymGroupContainsExpectedTerms(unittest.TestCase):
    """Guards against the specific audit-checklist concern: the พิกัด
    group must contain every documented synonym."""

    def test_location_group_has_all_documented_synonyms(self):
        groups = {g["canonical_term"]: set(g["synonyms"]) for g in
                  __import__("rag.synonym_service", fromlist=["get_synonym_groups"]).get_synonym_groups()}
        self.assertEqual(
            groups["พิกัด"],
            {"โลเคชั่น", "location", "map", "google map", "แผนที่", "gps"},
        )


if __name__ == "__main__":
    unittest.main()
