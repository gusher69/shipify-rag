"""Regression tests for ingestion/ingest.py::_generate_section_suggested_question
— every chunk in a heading-based file used to get the SAME whole-file
suggested_questions list assigned regardless of which section it actually
was (so "Our Mission" displayed the file's generic first question, "What
is Shipify?"). Each section must now get its own, section-specific
question."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.ingest import _generate_section_suggested_question


class TestGenericSectionQuestionPatterns(unittest.TestCase):
    def test_mission_heading_generates_mission_question(self):
        q = _generate_section_suggested_question("Our Mission", "Shipify", [])
        self.assertIn("mission", q.lower())
        self.assertIn("Shipify", q)

    def test_services_heading_generates_services_question(self):
        q = _generate_section_suggested_question("Our Services", "Shipify", [])
        self.assertIn("services", q.lower())

    def test_contact_heading_generates_contact_question(self):
        q = _generate_section_suggested_question("Contact Us", "Shipify", [])
        self.assertIn("contact", q.lower())

    def test_not_hardcoded_to_shipify_specifically(self):
        q = _generate_section_suggested_question("Our Mission", "Acme Corp", [])
        self.assertIn("Acme Corp", q)
        self.assertIn("mission", q.lower())

    def test_prefers_existing_file_level_question_that_matches_heading(self):
        file_qs = ["What is Shipify?", "What is Shipify's mission?", "How can I contact Shipify?"]
        q = _generate_section_suggested_question("Our Mission", "Shipify", file_qs)
        self.assertEqual(q, "What is Shipify's mission?")

    def test_question_shaped_heading_used_verbatim(self):
        q = _generate_section_suggested_question("Question: What does the box look like?", "flexible-attachment-test", [])
        self.assertEqual(q, "What does the box look like?")

    def test_different_sections_get_different_questions(self):
        mission_q = _generate_section_suggested_question("Our Mission", "Shipify", [])
        services_q = _generate_section_suggested_question("Our Services", "Shipify", [])
        contact_q = _generate_section_suggested_question("Contact Us", "Shipify", [])
        self.assertNotEqual(mission_q, services_q)
        self.assertNotEqual(services_q, contact_q)
        self.assertNotEqual(mission_q, contact_q)


if __name__ == "__main__":
    unittest.main()
