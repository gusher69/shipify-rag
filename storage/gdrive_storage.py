"""Google Drive StorageService.

storage_object_id is the Drive file id (the thing that actually identifies
a file in Drive — storage_path is not meaningful for this provider beyond
carrying a human-readable name).
"""
import hashlib
import mimetypes
from pathlib import Path
from typing import Optional, Dict

from storage.base import (
    StorageService, StoredFile, FileRef,
    UploadFailedError, DownloadFailedError, DeleteFailedError,
    FileNotFoundInStorageError, InvalidCredentialsError, PermissionDeniedError,
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


class GoogleDriveStorageService(StorageService):
    def __init__(self, root_folder_id: str, service_account_json: str):
        self.root_folder_id = root_folder_id
        self.sa_json = service_account_json
        self._svc = None
        self._folder_cache: Dict[str, str] = {}

    def _service(self):
        if self._svc is None:
            try:
                from googleapiclient.discovery import build
                from google.oauth2 import service_account as _sa
                creds = _sa.Credentials.from_service_account_file(
                    self.sa_json, scopes=["https://www.googleapis.com/auth/drive.file"]
                )
                self._svc = build("drive", "v3", credentials=creds)
            except FileNotFoundError as e:
                raise InvalidCredentialsError(
                    f"Google service account file not found: {self.sa_json}", detail=str(e)
                ) from e
            except Exception as e:
                raise InvalidCredentialsError(f"Google Drive credentials invalid: {e}", detail=str(e)) from e
        return self._svc

    def _resolve_folder(self, folder: Optional[str]) -> str:
        if not folder:
            return self.root_folder_id
        if folder in self._folder_cache:
            return self._folder_cache[folder]
        svc = self._service()
        try:
            q = (f"name='{folder}' and mimeType='application/vnd.google-apps.folder' "
                 f"and '{self.root_folder_id}' in parents and trashed=false")
            res = svc.files().list(q=q, fields="files(id)").execute()
            files = res.get("files", [])
            if files:
                fid = files[0]["id"]
            else:
                meta = {"name": folder, "mimeType": "application/vnd.google-apps.folder", "parents": [self.root_folder_id]}
                created = svc.files().create(body=meta, fields="id").execute()
                fid = created["id"]
            self._folder_cache[folder] = fid
            return fid
        except Exception as e:
            raise UploadFailedError(f"Could not resolve/create Drive folder {folder!r}: {e}", detail=str(e)) from e

    def upload_file(self, local_path: Path, *, dest_name: Optional[str] = None,
                     folder: Optional[str] = None) -> StoredFile:
        from googleapiclient.http import MediaFileUpload

        svc = self._service()
        mime = _guess_mime(local_path)
        checksum = _sha256_file(local_path)
        size = local_path.stat().st_size
        name = dest_name or local_path.name
        parent = self._resolve_folder(folder)

        try:
            meta = {"name": name, "parents": [parent]}
            media = MediaFileUpload(str(local_path), mimetype=mime, resumable=False)
            created = svc.files().create(body=meta, media_body=media, fields="id").execute()
            file_id = created.get("id")
            if not file_id:
                raise UploadFailedError("Google Drive upload returned no file id")
            svc.permissions().create(fileId=file_id, body={"type": "anyone", "role": "reader"}).execute()
        except UploadFailedError:
            raise
        except Exception as e:
            raise UploadFailedError(f"Google Drive upload failed: {e}", detail=str(e)) from e

        ref = FileRef(storage_path=file_id, storage_provider="google_drive", storage_object_id=file_id)
        return StoredFile(
            provider="google_drive", storage_path=file_id, storage_object_id=file_id,
            public_url=self.get_public_url(ref),
            mime_type=mime, file_size=size, checksum=checksum,
        )

    def download_file(self, ref: FileRef) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload
        import io
        svc = self._service()
        file_id = ref.storage_object_id or ref.storage_path
        try:
            request = svc.files().get_media(fileId=file_id)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buf.getvalue()
        except Exception as e:
            msg = str(e)
            if "404" in msg:
                raise FileNotFoundInStorageError(f"File not found: {file_id}", detail=msg) from e
            if "403" in msg:
                raise PermissionDeniedError(f"Permission denied for file: {file_id}", detail=msg) from e
            raise DownloadFailedError(f"Google Drive download failed: {e}", detail=msg) from e

    def delete_file(self, ref: FileRef) -> bool:
        file_id = ref.storage_object_id or ref.storage_path
        try:
            self._service().files().delete(fileId=file_id).execute()
            return True
        except Exception as e:
            msg = str(e)
            if "404" in msg:
                return True  # already gone — deletion is idempotent
            raise DeleteFailedError(f"Google Drive delete failed: {e}", detail=msg) from e

    def move_file(self, ref: FileRef, target_path: str) -> StoredFile:
        """target_path is treated as a folder name to move the file into
        (Drive has no path hierarchy the way a filesystem does)."""
        svc = self._service()
        file_id = ref.storage_object_id or ref.storage_path
        new_parent = self._resolve_folder(target_path)
        try:
            file = svc.files().get(fileId=file_id, fields="parents").execute()
            old_parents = ",".join(file.get("parents", []))
            svc.files().update(fileId=file_id, addParents=new_parent, removeParents=old_parents, fields="id,parents").execute()
        except Exception as e:
            raise UploadFailedError(f"Google Drive move failed: {e}", detail=str(e)) from e
        return StoredFile(provider="google_drive", storage_path=file_id, storage_object_id=file_id,
                           public_url=self.get_public_url(ref), mime_type="application/octet-stream", file_size=0, checksum="")

    def get_public_url(self, ref: FileRef) -> Optional[str]:
        file_id = ref.storage_object_id or ref.storage_path
        return f"https://drive.google.com/uc?id={file_id}&export=view"

    def get_signed_url(self, ref: FileRef, expires_in: int = 3600) -> Optional[str]:
        # Drive doesn't have native short-lived signed URLs for service-
        # account files without extra Apps Script/Cloud Function scaffolding
        # — the public share link (already access-controlled by Drive
        # permissions) is what this provider can offer.
        return self.get_public_url(ref)

    def get_metadata(self, ref: FileRef) -> Dict:
        svc = self._service()
        file_id = ref.storage_object_id or ref.storage_path
        try:
            return svc.files().get(fileId=file_id, fields="id,name,mimeType,size,modifiedTime").execute()
        except Exception as e:
            msg = str(e)
            if "404" in msg:
                raise FileNotFoundInStorageError(f"File not found: {file_id}", detail=msg) from e
            raise FileNotFoundInStorageError(f"Metadata lookup failed: {e}", detail=msg) from e
