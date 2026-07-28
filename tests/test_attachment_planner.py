"""Regression tests for services/attachment_planner.py — decides
whether/which/how-many attachments to send and in what order, from
attachments already resolved onto the FINAL retrieved evidence chunks.
Never fetches attachments itself, never invents a URL.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.attachment_planner import plan_attachments, DEFAULT_MAX_IMAGES, DEFAULT_MAX_DOCUMENTS


def _chunk(chunk_id, attachments):
    return {"chunk_id": chunk_id, "attachments": attachments}


MAP_CHUNK = _chunk("c1", [
    {"public_url": "https://example.com/map1.jpg", "filename": "map1.jpg", "mime_type": "image/jpeg"},
    {"public_url": "https://example.com/map2.jpg", "filename": "map2.jpg", "mime_type": "image/jpeg"},
])


class TestBasicSelection(unittest.TestCase):
    def test_no_attachments_available(self):
        r = plan_attachments("warehouse_map", ["map_url"], {}, [])
        self.assertFalse(r["should_send"])
        self.assertEqual(r["selected_attachments"], [])

    def test_selects_from_retrieved_evidence_only(self):
        r = plan_attachments("warehouse_map", ["map_url"], {"response_shape": "answer_then_links"}, [MAP_CHUNK])
        self.assertTrue(r["should_send"])
        urls = {a["url"] for a in r["selected_attachments"]}
        self.assertEqual(urls, {"https://example.com/map1.jpg", "https://example.com/map2.jpg"})

    def test_never_invents_a_url(self):
        r = plan_attachments("warehouse_map", ["map_url"], {}, [MAP_CHUNK])
        for a in r["selected_attachments"]:
            self.assertTrue(a["url"].startswith("https://example.com/"))

    def test_selection_reason_for_map_request(self):
        r = plan_attachments("warehouse_map", ["map_url"], {}, [MAP_CHUNK])
        self.assertIn("map", r["selection_reason"].lower())


class TestDeduplication(unittest.TestCase):
    def test_duplicate_urls_across_chunks_are_never_sent_twice(self):
        chunk2 = _chunk("c2", [{"public_url": "https://example.com/map1.jpg", "filename": "map1.jpg", "mime_type": "image/jpeg"}])
        r = plan_attachments("warehouse_map", ["map_url"], {}, [MAP_CHUNK, chunk2])
        urls = [a["url"] for a in r["selected_attachments"]]
        self.assertEqual(len(urls), len(set(urls)))


class TestCaps(unittest.TestCase):
    def test_respects_max_images_default_cap(self):
        many_images = _chunk("c1", [
            {"public_url": f"https://example.com/{i}.jpg", "filename": f"{i}.jpg", "mime_type": "image/jpeg"}
            for i in range(5)
        ])
        r = plan_attachments("attachment_request", ["attachment"], {}, [many_images])
        images = [a for a in r["selected_attachments"] if a["type"] == "image"]
        self.assertLessEqual(len(images), DEFAULT_MAX_IMAGES)
        self.assertTrue(r["omitted_attachments"])

    def test_respects_max_documents_default_cap(self):
        many_docs = _chunk("c1", [
            {"public_url": f"https://example.com/{i}.pdf", "filename": f"{i}.pdf", "mime_type": "application/pdf"}
            for i in range(3)
        ])
        r = plan_attachments("attachment_request", ["attachment"], {}, [many_docs])
        docs = [a for a in r["selected_attachments"] if a["type"] == "document"]
        self.assertLessEqual(len(docs), DEFAULT_MAX_DOCUMENTS)


class TestPolicyRespect(unittest.TestCase):
    def test_send_image_if_available_false_blocks_images(self):
        policy_set = {"config": {"attachment_rules": {"send_image_if_available": False, "send_file_link_if_available": True}}}
        r = plan_attachments("warehouse_map", ["map_url"], {}, [MAP_CHUNK], policy_set=policy_set)
        self.assertFalse(r["should_send"])

    def test_send_file_link_if_available_false_blocks_documents(self):
        doc_chunk = _chunk("c1", [{"public_url": "https://example.com/f.pdf", "filename": "f.pdf", "mime_type": "application/pdf"}])
        policy_set = {"config": {"attachment_rules": {"send_image_if_available": True, "send_file_link_if_available": False}}}
        r = plan_attachments("attachment_request", ["attachment"], {}, [doc_chunk], policy_set=policy_set)
        self.assertFalse(r["should_send"])


class TestOrdering(unittest.TestCase):
    def test_default_order_is_text_attachment_followup(self):
        r = plan_attachments("warehouse_map", ["map_url"], {"response_shape": "answer_then_links"}, [MAP_CHUNK])
        self.assertEqual(r["attachment_order"], ["text_intro", "attachment", "text_followup"])

    def test_attachment_only_shape_is_attachment_alone(self):
        r = plan_attachments("attachment_request", ["attachment"], {"response_shape": "attachment_only"}, [MAP_CHUNK])
        self.assertEqual(r["attachment_order"], ["attachment"])


class TestNoRelevantAttachment(unittest.TestCase):
    def test_no_evidence_no_claim(self):
        """If no relevant attachment exists, the planner just says
        should_send=False — never claims one exists."""
        r = plan_attachments("shipping_rate", ["rate_per_kg"], {}, [])
        self.assertFalse(r["should_send"])


if __name__ == "__main__":
    unittest.main()
