"""Supabase Storage StorageService.

Uses the Supabase Storage API (an S3-compatible object store bundled with
the Supabase project) — NOT the Postgres database. Binary bytes never touch
a Postgres column; only metadata (this module's return values) does.
"""
import hashlib
import mimetypes
import uuid
from pathlib import Path
from typing import Optional, Dict

from storage.base import (
    StorageService, StoredFile, FileRef,
    UploadFailedError, DownloadFailedError, DeleteFailedError,
    FileNotFoundInStorageError, SignedUrlFailedError,
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _guess_mime(path: Path) -> str:
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "application/octet-stream"


class SupabaseStorageService(StorageService):
    def __init__(self, supabase_client, bucket: str = "knowledge-files"):
        self.sb = supabase_client
        self.bucket = bucket

    def upload_file(self, local_path: Path, *, dest_name: Optional[str] = None,
                     folder: Optional[str] = None) -> StoredFile:
        name = dest_name or local_path.name
        key = f"{folder}/{uuid.uuid4().hex[:12]}_{name}" if folder else f"{uuid.uuid4().hex[:12]}_{name}"
        mime = _guess_mime(local_path)
        checksum = _sha256_file(local_path)
        size = local_path.stat().st_size

        try:
            with open(local_path, "rb") as f:
                self.sb.storage.from_(self.bucket).upload(key, f, {"content-type": mime})
        except Exception as e:
            raise UploadFailedError(f"Supabase Storage upload failed: {e}", detail=str(e)) from e

        ref = FileRef(storage_path=key, storage_provider="supabase", storage_bucket=self.bucket)
        return StoredFile(
            provider="supabase", storage_path=key, storage_bucket=self.bucket,
            public_url=self.get_public_url(ref),
            mime_type=mime, file_size=size, checksum=checksum,
        )

    def download_file(self, ref: FileRef) -> bytes:
        try:
            return self.sb.storage.from_(ref.storage_bucket or self.bucket).download(ref.storage_path)
        except Exception as e:
            msg = str(e)
            if "not found" in msg.lower() or "404" in msg:
                raise FileNotFoundInStorageError(f"File not found: {ref.storage_path}", detail=msg) from e
            raise DownloadFailedError(f"Supabase Storage download failed: {e}", detail=msg) from e

    def delete_file(self, ref: FileRef) -> bool:
        try:
            self.sb.storage.from_(ref.storage_bucket or self.bucket).remove([ref.storage_path])
            return True
        except Exception as e:
            raise DeleteFailedError(f"Supabase Storage delete failed: {e}", detail=str(e)) from e

    def move_file(self, ref: FileRef, target_path: str) -> StoredFile:
        bucket = ref.storage_bucket or self.bucket
        try:
            self.sb.storage.from_(bucket).move(ref.storage_path, target_path)
        except Exception as e:
            raise UploadFailedError(f"Supabase Storage move failed: {e}", detail=str(e)) from e
        new_ref = FileRef(storage_path=target_path, storage_provider="supabase", storage_bucket=bucket)
        return StoredFile(provider="supabase", storage_path=target_path, storage_bucket=bucket,
                           public_url=self.get_public_url(new_ref), mime_type="application/octet-stream", file_size=0, checksum="")

    def get_public_url(self, ref: FileRef) -> Optional[str]:
        try:
            res = self.sb.storage.from_(ref.storage_bucket or self.bucket).get_public_url(ref.storage_path)
            return res if isinstance(res, str) else (res.get("publicUrl") if res else None)
        except Exception:
            return None

    def get_signed_url(self, ref: FileRef, expires_in: int = 3600) -> Optional[str]:
        try:
            res = self.sb.storage.from_(ref.storage_bucket or self.bucket) \
                .create_signed_url(ref.storage_path, expires_in)
            return res.get("signedURL") or res.get("signedUrl") if isinstance(res, dict) else res
        except Exception as e:
            raise SignedUrlFailedError(f"Supabase Storage signed URL failed: {e}", detail=str(e)) from e

    def get_metadata(self, ref: FileRef) -> Dict:
        try:
            folder = "/".join(ref.storage_path.split("/")[:-1]) or None
            fname = ref.storage_path.split("/")[-1]
            res = self.sb.storage.from_(ref.storage_bucket or self.bucket).list(folder)
            for item in (res or []):
                if item.get("name") == fname:
                    return item
            raise FileNotFoundInStorageError(f"File not found: {ref.storage_path}")
        except FileNotFoundInStorageError:
            raise
        except Exception as e:
            raise FileNotFoundInStorageError(f"Metadata lookup failed: {e}", detail=str(e)) from e
