"""Builds/caches StorageService implementations by provider name.

Business logic must call get_storage_service() (no args) to get the
CURRENTLY CONFIGURED provider (STORAGE_PROVIDER env var) for new
uploads. But a file uploaded before a provider switch is still on the
OLD provider — its DB row's own `storage_provider` column says which.
Reading/deleting/signing an existing file must always go through
get_storage_service(row["storage_provider"]) so a provider switch never
orphans previously-uploaded files. This is what makes "switch
STORAGE_PROVIDER and nothing else" a safe migration path (see item 10 of
the storage abstraction spec).

To add a new provider (Azure Blob, ...):
  1. Write a new class in storage/ implementing storage.base.StorageService.
  2. Add one branch to _build().
  3. Set STORAGE_PROVIDER accordingly.
No other file in the codebase needs to change.
"""
import os
from typing import Optional
from storage.base import StorageService

_instances: dict = {}  # provider name -> StorageService


def _normalize(name: str) -> str:
    name = (name or "local").lower()
    return {"gdrive": "google_drive"}.get(name, name)


def _build(provider: str) -> StorageService:
    if provider == "local":
        from storage.local_storage import LocalStorageService
        from config import ATTACHMENT_BASE_URL, LOCAL_STORAGE_ROOT
        return LocalStorageService(base_url=ATTACHMENT_BASE_URL, root=LOCAL_STORAGE_ROOT)

    if provider == "supabase":
        from storage.supabase_storage import SupabaseStorageService
        from config import SUPABASE_URL, SUPABASE_KEY, SUPABASE_STORAGE_BUCKET
        from supabase import create_client
        return SupabaseStorageService(create_client(SUPABASE_URL, SUPABASE_KEY), bucket=SUPABASE_STORAGE_BUCKET)

    if provider == "google_drive":
        from storage.gdrive_storage import GoogleDriveStorageService
        from config import GOOGLE_DRIVE_ROOT_FOLDER_ID, GOOGLE_APPLICATION_CREDENTIALS
        return GoogleDriveStorageService(GOOGLE_DRIVE_ROOT_FOLDER_ID, GOOGLE_APPLICATION_CREDENTIALS)

    if provider in ("s3", "minio"):
        from storage.s3_storage import S3StorageService
        from config import S3_BUCKET, S3_REGION, S3_ENDPOINT, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY
        return S3StorageService(
            bucket=S3_BUCKET, region=S3_REGION, endpoint_url=S3_ENDPOINT,
            access_key=S3_ACCESS_KEY_ID, secret_key=S3_SECRET_ACCESS_KEY,
        )

    raise ValueError(
        f"Unknown STORAGE_PROVIDER={provider!r}. "
        f"Supported: local, supabase, google_drive, s3, minio."
    )


def get_storage_service(provider: Optional[str] = None) -> StorageService:
    """provider=None returns the currently configured default (for new
    uploads). Pass an explicit provider name (typically a DB row's
    storage_provider column) to read/delete/sign a file that may have
    been stored under a provider different from today's default."""
    name = _normalize(provider or os.getenv("STORAGE_PROVIDER", "local"))
    if name not in _instances:
        _instances[name] = _build(name)
    return _instances[name]


def reset_storage_service():
    """Test/dev helper — forces the next get_storage_service() call(s) to
    re-read config and re-instantiate."""
    global _instances
    _instances = {}
