"""Regression tests for the RAG hybrid retrieval fix (rag/hybrid_scoring.py).

Covers the confirmed failure case: "Google Drive ต้องใช้ python version
อะไร" retrieved two irrelevant Excel FAQ chunks ranked ahead of/alongside
the actual requirements.txt "# Google Drive" section, purely because pure
vector similarity doesn't reward exact keyword/heading overlap.

Also includes a second, unrelated domain (database driver vs OS version)
to prove the fix is generic — not a special case for "Google Drive" or
"Python" specifically.

Root-cause fix (this revision): keyword/heading scoring is now computed
across the FULL query_variants list (query expansion), not just the raw
question, and the hard "weak_semantic" absolute-vector cutoff is replaced
by adaptive filtering over the combined HYBRID score. Tests that probe
small (2-3 candidate) pools pass an explicit `settings` override with
minimum_candidates=1 so the "always keep at least N" floor (correct and
intentional at realistic pool sizes of 12) doesn't make a 2-item test
pool vacuously pass everything.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.hybrid_scoring import compute_keyword_score, compute_heading_score, apply_hybrid_ranking
from services.retrieval_settings import RetrievalSettings

# Used by tests that want the OLD "no artificial floor" behavior on a
# small, hand-built pool — realistic production pools (candidate_top_k=12)
# make the minimum_candidates=3 default floor a small fraction, not a
# free pass; these tiny fixtures need it turned down to actually test
# exclusion.
_NO_FLOOR = RetrievalSettings(minimum_candidates=1, maximum_candidates=12)


def _chunk(text, score, heading_path=None, section_title=None, file_name=None, **extra):
    c = {
        "text": text, "score": score, "heading_path": heading_path or [],
        "section_title": section_title, "file_name": file_name or "unknown",
        "source": file_name or "unknown",
    }
    c.update(extra)
    return c


REQUIREMENTS_CHUNK = _chunk(
    "# Google Drive\ngoogle-api-python-client>=2.100.0\ngoogle-auth>=2.20.0",
    score=0.27, heading_path=["Google Drive"], section_title="Google Drive",
    file_name="requirements (1).txt",
)
# Real audit data: these Excel FAQ chunks actually scored HIGHER on raw
# vector similarity than the correct requirements chunk for some phrasings
# (e.g. 0.35 vs 0.27) — the fixtures intentionally reproduce that, not an
# artificially-tied score, since that's the actual failure being guarded against.
WARRANTY_CHUNK = _chunk(
    "Question: Any warranty doc?\nAnswer: Yes see the PDF.",
    score=0.35, file_name="flexible-attachment-test.xlsx",
)
DOCS_CHUNK = _chunk(
    "Question: Any documents available?\nAnswer: Yes, see files.",
    score=0.37, file_name="smart-import-test.xlsx",
)


class TestKeywordAndHeadingScore(unittest.TestCase):
    def test_exact_heading_match_scores_high(self):
        score = compute_heading_score("Google Drive ต้องใช้ python version อะไร", REQUIREMENTS_CHUNK)
        self.assertGreater(score, 0.3)

    def test_unrelated_chunk_has_zero_heading_score(self):
        score = compute_heading_score("Google Drive ต้องใช้ python version อะไร", WARRANTY_CHUNK)
        self.assertEqual(score, 0.0)

    def test_keyword_overlap_detects_package_name_tokens(self):
        score = compute_keyword_score("google-api-python-client ใช้ version อะไร", REQUIREMENTS_CHUNK)
        self.assertGreater(score, 0)

    def test_unrelated_chunks_have_zero_keyword_overlap(self):
        question = "Google Drive ต้องใช้ python version อะไร"
        self.assertEqual(compute_keyword_score(question, WARRANTY_CHUNK), 0.0)
        self.assertEqual(compute_keyword_score(question, DOCS_CHUNK), 0.0)

    def test_ancestor_document_title_in_heading_path_does_not_leak_a_false_match(self):
        """Found during live verification: heading_path carries the
        document's own top-level title as its FIRST element for every
        section (e.g. every section of company-profile-test.md has
        "Shipify Company Profile" as heading_path[0]). A query about
        "vision" (which has no real section in this document) must not
        get a false partial-match boost on "Our Services"/"Our Mission"/
        "Contact Us" merely because their shared ancestor heading
        ("Shipify Company Profile") happens to contain the word "company",
        which overlaps with an expanded "company vision" variant."""
        from rag.hybrid_scoring import compute_heading_match_info
        services_chunk = {"heading_path": ["Shipify Company Profile", "Our Services"],
                           "section_title": "Our Services"}
        info = compute_heading_match_info(
            "วิสัยทัศน์ของบริษัทคืออะไร", services_chunk,
            query_variants=["วิสัยทัศน์ของบริษัทคืออะไร", "vision company vision", "vision", "company vision"],
        )
        self.assertEqual(info["match_type"], "none")
        self.assertIsNone(info["matched_variant"])

    def test_heading_score_uses_best_of_all_query_variants(self):
        """The root-cause fix: a Thai-only query with an expanded English
        variant must be able to match an English-only heading — computed
        across ALL variants, not just the raw original question."""
        chunk = _chunk("We aim to make cross-border shipping easy.",
                        heading_path=["Our Mission"], section_title="Our Mission",
                        score=0.5, file_name="company-profile-test.md")
        # Original Thai question alone shares no tokens with "Our Mission".
        no_variant_score = compute_heading_score("แล้วมิชชั่น คือ อะไร", chunk, query_variants=None)
        with_variant_score = compute_heading_score("แล้วมิชชั่น คือ อะไร", chunk,
                                                    query_variants=["แล้วมิชชั่น คือ อะไร", "mission"])
        self.assertGreater(with_variant_score, no_variant_score)
        self.assertGreater(with_variant_score, 0.0)


class TestApplyHybridRanking(unittest.TestCase):
    def test_requirements_chunk_ranks_first_and_excel_chunks_excluded(self):
        question = "Google Drive ต้องใช้ python version อะไร"
        chunks = [WARRANTY_CHUNK.copy(), REQUIREMENTS_CHUNK.copy(), DOCS_CHUNK.copy()]
        result = apply_hybrid_ranking(question, chunks, settings=_NO_FLOOR)
        self.assertEqual(result[0]["file_name"], "requirements (1).txt")
        # The requirements chunk has the LOWEST raw vector score (0.27) of
        # the three, yet must still rank first thanks to keyword/heading
        # evidence — that's the actual behavior under test, not a specific
        # hybrid-vs-raw-score inequality (whose exact value depends on the
        # configured weights).
        self.assertGreater(result[0]["keyword_score"], 0)
        self.assertGreater(result[0]["heading_score"], 0)

    def test_result_carries_score_breakdown(self):
        result = apply_hybrid_ranking("Google Drive ต้องใช้ python version อะไร", [REQUIREMENTS_CHUNK.copy()],
                                       settings=_NO_FLOOR)
        self.assertIn("keyword_score", result[0])
        self.assertIn("heading_score", result[0])
        self.assertIn("hybrid_score", result[0])
        self.assertIn("raw_vector_rank", result[0])
        self.assertIn("raw_vector_similarity", result[0])
        self.assertIn("normalized_vector_score", result[0])
        self.assertIn("classification", result[0])
        self.assertIn("evidence_label", result[0])
        self.assertEqual(result[0]["classification"], "direct_evidence")

    def test_return_excluded_reports_reason(self):
        question = "Google Drive ต้องใช้ python version อะไร"
        chunks = [WARRANTY_CHUNK.copy(), REQUIREMENTS_CHUNK.copy(), DOCS_CHUNK.copy()]
        kept, excluded = apply_hybrid_ranking(question, chunks, return_excluded=True, settings=_NO_FLOOR)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(excluded), 2)
        for e in excluded:
            self.assertIn("exclusion_reason", e)

    def test_lexical_evidence_always_outranks_weak_semantic_regardless_of_raw_score(self):
        """A chunk with a much LOWER raw vector score but real keyword/
        heading evidence must still rank above a chunk with a higher raw
        vector score but no lexical evidence at all."""
        weak_high_vector = _chunk("some vaguely related filler text", score=0.9, file_name="noise.xlsx")
        lexical_low_vector = REQUIREMENTS_CHUNK.copy()
        lexical_low_vector["score"] = 0.15
        result = apply_hybrid_ranking("Google Drive ต้องใช้ python version อะไร",
                                       [weak_high_vector, lexical_low_vector], settings=_NO_FLOOR)
        self.assertEqual(result[0]["file_name"], "requirements (1).txt")

    def test_structured_and_calculated_chunks_are_never_filtered(self):
        structured = _chunk("some aggregate table", score=0.1, is_structured=True)
        calculated = _chunk("CALCULATED RESULT: 42", score=0.05, is_calculated=True)
        result = apply_hybrid_ranking("unrelated question about nothing", [structured, calculated], settings=_NO_FLOOR)
        self.assertEqual(len(result), 2)

    def test_weak_vector_candidate_dropped_relative_to_a_stronger_peer(self):
        """A candidate with zero lexical evidence, far below the pool's
        best hybrid score and below the absolute floor, is dropped —
        judged adaptively, not by a fixed constant."""
        weak = _chunk("completely unrelated filler content about weather", score=0.2, file_name="weather.xlsx")
        strong = _chunk("a much closer semantic match for this exact query", score=0.9, file_name="strong-match.md")
        result = apply_hybrid_ranking("Google Drive python version", [weak, strong], settings=_NO_FLOOR)
        file_names = [c["file_name"] for c in result]
        self.assertNotIn(weak["file_name"], file_names)
        self.assertIn(strong["file_name"], file_names)

    def test_sole_candidate_with_no_peer_to_compare_against_is_not_punished(self):
        """A pool of exactly one candidate has nothing to be "relatively
        weak" against — it should not be dropped just because there's no
        comparison point."""
        only_candidate = _chunk("a chunk with no literal overlap but some embedding similarity", score=0.4)
        result = apply_hybrid_ranking("Google Drive python version", [only_candidate], settings=_NO_FLOOR)
        self.assertEqual(len(result), 1)

    def test_strong_vector_score_alone_can_still_survive(self):
        strong_paraphrase = _chunk("a chunk with no literal overlap but very high embedding similarity", score=0.9)
        weaker_peer = _chunk("a somewhat related but much weaker peer", score=0.3)
        result = apply_hybrid_ranking("Google Drive python version", [weaker_peer, strong_paraphrase], settings=_NO_FLOOR)
        file_names = [c["file_name"] for c in result]
        self.assertIn(strong_paraphrase["file_name"], file_names)

    def test_ranking_is_generic_not_hardcoded_to_google_drive(self):
        """Same mechanism, unrelated domain: database driver version vs OS
        version. Proves the fix isn't special-cased for this one failure."""
        driver_chunk = _chunk(
            "# PostgreSQL Driver\npsycopg2>=2.9.0\nSupports PostgreSQL 12+.",
            score=0.3, heading_path=["PostgreSQL Driver"], section_title="PostgreSQL Driver",
            file_name="db_requirements.txt",
        )
        unrelated_chunk = _chunk("Question: How do I reset my password?\nAnswer: Use the reset link.",
                                  score=0.29, file_name="faq.xlsx")
        result = apply_hybrid_ranking("What operating system version does the database driver need?",
                                       [unrelated_chunk, driver_chunk], settings=_NO_FLOOR)
        self.assertEqual(result[0]["file_name"], "db_requirements.txt")

    def test_minimum_candidates_floor_keeps_n_results_in_a_realistic_pool(self):
        """Part 4: with a realistic candidate_top_k=12 pool where NOTHING
        has strong lexical evidence, at least minimum_candidates survivors
        must be kept rather than all being dropped."""
        settings = RetrievalSettings(minimum_candidates=3, absolute_minimum_score=0.9,
                                      relative_to_best_ratio=0.99, maximum_candidates=12)
        pool = [_chunk(f"filler unrelated content number {i}", score=0.1 + i * 0.01, file_name=f"f{i}.md")
                for i in range(10)]
        result = apply_hybrid_ranking("completely unrelated query", pool, settings=settings)
        self.assertGreaterEqual(len(result), 3)

    def test_mission_case_direct_heading_synonym_survives_with_default_settings(self):
        """The exact reported bug: "แล้วมิชชั่น คือ อะไร" against a chunk
        headed "Our Mission" — with the ORIGINAL default retrieval
        settings (no _NO_FLOOR override) — must survive as real evidence,
        not be excluded for having a normalized vector score below 0.75."""
        mission_chunk = _chunk("We aim to make cross-border shipping as easy as ordering food delivery.",
                                heading_path=["Our Mission"], section_title="Our Mission",
                                score=0.66, file_name="company-profile-test.md")
        profile_chunk = _chunk("Shipify is a cross-border logistics platform founded in 2020.",
                                heading_path=["Shipify Company Profile"], section_title="Shipify Company Profile",
                                score=0.60, file_name="company-profile-test.md")
        unrelated_chunk = _chunk("Question: What does the box look like?\nAnswer: See attached photo.",
                                  score=0.30, file_name="flexible-attachment-test.xlsx")
        from rag.query_expansion import expand_query
        question = "แล้วมิชชั่น คือ อะไร"
        variants = expand_query(question)
        self.assertIn("mission", [v.lower() for v in variants])

        result = apply_hybrid_ranking(question, [mission_chunk, profile_chunk, unrelated_chunk],
                                       query_variants=variants)
        file_names = [c["file_name"] for c in result]
        self.assertIn("company-profile-test.md", file_names)
        mission_result = next(c for c in result if c.get("section_title") == "Our Mission")
        self.assertNotEqual(mission_result["evidence_label"], "weak")
        self.assertIn(mission_result["evidence_label"], ("synonym_heading", "direct_heading", "semantic_supporting"))


class TestPurposeAwareRankingBoost(unittest.TestCase):
    """Regression tests for the confirmed cross-document failure: a
    coverage-benefit question like "Plan 4 ค่าห้องเท่าไหร่" retrieved a
    premium-rate-table chunk instead of the benefit-brochure chunk,
    because both documents legitimately repeat "Plan 1/2/3/4" constantly.
    document_purpose (services/document_purpose.py) + query_intent
    (rag/intent_classifier.py) give hybrid_scoring a ranking preference —
    never a hard filter — for the document whose purpose matches."""

    def test_coverage_intent_boosts_brochure_chunk_above_premium_chunk(self):
        # Equal vector score and equal "Plan 1-4" keyword overlap — the
        # exact real-world tie this fix targets (both documents
        # legitimately repeat "Plan 1/2/3/4" constantly) — so the
        # purpose-aware boost is the only thing that can break the tie.
        from rag.intent_classifier import classify_query_intent
        brochure_chunk = _chunk(
            "1.1 ค่าห้องผู้ป่วยปกติ (สูงสุดต่อวัน) Plan 1 Plan 2 Plan 3 Plan 4 4,000 5,000 6,000 7,000",
            score=0.40, file_name="brochure.pdf", document_purpose="coverage_brochure",
        )
        premium_chunk = _chunk(
            "Plan 1 Plan 2 Plan 3 Plan 4 1,420 1,554 1,729 1,848",
            score=0.40, file_name="premium-monthly.pdf", document_purpose="premium_monthly",
        )
        intent = classify_query_intent("Plan 4 ค่าห้องเท่าไหร่")
        self.assertEqual(intent, "coverage_benefit")

        result = apply_hybrid_ranking("Plan 4 ค่าห้องเท่าไหร่", [brochure_chunk, premium_chunk],
                                       query_intent=intent, settings=_NO_FLOOR)
        file_order = [c["file_name"] for c in result]
        self.assertEqual(file_order[0], "brochure.pdf")

    def test_premium_intent_boosts_premium_chunk_above_brochure_chunk(self):
        from rag.intent_classifier import classify_query_intent
        brochure_chunk = _chunk(
            "1.1 ค่าห้องผู้ป่วยปกติ Plan 1 Plan 2 Plan 3 Plan 4 4,000 5,000 6,000 7,000",
            score=0.40, file_name="brochure.pdf", document_purpose="coverage_brochure",
        )
        premium_chunk = _chunk(
            "Plan 1 Plan 2 Plan 3 Plan 4 1,420 1,554 1,729 1,848",
            score=0.40, file_name="premium-monthly.pdf", document_purpose="premium_monthly",
        )
        intent = classify_query_intent("Plan 4 เบี้ยรายเดือนเท่าไหร่")
        self.assertEqual(intent, "premium_price")

        result = apply_hybrid_ranking("Plan 4 เบี้ยรายเดือนเท่าไหร่", [brochure_chunk, premium_chunk],
                                       query_intent=intent, settings=_NO_FLOOR)
        file_order = [c["file_name"] for c in result]
        self.assertEqual(file_order[0], "premium-monthly.pdf")

    def test_purpose_boost_never_excludes_the_other_document(self):
        """A ranking preference, not a hard filter — the non-matching
        document's chunk must still survive in the result (both chunks
        share real "Plan 4" keyword evidence, same as the actual reported
        case, so neither is a pure-vector-only candidate subject to
        adaptive filtering)."""
        brochure_chunk = _chunk("Plan 4 coverage text", score=0.40, file_name="brochure.pdf",
                                 document_purpose="coverage_brochure")
        premium_chunk = _chunk("Plan 4 premium text", score=0.39, file_name="premium-monthly.pdf",
                                document_purpose="premium_monthly")
        result = apply_hybrid_ranking("Plan 4 ค่าห้องเท่าไหร่", [brochure_chunk, premium_chunk],
                                       query_intent="coverage_benefit", settings=_NO_FLOOR)
        file_names = {c["file_name"] for c in result}
        self.assertIn("premium-monthly.pdf", file_names)

    def test_unknown_intent_applies_no_boost(self):
        from rag.hybrid_scoring import compute_purpose_boost
        chunk = _chunk("text", score=0.5, document_purpose="coverage_brochure")
        self.assertEqual(compute_purpose_boost("unknown", chunk), 0.0)
        self.assertEqual(compute_purpose_boost(None, chunk), 0.0)


if __name__ == "__main__":
    unittest.main()
