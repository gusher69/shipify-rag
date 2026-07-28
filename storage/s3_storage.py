"""S3 / MinIO StorageService.

MinIO is S3-API-compatible, so one implementation covers both — set
S3_ENDPOINT to the MinIO server URL for MinIO, or leave it unset for real
AWS S3. Requires `boto3` (not installed by default in this project — add
it to requirements before setting STORAGE_PROVIDER=s3 or minio):

    pip install boto3

This module lazy-imports boto3 (only inside methods) so the rest of the
app is unaffected if boto3 isn't installed and this provider isn't
selected — consistent with how the optional Google Drive dependency is
handled elsewhere in this package.
"""
import hashlib
import mimetypes
import uuid
from pathlib import Path
from typing import Optional, Dict

from storage.base import (
    StorageService, StoredFile, FileRef,
    UploadFailedError, DownloadFailedError, DeleteFailedError,
    FileNotFoundInStorageError, InvalidCredentialsError, SignedUrlFailedError,
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


class S3StorageService(StorageService):
    def __init__(self, bucket: str, region: str = "us-east-1",
                 endpoint_url: Optional[str] = None,
                 access_key: Optional[str] = None, secret_key: Optional[str] = None):
        self.bucket = bucket
        self.region = region
        self.endpoint_url = endpoint_url
        self.access_key = access_key
        self.secret_key = secret_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError as e:
                raise InvalidCredentialsError(
                    "boto3 is not installed — run `pip install boto3` to use "
                    "the S3/MinIO storage provider.", detail=str(e)
                ) from e
            try:
                self._client = boto3.client(
                    "s3", region_name=self.region, endpoint_url=self.endpoint_url,
                    aws_access_key_id=self.access_key, aws_secret_access_key=self.secret_key,
                )
            except Exception as e:
                raise InvalidCredentialsError(f"S3/MinIO client init failed: {e}", detail=str(e)) from e
        return self._client

    def upload_file(self, local_path: Path, *, dest_name: Optional[str] = None,
                     folder: Optional[str] = None) -> StoredFile:
        name = dest_name or local_path.name
        key = f"{folder}/{uuid.uuid4().hex[:12]}_{name}" if folder else f"{uuid.uuid4().hex[:12]}_{name}"
        mime = _guess_mime(local_path)
        checksum = _sha256_file(local_path)
        size = local_path.stat().st_size

        try:
            self._get_client().upload_file(str(local_path), self.bucket, key,
                                            ExtraArgs={"ContentType": mime})
        except InvalidCredentialsError:
            raise
        except Exception as e:
            raise UploadFailedError(f"S3/MinIO upload failed: {e}", detail=str(e)) from e

        ref = FileRef(storage_path=key, storage_provider="s3", storage_bucket=self.bucket)
        return StoredFile(
            provider="s3", storage_path=key, storage_bucket=self.bucket,
            storage_object_id=key, public_url=self.get_public_url(ref),
            mime_type=mime, file_size=size, checksum=checksum,
        )

    def download_file(self, ref: FileRef) -> bytes:
        try:
            obj = self._get_client().get_object(Bucket=ref.storage_bucket or self.bucket, Key=ref.storage_path)
            return obj["Body"].read()
        except InvalidCredentialsError:
            raise
        except Exception as e:
            msg = str(e)
            if "NoSuchKey" in msg or "404" in msg:
                raise FileNotFoundInStorageError(f"File not found: {ref.storage_path}", detail=msg) from e
            raise DownloadFailedError(f"S3/MinIO download failed: {e}", detail=msg) from e

    def delete_file(self, ref: FileRef) -> bool:
        try:
            self._get_client().delete_object(Bucket=ref.storage_bucket or self.bucket, Key=ref.storage_path)
            return True
        except InvalidCredentialsError:
            raise
        except Exception as e:
            raise DeleteFailedError(f"S3/MinIO delete failed: {e}", detail=str(e)) from e

    def move_file(self, ref: FileRef, target_path: str) -> StoredFile:
        bucket = ref.storage_bucket or self.bucket
        try:
            client = self._get_client()
            client.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": ref.storage_path}, Key=target_path)
            client.delete_object(Bucket=bucket, Key=ref.storage_path)
        except InvalidCredentialsError:
            raise
        except Exception as e:
            raise UploadFailedError(f"S3/MinIO move failed: {e}", detail=str(e)) from e
        new_ref = FileRef(storage_path=target_path, storage_provider="s3", storage_bucket=bucket)
        return StoredFile(provider="s3", storage_path=target_path, storage_bucket=bucket,
                           storage_object_id=target_path, public_url=self.get_public_url(new_ref),
                           mime_type="application/octet-stream", file_size=0, checksum="")

    def get_public_url(self, ref: FileRef) -> Optional[str]:
        bucket = ref.storage_bucket or self.bucket
        if self.endpoint_url:
            return f"{self.endpoint_url.rstrip('/')}/{bucket}/{ref.storage_path}"
        return f"https://{bucket}.s3.{self.region}.amazonaws.com/{ref.storage_path}"

    def get_signed_url(self, ref: FileRef, expires_in: int = 3600) -> Optional[str]:
        try:
            return self._get_client().generate_presigned_url(
                "get_object",
                Params={"Bucket": ref.storage_bucket or self.bucket, "Key": ref.storage_path},
                ExpiresIn=expires_in,
            )
        except InvalidCredentialsError:
            raise
        except Exception as e:
            raise SignedUrlFailedError(f"S3/MinIO signed URL failed: {e}", detail=str(e)) from e

    def get_metadata(self, ref: FileRef) -> Dict:
        try:
            head = self._get_client().head_object(Bucket=ref.storage_bucket or self.bucket, Key=ref.storage_path)
            return {
                "file_size": head.get("ContentLength"),
                "mime_type": head.get("ContentType"),
                "last_modified": str(head.get("LastModified")),
            }
        except InvalidCredentialsError:
            raise
        except Exception as e:
            msg = str(e)
            if "404" in msg or "Not Found" in msg:
                raise FileNotFoundInStorageError(f"File not found: {ref.storage_path}", detail=msg) from e
            raise FileNotFoundInStorageError(f"Metadata lookup failed: {e}", detail=msg) from e
