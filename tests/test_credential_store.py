"""Tests for the Credential Store (services/credential_store.py) —
encryption/decryption, lifecycle (create/rotate/disable/revoke/delete),
tenant isolation, metadata-only listing (never plaintext), and runtime
resolution. Uses the same _FakeSupabase mock as the rest of the
Business Action test suite — never a real DB. The master key is a
per-test generated Fernet key patched into config, never a real secret.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet

from tests.test_business_action_registry import _FakeSupabase
from services.credential_store import (
    CredentialStore, MasterKeyInvalidError, MasterKeyMissingError, mask_value,
)

TEST_KEY = Fernet.generate_key().decode()


def _store():
    return CredentialStore(_FakeSupabase())


class TestMasking(unittest.TestCase):
    def test_masks_all_but_last_four_chars(self):
        self.assertEqual(mask_value("16513268151"), "********8151")

    def test_short_value_still_masked(self):
        masked = mask_value("abc")
        self.assertNotEqual(masked, "abc")

    def test_empty_value(self):
        self.assertEqual(mask_value(""), "")


class TestEncryptionRoundTrip(unittest.TestCase):
    def test_create_then_resolve_returns_original_value(self):
        with patch("config.CREDENTIAL_ENCRYPTION_KEY", TEST_KEY):
            store = _store()
            store.create("default", "fasttrade_erp_secret", "FastTrade ERP SecretCode", "secret_code", "16513268151")
            outcome = store.resolve("default", "fasttrade_erp_secret")
            self.assertTrue(outcome["ok"])
            self.assertEqual(outcome["value"], "16513268151")

    def test_list_and_metadata_never_include_plaintext_or_ciphertext(self):
        with patch("config.CREDENTIAL_ENCRYPTION_KEY", TEST_KEY):
            store = _store()
            store.create("default", "erp_secret", "ERP Secret", "secret_code", "16513268151")
            listed = store.list_credentials("default")
            dumped = str(listed)
            self.assertNotIn("16513268151", dumped)
            self.assertNotIn("encrypted_value", listed[0])
            self.assertIn("masked_preview", listed[0])
            self.assertEqual(listed[0]["masked_preview"], "********8151")

    def test_wrong_master_key_fails_to_decrypt(self):
        with patch("config.CREDENTIAL_ENCRYPTION_KEY", TEST_KEY):
            store = _store()
            store.create("default", "erp_secret", "ERP Secret", "secret_code", "16513268151")
        other_key = Fernet.generate_key().decode()
        with patch("config.CREDENTIAL_ENCRYPTION_KEY", other_key):
            outcome = store.resolve("default", "erp_secret")
            self.assertFalse(outcome["ok"])
            self.assertEqual(outcome["error"], "encryption_key_invalid")
            self.assertIsNone(outcome["value"])

    def test_missing_master_key_blocks_resolution(self):
        with patch("config.CREDENTIAL_ENCRYPTION_KEY", TEST_KEY):
            store = _store()
            store.create("default", "erp_secret", "ERP Secret", "secret_code", "16513268151")
        with patch("config.CREDENTIAL_ENCRYPTION_KEY", None):
            outcome = store.resolve("default", "erp_secret")
            self.assertFalse(outcome["ok"])
            self.assertEqual(outcome["error"], "encryption_key_unavailable")

    def test_missing_master_key_blocks_creation(self):
        with patch("config.CREDENTIAL_ENCRYPTION_KEY", None):
            store = _store()
            with self.assertRaises(MasterKeyMissingError):
                store.create("default", "erp_secret", "ERP Secret", "secret_code", "16513268151")


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.patcher = patch("config.CREDENTIAL_ENCRYPTION_KEY", TEST_KEY)
        self.patcher.start()
        self.store = _store()

    def tearDown(self):
        self.patcher.stop()

    def test_create_rejects_invalid_credential_type(self):
        with self.assertRaises(ValueError):
            self.store.create("default", "x", "X", "not_a_type", "value")

    def test_create_rejects_duplicate_key_same_tenant(self):
        self.store.create("default", "erp_secret", "ERP Secret", "secret_code", "value1")
        with self.assertRaises(ValueError):
            self.store.create("default", "erp_secret", "ERP Secret 2", "secret_code", "value2")

    def test_get_or_create_is_idempotent(self):
        first, created1 = self.store.get_or_create("default", "erp_secret", "ERP Secret", "secret_code", "v1")
        second, created2 = self.store.get_or_create("default", "erp_secret", "ERP Secret", "secret_code", "v2")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first["id"], second["id"])
        # value from the SECOND call is never used since it already existed
        self.assertEqual(self.store.resolve("default", "erp_secret")["value"], "v1")

    def test_rotate_replaces_value_keeps_key(self):
        self.store.create("default", "erp_secret", "ERP Secret", "secret_code", "old_value")
        self.store.rotate("default", "erp_secret", "new_value", updated_by="admin")
        outcome = self.store.resolve("default", "erp_secret")
        self.assertEqual(outcome["value"], "new_value")
        meta = self.store.get_metadata("default", "erp_secret")
        self.assertIsNotNone(meta["rotated_at"])

    def test_business_action_never_needs_change_after_rotation(self):
        # The whole point: credential_key stays identical across rotation.
        self.store.create("default", "erp_secret", "ERP Secret", "secret_code", "v1")
        before_key = self.store.get_metadata("default", "erp_secret")["credential_key"]
        self.store.rotate("default", "erp_secret", "v2")
        after_key = self.store.get_metadata("default", "erp_secret")["credential_key"]
        self.assertEqual(before_key, after_key)

    def test_disabled_credential_cannot_resolve(self):
        self.store.create("default", "erp_secret", "ERP Secret", "secret_code", "v1")
        self.store.set_status("default", "erp_secret", "disabled")
        outcome = self.store.resolve("default", "erp_secret")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["error"], "credential_disabled")

    def test_revoked_credential_cannot_resolve(self):
        self.store.create("default", "erp_secret", "ERP Secret", "secret_code", "v1")
        self.store.set_status("default", "erp_secret", "revoked")
        outcome = self.store.resolve("default", "erp_secret")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["error"], "credential_revoked")

    def test_delete_blocked_when_in_use(self):
        self.store.create("default", "erp_secret", "ERP Secret", "secret_code", "v1")
        with self.assertRaises(ValueError):
            self.store.delete_if_unused("default", "erp_secret", in_use_checker=lambda k: True)

    def test_delete_succeeds_when_unused(self):
        self.store.create("default", "erp_secret", "ERP Secret", "secret_code", "v1")
        deleted = self.store.delete_if_unused("default", "erp_secret", in_use_checker=lambda k: False)
        self.assertTrue(deleted)
        self.assertIsNone(self.store.get_metadata("default", "erp_secret"))

    def test_test_reference_never_returns_value(self):
        self.store.create("default", "erp_secret", "ERP Secret", "secret_code", "16513268151")
        result = self.store.test_reference("default", "erp_secret")
        self.assertTrue(result["ok"])
        self.assertNotIn("16513268151", str(result))
        self.assertEqual(result["masked_preview"], "********8151")

    def test_resolve_unknown_credential(self):
        outcome = self.store.resolve("default", "does_not_exist")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["error"], "credential_not_found")


class TestTenantIsolation(unittest.TestCase):
    def setUp(self):
        self.patcher = patch("config.CREDENTIAL_ENCRYPTION_KEY", TEST_KEY)
        self.patcher.start()
        self.store = _store()  # shared fake DB, two tenants

    def tearDown(self):
        self.patcher.stop()

    def test_same_credential_key_isolated_by_tenant(self):
        self.store.create("tenant_a", "erp_secret", "A Secret", "secret_code", "value_for_a")
        self.store.create("tenant_b", "erp_secret", "B Secret", "secret_code", "value_for_b")
        a = self.store.resolve("tenant_a", "erp_secret")
        b = self.store.resolve("tenant_b", "erp_secret")
        self.assertEqual(a["value"], "value_for_a")
        self.assertEqual(b["value"], "value_for_b")

    def test_tenant_a_list_never_includes_tenant_b(self):
        self.store.create("tenant_a", "erp_secret", "A Secret", "secret_code", "value_for_a")
        self.store.create("tenant_b", "other_secret", "B Secret", "secret_code", "value_for_b")
        listed_a = self.store.list_credentials("tenant_a")
        self.assertEqual(len(listed_a), 1)
        self.assertEqual(listed_a[0]["credential_key"], "erp_secret")

    def test_tenant_a_cannot_resolve_tenant_b_credential(self):
        self.store.create("tenant_b", "only_in_b", "B Secret", "secret_code", "value_for_b")
        outcome = self.store.resolve("tenant_a", "only_in_b")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["error"], "credential_not_found")


class TestSharedCredentialAcrossActions(unittest.TestCase):
    def test_two_actions_can_share_one_credential_reference(self):
        with patch("config.CREDENTIAL_ENCRYPTION_KEY", TEST_KEY):
            store = _store()
            store.create("default", "fasttrade_erp_secret", "FastTrade ERP SecretCode", "secret_code", "16513268151")
            r1 = store.resolve("default", "fasttrade_erp_secret")
            r2 = store.resolve("default", "fasttrade_erp_secret")
            self.assertEqual(r1["value"], r2["value"])


if __name__ == "__main__":
    unittest.main()
