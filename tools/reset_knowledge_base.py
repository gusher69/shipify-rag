"""Phase 2 clean-reset tool — deletes ALL RAG knowledge data (files,
chunks, vectors, graph, attachments, structured Excel storage, sync/
ingestion jobs) so the OpenAI embedding migration starts from an empty,
unambiguous state. Never touches unrelated tables (ai_sessions,
ai_session_messages, ai_prompt_templates, ai_prompt_assignments, users,
auth, settings) — those are outside RAG_TABLES entirely.

Usage:
    python -m tools.reset_knowledge_base --dry-run
    python -m tools.reset_knowledge_base --confirm

Deletion order matters: knowledge_sync_jobs/knowledge_sync_job_files have
no FK to knowledge_files (cleared explicitly first); everything else
(knowledge_chunks, knowledge_graph_nodes/edges, knowledge_attachments,
knowledge_items, excel_workbooks->sheets->rows) cascades automatically
from deleting knowledge_files rows (see migrations/012_delete_cascade_fixes.sql,
migrations/015_knowledge_graph.sql) — deleting knowledge_files last, after
confirming cascade will reach everything, is what actually empties them.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

BACKUP_DIR = Path("backups")

# Tables considered part of "RAG knowledge data" for this reset. Order is
# backup/report order, not deletion order.
RAG_TABLES = [
    "knowledge_files",
    "knowledge_chunks",
    "knowledge_graph_nodes",
    "knowledge_graph_edges",
    "knowledge_attachments",
    "knowledge_items",
    "excel_workbooks",
    "excel_sheets",
    "excel_rows",
    "knowledge_sync_jobs",
    "knowledge_sync_job_files",
]

# Explicitly NOT touched — unrelated application data confirmed safe by
# the migration history (ai_sessions/ai_session_messages = Playground
# history, ai_prompt_templates/ai_prompt_assignments = Prompt Studio).
PRESERVED_TABLES = [
    "ai_sessions", "ai_session_messages", "ai_prompt_templates", "ai_prompt_assignments",
]


def _sb():
    from ingestion.embedder import _get_supabase
    return _get_supabase()


def _count(sb, table: str) -> int:
    try:
        res = sb.table(table).select("id", count="exact").limit(1).execute()
        return res.count or 0
    except Exception as e:
        print(f"[reset] could not count {table}: {e}")
        return -1


def _fetch_all(sb, table: str):
    try:
        return sb.table(table).select("*").execute().data or []
    except Exception as e:
        print(f"[reset] could not fetch {table} for backup: {e}")
        return []


def dry_run() -> dict:
    sb = _sb()
    counts = {t: _count(sb, t) for t in RAG_TABLES}
    files = sb.table("knowledge_files").select("id,filename").execute().data or []
    return {"counts": counts, "files": [f["filename"] for f in files], "preserved_tables": PRESERVED_TABLES}


def backup() -> dict:
    """Exports every RAG table's full current contents to one timestamped
    JSON file per table under backups/<timestamp>/. Embedding vectors are
    included as-is (local 768-dim float arrays) — this is a test-data
    emergency-recovery backup, not a production export pipeline."""
    sb = _sb()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = BACKUP_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    row_counts = {}
    for table in RAG_TABLES:
        rows = _fetch_all(sb, table)
        (out_dir / f"{table}.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
        row_counts[table] = len(rows)
    return {"backup_path": str(out_dir), "timestamp": ts, "tables": RAG_TABLES, "row_counts": row_counts}


def execute_reset() -> dict:
    sb = _sb()
    deleted = {}

    # Standalone tables with no FK to knowledge_files — clear explicitly.
    for table in ("knowledge_sync_job_files", "knowledge_sync_jobs"):
        before = _count(sb, table)
        try:
            sb.table(table).delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
        except Exception as e:
            print(f"[reset] delete failed for {table}: {e}")
        deleted[table] = before

    # Deleting knowledge_files cascades to knowledge_chunks,
    # knowledge_graph_nodes/edges, knowledge_attachments, knowledge_items,
    # excel_workbooks (-> excel_sheets -> excel_rows) per migrations
    # 012/015. This must run AFTER the sync-job cleanup above so nothing
    # references a file mid-cleanup.
    before_files = _count(sb, "knowledge_files")
    before_chunks = _count(sb, "knowledge_chunks")
    try:
        sb.table("knowledge_files").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
    except Exception as e:
        raise RuntimeError(f"knowledge_files delete failed — reset aborted mid-way: {e}")
    deleted["knowledge_files"] = before_files
    deleted["knowledge_chunks (cascaded)"] = before_chunks

    after = {t: _count(sb, t) for t in RAG_TABLES}
    return {"deleted_before_counts": deleted, "after_counts": after}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        plan = dry_run()
        print("\n=== Reset dry-run (no writes) ===")
        print("Rows to remove per table:")
        for t, c in plan["counts"].items():
            print(f"  {t}: {c}")
        print(f"\nKnowledge files to remove ({len(plan['files'])}):")
        for f in plan["files"]:
            print(f"  - {f}")
        print(f"\nPreserved (NOT touched): {', '.join(plan['preserved_tables'])}")
        return

    print("\n=== Backing up before reset ===")
    b = backup()
    print(json.dumps(b, indent=2))

    print("\n=== Executing reset ===")
    result = execute_reset()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
