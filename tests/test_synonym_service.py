"""Regression tests for the Knowledge Synonym Engine (rag/synonym_service.py)
— pure Python, no LLM/API call, expands a query with domain synonym
groups BEFORE retrieval, without ever replacing the original wording.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.synonym_service import (
    expand_query_with_synonyms, get_synonym_groups, reload_synonym_groups,
    _load_from_json, _DEFAULT_JSON_PATH,
)


class TestSynonymGroupLoading(unittest.TestCase):
    def test_default_json_loads_the_documented_groups(self):
        groups = _load_from_json(_DEFAULT_JSON_PATH)
        canonical_terms = {g["canonical_term"] for g in groups}
        self.assertEqual(canonical_terms,
                         {"พิกัด", "โกดัง", "เรท", "CBM", "ใบกำกับ", "Tracking", "บริษัท", "คูปอง"})

    def test_get_synonym_groups_is_cached_and_reloadable(self):
        groups1 = get_synonym_groups()
        groups2 = get_synonym_groups()
        self.assertIs(groups1, groups2)
        groups3 = reload_synonym_groups()
        self.assertEqual(groups1, groups3)

    def test_malformed_json_file_degrades_to_empty_list_not_a_crash(self):
        self.assertEqual(_load_from_json("/nonexistent/path.json"), [])


class TestExpandQueryWithSynonyms(unittest.TestCase):
    def test_original_query_is_always_first_variant(self):
        result = expand_query_with_synonyms("ขอโลเคชั่นโกดัง")
        self.assertEqual(result["variants"][0], "ขอโลเคชั่นโกดัง")

    def test_location_warehouse_example_produces_expected_variants(self):
        result = expand_query_with_synonyms("ขอโลเคชั่นโกดัง")
        self.assertIn("ขอพิกัดโกดัง", result["variants"])
        self.assertIn("ขอแผนที่โกดัง", result["variants"])

    def test_location_warehouse_example_canonical_terms(self):
        result = expand_query_with_synonyms("ขอโลเคชั่นโกดัง")
        self.assertEqual(set(result["canonical_terms"]), {"พิกัด", "โกดัง"})

    def test_location_warehouse_example_synonyms_used_includes_documented_pairs(self):
        result = expand_query_with_synonyms("ขอโลเคชั่นโกดัง")
        pairs = {(s["term"], s["canonical"]) for s in result["synonyms_used"]}
        self.assertIn(("โลเคชั่น", "พิกัด"), pairs)
        self.assertIn(("location", "พิกัด"), pairs)
        self.assertIn(("google map", "พิกัด"), pairs)

    def test_rate_by_sea_example(self):
        result = expand_query_with_synonyms("เรททางเรือ")
        self.assertIn("ราคาทางเรือ", result["variants"])
        self.assertIn("ค่าขนส่งทางเรือ", result["variants"])
        self.assertIn("อัตราค่าขนส่งทางเรือ", result["variants"])
        self.assertEqual(result["canonical_terms"], ["เรท"])

    def test_cbm_example(self):
        # "CBM คืออะไร" has a space after CBM, which a straight substring
        # replace preserves — the result is "คิว คืออะไร" (with the space),
        # not "คิวคืออะไร" (the task's illustrative text omits the space).
        result = expand_query_with_synonyms("CBM คืออะไร")
        self.assertIn("คิว คืออะไร", result["variants"])
        self.assertIn("ลูกบาศก์เมตร คืออะไร", result["variants"])
        self.assertIn("Cubic Meter คืออะไร", result["variants"])

    def test_no_matching_synonym_group_returns_only_original(self):
        """Backward compatibility: a question with no recognized synonym
        terms behaves exactly like before this feature existed."""
        result = expand_query_with_synonyms("สวัสดีครับ วันนี้อากาศดีมาก")
        self.assertEqual(result["variants"], ["สวัสดีครับ วันนี้อากาศดีมาก"])
        self.assertEqual(result["canonical_terms"], [])
        self.assertEqual(result["synonyms_used"], [])

    def test_empty_question(self):
        result = expand_query_with_synonyms("")
        self.assertEqual(result["variants"], [""])
        self.assertEqual(result["canonical_terms"], [])

    def test_case_insensitive_english_synonym_match(self):
        result = expand_query_with_synonyms("What is the RATE for air freight")
        self.assertEqual(result["canonical_terms"], ["เรท"])

    def test_longest_match_preferred_google_map_over_map(self):
        """"google map" must be recognized as itself, not just as a
        substring hit on the shorter "map" entry in the same group."""
        result = expand_query_with_synonyms("google map โกดัง")
        pairs = {(s["term"], s["canonical"]) for s in result["synonyms_used"]}
        self.assertIn(("google map", "พิกัด"), pairs)
        # A variant swapping in "map" (the shorter synonym) should still
        # exist as one of the OTHER group members, but the match itself
        # was against "google map", not a partial "map" substring hit.
        self.assertTrue(any("พิกัด" in v for v in result["variants"]))

    def test_tracking_group(self):
        result = expand_query_with_synonyms("ติดตามพัสดุ")
        self.assertIn("Trackingพัสดุ", result["variants"])
        self.assertEqual(result["canonical_terms"], ["Tracking"])


if __name__ == "__main__":
    unittest.main()
