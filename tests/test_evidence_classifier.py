"""Regression tests for rag/evidence_classifier.py (Phase 2 follow-up) —
verifies a chunk is only DIRECT_EVIDENCE when it actually supports the
SPECIFIC attribute asked about, not merely because it mentions the same
entity, has keyword overlap, or survived retrieval. Uses the exact
"Shipify mission" case from the real audit as its primary fixture."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.evidence_classifier import (
    classify_evidence, select_citation_sources,
    DIRECT_EVIDENCE, PARTIAL_EVIDENCE, RELATED_CONTEXT, IRRELEVANT,
)


def _chunk(text, section_title, document_title="company-profile-test", file_name="company-profile-test.md",
           classification="direct_evidence", chunk_id=None, heading_path=None, **extra):
    c = {
        "text": text, "section_title": section_title, "document_title": document_title,
        "file_name": file_name, "classification": classification,
        "chunk_id": chunk_id or (file_name + "::" + section_title),
        "heading_path": heading_path or [section_title],
    }
    c.update(extra)
    return c


class TestClassifyEvidenceMissionCase(unittest.TestCase):
    """The exact case from the audit: 'What is Shipify's mission?' must
    classify the Our Mission chunk as DIRECT_EVIDENCE and the two generic
    company-profile chunks (from different files, same entity) as
    RELATED_CONTEXT — never DIRECT_EVIDENCE just for mentioning Shipify."""

    def setUp(self):
        self.question = "What is Shipify's mission?"
        self.mission = _chunk("We aim to make cross-border shipping as easy as ordering food delivery.",
                               "Our Mission")
        self.profile = _chunk("Shipify is a cross-border logistics platform founded in 2020.",
                               "Shipify Company Profile")
        self.graph_profile = _chunk("Shipify provides cross-border Shipping Service.",
                                     "Shipify Company Profile", document_title="graph-test",
                                     file_name="graph-test.md")
        self.chunks = [self.mission, self.profile, self.graph_profile]

    def test_mission_chunk_is_direct_evidence(self):
        classify_evidence(self.question, self.chunks)
        self.assertEqual(self.mission["evidence_classification"], DIRECT_EVIDENCE)

    def test_company_profile_chunk_is_related_context_not_direct(self):
        classify_evidence(self.question, self.chunks)
        self.assertEqual(self.profile["evidence_classification"], RELATED_CONTEXT)

    def test_graph_test_company_profile_is_related_context(self):
        classify_evidence(self.question, self.chunks)
        self.assertEqual(self.graph_profile["evidence_classification"], RELATED_CONTEXT)

    def test_retrieval_order_and_chunk_list_are_unchanged(self):
        before = list(self.chunks)
        classify_evidence(self.question, self.chunks)
        self.assertEqual(self.chunks, before)  # same objects, same order — classification is additive only

    def test_final_citation_list_contains_only_mission_chunk(self):
        classify_evidence(self.question, self.chunks)
        answer = "ภารกิจของ Shipify คือการทำให้การขนส่งสินค้าข้ามพรมแดนง่ายเหมือนการสั่งอาหารเดลิเวอรี่ค่ะ"
        sources = select_citation_sources(self.question, answer, self.chunks)
        self.assertEqual(sources, [self.mission])

    def test_transliteration_question_needs_query_variants_to_reach_direct_evidence(self):
        """The real bug found in production: "แล้วมิชชั่น คือ อะไร" shares NO
        literal tokens with "Our Mission" — classify_evidence only reaches
        DIRECT_EVIDENCE (and thus gets cited) once the caller passes
        query-expansion's variants (which include "mission")."""
        question = "แล้วมิชชั่น คือ อะไร"
        mission = _chunk("We aim to make cross-border shipping as easy as ordering food delivery.",
                          "Our Mission", classification="direct_evidence")
        without_variants = classify_evidence(question, [dict(mission)])
        self.assertNotEqual(without_variants[0]["evidence_classification"], DIRECT_EVIDENCE)

        with_variants = classify_evidence(question, [dict(mission)], query_variants=[question, "mission"])
        self.assertEqual(with_variants[0]["evidence_classification"], DIRECT_EVIDENCE)


class TestMultiSourceAnswerKeepsMultipleCitations(unittest.TestCase):
    """Citation filtering must not always collapse to exactly one source
    — a question spanning two attributes, both genuinely answered by two
    different sections, must keep both as citations."""

    def test_two_genuinely_required_chunks_both_cited(self):
        question = "What is Shipify's mission and what services does Shipify provide?"
        mission = _chunk("We aim to make cross-border shipping as easy as ordering food delivery.",
                          "Our Mission")
        services = _chunk("We provide warehouse consolidation, customs clearance, and last-mile delivery.",
                           "Our Services")
        contact = _chunk("You can reach our support team at support@shipify-example.com.", "Contact Us")
        chunks = [mission, services, contact]
        classify_evidence(question, chunks)

        self.assertEqual(mission["evidence_classification"], DIRECT_EVIDENCE)
        self.assertEqual(services["evidence_classification"], DIRECT_EVIDENCE)
        self.assertEqual(contact["evidence_classification"], RELATED_CONTEXT)

        answer = ("ภารกิจของ Shipify คือทำให้การขนส่งข้ามพรมแดนง่าย mission. "
                  "Shipify provides warehouse consolidation, customs clearance, and last-mile delivery services.")
        sources = select_citation_sources(question, answer, chunks)
        self.assertIn(mission, sources)
        self.assertIn(services, sources)
        self.assertNotIn(contact, sources)
        self.assertGreaterEqual(len(sources), 2, "citation filtering must not always reduce sources to one")


class TestStructuredAndCalculatedAlwaysDirect(unittest.TestCase):
    def test_calculated_chunk_is_direct_evidence(self):
        chunks = [_chunk("Total = 42", "Calculation", is_calculated=True)]
        classify_evidence("what is the total?", chunks)
        self.assertEqual(chunks[0]["evidence_classification"], DIRECT_EVIDENCE)

    def test_structured_chunk_is_direct_evidence(self):
        chunks = [_chunk("row data", "Sheet1", is_structured=True)]
        classify_evidence("show me the data", chunks)
        self.assertEqual(chunks[0]["evidence_classification"], DIRECT_EVIDENCE)


class TestNoEligibleChunksReturnsEmptyCitations(unittest.TestCase):
    def test_all_irrelevant_yields_no_citations(self):
        chunks = [_chunk("unrelated text", "Unrelated Topic", classification="weak_semantic")]
        # Force IRRELEVANT by making section share nothing with question and hybrid tier unsupported.
        chunks[0]["classification"] = "excluded_weak_semantic"
        classify_evidence("what is the delivery fee?", chunks)
        sources = select_citation_sources("what is the delivery fee?", "no answer", chunks)
        self.assertEqual(sources, [])


class TestCitationAttributionFix(unittest.TestCase):
    """Grounding Failure Audit fix: an English question against a
    Thai-only knowledge base (or vice versa) can have zero literal token
    overlap in either script, so classify_evidence() can only ever
    reach RELATED_CONTEXT — select_citation_sources() must still cite
    the best-supported chunk rather than return nothing when an answer
    was actually generated from it."""

    def test_cross_script_question_still_cites_best_related_context_chunk(self):
        question = "What is the shipping cost?"
        best = _chunk("ค่าขนส่งคำนวนจากปริมาตรเเละน้ำหนักนะคะ ทางเรือ 4500B./cbm 19B./kg",
                       "Question: ค่าขนส่งคิดยังไง คำนวนค่าส่งให้หน่อย",
                       file_name="AI Knowledge Master.xlsx", classification="weak_semantic", hybrid_score=0.5556)
        weaker = _chunk("เรทนำเข้าและฝากสั่งกับทางเรานะคะ", "Question: เรทเท่าไหร่คะ",
                         file_name="AI Knowledge Master.xlsx", classification="weak_semantic", hybrid_score=0.4845)
        chunks = [best, weaker]
        classify_evidence(question, chunks)
        self.assertEqual(best["evidence_classification"], RELATED_CONTEXT)
        self.assertEqual(weaker["evidence_classification"], RELATED_CONTEXT)

        sources = select_citation_sources(question, "The shipping cost is calculated based on volume and weight.", chunks)
        self.assertEqual(sources, [best])

    def test_no_answer_text_still_returns_no_citations(self):
        """The fallback only fires when an answer was actually generated
        — it must never invent a citation for an empty/no-answer case."""
        chunk = _chunk("some Thai content here", "Question: อะไรก็ตาม",
                        file_name="AI Knowledge Master.xlsx", classification="weak_semantic", hybrid_score=0.5)
        chunk["evidence_classification"] = RELATED_CONTEXT  # bypass classify_evidence — isolate select_citation_sources only
        sources = select_citation_sources("some unrelated english question", "", [chunk])
        self.assertEqual(sources, [])

    def test_direct_evidence_still_takes_priority_over_fallback(self):
        """The last-resort RELATED_CONTEXT fallback must never override
        a genuine DIRECT_EVIDENCE match."""
        question = "What is Shipify's mission?"
        mission = _chunk("We aim to make cross-border shipping as easy as ordering food delivery.",
                          "Our Mission", hybrid_score=0.9)
        unrelated = _chunk("เนื้อหาภาษาไทยที่ไม่เกี่ยวข้อง", "Question: อื่นๆ",
                            file_name="AI Knowledge Master.xlsx", classification="weak_semantic", hybrid_score=0.99)
        chunks = [mission, unrelated]
        classify_evidence(question, chunks)
        sources = select_citation_sources(question, "Our mission is to make shipping easy.", chunks)
        self.assertEqual(sources, [mission])

    def test_same_script_entity_name_substring_does_not_over_credit(self):
        """Regression guard: an English q_token like 'shipify' must not
        substring-match an unrelated content token like an email domain
        ('shipify-example.com') just because both are ASCII — the
        cross-script fix must stay scoped to genuine script mismatches."""
        question = "What services does Shipify provide?"
        contact = _chunk("You can reach our support team at support@shipify-example.com.", "Contact Us")
        classify_evidence(question, [contact])
        self.assertNotEqual(contact["evidence_classification"], PARTIAL_EVIDENCE)


if __name__ == "__main__":
    unittest.main()
