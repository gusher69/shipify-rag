import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.document_purpose import classify_document_purpose, detect_table_semantic_labels


class TestClassifyDocumentPurpose(unittest.TestCase):
    def test_dense_numeric_rate_table_classified_as_premium_monthly(self):
        # Real shape of the reported production case: filename says
        # "Monthly", sampled rows are almost pure digits with no
        # "premium"/"เบี้ย" vocabulary anywhere near them.
        sample = "\n".join([
            "| 15 days - 25 | 1,420 | 1,554 | 1,729 | 1,848 |",
            "| 26 - 30 | 1,426 | 1,661 | 1,847 | 1,974 |",
            "| 31 - 35 | 1,440 | 1,749 | 1,886 | 2,016 |",
        ])
        purpose = classify_document_purpose(
            filename="Allianz - Smarter Health-Monthly eff.01.03.2025.pdf",
            headings=["Hospitalization with OPD 1,000 (Monthly)"],
            sample_text=sample,
        )
        self.assertEqual(purpose, "premium_monthly")

    def test_annual_rate_table_classified_as_premium_annual(self):
        sample = "| 46 - 50 | 29,938 | 35,116 | 39,145 | 41,555 |"
        purpose = classify_document_purpose(
            filename="Allianz - Smarter Health- Annual eff.01.03.2025.pdf",
            headings=["Hospitalization with OPD 1,000 (Annual)"],
            sample_text=sample,
        )
        self.assertEqual(purpose, "premium_annual")

    def test_benefit_brochure_not_misclassified_by_incidental_premium_mention(self):
        # Real reported false-positive: a brochure that briefly mentions
        # "you can pay your premium monthly" in marketing copy must NOT
        # flip the whole file to premium_monthly — coverage vocabulary
        # elsewhere in the document dominates.
        sample = "\n".join([
            "ความคุ้มครองค่ารักษาพยาบาล ค่าห้องผู้ป่วยปกติ การรักษาในห้องผู้ป่วยวิกฤต ICU",
            "ผลประโยชน์สูงสุดต่อรอบปีกรมธรรม์ประกันภัย",
            "สามารถชําระค่าเบี้ยประกันภัยเป็นรายเดือน",
        ])
        purpose = classify_document_purpose(
            filename="Allianz - Brochure_Smart Health TH - A5_eff.01.03.2025.pdf",
            headings=["Smarter Health", "Allianz AYUDHYA"],
            sample_text=sample,
        )
        self.assertEqual(purpose, "coverage_brochure")

    def test_faq_qa_chunk_strategy_wins_outright(self):
        purpose = classify_document_purpose(filename="faq.xlsx", sample_text="", chunk_strategy="faq_qa")
        self.assertEqual(purpose, "faq")

    def test_no_signal_falls_back_to_general(self):
        purpose = classify_document_purpose(filename="random.txt", sample_text="just some unrelated prose")
        self.assertEqual(purpose, "general")

    def test_policy_wording_classified_as_policy(self):
        sample = "เงื่อนไขทั่วไป ข้อยกเว้น นิยาม policy wording terms and conditions"
        purpose = classify_document_purpose(filename="terms.pdf", sample_text=sample)
        self.assertEqual(purpose, "policy")


class TestDetectTableSemanticLabels(unittest.TestCase):
    def test_coverage_text_gets_coverage_label(self):
        labels = detect_table_semantic_labels("ค่าห้องผู้ป่วยปกติ ICU ผลประโยชน์")
        self.assertIn("coverage", labels)
        self.assertNotIn("premium", labels)

    def test_premium_text_gets_premium_label(self):
        labels = detect_table_semantic_labels("เบี้ยประกันภัยรายเดือน 1,420 บาท")
        self.assertIn("premium", labels)

    def test_empty_text_returns_no_labels(self):
        self.assertEqual(detect_table_semantic_labels(""), [])


if __name__ == "__main__":
    unittest.main()
