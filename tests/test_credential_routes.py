"""Route-level tests for Credential admin endpoints (admin/routes.py) —
uses the real FastAPI app with a mocked Supabase-backed registry
(_FakeSupabase). Never a real DB, never a real network call. The
master key is a per-test generated Fernet key, never a real secret."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from tests.test_business_action_registry import _FakeSupabase
from tests._admin_test_auth import login_as_test_admin

TEST_KEY = Fernet.generate_key().decode()


class TestCredentialRoutes(unittest.TestCase):
    def setUp(self):
        from admin.routes import app
        self.client = TestClient(app)
        login_as_test_admin(self.client)
        self.fake_sb = _FakeSupabase()
        self.patcher_sb = patch("admin.routes.get_sb", return_value=self.fake_sb)
        self.patcher_sb.start()
        self.patcher_key = patch("config.CREDENTIAL_ENCRYPTION_KEY", TEST_KEY)
        self.patcher_key.start()

    def tearDown(self):
        self.patcher_sb.stop()
        self.patcher_key.stop()

    def test_create_credential_returns_no_plaintext(self):
        resp = self.client.post("/admin/api/credentials", json={
            "credential_key": "fasttrade_erp_secret", "display_name": "FastTrade ERP SecretCode",
            "credential_type": "secret_code", "value": "16513268151",
        })
        data = resp.json()
        self.assertTrue(data["ok"])
        dumped = str(data)
        self.assertNotIn("16513268151", dumped)
        self.assertEqual(data["credential"]["masked_preview"], "********8151")
        self.assertNotIn("encrypted_value", data["credential"])

    def test_list_credentials_never_includes_plaintext_or_ciphertext(self):
        self.client.post("/admin/api/credentials", json={
            "credential_key": "erp_secret", "display_name": "ERP Secret",
            "credential_type": "secret_code", "value": "16513268151",
        })
        resp = self.client.get("/admin/api/credentials")
        data = resp.json()
        dumped = str(data)
        self.assertNotIn("16513268151", dumped)
        self.assertNotIn("encrypted_value", dumped)
        self.assertIn("masked_preview", data["credentials"][0])

    def test_no_endpoint_returns_decrypted_value(self):
        """There must be no admin endpoint anywhere that returns a
        decrypted credential value — spot-check the ones that exist."""
        self.client.post("/admin/api/credentials", json={
            "credential_key": "erp_secret", "display_name": "ERP Secret",
            "credential_type": "secret_code", "value": "16513268151",
        })
        for resp in (
            self.client.get("/admin/api/credentials"),
            self.client.post("/admin/api/credentials/erp_secret/test"),
        ):
            self.assertNotIn("16513268151", resp.text)

    def test_rotate_credential_keeps_key_changes_value(self):
        self.client.post("/admin/api/credentials", json={
            "credential_key": "erp_secret", "display_name": "ERP Secret",
            "credential_type": "secret_code", "value": "old_value",
        })
        resp = self.client.post("/admin/api/credentials/erp_secret/rotate", json={"value": "new_value"})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["credential"]["credential_key"], "erp_secret")
        self.assertIsNotNone(data["credential"]["rotated_at"])

    def test_disable_then_test_reference_fails(self):
        self.client.post("/admin/api/credentials", json={
            "credential_key": "erp_secret", "display_name": "ERP Secret",
            "credential_type": "secret_code", "value": "v1",
        })
        self.client.post("/admin/api/credentials/erp_secret/disable")
        resp = self.client.post("/admin/api/credentials/erp_secret/test")
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "credential_disabled")

    def test_revoke_credential(self):
        self.client.post("/admin/api/credentials", json={
            "credential_key": "erp_secret", "display_name": "ERP Secret",
            "credential_type": "secret_code", "value": "v1",
        })
        resp = self.client.post("/admin/api/credentials/erp_secret/revoke")
        self.assertEqual(resp.json()["credential"]["status"], "revoked")

    def test_delete_blocked_when_referenced_by_business_action(self):
        from services.business_action_registry import BusinessActionRegistry
        reg = BusinessActionRegistry(self.fake_sb)
        self.client.post("/admin/api/credentials", json={
            "credential_key": "erp_secret", "display_name": "ERP Secret",
            "credential_type": "secret_code", "value": "v1",
        })
        action = reg.create({"action_key": "uses_cred", "name": "Uses Cred", "action_type": "API"})
        reg.replace_parameters(action["id"], [
            {"name": "SecretCode", "required": True, "input_source": "credential_store", "credential_ref": "erp_secret"},
        ])
        resp = self.client.delete("/admin/api/credentials/erp_secret")
        self.assertEqual(resp.status_code, 409)

    def test_delete_succeeds_when_unreferenced(self):
        self.client.post("/admin/api/credentials", json={
            "credential_key": "erp_secret", "display_name": "ERP Secret",
            "credential_type": "secret_code", "value": "v1",
        })
        resp = self.client.delete("/admin/api/credentials/erp_secret")
        self.assertTrue(resp.json()["ok"])

    def test_missing_master_key_blocks_create(self):
        with patch("config.CREDENTIAL_ENCRYPTION_KEY", None):
            resp = self.client.post("/admin/api/credentials", json={
                "credential_key": "erp_secret", "display_name": "ERP Secret",
                "credential_type": "secret_code", "value": "v1",
            })
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])


if __name__ == "__main__":
    unittest.main()
