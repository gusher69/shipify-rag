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


if __name__ == "__main__":
    unittest.main()
