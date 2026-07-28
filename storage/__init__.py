"""Storage provider abstraction — see storage/base.py for the interface
and storage/factory.py for how a provider is selected at runtime.

The rest of the application must import from here and never talk to
Supabase Storage, Google Drive, S3, etc. directly.
"""
from storage.factory import get_storage_service, reset_storage_service
from storage.base import (
    StorageService, StoredFile, FileRef,
    StorageError, UploadFailedError, DownloadFailedError,
    FileNotFoundInStorageError, DeleteFailedError,
    PermissionDeniedError, InvalidCredentialsError, SignedUrlFailedError,
)

__all__ = [
    "get_storage_service", "reset_storage_service",
    "StorageService", "StoredFile", "FileRef",
    "StorageError", "UploadFailedError", "DownloadFailedError",
    "FileNotFoundInStorageError", "DeleteFailedError",
    "PermissionDeniedError", "InvalidCredentialsError", "SignedUrlFailedError",
]
