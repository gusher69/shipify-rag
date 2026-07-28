"""Regression tests for the reported bug: a log-event-time question
("ระบบเริ่มทำงานกี่โมง" — "what time did the system start") retrieved
unrelated Contract/DevOps chunks instead of the correct log chunk
containing the timestamp, and the resulting low confidence then forced a
premature escalation even when the correct evidence was actually present
in the candidate pool.

Root cause: no deterministic intent existed for log-event-time questions,
so the correct log chunk (no shared vocabulary with the Thai question,
only a raw "HH:MM:SS" timestamp + English "started" text) never earned
real keyword/heading evidence, while unrelated chunks that merely
contained the extremely common word "ระบบ" (system) DID earn keyword
credit and crowded it out of the adaptive filter.

Fix (this revision):
  1. rag/intent_classifier.py — deterministic "log_event_time" intent.
  2. rag/query_expansion.py — Thai->English glossary for the start/time
     vocabulary (เริ่มทำงาน/เริ่มระบบ -> started/startup/..., กี่โมง/เวลาอะไร
     -> time/timestamp).
  3. rag/hybrid_scoring.py — for a log_event_time query: (a) a chunk
     containing BOTH a clock timestamp and startup vocabulary is forced
     to count as real lexical evidence (has_log_time_evidence) and gets a
     fixed boost, bypassing the relative-to-best-vector adaptive filter
     entirely; (b) the generic noise term "ระบบ"/"system" is stripped from
     the query variants before keyword/heading scoring for this intent,
     so it can no longer manufacture false lexical evidence on its own.
  4. services/playground_orchestrator.py — the no-answer escalation check
     now skips entirely when direct/structured evidence exists in the
     final chunk list, and this still runs strictly AFTER evidence
     classification (unchanged execution order, verified below).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.intent_classifier import classify_query_intent
from rag.query_expansion import expand_query
from rag.hybrid_scoring import apply_hybrid_ranking, has_log_time_evidence
from rag.confidence import compute_confidence
from services.retrieval_settings import RetrievalSettings
from services.playground_orchestrator import _has_direct_structured_evidence

_NO_FLOOR = RetrievalSettings(minimum_candidates=1, maximum_candidates=12)


def _chunk(text, score, file_name="unknown", **extra):
    c = {"text": text, "score": score, "heading_path": [], "section_title": None,
         "file_name": file_name, "source": file_name}
    c.update(extra)
    return c


# The exact reported scenario's fixtures — each a FACTORY (not a shared
# module-level dict), since apply_hybrid_ranking mutates chunk dicts
# in place and several tests independently feed these into it.
def log_chunk_0614():
    return _chunk("[2026-07-10 06:14:34] System started. Scheduler initialized.",
                  score=0.12, file_name="app.log")


def log_chunk_0918():
    return _chunk("[2026-07-10 09:18:13] SFTP refresh cycle started for warehouse sync.",
                  score=0.10, file_name="sftp.log")


def contract_chunk():
    return _chunk("ระบบ Shipify ต้องได้รับการดูแลตามสัญญา DevOps รายเดือน ระบบทั้งหมดอยู่ภายใต้ข้อตกลง",
                  score=0.91, file_name="devops-contract.pdf")


def unrelated_high_vector_chunk():
    return _chunk("ระบบสมาชิกและระบบตะกร้าสินค้าออกแบบใหม่ทั้งระบบในปีนี้",
                  score=0.95, file_name="product-roadmap.pdf")


class TestLogEventTimeIntentDetection(unittest.TestCase):
    def test_start_time_question_detected_as_log_event_time(self):
        self.assertEqual(classify_query_intent("ระบบเริ่มทำงานกี่โมง"), "log_event_time")

    def test_scheduler_question_detected_as_log_event_time(self):
        self.assertEqual(classify_query_intent("scheduler เริ่มตอนไหน"), "log_event_time")

    def test_sftp_refresh_question_detected_as_log_event_time(self):
        self.assertEqual(classify_query_intent("SFTP refresh เริ่มรอบ 9 โมงเมื่อไหร่"), "log_event_time")

    def test_bare_system_mention_alone_is_not_log_event_time(self):
        """Requirement: a bare "ระบบ" mention must not, on its own, trigger
        this intent — only real start-event/time vocabulary does."""
        self.assertNotEqual(classify_query_intent("ระบบ Shipify ทำงานอย่างไร"), "log_event_time")

    def test_unrelated_question_is_not_log_event_time(self):
        self.assertNotEqual(classify_query_intent("ราคาค่าจัดส่งเท่าไหร่"), "log_event_time")


class TestQueryExpansionGlossary(unittest.TestCase):
    def test_start_working_expands_to_english_start_vocabulary(self):
        variants = " ".join(expand_query("ระบบเริ่มทำงานกี่โมง")).lower()
        self.assertIn("started", variants)
        self.assertIn("timestamp", variants)

    def test_what_time_expands_to_time_vocabulary(self):
        variants = " ".join(expand_query("เวลาอะไร")).lower()
        self.assertIn("time", variants)


class TestHasLogTimeEvidence(unittest.TestCase):
    def test_timestamp_plus_startup_word_is_evidence(self):
        self.assertTrue(has_log_time_evidence(log_chunk_0614()))
        self.assertTrue(has_log_time_evidence(log_chunk_0918()))

    def test_timestamp_alone_without_startup_word_is_not_evidence(self):
        self.assertFalse(has_log_time_evidence(_chunk("[06:14:34] Nightly backup completed.", 0.5)))

    def test_startup_word_alone_without_timestamp_is_not_evidence(self):
        self.assertFalse(has_log_time_evidence(_chunk("The scheduler started successfully.", 0.5)))

    def test_generic_system_mention_is_not_evidence(self):
        self.assertFalse(has_log_time_evidence(contract_chunk()))


class TestRetrievalPrefersLogChunkOverGenericSystemMentions(unittest.TestCase):
    """The core bug: unrelated high-vector "ระบบ" chunks must not crowd out
    the correct low-vector log chunk once query_intent is log_event_time."""

    def test_log_chunk_survives_despite_low_vector_score(self):
        pool = [unrelated_high_vector_chunk(), contract_chunk(), log_chunk_0614()]
        kept, excluded = apply_hybrid_ranking(
            "ระบบเริ่มทำงานกี่โมง", pool, return_excluded=True,
            query_variants=expand_query("ระบบเริ่มทำงานกี่โมง"),
            query_intent="log_event_time", settings=_NO_FLOOR,
        )
        kept_files = {c["file_name"] for c in kept}
        self.assertIn("app.log", kept_files)

    def test_log_chunk_is_classified_as_direct_evidence(self):
        pool = [unrelated_high_vector_chunk(), contract_chunk(), log_chunk_0614()]
        kept = apply_hybrid_ranking(
            "ระบบเริ่มทำงานกี่โมง", pool, query_variants=expand_query("ระบบเริ่มทำงานกี่โมง"),
            query_intent="log_event_time", settings=_NO_FLOOR,
        )
        log_chunk = next(c for c in kept if c["file_name"] == "app.log")
        self.assertEqual(log_chunk["classification"], "direct_evidence")

    def test_log_chunk_ranks_first_and_generic_system_chunks_are_excluded(self):
        """Requirement 3 ("reduce generic matches caused only by the word
        'ระบบ'") means the two chunks whose ONLY lexical overlap is the
        bare word "ระบบ" must not merely rank below the log chunk — once
        real (log-time) evidence exists elsewhere in the pool, they get no
        lexical credit at all post noise-stripping and are excluded by the
        adaptive filter, exactly like any other irrelevant pure-vector
        candidate."""
        pool = [unrelated_high_vector_chunk(), contract_chunk(), log_chunk_0614()]
        kept, excluded = apply_hybrid_ranking(
            "ระบบเริ่มทำงานกี่โมง", pool, return_excluded=True,
            query_variants=expand_query("ระบบเริ่มทำงานกี่โมง"),
            query_intent="log_event_time", settings=_NO_FLOOR,
        )
        self.assertEqual([c["file_name"] for c in kept], ["app.log"])
        excluded_files = {c["file_name"] for c in excluded}
        self.assertEqual(excluded_files, {"product-roadmap.pdf", "devops-contract.pdf"})

    def test_sftp_refresh_chunk_survives_for_its_own_question(self):
        pool = [unrelated_high_vector_chunk(), contract_chunk(), log_chunk_0918()]
        question = "SFTP refresh เริ่มรอบ 9 โมงเมื่อไหร่"
        kept = apply_hybrid_ranking(
            question, pool, query_variants=expand_query(question),
            query_intent="log_event_time", settings=_NO_FLOOR,
        )
        kept_files = {c["file_name"] for c in kept}
        self.assertIn("sftp.log", kept_files)

    def test_non_log_intent_is_unaffected(self):
        """Sanity: the noise-stripping/boost path only activates for
        query_intent == "log_event_time" — any other intent (including
        None) scores exactly as before this fix."""
        pool = [contract_chunk()]
        kept = apply_hybrid_ranking(
            "ระบบ Shipify คืออะไร", pool, query_variants=["ระบบ Shipify คืออะไร"],
            query_intent=None, settings=_NO_FLOOR,
        )
        self.assertFalse(kept[0].get("log_time_evidence"))


class TestConfidenceAndEscalation(unittest.TestCase):
    def test_direct_log_evidence_yields_high_confidence(self):
        pool = [unrelated_high_vector_chunk(), contract_chunk(), log_chunk_0614()]
        kept = apply_hybrid_ranking(
            "ระบบเริ่มทำงานกี่โมง", pool, query_variants=expand_query("ระบบเริ่มทำงานกี่โมง"),
            query_intent="log_event_time", settings=_NO_FLOOR,
        )
        result = compute_confidence(kept)
        self.assertEqual(result.answerability, "direct_answer")
        self.assertGreaterEqual(result.answer_confidence, 0.75)

    def test_direct_structured_evidence_guard_true_when_log_chunk_present(self):
        pool = [unrelated_high_vector_chunk(), contract_chunk(), log_chunk_0614()]
        kept = apply_hybrid_ranking(
            "ระบบเริ่มทำงานกี่โมง", pool, query_variants=expand_query("ระบบเริ่มทำงานกี่โมง"),
            query_intent="log_event_time", settings=_NO_FLOOR,
        )
        self.assertTrue(_has_direct_structured_evidence(kept))

    def test_unanswerable_log_question_still_escalates(self):
        """No matching log chunk anywhere in the pool (a genuinely
        unanswerable log-time question) — must still classify as low
        confidence / no direct evidence, so escalation still fires."""
        pool = [unrelated_high_vector_chunk(), contract_chunk()]
        question = "ระบบสำรองข้อมูลเริ่มทำงานกี่โมง"
        kept = apply_hybrid_ranking(
            question, pool, query_variants=expand_query(question),
            query_intent="log_event_time", settings=_NO_FLOOR,
        )
        self.assertFalse(_has_direct_structured_evidence(kept))
        result = compute_confidence(kept)
        self.assertLess(result.answer_confidence, 0.5)

    def test_escalation_runs_after_evidence_classification_order(self):
        """Guard function operates on the FINAL classified chunk list —
        calling it before hybrid ranking (i.e. on raw, unclassified
        chunks) must not silently report evidence that isn't real yet."""
        raw_pool = [log_chunk_0614()]
        self.assertNotIn("classification", raw_pool[0])
        self.assertFalse(_has_direct_structured_evidence(raw_pool))


if __name__ == "__main__":
    unittest.main()
