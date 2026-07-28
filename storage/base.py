"""StorageService interface.

Every concrete provider (local disk, Google Drive, Supabase Storage, S3/
MinIO) implements this same interface. Callers (upload, sync, delete,
attachment ingestion, RAG response building) depend ONLY on this interface
— never on a specific provider's SDK or API. Swapping providers means
writing a new class here and changing STORAGE_PROVIDER; nothing else in
the codebase should need to change.

Design notes:
- `upload_file()` takes a local file path (the caller already has the
  bytes on disk — this system never buffers file binaries in a Postgres
  column) and returns a StoredFile describing where it now lives.
- `storage_path`/`storage_object_id` are provider-specific (a local
  relative path, a Drive file id, an S3/MinIO object key...) and are
  opaque to callers — only ever passed back into the same provider's
  download_file()/delete_file()/move_file().
- `get_public_url()` may return None for providers that require a signed
  URL per request instead of a stable public one; callers should treat a
  None url as "call get_signed_url() instead" rather than caching an
  empty value forever.
- All methods raise a StorageError subclass on failure — callers (admin
  routes, LINE webhook) catch StorageError and translate to a
  user-friendly message; the original technical detail is preserved on
  the exception for logging.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict


# ── Errors ────────────────────────────────────────────────────────
# Deliberately provider-agnostic: a caller catching StorageError never
# needs to know or care which provider raised it.

class StorageError(Exception):
    """Base class for all storage failures. `detail` carries the full
    technical message for backend logs; str(exc) is safe to show as-is
    to an admin but callers should still map to a friendlier string for
    end users (see admin/routes.py's error translation)."""
    def __init__(self, message: str, *, detail: Optional[str] = None):
        super().__init__(message)
        self.detail = detail or message


class UploadFailedError(StorageError):
    pass


class DownloadFailedError(StorageError):
    pass


class FileNotFoundInStorageError(StorageError):
    pass


class DeleteFailedError(StorageError):
    pass


class PermissionDeniedError(StorageError):
    pass


class InvalidCredentialsError(StorageError):
    pass


class SignedUrlFailedError(StorageError):
    pass


# ── Data shapes ───────────────────────────────────────────────────

@dataclass
class StoredFile:
    provider: str                 # "local" | "supabase" | "gdrive" | "s3"
    storage_path: str             # opaque provider-specific locator (bucket key, local relative path, ...)
    storage_object_id: Optional[str] = None   # provider's own id when distinct from storage_path (e.g. Drive file id)
    storage_bucket: Optional[str] = None
    public_url: Optional[str] = None
    mime_type: str = "application/octet-stream"
    file_size: int = 0
    checksum: str = ""             # sha256 hex digest of the file contents
    storage_metadata: Dict = field(default_factory=dict)


@dataclass
class FileRef:
    """Everything a provider needs to locate a file it previously stored.
    Built from the storage_* columns on knowledge_files / knowledge_attachments."""
    storage_path: str
    storage_provider: str
    storage_bucket: Optional[str] = None
    storage_object_id: Optional[str] = None


# ── Interface ─────────────────────────────────────────────────────

class StorageService(ABC):
    """Abstract storage backend. Implementations must never be assumed by
    callers to be any specific provider — code outside this package should
    only call these methods and read StoredFile/FileRef fields."""

    @abstractmethod
    def upload_file(self, local_path: Path, *, dest_name: Optional[str] = None,
                     folder: Optional[str] = None) -> StoredFile:
        """Persist the file at local_path into this storage backend.

        dest_name, if given, is a hint for the destination filename/key —
        providers may deduplicate or rename to avoid collisions regardless.
        folder is an optional logical grouping (e.g. "attachments" vs the
        default knowledge-files area) — providers map it to whatever makes
        sense (a subfolder, a Drive folder, an S3 key prefix).
        Raises UploadFailedError on failure.
        """
        raise NotImplementedError

    @abstractmethod
    def download_file(self, ref: FileRef) -> bytes:
        """Return the raw bytes for a previously-uploaded file.
        Raises FileNotFoundInStorageError or DownloadFailedError."""
        raise NotImplementedError

    @abstractmethod
    def delete_file(self, ref: FileRef) -> bool:
        """Remove the file from storage. Returns True if deleted (or
        already absent). Raises DeleteFailedError on a real failure."""
        raise NotImplementedError

    @abstractmethod
    def move_file(self, ref: FileRef, target_path: str) -> "StoredFile":
        """Move/rename a stored file to a new logical path within the same
        provider (used for archive-on-delete: move to deleted/yyyy/mm/).
        Returns the updated StoredFile. Raises StorageError on failure."""
        raise NotImplementedError

    @abstractmethod
    def get_public_url(self, ref: FileRef) -> Optional[str]:
        """Return a stable public URL, or None if this provider can't
        produce one for this file (use get_signed_url instead)."""
        raise NotImplementedError

    @abstractmethod
    def get_signed_url(self, ref: FileRef, expires_in: int = 3600) -> Optional[str]:
        """Return a time-limited URL valid for expires_in seconds, or None
        if this provider doesn't support signed URLs (falls back to
        get_public_url in that case). Raises SignedUrlFailedError on a
        real failure (as opposed to "not supported", which returns None)."""
        raise NotImplementedError

    @abstractmethod
    def get_metadata(self, ref: FileRef) -> Dict:
        """Return whatever metadata the provider can report for this file
        (size, mime type, last modified, ...). Raises FileNotFoundInStorageError
        if the file doesn't exist."""
        raise NotImplementedError
