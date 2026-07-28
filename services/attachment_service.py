"""AttachmentService — thin, named wrapper around ingestion/attachment_handler.py's
read-side helpers (attachment resolution for RAG/LINE responses).

Upload-side attachment processing (Excel import detection/download) stays
in ingestion/attachment_handler.py where it's tightly coupled to the Excel
sync pipeline — this wrapper only covers the "resolve attachments for a
set of chunks/files" read path, which is what the AI Playground and LINE
response layer actually need.
"""
from typing import List, Dict
from ingestion.attachment_handler import get_attachments_for_chunks, get_attachments_for_file


class AttachmentService:
    def for_chunks(self, sb, chunk_ids: List[str]) -> Dict[str, List[Dict]]:
        return get_attachments_for_chunks(sb, chunk_ids)

    def for_file(self, sb, file_id: str) -> List[Dict]:
        return get_attachments_for_file(sb, file_id)


_instance = AttachmentService()


def get_attachment_service() -> AttachmentService:
    return _instance
