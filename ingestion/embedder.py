import uuid
from datetime import datetime, timezone
from typing import List, Dict, Optional
from supabase import create_client

from config import SUPABASE_URL, SUPABASE_KEY

_supabase = None


def _get_supabase():
    # Shared bounded-timeout client (services/supabase_client.py) —
    # services/retrieval_settings.py imports this on the RAG hot path.
    from services.supabase_client import get_supabase
    return get_supabase()


FILES_TABLE  = "knowledge_files"
CHUNKS_TABLE = "knowledge_chunks"


def embed_text(text: str) -> List[float]:
    """Single-text embed via the shared provider (services/embedding_service.py)
    — document ingestion and query embedding (rag/searcher.py) must always
    go through the SAME provider/model, never a separate hardcoded local
    model, so they can never silently diverge onto different vector
    spaces."""
    from services.embedding_service import get_embedding_provider
    return get_embedding_provider().embed_documents([text])[0]


# ── File registry ──────────────────────────────────────────────

def register_file(filename: str, size: int) -> str:
    """Insert a brand-new knowledge_files row and return its UUID.

    Always INSERTs — never looks up an existing row by filename. Filename is
    display-only; identity is the UUID. Previously this SELECTed an existing
    row WHERE filename=... AND deleted_at IS NULL and reused it (only
    updating `size`), which meant re-uploading a filename whose old row
    hadn't actually been soft-deleted (a failed/partial delete, a race, an
    RLS hiccup — any of it) silently resurrected that row's old status,
    synced_at, and chunk_count. A brand new upload could show as "Completed"
    without ever being synced. Every upload now gets a fresh UUID and fresh
    (default) status/synced_at/chunk_count, full stop — uploading a filename
    that was used before behaves identically to uploading a new one.
    """
    sb = _get_supabase()
    res = sb.table(FILES_TABLE).insert({"filename": filename, "size": size}).execute()
    return res.data[0]["id"]


def get_file_id(filename: str) -> Optional[str]:
    res = _get_supabase().table(FILES_TABLE).select("id").eq("filename", filename).is_("deleted_at", "null").execute()
    return res.data[0]["id"] if res.data else None


def get_file_version(file_id: str) -> int:
    """Return current version number stored in knowledge_files, default 1."""
    try:
        res = _get_supabase().table(FILES_TABLE).select("version").eq("id", file_id).execute()
        return int((res.data or [{}])[0].get("version") or 1)
    except Exception:
        return 1


def mark_file_synced(file_id: str, chunk_count: int) -> dict:
    """Single atomic UPDATE that flips status, synced_at, and chunk_count together.

    Previously the caller issued this as two separate .update() calls (one for
    chunk_count/synced_at, a second for status) — if the second call failed
    (or its exception was swallowed), a file ended up with synced_at set but
    status stuck at its old value. That file then vanished from BOTH the
    Import Queue (filtered out because synced_at is no longer NULL) and File
    Library (filtered out because status != 'completed'). A single UPDATE
    statement touching all columns for one row is atomic in Postgres, so
    doing it here in one call removes that window entirely.

    Raises on failure — the caller must NOT swallow this exception, so a
    write failure here correctly fails the whole file (and thus the sync
    job) instead of silently producing an inconsistent row.
    """
    now = datetime.now(timezone.utc).isoformat()
    res = _get_supabase().table(FILES_TABLE).update({
        "status":      "completed",
        "chunk_count": chunk_count,
        "synced_at":   now,
        "last_error":  "",
    }).eq("id", file_id).execute()
    row = (res.data or [None])[0]
    if not row or row.get("status") != "completed" or not row.get("synced_at"):
        raise RuntimeError(f"mark_file_synced: update did not persist for file_id={file_id}")
    return row


def soft_delete_file(file_id: str):
    """Set deleted_at on a knowledge_files row and verify it actually took.

    Callers previously assumed success as long as .execute() didn't raise —
    but a Supabase update that matches zero rows (wrong id, RLS blocking the
    write, etc.) returns an empty data list without raising. That silent
    no-op left the row looking active (deleted_at IS NULL) after a "delete",
    letting the row be found again later (e.g. by a filename-based lookup)
    and its stale synced/completed state resurface. Now this raises so the
    caller's own error handling has to deal with it instead of silently
    treating the file as deleted when it isn't.
    """
    now = datetime.now(timezone.utc).isoformat()
    res = _get_supabase().table(FILES_TABLE).update({
        "deleted_at": now,
    }).eq("id", file_id).execute()
    if not res.data:
        raise RuntimeError(f"soft_delete_file: update matched 0 rows for file_id={file_id}")


# ── Chunks ─────────────────────────────────────────────────────

def deactivate_old_chunks(file_id: str):
    """Mark previous chunks inactive instead of deleting — preserves history."""
    _get_supabase().table(CHUNKS_TABLE).update({"is_active": False}).eq("file_id", file_id).execute()


def upsert_chunks(chunks: List[Dict], batch_size: int = 50):
    """Embed and insert chunks with full metadata.
    Caller must call deactivate_old_chunks (or delete) before this.

    Embeds each batch in ONE call to the shared provider (real batching —
    for OpenAI this is one HTTP request per batch, not one per chunk) using
    a contextual embedding string (`Document: ... / Section: ... /
    Content: ...`, see services.embedding_service.build_embedding_text) so
    the model sees which document/section a chunk belongs to, not just an
    isolated paragraph. The contextual string is stored in
    metadata["embedding_text"] for debugging/evaluation — the UI always
    displays `content` (the verbatim source text), never embedding_text."""
    if not chunks:
        return 0

    from services.embedding_service import get_embedding_provider, build_embedding_text
    provider = get_embedding_provider()

    now = datetime.now(timezone.utc).isoformat()
    total = len(chunks)
    print(f"Embedding {total} chunks via {provider.provider_name}/{provider.model_name()}...")

    inserted = 0
    embed_failures = 0
    for i in range(0, total, batch_size):
        batch = chunks[i:i + batch_size]
        embedding_texts = [
            build_embedding_text(
                file_name=(c.get("metadata") or {}).get("file_name") or c.get("source", ""),
                document_title=(c.get("metadata") or {}).get("document_title"),
                heading_path=(c.get("metadata") or {}).get("heading_path"),
                section_title=(c.get("metadata") or {}).get("section_title"),
                chunk_type=(c.get("metadata") or {}).get("chunk_strategy"),
                content=c["text"],
            )
            for c in batch
        ]
        try:
            vectors = provider.embed_documents(embedding_texts)
        except Exception as e:
            embed_failures += len(batch)
            print(f"  batch embed failed ({len(batch)} chunk(s)): {e}")
            continue

        rows = []
        for chunk, vector, embedding_text in zip(batch, vectors, embedding_texts):
            chunk_id = str(uuid.uuid4())
            # Build metadata — start from whatever chunk_text() produced, then
            # fill in the time-sensitive fields and ensure chunk_id is consistent.
            meta = dict(chunk.get("metadata") or {})
            meta["chunk_id"]       = chunk_id
            meta["created_at"]     = now
            meta["last_synced_at"] = now
            meta["is_active"]      = True
            meta["embedding_text"] = embedding_text
            meta["embedding_provider"]  = provider.provider_name
            meta["embedding_model"]     = provider.model_name()
            meta["embedding_version"]   = provider.version()
            meta["embedding_dimensions"] = provider.dimensions()
            # Ensure file_id is mirrored inside metadata
            if chunk.get("file_id"):
                meta["file_id"] = chunk["file_id"]

            rows.append({
                "id":        chunk_id,
                "file_id":   chunk.get("file_id"),
                "content":   chunk["text"],
                "source":    chunk["source"],
                "intent":    chunk["intent"],
                "embedding": vector,
                "metadata":  meta,
                "is_active": True,
            })

        if rows:
            _get_supabase().table(CHUNKS_TABLE).insert(rows).execute()
            inserted += len(rows)
            print(f"  inserted {inserted}/{total}")

    print(f"Done! {inserted}/{total} chunks inserted" + (f" ({embed_failures} embed failures)" if embed_failures else "") + ".")
    # Return the ACTUAL number of rows persisted to knowledge_chunks — not the
    # requested count. Callers use this to write knowledge_files.chunk_count,
    # so an inflated return here previously let a file be marked "synced" with
    # fewer real chunks than reported (or zero, if every embed call failed).
    return inserted


def delete_source(filename: str):
    """Delete all chunks for a source filename (fallback without file_id)."""
    _get_supabase().table(CHUNKS_TABLE).delete().eq("source", filename).execute()


# ── Excel structured storage ───────────────────────────────────

WORKBOOKS_TABLE = "excel_workbooks"
SHEETS_TABLE    = "excel_sheets"
ROWS_TABLE      = "excel_rows"


def store_excel_workbook(file_id: str, filename: str, workbook_data: dict):
    """Persist structured Excel data (workbooks → sheets → rows) in Supabase.

    Deletes any previous data for this file_id before inserting fresh rows.
    """
    sb = _get_supabase()

    # Remove previous structured data (CASCADE deletes sheets + rows)
    sb.table(WORKBOOKS_TABLE).delete().eq("file_id", file_id).execute()

    # Insert workbook record
    wb_res = sb.table(WORKBOOKS_TABLE).insert({
        "file_id":     file_id,
        "filename":    filename,
        "sheet_count": workbook_data.get("sheet_count", 0),
        "sheet_names": workbook_data.get("sheet_names", []),
    }).execute()

    if not wb_res.data:
        print(f"[embedder] store_excel_workbook: failed to insert workbook for {filename}")
        return

    workbook_id = wb_res.data[0]["id"]

    for sheet in workbook_data.get("sheets", []):
        if sheet.get("row_count", 0) == 0:
            continue

        sheet_res = sb.table(SHEETS_TABLE).insert({
            "workbook_id":      workbook_id,
            "file_id":          file_id,
            "sheet_name":       sheet["sheet_name"],
            "sheet_index":      sheet["sheet_index"],
            "row_count":        sheet["row_count"],
            "column_count":     sheet["column_count"],
            "headers":          sheet["headers"],
            "numeric_columns":  sheet["numeric_columns"],
            "date_columns":     sheet["date_columns"],
            "currency_columns": sheet.get("currency_columns", []),
            "percentage_columns": sheet.get("percentage_columns", []),
            "boolean_columns":  sheet.get("boolean_columns", []),
            "has_formula":      sheet.get("has_formula", False),
            "formula_cells":    sheet.get("formula_cells", {}),
            "cell_range":       sheet.get("cell_range", ""),
            "markdown_preview": sheet.get("markdown_preview", ""),
            "is_hidden":        sheet.get("is_hidden", False),
        }).execute()

        if not sheet_res.data:
            continue

        sheet_id = sheet_res.data[0]["id"]

        # Batch-insert rows (100 at a time to stay under Supabase request limits)
        rows = sheet.get("rows", [])
        BATCH = 100
        for i in range(0, len(rows), BATCH):
            batch = rows[i:i + BATCH]
            row_records = [
                {
                    "sheet_id":  sheet_id,
                    "file_id":   file_id,
                    "row_index": r["row_index"],
                    "row_data":  r["row_data"],
                }
                for r in batch
            ]
            sb.table(ROWS_TABLE).insert(row_records).execute()

    print(f"[embedder] stored Excel structure: {workbook_data.get('sheet_count')} sheet(s), file_id={file_id}")


def delete_excel_data(file_id: str) -> dict:
    """Remove all structured Excel data for a file.

    Deletes explicitly in child-to-parent order (rows -> sheets -> workbook)
    using each table's own redundant file_id column, instead of relying
    solely on ON DELETE CASCADE via workbook_id/sheet_id. Those CASCADE
    constraints ARE declared in migrations/003_excel_structured.sql, but
    whether they actually exist on any given live database depends on
    whether that migration was ever applied there — if it wasn't (or was
    only partially applied), CASCADE silently does nothing and excel_rows/
    excel_sheets orphan forever every time a file is deleted, since the
    previous version of this function only deleted excel_workbooks and
    trusted CASCADE for the rest. Deleting explicitly by file_id here works
    regardless of whether those constraints exist.

    Returns per-table deleted counts so the caller can log/report them
    instead of this failing completely silently.
    """
    sb = _get_supabase()
    counts = {"excel_rows": 0, "excel_sheets": 0, "excel_workbooks": 0}
    try:
        res = sb.table(ROWS_TABLE).delete().eq("file_id", file_id).execute()
        counts["excel_rows"] = len(res.data or [])
    except Exception as e:
        print(f"[embedder] delete_excel_data: excel_rows delete failed for file_id={file_id}: {e}")
    try:
        res = sb.table(SHEETS_TABLE).delete().eq("file_id", file_id).execute()
        counts["excel_sheets"] = len(res.data or [])
    except Exception as e:
        print(f"[embedder] delete_excel_data: excel_sheets delete failed for file_id={file_id}: {e}")
    try:
        res = sb.table(WORKBOOKS_TABLE).delete().eq("file_id", file_id).execute()
        counts["excel_workbooks"] = len(res.data or [])
    except Exception as e:
        print(f"[embedder] delete_excel_data: excel_workbooks delete failed for file_id={file_id}: {e}")
    return counts
