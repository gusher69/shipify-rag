"""Regression tests for rag/query_expansion.py — the deterministic
Thai-English glossary expansion (Level 1) that gives keyword/heading
scoring a chance against cross-language chunks (the audit's confirmed
gap: no query translation/expansion existed before this)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag import query_expansion as qe


class TestNormalizeQuery(unittest.TestCase):
    def test_strips_trailing_punctuation_and_collapses_whitespace(self):
        self.assertEqual(qe.normalize_query("  What  is   Shipify's mission?  "), "What is Shipify's mission")


class TestExpandQuery(unittest.TestCase):
    def test_original_query_always_first(self):
        variants = qe.expand_query("พันธกิจของ Shipify คืออะไร")
        self.assertEqual(variants[0], qe.normalize_query("พันธกิจของ Shipify คืออะไร"))

    def test_thai_mission_expands_to_include_english_mission(self):
        variants = qe.expand_query("พันธกิจของ Shipify คืออะไร")
        joined = " ".join(variants).lower()
        self.assertIn("mission", joined)

    def test_thai_goal_expands_to_english_goal_terms(self):
        variants = qe.expand_query("เป้าหมายของ Shipify คืออะไร")
        joined = " ".join(variants).lower()
        self.assertTrue(any(t in joined for t in ("goal", "objective", "aim")))

    def test_preserves_entity_name_in_the_original_and_combined_variants(self):
        """The original query and the combined technical-terms+glossary
        variant always keep the entity name verbatim. Newer single-term
        synonym variants (e.g. bare "mission") are intentionally added
        WITHOUT the entity name — they exist specifically to match a
        short heading like "Our Mission" on its own, per the heading-
        matching fix; the entity is never lost since the original query
        (with "Shipify" intact) is always variants[0]."""
        variants = qe.expand_query("พันธกิจของ Shipify คืออะไร")
        self.assertIn("Shipify", variants[0])
        combined_variants = [v for v in variants if "Shipify" in v]
        self.assertTrue(combined_variants, "at least the technical-terms+glossary variant must keep the entity name")

    def test_preserves_package_names_verbatim(self):
        variants = qe.expand_query("google-api-python-client ใช้ version อะไร")
        for v in variants:
            self.assertIn("google-api-python-client", v)

    def test_does_not_translate_filenames_or_technical_terms(self):
        variants = qe.expand_query("Google Drive ต้องใช้ python version อะไร")
        # "Google", "Drive", "python" must never be replaced/removed from
        # any variant — expansion only ADDS terms, never substitutes.
        for v in variants:
            self.assertIn("Google", v)
            self.assertIn("Drive", v)

    def test_pure_english_query_with_no_glossary_hits_returns_only_original(self):
        variants = qe.expand_query("What is Shipify's mission?")
        # "mission" has no Thai counterpart to add (already English) —
        # expansion still shouldn't error or produce nonsense duplicates.
        self.assertEqual(variants[0], "What is Shipify's mission")

    def test_no_duplicate_variants(self):
        variants = qe.expand_query("พันธกิจของ Shipify คืออะไร")
        self.assertEqual(len(variants), len(set(v.lower() for v in variants)))


class TestLevel2QueryRewriteSafety(unittest.TestCase):
    def test_disabled_by_default_returns_empty_without_calling_llm(self):
        with patch("config.RAG_QUERY_REWRITE_ENABLED", False):
            result = qe.rewrite_query_with_llm("any question")
        self.assertEqual(result, [])

    def test_llm_failure_degrades_to_empty_not_raise(self):
        with patch("config.RAG_QUERY_REWRITE_ENABLED", True), \
             patch("services.llm_service.get_llm_service", side_effect=Exception("no api key")):
            result = qe.rewrite_query_with_llm("any question")
        self.assertEqual(result, [])


class TestAttributeHintsGeneric(unittest.TestCase):
    """Part 17 — the attribute-distinguishing vocabulary must be generic
    (runtime/package/API/OS/model/driver version), not a Google-Drive-
    or Shipify-specific list."""
    def test_attribute_hints_cover_generic_categories(self):
        self.assertIn("runtime_version", qe.ATTRIBUTE_HINTS)
        self.assertIn("package_version", qe.ATTRIBUTE_HINTS)
        self.assertIn("api_version", qe.ATTRIBUTE_HINTS)
        self.assertIn("os_version", qe.ATTRIBUTE_HINTS)
        self.assertIn("model_version", qe.ATTRIBUTE_HINTS)
        self.assertIn("driver_version", qe.ATTRIBUTE_HINTS)

    def test_no_hardcoded_entity_names_in_hints(self):
        flat = " ".join(" ".join(v) for v in qe.ATTRIBUTE_HINTS.values()).lower()
        self.assertNotIn("shipify", flat)
        self.assertNotIn("google drive", flat)


class TestNewSynonymTerms(unittest.TestCase):
    """The specific gap the reported bug traced to: "มิชชั่น" (a common
    transliteration) was entirely absent from the glossary — only the
    formal translation "พันธกิจ" was mapped, so the transliteration never
    expanded to "mission" at all."""

    def test_mission_transliteration_expands_to_mission(self):
        variants = qe.expand_query("แล้วมิชชั่น คือ อะไร")
        self.assertIn("mission", [v.lower() for v in variants])

    def test_mission_transliteration_cross_matches_formal_thai_synonym(self):
        """มิชชั่น <-> พันธกิจ <-> ภารกิจ are Thai-to-Thai synonyms of each
        other, independent of any English translation."""
        variants = qe.expand_query("แล้วมิชชั่น คือ อะไร")
        self.assertTrue(any("พันธกิจ" in v or "ภารกิจ" in v for v in variants))

    def test_vision_expands(self):
        variants = qe.expand_query("วิสัยทัศน์ของบริษัทคืออะไร")
        self.assertIn("vision", [v.lower() for v in variants] + [v.lower() for v in " ".join(variants).split()])

    def test_china_warehouse_expands(self):
        variants = qe.expand_query("โกดังจีนอยู่ที่ไหน")
        joined = " ".join(variants).lower()
        self.assertIn("china warehouse", joined)

    def test_line_expands(self):
        variants = qe.expand_query("ไลน์ติดต่อยังไง")
        joined = " ".join(variants)
        self.assertIn("LINE", joined)

    def test_explain_query_expansion_payload_shape(self):
        info = qe.explain_query_expansion("แล้วมิชชั่น คือ อะไร")
        self.assertEqual(info["original_query"], "แล้วมิชชั่น คือ อะไร")
        self.assertIn("mission", [v.lower() for v in info["expanded_queries"]])
        self.assertEqual(info["detected_language"], "Thai")

    def test_detect_query_language(self):
        self.assertEqual(qe.detect_query_language("What is Shipify's mission?"), "English")
        self.assertEqual(qe.detect_query_language("แล้วมิชชั่น คือ อะไร"), "Thai")
        self.assertEqual(qe.detect_query_language("Please contact Shipify support ที่ support@shipify-example.com"),
                         "Mixed Thai-English")


if __name__ == "__main__":
    unittest.main()
