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


class TestTask04ShippingDurationGrounding(unittest.TestCase):
    """Task 04 — Fix RAG / Knowledge Answer Accuracy (2026-08-26).

    Confirmed live root cause of the reported customer defect
    ("ช่วงนี้ขนส่งทางรถใช้เวลานานไหมครับ" answered "no specific info", even
    though the customer said the info IS on the website): the correct
    answer genuinely exists in the knowledge base — an FAQ row titled
    "เรทเท่าไหร่คะ" (a RATE/PRICE question) whose body ALSO states "เรท
    ทางรถ...ระยะเวลา 7-10วัน...ด่านเวียดนามยังตรวจสอบเข้มอยู่" — but never
    outranked a DIFFERENT, wrong-topic chunk ("ระยะเวลาการส่งจากร้านจีน-
    โกดังจีน", answering a different leg of the shipping process) for the
    customer's exact natural phrasing.

    Root cause was NOT missing/stale data, NOT a threshold, NOT prompt/
    grounding instructions (the prompt already forbids fabrication) — it
    was query TOKENIZATION: rag/hybrid_scoring.py's _TOKEN_RE lumps an
    entire punctuation-free Thai sentence into ONE token (Thai has no
    spaces), so compute_keyword_score degenerated into a binary "does any
    haystack token happen to recur verbatim inside this one giant blob"
    check — rewarding whichever chunk had a generic, common word as an
    isolated token (a bare "ขนส่ง" Tag, shared by nearly every shipping FAQ
    row) while the chunk whose own specific term ("ทางรถ") was merely
    GLUED to other characters ("เรททางรถ", never its own isolated token)
    got zero credit despite containing the exact topic phrase.

    Two additive fixes, both dependency-free (no new NLP library):
    (1) Long-Glued-Query Partial Overlap (_thai_ngram_overlap_score) —
        when whole-token containment fails for a long q_token, falls back
        to longest-common-substring scoring against haystack tokens,
        gated at _MIN_SHARED_SUBSTRING_LEN=8 so a coincidental short,
        generic Thai morpheme (e.g. "เกี่ยว", shared by both "เกี่ยวกับ"
        and "เกี่ยวข้อง" with no topical relation at all — confirmed to
        leak false partial credit at a lower threshold) never counts.
    (2) duration_query intent (rag/intent_classifier.py) + has_duration_
        evidence (rag/hybrid_scoring.py) — mirrors the existing
        log_event_time / has_log_time_evidence mechanism exactly: a
        generic "how long does X take" query intent, and a chunk-level
        check for an explicit day-count/range in the TEXT (never a
        hardcoded fact/number) — boosts any chunk carrying real duration
        evidence regardless of its own heading wording, for ANY future
        "how long" question (not just shipping).

    Both fixes are pure text-pattern/scoring changes — no knowledge base
    content was edited, no new dependency, no prompt rewrite."""

    # ---- has_duration_evidence (generic day-count/range detector) ----

    def test_has_duration_evidence_thai_range(self):
        from rag.hybrid_scoring import has_duration_evidence
        self.assertTrue(has_duration_evidence(_chunk("ระยะเวลา 7-10วัน นับจากวันที่สินค้าถึงโกดังจีน", 0.5)))

    def test_has_duration_evidence_thai_single_count(self):
        from rag.hybrid_scoring import has_duration_evidence
        self.assertTrue(has_duration_evidence(_chunk("ใช้เวลาประมาณ 2-4 วันค่ะ", 0.5)))

    def test_has_duration_evidence_english(self):
        from rag.hybrid_scoring import has_duration_evidence
        self.assertTrue(has_duration_evidence(_chunk("Processing takes 5-7 days depending on volume.", 0.5)))

    def test_has_duration_evidence_absent(self):
        from rag.hybrid_scoring import has_duration_evidence
        self.assertFalse(has_duration_evidence(_chunk("สินค้าห้ามนำเข้ามีดังนี้ ของเหลว เครื่องสำอาง", 0.5)))

    def test_has_duration_evidence_never_matches_english_word_containing_day(self):
        from rag.hybrid_scoring import has_duration_evidence
        self.assertFalse(has_duration_evidence(_chunk("Delivered on Monday as scheduled.", 0.5)))

    # ---- duration_query intent classification ----

    def test_duration_query_intent_detected(self):
        from rag.intent_classifier import classify_query_intent
        for q in ["ช่วงนี้ขนส่งทางรถใช้เวลานานไหมครับ", "ทางรถใช้เวลากี่วัน", "ขนส่งทางรถกี่วัน",
                  "รถจากจีนมาไทยกี่วัน", "how long does shipping take"]:
            self.assertEqual(classify_query_intent(q), "duration_query", q)

    def test_duration_query_intent_not_misfired_for_unrelated_questions(self):
        from rag.intent_classifier import classify_query_intent
        self.assertNotEqual(classify_query_intent("CBM คืออะไร"), "duration_query")
        self.assertNotEqual(classify_query_intent("คูปองใช้ยังไง"), "duration_query")
        self.assertNotEqual(classify_query_intent("มีสินค้าอะไรห้ามนำเข้าบ้าง"), "duration_query")

    def test_log_event_time_still_takes_priority_over_duration(self):
        # Both clusters are checked early in classify_query_intent — a
        # message with distinctive log/startup vocabulary must still
        # resolve to log_event_time, not be shadowed by the new bucket.
        from rag.intent_classifier import classify_query_intent
        self.assertEqual(classify_query_intent("ระบบเริ่มทำงานกี่โมง"), "log_event_time")

    # ---- The real customer scenario, reproduced with synthetic chunks
    #      mirroring the actual production knowledge content ----

    RATE_CHUNK_TEXT = (
        "Question: เรทเท่าไหร่คะ\n"
        "Answer: สวัสดีค่ะ เรทนำเข้าและฝากสั่งกับทางเรานะคะ\n\n"
        "เรทฝากสั่ง 5.11 บาทต่อหยวน\n\n"
        "เรททางรถ 35บาท/กิโลกรัม ปริมาตร 6900บาท/คิวค่ะ ระยะเวลา 7-10วัน "
        "(ช่วงนี้เผื่อเวลาจากเดิมอีก 3-5 วัน เนื่องจาก ด่านเวียดนามยังตรวจสอบเข้มอยู่นะคะ) "
        "นับจากวันที่สินค้าถึงโกดังจีนนะคะ\n\n"
        "เรททางเรือ 19บาท/กิโลกรัม ปริมาตร 4500บาท/คิวค่ะ ระยะเวลา 14-20วัน "
        "นับจากวันที่สินค้าถึงโกดังจีนนะคะ\n"
        "Alternative phrasings: เรทนำเข้าเท่าไหร่ / เรทฝากสั่งกี่บาทต่อหยวน / ค่าส่งทางรถ"
    )
    WRONG_LEG_CHUNK_TEXT = (
        "Question: ระยะเวลาการส่งจากร้านจีน -โกดังจีน\n"
        "Answer: โดยปกติจะใช้เวลาประมาณ 2–4 วันค่ะ ทั้งนี้ขึ้นอยู่กับเวลาทำการของร้านค้าฝั่งจีนด้วยนะคะ\n"
        "Alternative phrasings: ร้านจีนส่งถึงโกดังกี่วัน / จากร้านถึงโกดังจีนใช้เวลากี่วัน\n"
        "Tags: ขนส่ง, นำเข้า, โกดัง, โกดังจีน"
    )

    def _seed_pool(self):
        rate_chunk = _chunk(self.RATE_CHUNK_TEXT, score=0.4507, file_name="AI Knowledge Master.xlsx",
                             section_title="Question: เรทเท่าไหร่คะ")
        wrong_leg_chunk = _chunk(self.WRONG_LEG_CHUNK_TEXT, score=0.4301, file_name="AI Knowledge Master.xlsx",
                                  section_title="Question: ระยะเวลาการส่งจากร้านจีน -โกดังจีน")
        filler_chunk = _chunk("Question: ขนส่งเอกชนมีอะไรบ้าง\nAnswer: Nim, EMS, J&T, FLASH",
                               score=0.3787, file_name="AI Knowledge Master.xlsx",
                               section_title="Question: ขนส่งเอกชนมีอะไรบ้าง")
        return [rate_chunk, wrong_leg_chunk, filler_chunk]

    # TEST 01 — the exact reproduced customer query: the correct chunk
    # (containing the real "ทางรถ...7-10วัน" answer) must rank within the
    # top candidates sent to the LLM, not be silently pushed out by the
    # wrong-leg chunk's generic "ขนส่ง" tag match.
    def test_01_exact_customer_query_correct_chunk_ranks_in_top_results(self):
        from rag.intent_classifier import classify_query_intent
        question = "ช่วงนี้ขนส่งทางรถใช้เวลานานไหมครับ"
        intent = classify_query_intent(question)
        self.assertEqual(intent, "duration_query")
        result = apply_hybrid_ranking(question, self._seed_pool(), query_intent=intent, settings=_NO_FLOOR)
        top_2_files = [c["section_title"] for c in result[:2]]
        self.assertIn("Question: เรทเท่าไหร่คะ", top_2_files)

    # TEST 02 — short variant: the correct chunk must win outright.
    def test_02_short_variant_correct_chunk_wins(self):
        from rag.intent_classifier import classify_query_intent
        question = "ทางรถใช้เวลากี่วัน"
        intent = classify_query_intent(question)
        result = apply_hybrid_ranking(question, self._seed_pool(), query_intent=intent, settings=_NO_FLOOR)
        self.assertEqual(result[0]["section_title"], "Question: เรทเท่าไหร่คะ")

    # TEST 03 — different phrasing: same authoritative knowledge is
    # recoverable without requiring exact-string matching.
    def test_03_different_phrasing_correct_chunk_in_top_results(self):
        from rag.intent_classifier import classify_query_intent
        question = "รถจากจีนมาไทยนานไหม"
        intent = classify_query_intent(question)
        result = apply_hybrid_ranking(question, self._seed_pool(), query_intent=intent, settings=_NO_FLOOR)
        top_2_files = [c["section_title"] for c in result[:2]]
        self.assertIn("Question: เรทเท่าไหร่คะ", top_2_files)

    # TEST 07 — Semantic neighbor / no-irrelevant-dump protection: a
    # duration_query must never boost an off-topic chunk (e.g. prohibited
    # goods) that carries no duration evidence of its own.
    def test_07_semantic_neighbor_without_duration_evidence_not_boosted(self):
        from rag.hybrid_scoring import has_duration_evidence
        prohibited_chunk = _chunk(
            "Question: สินค้าที่ห้ามนำเข้ามีอะไรบ้าง\nAnswer: สินค้าผิดกฎหมาย ของเหลว เครื่องสำอาง",
            score=0.3, file_name="x", section_title="Question: สินค้าที่ห้ามนำเข้ามีอะไรบ้าง")
        self.assertFalse(has_duration_evidence(prohibited_chunk))
        result = apply_hybrid_ranking("ทางรถกี่วัน", self._seed_pool() + [prohibited_chunk],
                                       query_intent="duration_query", settings=_NO_FLOOR)
        self.assertEqual(prohibited_chunk["duration_evidence"], False)
        self.assertNotEqual(result[0]["section_title"], "Question: สินค้าที่ห้ามนำเข้ามีอะไรบ้าง")

    # ---- Regression: the long-glued-query fallback must never leak
    #      false-positive evidence for an unrelated, non-duration query ----

    # TEST — confirmed false-positive found during implementation: a
    # generic Thai morpheme shared by two otherwise-unrelated words
    # ("เกี่ยว" in both "เกี่ยวกับ" and "เกี่ยวข้อง") must not count as real
    # evidence — this is exactly why _MIN_SHARED_SUBSTRING_LEN exists.
    def test_generic_short_morpheme_never_counts_as_keyword_evidence(self):
        from rag.hybrid_scoring import compute_keyword_score
        irrelevant_chunk = _chunk("เนื้อหาที่ไม่เกี่ยวข้องกันเลยเรื่องอื่นโดยสิ้นเชิง",
                                   score=0.05, section_title="หัวข้ออื่น")
        score = compute_keyword_score("บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร", irrelevant_chunk)
        self.assertEqual(score, 0.0)

    # TEST 09 (regression guard) — a company-policy question genuinely
    # unrelated to duration must never receive duration_evidence credit,
    # confirming the new mechanism doesn't widen what counts as relevant
    # for anything other than a genuine duration_query.
    def test_09_non_duration_query_gets_no_duration_boost(self):
        chunk_with_days = _chunk(self.RATE_CHUNK_TEXT, score=0.3, section_title="Question: เรทเท่าไหร่คะ")
        result = apply_hybrid_ranking("บริษัทมีนโยบายเรื่องการรีไซเคิลกล่องพัสดุอย่างไร", [chunk_with_days],
                                       query_intent="unknown", settings=_NO_FLOOR)
        self.assertEqual(result[0]["duration_evidence"], False)
        self.assertEqual(result[0]["purpose_boost"], 0.0)


if __name__ == "__main__":
    unittest.main()
