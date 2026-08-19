"""Regression test for GET / on the Admin FastAPI app (admin/routes.py)
-- the app has no content of its own at the bare root, only /admin/*
routes; without an explicit handler this 404s with a raw
{"detail":"Not Found"} when the public domain is hit directly. Uses the
real FastAPI app via TestClient, never a real DB/network call.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient

from tests._admin_test_auth import login_as_test_admin


class TestRootRedirect(unittest.TestCase):
    def setUp(self):
        from admin.routes import app
        self.app = app

    def test_unauthenticated_root_redirects_to_login(self):
        client = TestClient(self.app)
        resp = client.get("/", follow_redirects=False)
        self.assertIn(resp.status_code, (302, 307))
        self.assertEqual(resp.headers["location"], "/admin/login")

    def test_authenticated_root_redirects_to_dashboard(self):
        client = TestClient(self.app)
        login_as_test_admin(client)
        resp = client.get("/", follow_redirects=False)
        self.assertIn(resp.status_code, (302, 307))
        self.assertEqual(resp.headers["location"], "/admin/dashboard")

    def test_root_never_returns_raw_404(self):
        client = TestClient(self.app)
        resp = client.get("/", follow_redirects=False)
        self.assertNotEqual(resp.status_code, 404)

    def test_existing_admin_login_route_unaffected(self):
        client = TestClient(self.app)
        resp = client.get("/admin/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
