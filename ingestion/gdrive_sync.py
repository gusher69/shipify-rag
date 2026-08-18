import os
import io
from pathlib import Path
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account
import requests

from config import GOOGLE_DRIVE_FOLDER_ID, GOOGLE_SERVICE_ACCOUNT_JSON, LINE_NOTIFY_TOKEN, LOCAL_STORAGE_ROOT

KNOWLEDGE_DIR = Path(LOCAL_STORAGE_ROOT) / "products"
SCOPES        = ["https://www.googleapis.com/auth/drive.readonly"]


def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def list_new_files(service, last_sync_time: str = None):
    """หาไฟล์ใหม่/แก้ไขหลัง last_sync_time"""
    query = f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents and trashed=false"
    if last_sync_time:
        query += f" and modifiedTime > '{last_sync_time}'"

    results = service.files().list(
        q=query,
        fields="files(id, name, mimeType, modifiedTime)"
    ).execute()
    return results.get("files", [])


def download_file(service, file_id: str, file_name: str) -> Path:
    """download ไฟล์มาเก็บใน knowledge/products/"""
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    dest = KNOWLEDGE_DIR / file_name

    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    with open(dest, "wb") as f:
        f.write(fh.getvalue())
    return dest


def notify_mod(message: str):
    if not LINE_NOTIFY_TOKEN:
        return
    try:
        requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {LINE_NOTIFY_TOKEN}"},
            data={"message": message},
            timeout=5,
        )
    except Exception as e:
        print(f"❌ notify ล้มเหลว: {e}")


def sync():
    """sync ไฟล์ใหม่จาก Google Drive → embed เข้า Supabase"""
    print("🔄 Google Drive sync เริ่มต้น...")
    try:
        service   = get_drive_service()
        new_files = list_new_files(service)

        if not new_files:
            print("✅ ไม่มีไฟล์ใหม่")
            return

        downloaded = []
        for f in new_files:
            print(f"  ⬇️ download: {f['name']}")
            dest = download_file(service, f["id"], f["name"])
            downloaded.append(dest)

        # re-embed ไฟล์ที่ download มาใหม่
        from ingestion.ingest import read_file, chunk_text
        from ingestion.embedder import upsert_chunks

        all_chunks = []
        for path in downloaded:
            text = read_file(path)
            if text:
                chunks = chunk_text(text, source=path.name)
                all_chunks.extend(chunks)

        if all_chunks:
            upsert_chunks(all_chunks)

        msg = f"\n✅ Google Drive sync เสร็จแล้ว\nไฟล์ใหม่: {len(downloaded)} ไฟล์\nChunks: {len(all_chunks)}"
        print(msg)
        notify_mod(msg)

    except Exception as e:
        msg = f"\n❌ Google Drive sync ล้มเหลว: {e}"
        print(msg)
        notify_mod(msg)


if __name__ == "__main__":
    sync()
