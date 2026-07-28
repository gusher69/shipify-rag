"""One-off metadata-only backfill: regenerates suggested_questions for
EXISTING active chunks using the new per-section generator
(ingestion/ingest.py::_generate_section_suggested_question), without
re-embedding or touching the embedding vector at all — suggested_questions
is not part of embedding_text, so this is a pure metadata PATCH.

Usage:
    python -m tools.backfill_suggested_questions --dry-run
    python -m tools.backfill_suggested_questions --confirm
"""
import argparse

from ingestion.ingest import _generate_section_suggested_question


def _plan():
    from ingestion.embedder import _get_supabase
    sb = _get_supabase()
    rows = sb.table("knowledge_chunks").select("id,metadata").eq("is_active", True).execute().data or []

    updates = []
    for r in rows:
        meta = r.get("metadata") or {}
        heading = meta.get("section_title")
        entity = meta.get("document_title") or meta.get("file_name")
        old = (meta.get("suggested_questions") or [None])[0]
        new = _generate_section_suggested_question(heading, entity, meta.get("suggested_questions") or [])
        if new and new != old:
            updates.append({"id": r["id"], "file_name": meta.get("file_name"), "section_title": heading,
                             "old": old, "new": new})
    return updates


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    updates = _plan()
    print(f"\n{len(updates)} chunk(s) would have suggested_questions updated:")
    for u in updates:
        print(f"  {u['file_name']} > {u['section_title']}: {u['old']!r} -> {u['new']!r}")

    if args.dry_run:
        print("\nDry run only — no writes made.")
        return

    from ingestion.embedder import _get_supabase
    sb = _get_supabase()
    for u in updates:
        row = sb.table("knowledge_chunks").select("metadata").eq("id", u["id"]).execute().data[0]
        meta = row.get("metadata") or {}
        meta["suggested_questions"] = [u["new"]]
        sb.table("knowledge_chunks").update({"metadata": meta}).eq("id", u["id"]).execute()
    print(f"\nUpdated {len(updates)} chunk(s). No embeddings were touched.")


if __name__ == "__main__":
    main()
