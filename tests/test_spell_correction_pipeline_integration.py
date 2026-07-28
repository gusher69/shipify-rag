"""End-to-end regression tests for the full pre-retrieval query pipeline:

    Original Query -> Normalize -> Spell Correction -> Follow-up
    Resolution -> Intent Detection -> Query Rewrite -> Synonym Expansion
    -> FAQ Exact Match -> Hybrid Retrieval

Covers regression scenarios A-H from the Query Spell Correction task,
composing rag.spell_correction -> rag.synonym_service -> rag.faq_matcher
exactly as rag/searcher.py::search() does (see that module for the real
wiring — these tests exercise the same function composition directly,
without needing a live DB/embedding call).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.spell_correction import correct_query
from rag.synonym_service import expand_query_with_synonyms
from rag.faq_matcher import find_best_faq_match
from rag.hybrid_scoring import compute_keyword_score

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
BILL_PAYMENT_ROW = {
    "id": "bill-payment",
    "question": "จ่ายบิลยังไง",
    "alt_questions": ["ชำระบิลอย่างไร", "ชำระเงินยังไง"],
}
SEA_RATE_ROW = {
    "id": "sea-rate",
    "question": "ขอเรททางเรือ",
    "alt_questions": ["เรททางเรือเท่าไหร่", "ราคาทางเรือ"],
}
CBM_ROW = {
    "id": "cbm-explainer",
    "question": "CBM คืออะไร",
    "alt_questions": ["คิวคืออะไร", "วิธีคำนวณ CBM"],
}
ALL_ROWS = [THAI_WAREHOUSE_ROW, CHINA_WAREHOUSE_ROW, BILL_PAYMENT_ROW, SEA_RATE_ROW, CBM_ROW]

# Stand-in for the actual knowledge_chunk text ingested alongside the Thai
# warehouse FAQ row — used when a corrected query's phrasing is too
# different from the row's Question/Alt Questions to clear the FAQ
# exact-match threshold, proving the ordinary hybrid retrieval fallback
# (not just the FAQ short-circuit) still finds the right evidence.
THAI_WAREHOUSE_CHUNK = {
    "text": ("มีโกดังไทย 2 ที่นะคะ\nพิกัดจุดรับสินค้าใหม่ อ่อนนุช 46\n"
              "https://maps.app.goo.gl/yRz6MpZot1337b3w6\n"
              "จุดรับสินค้า Location : https://maps.app.goo.gl/bkqFVxT2uTcUcjJh8"),
    "heading_path": [], "section_title": "ขอที่อยู่โกดังหน่อย", "file_name": "RAG_Knowledge",
}


def _pipeline_faq_match(raw_question):
    """Mirrors rag/searcher.py::search()'s pre-retrieval composition:
    spell correction -> synonym expansion -> FAQ exact match (original
    checked first, extras enrich)."""
    spell_result = correct_query(raw_question)
    corrected = spell_result["corrected_query"]
    variants = expand_query_with_synonyms(corrected)["variants"]

    best = None
    for v in variants:
        result = find_best_faq_match(v, ALL_ROWS)
        if not result:
            continue
        if result["match_type"] == "exact":
            return result, spell_result
        if best is None or result["score"] > best["score"]:
            best = result
    return best, spell_result


class TestRegressionScenarios(unittest.TestCase):
    def test_A_map_typo_resolves_query_and_retrieves_warehouse_map_evidence(self):
        """This phrasing doesn't clear the FAQ exact-match near-exact
        threshold (structurally different from the row's Question/Alt
        Questions), so the FAQ short-circuit correctly abstains — but
        the corrected query still finds strong evidence in the actual
        warehouse-map chunk via ordinary hybrid keyword scoring, exactly
        as "only enrich, never replace existing logic" guarantees."""
        match, spell = _pipeline_faq_match("ส่งแผนี่โกดังให้หน่อย")
        self.assertIn("แผนที่", spell["corrected_query"])
        self.assertIsNone(match)
        variants = expand_query_with_synonyms(spell["corrected_query"])["variants"]
        score = compute_keyword_score(spell["corrected_query"], THAI_WAREHOUSE_CHUNK, variants)
        self.assertEqual(score, 1.0)

    def test_B_location_warehouse_typo_retrieves_thai_warehouse_faq(self):
        match, spell = _pipeline_faq_match("ขอโลเคชันโกดดัง")
        self.assertEqual(spell["corrected_query"], "ขอโลเคชั่นโกดัง")
        self.assertIsNotNone(match)
        self.assertEqual(match["row"]["id"], "thai-warehouse")

    def test_C_bill_typo_retrieves_bill_payment_faq(self):
        match, spell = _pipeline_faq_match("ชำระบิวอย่างไร")
        self.assertEqual(spell["corrected_query"], "ชำระบิลอย่างไร")
        self.assertIsNotNone(match)
        self.assertEqual(match["row"]["id"], "bill-payment")

    def test_D_rate_typo_retrieves_sea_rate_faq(self):
        match, spell = _pipeline_faq_match("เรดทางเรือ")
        self.assertEqual(spell["corrected_query"], "เรททางเรือ")
        self.assertIsNotNone(match)
        self.assertEqual(match["row"]["id"], "sea-rate")

    def test_E_cbm_transliteration_typo_retrieves_cbm_faq(self):
        match, spell = _pipeline_faq_match("CBเอ็มคืออะไร")
        self.assertIn("CBM", spell["corrected_query"])
        self.assertIsNotNone(match)
        self.assertEqual(match["row"]["id"], "cbm-explainer")

    def test_F_tracking_id_remains_unchanged(self):
        match, spell = _pipeline_faq_match("FT123456789 อยู่ไหน")
        self.assertIn("FT123456789", spell["corrected_query"])
        self.assertEqual(spell["corrections"], [])

    def test_G_price_remains_unchanged(self):
        match, spell = _pipeline_faq_match("ราคา 4,500 บาท")
        self.assertEqual(spell["corrected_query"], "ราคา 4,500 บาท")
        self.assertEqual(spell["corrections"], [])

    def test_H_unknown_legitimate_word_remains_unchanged(self):
        _, spell = _pipeline_faq_match("อยากทราบเวลาทำการของร้าน")
        self.assertEqual(spell["corrected_query"], "อยากทราบเวลาทำการของร้าน")
        self.assertEqual(spell["corrections"], [])

    def test_china_warehouse_typo_does_not_match_thai_warehouse(self):
        """The corrected/expanded query for a CHINA warehouse question
        must retrieve the china row specifically, never the Thai one."""
        match, _ = _pipeline_faq_match("ขอโลเคชันโกดังจีน")
        self.assertIsNotNone(match)
        self.assertEqual(match["row"]["id"], "china-warehouse")


if __name__ == "__main__":
    unittest.main()
