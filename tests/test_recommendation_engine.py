import os
import unittest

os.environ.setdefault("ENABLE_KNOWLEDGE_ANALYZER_LLM", "false")
os.environ.setdefault("ENABLE_KNOWLEDGE_GRAPH", "false")

from services.recommendation_engine import (
    analyze_characteristics, recommend_profile, generate_warnings, get_recommendation_engine,
)


def qa_pages(n):
    return [{"is_qa_item": True, "text": f"Q{i}: question? A{i}: answer."} for i in range(n)]


def doc_pages(text, page_count=1):
    return [{"page_number": i, "text": text} for i in range(page_count)]


class TestCharacteristics(unittest.TestCase):
    def test_qa_workbook_detected(self):
        chars = analyze_characteristics(qa_pages(3), "faq.xlsx")
        self.assertTrue(chars.is_qa_workbook)
        self.assertEqual(chars.qa_row_count, 3)
        self.assertEqual(chars.likely_doc_type, "FAQ")

    def test_document_sections_counted(self):
        text = "# Company Profile\n\nWe are Shipify.\n\n## Services\n\nWe ship things.\n\n## Locations\n\nChina."
        chars = analyze_characteristics(doc_pages(text), "profile.md")
        self.assertGreaterEqual(chars.section_count, 2)
        self.assertFalse(chars.is_qa_workbook)


class TestWarnings(unittest.TestCase):
    def test_few_faq_rows_warns(self):
        chars = analyze_characteristics(qa_pages(3), "small.xlsx")
        warnings = generate_warnings(chars)
        self.assertTrue(any("only 3 FAQ row" in w for w in warnings))

    def test_many_pages_warns(self):
        chars = analyze_characteristics(doc_pages("word " * 500, page_count=250), "big.pdf")
        warnings = generate_warnings(chars)
        self.assertTrue(any("250 pages" in w for w in warnings))

    def test_many_attachments_warns(self):
        chars = analyze_characteristics(doc_pages("hello world " * 50), "doc.pdf", attachment_count=10)
        warnings = generate_warnings(chars)
        self.assertTrue(any("images" in w for w in warnings))


class TestRecommendation(unittest.TestCase):
    def test_small_faq_excel_recommends_basic(self):
        chars = analyze_characteristics(qa_pages(3), "faq.xlsx")
        rec = recommend_profile(chars)
        self.assertEqual(rec.profile, "basic")
        self.assertTrue(any("FAQ" in r for r in rec.reason))

    def test_company_profile_recommends_advanced(self):
        text = ("# Company Profile\n\n" + ("Shipify is a logistics company. " * 60) +
                "\n\n## Services\n\n" + ("We provide shipping. " * 60) +
                "\n\n## Locations\n\n" + ("We operate a warehouse. " * 60))
        chars = analyze_characteristics(doc_pages(text), "company.md")
        chars.likely_doc_type = "Company Profile"
        rec = recommend_profile(chars)
        self.assertEqual(rec.profile, "advanced")

    def test_financial_report_recommends_standard(self):
        chars = analyze_characteristics(doc_pages("revenue " * 200), "financials.xlsx")
        chars.likely_doc_type = "Financial Report"
        rec = recommend_profile(chars)
        self.assertEqual(rec.profile, "standard")

    def test_sop_recommends_advanced(self):
        chars = analyze_characteristics(doc_pages("step " * 200), "sop.md")
        chars.likely_doc_type = "SOP"
        rec = recommend_profile(chars)
        self.assertEqual(rec.profile, "advanced")

    def test_empty_document_recommends_disabled(self):
        chars = analyze_characteristics(doc_pages("hi"), "empty.txt")
        rec = recommend_profile(chars)
        self.assertEqual(rec.profile, "disabled")

    def test_engine_singleton_end_to_end(self):
        engine = get_recommendation_engine()
        chars = engine.analyze(qa_pages(2), "faq.xlsx")
        rec = engine.recommend(chars)
        self.assertIn(rec.profile, ("disabled", "basic", "standard", "advanced"))


if __name__ == "__main__":
    unittest.main()
