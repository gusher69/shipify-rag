"""Regression tests for strict-grounding prompt rules
(services/prompt_builder.py's STRICT_GROUNDING_RULES) — the fix that
stops the LLM from filling a gap in retrieved context with invented
general/external knowledge (e.g. a fabricated Python runtime version when
the context only lists package version constraints).
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.prompt_builder as pb


class TestStrictGroundingDefault(unittest.TestCase):
    def test_strict_mode_is_the_default(self):
        self.assertEqual(pb.RAG_GROUNDING_MODE, "strict")

    def test_build_prompt_includes_grounding_rules_in_strict_mode(self):
        template = pb.PromptTemplate(id="t1", name="Test", version="1", system_prompt="Base prompt.")
        built = pb.build_prompt(
            "Google Drive ต้องใช้ python version อะไร",
            "# Google Drive\ngoogle-api-python-client>=2.100.0\ngoogle-auth>=2.20.0",
            template=template,
        )
        self.assertIn("STRICT GROUNDING RULES", built.final_prompt_text)
        self.assertIn("Do not infer or invent a version", built.final_prompt_text)

    def test_grounding_rules_forbid_hedging_phrases(self):
        self.assertIn("โดยทั่วไป", pb.STRICT_GROUNDING_RULES)
        self.assertIn("ควรใช้", pb.STRICT_GROUNDING_RULES)

    def test_grounding_rules_distinguish_related_attributes_generically(self):
        # Generic language, not a Google-Drive/Python special case.
        text = pb.STRICT_GROUNDING_RULES.lower()
        self.assertIn("runtime", text)
        self.assertIn("package", text)
        self.assertIn("different attribute", text)

    def test_disabling_strict_mode_removes_the_rules_block(self):
        template = pb.PromptTemplate(id="t1", name="Test", version="1", system_prompt="Base prompt.")
        with patch("services.prompt_builder.RAG_GROUNDING_MODE", "off"):
            built = pb.build_prompt("some question", "some context", template=template)
        self.assertNotIn("STRICT GROUNDING RULES", built.final_prompt_text)


class TestStrictGroundingForbidsIndustryFiller(unittest.TestCase):
    """Strict Shipify RAG Grounding (2026-08-27, RAG-042 hard regression)
    -- confirmed live: even with RAG-042's genuine content correctly
    retrieved and placed in Context, the LLM still completed the answer
    with generic industry-standard import steps (supplier vetting,
    contract negotiation, invoices/certificates, customs clearance, HS
    codes, import duties/taxes) that are not written in RAG-042 at all.
    The pre-existing STRICT_GROUNDING_RULES (focused on "don't substitute
    a different but related attribute", e.g. runtime vs package version)
    did not cover "don't complete an apparently missing PROCESS STEP
    using outside/industry knowledge" -- this extends the SAME rules
    block with that explicit case, reusing the task's own required
    wording."""

    def test_grounding_rules_forbid_completing_missing_process_steps(self):
        text = pb.STRICT_GROUNDING_RULES
        self.assertIn("PROCESS/PROCEDURE", text)
        self.assertIn("never complete an apparently missing step", text)

    def test_grounding_rules_name_the_forbidden_rag_042_categories(self):
        """The exact forbidden-output categories from the task's own
        Section 6 (customs/HS-code/import-license style additions)."""
        text = pb.STRICT_GROUNDING_RULES
        for term in ("customs clearance", "duties/taxes", "HS codes", "import licenses"):
            self.assertIn(term, text)

    def test_build_prompt_carries_the_process_rule_through(self):
        template = pb.PromptTemplate(id="t1", name="Test", version="1", system_prompt="Base prompt.")
        built = pb.build_prompt("ขั้นตอนการนำเข้าสินค้าจากจีนเข้าไทยมีอะไรบ้าง",
                                 "Question: ขั้นตอนการนำเข้าสินค้าจากจีนเข้าไทยทำอย่างไร\n"
                                 "Answer: 1. คัดลอกที่อยู่โกดังจีน... 2. ร้านจีนจัดส่งไปโกดังจีน...",
                                 template=template)
        self.assertIn("never complete an apparently missing step", built.final_prompt_text)


if __name__ == "__main__":
    unittest.main()
