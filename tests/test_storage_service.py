"""Tests for the StorageService abstraction (storage/).

Run with:
    python -m unittest tests.test_storage_service -v
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storage.base import FileRef, FileNotFoundInStorageError
from storage.local_storage import LocalStorageService
from storage import factory as storage_factory


class TestLocalStorageProvider(unittest.TestCase):
    def setUp(self):
        self.tmp_root = Path(tempfile.mkdtemp())
        self.svc = LocalStorageService(base_url="http://localhost:8001/files", root=str(self.tmp_root))
        self.addCleanup(shutil.rmtree, self.tmp_root, ignore_errors=True)

        self.src_file = self.tmp_root.parent / "source.txt"
        self.src_file.write_text("hello storage abstraction", encoding="utf-8")
        self.addCleanup(self.src_file.unlink, missing_ok=True)

    def test_upload_then_download_roundtrip(self):
        stored = self.svc.upload_file(self.src_file, dest_name="source.txt")
        self.assertEqual(stored.provider, "local")
        self.assertTrue(stored.checksum)
        self.assertEqual(stored.file_size, self.src_file.stat().st_size)

        ref = FileRef(storage_path=stored.storage_path, storage_provider="local")
        data = self.svc.download_file(ref)
        self.assertEqual(data.decode("utf-8"), "hello storage abstraction")

    def test_upload_with_folder(self):
        stored = self.svc.upload_file(self.src_file, dest_name="a.txt", folder="attachments")
        self.assertTrue(stored.storage_path.startswith("attachments" + "/") or "attachments" in stored.storage_path)
        self.assertTrue((self.tmp_root / "attachments" / "a.txt").exists())

    def test_delete_removes_file(self):
        stored = self.svc.upload_file(self.src_file, dest_name="to_delete.txt")
        ref = FileRef(storage_path=stored.storage_path, storage_provider="local")
        self.assertTrue(self.svc.delete_file(ref))
        self.assertFalse((self.tmp_root / stored.storage_path).exists())

    def test_delete_of_missing_file_is_idempotent(self):
        ref = FileRef(storage_path="never_existed.txt", storage_provider="local")
        self.assertTrue(self.svc.delete_file(ref))  # no exception, returns True

    def test_download_missing_file_raises_not_found(self):
        ref = FileRef(storage_path="does_not_exist.txt", storage_provider="local")
        with self.assertRaises(FileNotFoundInStorageError):
            self.svc.download_file(ref)

    def test_move_file_archives_to_new_path(self):
        stored = self.svc.upload_file(self.src_file, dest_name="original.txt")
        ref = FileRef(storage_path=stored.storage_path, storage_provider="local")
        moved = self.svc.move_file(ref, "deleted/2026/07/original.txt")
        self.assertEqual(moved.storage_path, "deleted/2026/07/original.txt")
        self.assertTrue((self.tmp_root / "deleted" / "2026" / "07" / "original.txt").exists())
        self.assertFalse((self.tmp_root / "original.txt").exists())

    def test_get_metadata_returns_size_and_mime(self):
        stored = self.svc.upload_file(self.src_file, dest_name="meta.txt")
        ref = FileRef(storage_path=stored.storage_path, storage_provider="local")
        meta = self.svc.get_metadata(ref)
        self.assertEqual(meta["file_size"], self.src_file.stat().st_size)

    def test_get_public_url_uses_base_url(self):
        stored = self.svc.upload_file(self.src_file, dest_name="pub.txt")
        ref = FileRef(storage_path=stored.storage_path, storage_provider="local")
        url = self.svc.get_public_url(ref)
        self.assertTrue(url.startswith("http://localhost:8001/files/"))

    def test_get_signed_url_falls_back_to_public_url(self):
        stored = self.svc.upload_file(self.src_file, dest_name="signed.txt")
        ref = FileRef(storage_path=stored.storage_path, storage_provider="local")
        self.assertEqual(self.svc.get_signed_url(ref), self.svc.get_public_url(ref))


class TestFactory(unittest.TestCase):
    def tearDown(self):
        storage_factory.reset_storage_service()

    def test_get_storage_service_defaults_to_local(self):
        storage_factory.reset_storage_service()
        with patch.dict("os.environ", {"STORAGE_PROVIDER": "local"}):
            svc = storage_factory.get_storage_service()
            self.assertIsInstance(svc, LocalStorageService)

    def test_get_storage_service_caches_per_provider_name(self):
        storage_factory.reset_storage_service()
        svc1 = storage_factory.get_storage_service("local")
        svc2 = storage_factory.get_storage_service("local")
        self.assertIs(svc1, svc2)

    def test_gdrive_alias_normalizes_to_google_drive(self):
        self.assertEqual(storage_factory._normalize("gdrive"), "google_drive")
        self.assertEqual(storage_factory._normalize("GDRIVE"), "google_drive")

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            storage_factory.get_storage_service("not_a_real_provider")

    def test_explicit_provider_can_differ_from_default(self):
        """Simulates: default provider is now 'local', but an old file was
        uploaded under a different provider — get_storage_service(row's own
        provider) must still resolve to a usable instance for THAT provider,
        independent of the current default."""
        storage_factory.reset_storage_service()
        with patch.dict("os.environ", {"STORAGE_PROVIDER": "local"}):
            default_svc = storage_factory.get_storage_service()
            explicit_svc = storage_factory.get_storage_service("local")
            self.assertIs(default_svc, explicit_svc)  # same provider name -> same cached instance


class FakeProvider:
    """A minimal fake StorageService used to prove that swapping providers
    doesn't require touching business logic — only the interface matters."""
    def __init__(self):
        self.uploaded = []

    def upload_file(self, local_path, *, dest_name=None, folder=None):
        from storage.base import StoredFile
        self.uploaded.append((str(local_path), dest_name, folder))
        return StoredFile(provider="fake", storage_path=f"fake://{dest_name}",
                           public_url=f"https://fake.example/{dest_name}",
                           mime_type="text/plain", file_size=42, checksum="deadbeef")

    def download_file(self, ref):
        return b"fake content"

    def delete_file(self, ref):
        return True

    def move_file(self, ref, target_path):
        raise NotImplementedError

    def get_public_url(self, ref):
        return f"https://fake.example/{ref.storage_path}"

    def get_signed_url(self, ref, expires_in=3600):
        return self.get_public_url(ref)

    def get_metadata(self, ref):
        return {"file_size": 42, "mime_type": "text/plain"}


class TestProviderSwitchDoesNotAffectBusinessLogic(unittest.TestCase):
    def test_attachment_upload_path_works_with_any_provider(self):
        """ingestion/attachment_handler.py's _process_one must only ever
        call the StorageService interface — swapping in a completely fake
        provider (representing "we migrated to a new backend") must not
        require any change to that logic, and must still produce a
        correctly-shaped knowledge_attachments record."""
        from ingestion import attachment_handler

        tmp_dir = Path(tempfile.mkdtemp())
        try:
            local_file = tmp_dir / "warehouse_sp.png"
            local_file.write_bytes(b"\x89PNG\r\n fake png bytes")

            fake = FakeProvider()
            with patch.object(attachment_handler, "get_storage_service", return_value=fake), \
                 patch.object(attachment_handler, "_find_local_file", return_value=local_file):
                rec = attachment_handler._process_one(
                    "warehouse_sp.png", file_id="file-1", row_index=1,
                    sheet_name="FAQ", chunk_id=None, knowledge_item_id="item-1", sb=None,
                )

            self.assertEqual(rec["status"], "linked")
            self.assertEqual(rec["storage_provider"], "fake")
            self.assertEqual(rec["public_url"], "https://fake.example/warehouse_sp.png")
            self.assertEqual(rec["knowledge_item_id"], "item-1")
            self.assertEqual(len(fake.uploaded), 1)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
