"""Minimal in-place trusted-knowledge alignment (approved 2026-09-01).

Two corrections to the source-of-truth knowledge so retrieval AND the
generated answer read from the same values — not a prompt/code override:

1. Transport duration — align to the customer-accepted figures:
     road  6-10 -> 7-10 days
     sea  15-20 -> 14-20 days
   counted from the date goods arrive at the China warehouse.
   (No fixed "Vietnam checkpoint +3-5 days" note exists in the current
   chunks; the generic "อาจคลาดเคลื่อนจากด่าน/ศุลกากร" caveat is left as
   is and is NOT that note.)

2. Wooden-crate wording — an image may not exist in the conversation, so
   "เงื่อนไขการตีลังตามรูปภาพที่แอดมินส่งให้" ->
   "เงื่อนไขการตีลังเป็นไปตามเงื่อนไขที่บริษัทกำหนด".
   Facts preserved: Shipify offers wooden crating; the customer selects
   it when opening the bill.

For every edited chunk: content + metadata.embedding_text are updated and
ONLY that chunk is re-embedded (no re-ingest, no unrelated chunk touched).
Mirrored knowledge_items.answer rows are kept in sync. Idempotent.

    python -m tools.align_trusted_knowledge_2026_09
"""
import io
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")

from config import SUPABASE_URL, SUPABASE_KEY
from supabase import create_client
from services.embedding_service import get_embedding_provider

# chunk_id -> ordered list of (old, new) literal replacements
EDITS = {
    # --- transport duration ---
    "630e3d0c-2ffa-4ffa-9b8e-b771a0d394a6": [
        ("ประมาณ 6–10 วัน", "ประมาณ 7–10 วัน"),
        ("ประมาณ 15–20 วัน", "ประมาณ 14–20 วัน"),
    ],
    "49fa6534-b199-4bc7-9f18-60ef767cd329": [
        ("เริ่มนับ 6-10 วันตอนไหน", "เริ่มนับ 7-10 วันตอนไหน"),
    ],
    "405f94b8-6d03-4325-8f90-418d598dc332": [
        ("ทางรถประมาณ 6–10 วัน หรือทางเรือประมาณ 15–20 วัน",
         "ทางรถประมาณ 7–10 วัน หรือทางเรือประมาณ 14–20 วัน"),
    ],
    "5d168a64-2db6-4aa6-9241-e0752e200258": [
        ("ทางรถประมาณ 6–10 วัน หรือทางเรือประมาณ 15–20 วัน",
         "ทางรถประมาณ 7–10 วัน หรือทางเรือประมาณ 14–20 วัน"),
    ],
    # --- wooden crate wording ---
    "d164742f-dcc1-4e28-8b0f-ec9442425eb0": [
        ("เงื่อนไขการตีลังตามรูปภาพที่แอดมินส่งให้เลยนะคะ",
         "เงื่อนไขการตีลังเป็นไปตามเงื่อนไขที่บริษัทกำหนดนะคะ"),
    ],
    "c24215bc-d4de-4b68-b4b5-d8eb14e3cce6": [
        ("เงื่อนไขการตีลังตามรูปภาพที่แอดมินส่งให้เลยนะคะ",
         "เงื่อนไขการตีลังเป็นไปตามเงื่อนไขที่บริษัทกำหนดนะคะ"),
    ],
}


def _apply(text, pairs):
    if not text:
        return text, 0
    n = 0
    for old, new in pairs:
        if old in text:
            text = text.replace(old, new)
            n += 1
        elif new in text:
            pass  # already applied
    return text, n


def main():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    provider = get_embedding_provider()
    rows = sb.table("knowledge_chunks").select(
        "id,content,section_title,metadata").in_("id", list(EDITS)).execute().data
    by_id = {r["id"]: r for r in rows}

    for cid, pairs in EDITS.items():
        r = by_id.get(cid)
        if not r:
            print(f"  MISSING chunk {cid}"); continue
        new_content, c1 = _apply(r["content"], pairs)
        meta = r.get("metadata") or {}
        new_et, c2 = _apply(meta.get("embedding_text"), pairs)
        new_section, _ = _apply(r.get("section_title"), pairs)

        if new_content == r["content"] and new_et == meta.get("embedding_text"):
            print(f"  {cid[:8]}  no change (already aligned)"); continue

        embed_src = new_et or new_content
        vector = provider.embed_documents([embed_src])[0]
        meta["embedding_text"] = new_et or meta.get("embedding_text")
        sb.table("knowledge_chunks").update({
            "content": new_content,
            "section_title": new_section,
            "metadata": meta,
            "embedding": vector,
        }).eq("id", cid).execute()
        print(f"  {cid[:8]}  content_edits={c1} et_edits={c2} re-embedded")

        # keep any mirrored knowledge_items answer in sync
        items = sb.table("knowledge_items").select("id,answer").eq("chunk_id", cid).execute().data
        for it in items:
            new_ans, _ = _apply(it.get("answer"), pairs)
            if new_ans != it.get("answer"):
                sb.table("knowledge_items").update({"answer": new_ans}).eq("id", it["id"]).execute()
                print(f"      knowledge_items {it['id'][:8]} answer synced")

    print("done")


if __name__ == "__main__":
    main()
