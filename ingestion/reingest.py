"""Phase 2 Part 13 — targeted re-ingestion CLI for individual stored files.

Fixes the confirmed chunk_index-reset defect (ingestion/ingest.py's
chunk_pages_by_heading, now fixed) and stale one-giant-chunk staleness by
re-running the SAME Extract -> Analyze -> Chunk -> Embed pipeline the
Sync button uses (admin/routes.py's _run_sync_list), for ONE file, using
the file already stored on disk under knowledge/ — never re-uploads or
re-fetches from Google Drive.

Usage:
    python -m ingestion.reingest --filename "company-profile-test.md" --dry-run
    python -m ingestion.reingest --filename "company-profile-test.md" --force

Safety:
    - --dry-run (default) only prints what WOULD happen: analysis result,
      chunk count/boundaries/indices, and never touches the database.
    - --force is required to actually deactivate old chunks and write new
      ones. Without --force, a plan is printed and nothing is written.
    - Old chunks are deactivated (is_active=False), never deleted — an
      operator can inspect/restore prior state directly in the DB.
    - Uses the LOCAL embedding provider only (via ingestion.embedder,
      unchanged) — this command never calls OpenAI, so it costs nothing
      and needs no separate approval.
"""
import argparse
import sys
from pathlib import Path

from config import LOCAL_STORAGE_ROOT

KNOWLEDGE_DIR = Path(LOCAL_STORAGE_ROOT)


def _get_file_row(sb, filename: str):
    res = (sb.table("knowledge_files").select("id,filename,category,tags,description,scope,platform,version")
           .eq("filename", filename).is_("deleted_at", "null").execute())
    return res.data[0] if res.data else None


def reingest_file(filename: str, *, dry_run: bool = True, force: bool = False) -> dict:
    from ingestion.ingest import analyze_and_chunk, read_file_pages
    from ingestion.embedder import _get_supabase, deactivate_old_chunks, upsert_chunks, mark_file_synced

    file_path = KNOWLEDGE_DIR / filename
    if not file_path.exists():
        raise FileNotFoundError(
            f"{file_path} not found on disk — this command re-ingests EXISTING stored "
            f"files only and will not re-upload or re-download anything."
        )

    sb = _get_supabase()
    row = _get_file_row(sb, filename)
    if not row:
        raise RuntimeError(f"No active knowledge_files row found for filename={filename!r}")
    file_id = row["id"]

    pages = read_file_pages(file_path, vision_ocr_profile="advanced")
    if not pages:
        raise RuntimeError(f"No extractable text found in {file_path}")

    analysis, chunks, _pages = analyze_and_chunk(
        file_path, source=filename, knowledge_analysis_profile="advanced",
        vision_ocr_profile="advanced",
        file_id=file_id, file_name=filename,
        file_type=file_path.suffix.lstrip(".").lower(),
        file_size=file_path.stat().st_size,
        storage_path=f"knowledge/{filename}",
        document_title=row.get("description") or file_path.stem,
        category=row.get("category") or "",
        scope=row.get("scope") or "General Knowledge",
        platform=row.get("platform") or "Internal System",
        tags=[t.strip() for t in (row.get("tags") or "").split(",") if t.strip()],
        version=int(row.get("version") or 1),
    )

    plan = {
        "filename": filename, "file_id": file_id,
        "knowledge_type": analysis.knowledge_type if analysis else None,
        "chunk_strategy": analysis.chunk_strategy if analysis else None,
        "chunk_count": len(chunks),
        "chunk_indices": [c.get("metadata", {}).get("chunk_index") for c in chunks],
        "section_titles": [c.get("metadata", {}).get("section_title") for c in chunks],
    }

    # Regression guard for the exact defect this command exists to fix:
    # every chunk_index must be unique per (file, section-run) — a repeat
    # of "0, 0, 0, 0" across different sections means the fix didn't take.
    indices = plan["chunk_indices"]
    if len(indices) > 1 and len(set(indices)) == 1:
        plan["warning"] = (
            "All chunk_index values are identical — the chunk_index defect "
            "may not be fixed. Refusing to write until this is investigated."
        )
        if force:
            raise RuntimeError(plan["warning"])

    if dry_run or not force:
        plan["dry_run"] = True
        return plan

    deactivate_old_chunks(file_id)
    try:
        chunk_count = upsert_chunks(chunks)
        if chunks and chunk_count == 0:
            raise RuntimeError(f"Embedding produced 0/{len(chunks)} chunks — all embed calls failed")
        mark_file_synced(file_id, chunk_count)
    except Exception:
        # Old chunks are already deactivated — surface the failure rather
        # than silently leaving the file with zero active chunks. No
        # automatic re-activation: an operator should inspect and decide
        # (deactivate_old_chunks() only flips is_active, nothing is lost).
        raise

    plan["dry_run"] = False
    plan["chunks_written"] = chunk_count
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--filename", required=True, help="Exact filename as stored in knowledge_files, e.g. 'company-profile-test.md'")
    parser.add_argument("--force", action="store_true", help="Actually write (deactivate old chunks + embed new ones). Without this, only a dry-run plan is printed.")
    args = parser.parse_args()

    try:
        plan = reingest_file(args.filename, dry_run=not args.force, force=args.force)
    except Exception as e:
        print(f"[reingest] FAILED for {args.filename!r}: {e}")
        sys.exit(1)

    print(f"\n=== Re-ingest plan for {args.filename!r} ===")
    for k, v in plan.items():
        print(f"  {k}: {v}")
    if plan.get("dry_run"):
        print("\nDry run only — no database writes were made. Re-run with --force to apply.")
    else:
        print(f"\nWrote {plan.get('chunks_written')} active chunk(s) using the local embedding provider.")


if __name__ == "__main__":
    main()
