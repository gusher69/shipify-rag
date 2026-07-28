"""ImportService — the documented entry point for Full Auto Import.

The platform no longer has an Import Preview / manual confirmation step
(see admin/routes.py's /admin/upload and _run_sync_list): every uploaded
file is registered in `knowledge_files` and then run through the complete
pipeline automatically —

    Extract -> AI Knowledge Analyzer -> Advanced Analysis ->
    Knowledge Graph Generation -> Chunking -> Embedding -> Save

always with AI Analysis Profile "advanced" and Knowledge Graph enabled
(see config.DEFAULT_AI_PROFILE / config.KNOWLEDGE_GRAPH_ENABLED). If AI
analysis or Knowledge Graph extraction fails, the pipeline degrades
gracefully rather than failing the import (see
services.knowledge_analyzer / services.knowledge_graph_service).

This module is a thin, documented wrapper around the actual sync engine
(which lives in admin/routes.py — extracting the whole engine into a
separate module is out of scope here) so other code can start an import
without knowing that implementation detail.
"""
from typing import Dict


class ImportService:
    @staticmethod
    def start_full_auto_import(file_id: str) -> Dict:
        """Start the full advanced import pipeline for one already-uploaded
        file (a row that already exists in `knowledge_files`). Returns
        {"ok": True, "job_id": ...} once the background import has started,
        or {"ok": False, "msg": ...} if it could not be started (e.g. a
        sync is already running, or the file record/storage object is
        missing) — it never raises for those expected conditions."""
        from admin.routes import start_full_auto_import_for_file
        return start_full_auto_import_for_file(file_id)


def start_full_auto_import(file_id: str) -> Dict:
    """Module-level convenience alias for ImportService.start_full_auto_import."""
    return ImportService.start_full_auto_import(file_id)
