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

    def test_missing_local_bytes_returns_404_not_a_crash(self):
        """A DB row can exist (synced elsewhere) while its bytes are
        genuinely absent on this machine's local disk — must fail safely
        with 404, never a 500."""
        row = {"id": "f2", "filename": "never_actually_on_disk.txt",
               "storage_provider": "local", "storage_path": "never_actually_on_disk.txt", "deleted_at": None}
        with patch("admin.routes.get_sb", return_value=self._mock_sb([row])):
            resp = self.client.get("/admin/api/files/f2/download")
        self.assertEqual(resp.status_code, 404)

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
