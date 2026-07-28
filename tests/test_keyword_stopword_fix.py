"""Regression tests for the "Keyword 100% from generic phrases" bug —
rag/hybrid_scoring.py's _STOPWORDS was missing common Thai/English
question-particle words (ได้ไหม/ไหม/มีไหม/ขอ/can), so a short FAQ-style
question could score a perfect keyword_score against an UNRELATED chunk
purely because both shared one of these generic particles.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.hybrid_scoring import compute_keyword_score, tokenize, apply_hybrid_ranking
from services.retrieval_settings import RetrievalSettings

_NO_FLOOR = RetrievalSettings(minimum_candidates=1, maximum_candidates=12)


def _chunk(text, score, file_name="unknown", **extra):
    c = {"text": text, "score": score, "heading_path": [], "section_title": None,
         "file_name": file_name, "source": file_name}
    c.update(extra)
    return c


CREDIT_CARD_CHUNK = _chunk(
    "Question: ใช้ บัตรเครดิต ได้ไหม\nAnswer: ขั้นต่ำ 500 บาท ค่าธรรมเนียม 3% ไม่สามารถออกใบกำกับภาษีได้",
    score=0.3, file_name="credit-card-faq.xlsx",
)
TAX_INVOICE_CHUNK = _chunk(
    "Question: ออก ใบกำกับ ได้ไหม\nAnswer: ออกใบกำกับภาษีได้ค่ะ กรุณาแจ้งเลขผู้เสียภาษี",
    score=0.9, file_name="tax-invoice-faq.xlsx",
)


class TestStopwordsFilterGenericParticles(unittest.TestCase):
    def test_generic_particles_are_filtered_from_tokens(self):
        for word in ("ได้ไหม", "ไหม", "มีไหม", "ขอ", "can", "is", "are", "what", "how"):
            with self.subTest(word=word):
                self.assertEqual(tokenize(word), [])

    def test_question_that_is_only_filler_scores_zero_against_anything(self):
        self.assertEqual(compute_keyword_score("มีไหม", TAX_INVOICE_CHUNK), 0.0)
        self.assertEqual(compute_keyword_score("ได้ไหม ขอ", CREDIT_CARD_CHUNK), 0.0)


class TestCreditCardQuestionNeverMatchesTaxInvoiceChunk(unittest.TestCase):
    """The exact bug scenario: "ใช้บัตรเครดิตได้ไหม" must not give
    direct-keyword evidence to an unrelated "ออกใบกำกับได้ไหม" chunk."""

    def test_keyword_score_against_unrelated_chunk_is_not_a_false_hundred_percent(self):
        question = "ใช้ บัตรเครดิต ได้ไหม"
        score = compute_keyword_score(question, TAX_INVOICE_CHUNK)
        self.assertLess(score, 1.0)

    def test_keyword_score_against_the_correct_chunk_is_higher(self):
        question = "ใช้ บัตรเครดิต ได้ไหม"
        correct_score = compute_keyword_score(question, CREDIT_CARD_CHUNK)
        wrong_score = compute_keyword_score(question, TAX_INVOICE_CHUNK)
        self.assertGreater(correct_score, wrong_score)

    def test_high_vector_unrelated_chunk_does_not_outrank_correct_low_vector_chunk(self):
        """Reproduces the reported ranking failure: an unrelated chunk
        with a much higher raw vector score must not win purely because
        of shared generic-particle "evidence.\""""
        pool = [TAX_INVOICE_CHUNK, CREDIT_CARD_CHUNK]
        kept = apply_hybrid_ranking("ใช้ บัตรเครดิต ได้ไหม", pool,
                                     query_variants=["ใช้ บัตรเครดิต ได้ไหม"], settings=_NO_FLOOR)
        files_in_order = [c["file_name"] for c in kept]
        self.assertEqual(files_in_order[0], "credit-card-faq.xlsx")


if __name__ == "__main__":
    unittest.main()
