"""Route-level tests for the File Library "Download" action
(admin/routes.py::download_knowledge_file) — uses the real FastAPI app
with a mocked Supabase client, never a real DB. Exercises the same
_ensure_local_file resolver the sync pipeline already uses, so this
proves the new route wires into that existing, proven logic correctly
rather than reimplementing it."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient


class TestFileLibraryDownloadRoute(unittest.TestCase):
    def setUp(self):
        from admin.routes import app, KNOWLEDGE_DIR
        self.client = TestClient(app)
        self.client.post("/admin/login", data={"username": "admin", "password": "shipify2026"})
        self.knowledge_dir = KNOWLEDGE_DIR
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self.test_path = self.knowledge_dir / "test_download_route_file.txt"
        self.test_path.write_text("hello from download route test", encoding="utf-8")

    def tearDown(self):
        if self.test_path.exists():
            self.test_path.unlink()

    def _mock_sb(self, rows):
        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.eq.return_value.is_.return_value.execute.return_value.data = rows
        return mock_sb

    def test_download_returns_file_content_with_attachment_disposition(self):
        row = {"id": "f1", "filename": "test_download_route_file.txt",
               "storage_provider": "local", "storage_path": "test_download_route_file.txt", "deleted_at": None}
        with patch("admin.routes.get_sb", return_value=self._mock_sb([row])):
            resp = self.client.get("/admin/api/files/f1/download")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers.get("content-disposition", ""))
        self.assertIn("test_download_route_file.txt", resp.headers.get("content-disposition", ""))
        self.assertEqual(resp.text, "hello from download route test")

    def test_unknown_file_id_returns_404(self):
        with patch("admin.routes.get_sb", return_value=self._mock_sb([])):
            resp = self.client.get("/admin/api/files/does-not-exist/download")
        self.assertEqual(resp.status_code, 404)

    def test_malformed_id_fails_as_404_not_500(self):
        """A non-UUID file_id makes PostgREST reject the id filter with a
        type-cast error (confirmed live against the deployed route,
        2026-08-18: a single-segment non-UUID value like "nonexistent-id"
        surfaced as an uncaught 500) — must degrade to an ordinary 404,
        never an uncaught 500 that could leak internals in a differently-
        configured environment."""
        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.eq.return_value.is_.return_value.execute.side_effect = \
            Exception("invalid input syntax for type uuid")
        with patch("admin.routes.get_sb", return_value=mock_sb):
            resp = self.client.get("/admin/api/files/not-a-valid-uuid/download")
        self.assertEqual(resp.status_code, 404)

    def test_missing_local_bytes_returns_natural_message_no_path_leak(self):
        """A DB row can exist (synced elsewhere, or a pre-persistent-
        storage legacy row) while its bytes are genuinely absent on this
        machine's local disk — must fail safely with 404 and a natural
        Thai message, never a 500, never a raw filesystem path."""
        row = {"id": "f2", "filename": "never_actually_on_disk.txt",
               "storage_provider": "local", "storage_path": "never_actually_on_disk.txt", "deleted_at": None}
        with patch("admin.routes.get_sb", return_value=self._mock_sb([row])):
            resp = self.client.get("/admin/api/files/f2/download")
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertEqual(body["detail"]["reason"], "source_file_missing")
        self.assertIn("ไม่พบไฟล์ต้นฉบับ", body["detail"]["message"])
        dumped = str(body)
        self.assertNotIn(str(self.knowledge_dir.resolve()), dumped)
        self.assertNotIn("/app/", dumped)
        self.assertNotIn("C:\\", dumped)

    def test_path_traversal_via_filename_is_blocked(self):
        """A malicious/corrupted filename value in the DB row (e.g. from
        an upload-time gap elsewhere, or direct DB tampering) must never
        let this route walk outside KNOWLEDGE_DIR. Uses a real file that
        genuinely exists just outside KNOWLEDGE_DIR's parent to prove the
        guard rejects it even when the traversal target is real."""
        import os
        outside_dir = self.knowledge_dir.parent
        secret_path = outside_dir / "secret_outside_knowledge_dir.txt"
        secret_path.write_text("should never be served", encoding="utf-8")
        try:
            traversal_name = "../secret_outside_knowledge_dir.txt"
            row = {"id": "f3", "filename": traversal_name,
                   "storage_provider": "local", "storage_path": traversal_name, "deleted_at": None}
            with patch("admin.routes.get_sb", return_value=self._mock_sb([row])):
                resp = self.client.get("/admin/api/files/f3/download")
            self.assertEqual(resp.status_code, 404)
            self.assertNotIn("should never be served", resp.text)
        finally:
            secret_path.unlink(missing_ok=True)

    def test_requires_authentication(self):
        anon_client = TestClient(from_app_module())
        row = {"id": "f1", "filename": "test_download_route_file.txt",
               "storage_provider": "local", "storage_path": "test_download_route_file.txt", "deleted_at": None}
        with patch("admin.routes.get_sb", return_value=self._mock_sb([row])):
            resp = anon_client.get("/admin/api/files/f1/download", follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))


def from_app_module():
    from admin.routes import app
    return app


if __name__ == "__main__":
    unittest.main()
