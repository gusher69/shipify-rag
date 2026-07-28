"""Local filesystem StorageService — the default for this deployment, and
what STORAGE_PROVIDER=local means. Files live under `root`; storage_path
is the relative path under that directory (opaque to callers, but happens
to be human-readable here). Public URLs are served by the existing
/admin/attachments/{filename} route (or an equivalent for knowledge files).
"""
import hashlib
import mimetypes
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

from storage.base import (
    StorageService, StoredFile, FileRef,
    UploadFailedError, DownloadFailedError, DeleteFailedError,
    FileNotFoundInStorageError,
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


class LocalStorageService(StorageService):
    def __init__(self, base_url: Optional[str] = None, root: str = "./storage/knowledge"):
        self.base_url = base_url
        self.root = Path(root)

    def upload_file(self, local_path: Path, *, dest_name: Optional[str] = None,
                     folder: Optional[str] = None) -> StoredFile:
        try:
            target_dir = self.root / folder if folder else self.root
            target_dir.mkdir(parents=True, exist_ok=True)
            name = dest_name or local_path.name
            dest = target_dir / name
            if dest.exists() and dest.resolve() != local_path.resolve():
                dest = target_dir / f"{dest.stem}_{uuid.uuid4().hex[:6]}{dest.suffix}"
            if dest.resolve() != local_path.resolve():
                shutil.copyfile(local_path, dest)

            checksum = _sha256_file(dest)
            mime = _guess_mime(dest)
            size = dest.stat().st_size
            rel = str(dest.relative_to(self.root))
            return StoredFile(
                provider="local", storage_path=rel,
                public_url=f"{self.base_url}/{rel}" if self.base_url else None,
                mime_type=mime, file_size=size, checksum=checksum,
            )
        except Exception as e:
            raise UploadFailedError(f"Local upload failed: {e}", detail=str(e)) from e

    def download_file(self, ref: FileRef) -> bytes:
        path = self.root / ref.storage_path
        if not path.exists():
            raise FileNotFoundInStorageError(f"File not found: {ref.storage_path}")
        try:
            with open(path, "rb") as f:
                return f.read()
        except Exception as e:
            raise DownloadFailedError(f"Local download failed: {e}", detail=str(e)) from e

    def delete_file(self, ref: FileRef) -> bool:
        path = self.root / ref.storage_path
        try:
            if path.exists():
                path.unlink()
            return True
        except Exception as e:
            raise DeleteFailedError(f"Local delete failed: {e}", detail=str(e)) from e

    def move_file(self, ref: FileRef, target_path: str) -> StoredFile:
        src = self.root / ref.storage_path
        if not src.exists():
            raise FileNotFoundInStorageError(f"File not found: {ref.storage_path}")
        dest = self.root / target_path
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            return StoredFile(
                provider="local", storage_path=target_path,
                public_url=f"{self.base_url}/{target_path}" if self.base_url else None,
                mime_type=_guess_mime(dest), file_size=dest.stat().st_size,
                checksum=_sha256_file(dest),
            )
        except Exception as e:
            raise UploadFailedError(f"Local move failed: {e}", detail=str(e)) from e

    def get_public_url(self, ref: FileRef) -> Optional[str]:
        if not self.base_url:
            return None
        return f"{self.base_url}/{ref.storage_path}"

    def get_signed_url(self, ref: FileRef, expires_in: int = 3600) -> Optional[str]:
        # Local disk has no meaningful "signed, time-limited" URL concept —
        # fall back to the same stable public URL.
        return self.get_public_url(ref)

    def get_metadata(self, ref: FileRef) -> Dict:
        path = self.root / ref.storage_path
        if not path.exists():
            raise FileNotFoundInStorageError(f"File not found: {ref.storage_path}")
        stat = path.stat()
        return {
            "file_size": stat.st_size,
            "mime_type": _guess_mime(path),
            "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }
