"""Backfill the structured FAQ index (knowledge_items) for a Q&A-format
knowledge file whose chunks were ingested but whose knowledge_items rows
were never created — so rag/faq_matcher.py::match_faq_exact can return
the file's verbatim customer-approved answers (with their own natural
follow-up questions) instead of falling through to generic hybrid
retrieval + LLM rephrasing.

Discovered 2026-08-31: Quick_FAQ_Patch_2026-08-31.xlsx (file_id
40519066…) has 9 knowledge_chunks but 0 knowledge_items, so exact
customer questions ("ครีมอาบน้ำนำเข้าได้ไหม", "มีขนส่งทางเครื่องบินไหม",
"ใบกำกับค่าสินค้าออกได้ไหม", …) never short-circuit to the approved
answer.

Idempotent: deletes then re-inserts knowledge_items for the target file.
Parses each chunk's own "Question: / Answer: / Alternative phrasings: /
Tags:" content — no new schema, no re-embedding, no chunk changes.

    python -m tools.backfill_faq_index_for_patch [FILE_ID]
"""
import io
import re
import sys
import uuid

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

sys.path.insert(0, ".")
from config import SUPABASE_URL, SUPABASE_KEY
from supabase import create_client

DEFAULT_FILE_ID = "40519066-2685-4d12-a7e1-92686eeeb0d7"  # Quick_FAQ_Patch_2026-08-31.xlsx

# One category enrichment, NOT a per-product patch: the trusted rule is
# "ของเหลวไม่รับนำเข้า"; these are ordinary liquid personal-care products
# a customer would ask about with the exact same import question, added
# to the liquid FAQ's alt_questions so paraphrases like "แชมพูนำเข้าได้ไหม"
# resolve to the same approved answer without one FAQ row per product.
_LIQUID_PRODUCT_TERMS = ["แชมพู", "ครีม", "โลชั่น", "เจล", "น้ำยา", "สบู่เหลว",
                          "เซรั่ม", "โฟมล้างหน้า", "ยาสระผม", "ครีมนวดผม"]


def _parse_chunk(content: str):
    q = re.search(r"Question:\s*(.+?)(?:\n|$)", content)
    a = re.search(r"Answer:\s*(.+?)(?:\nAlternative phrasings:|\nTags:|$)", content, re.DOTALL)
    alt = re.search(r"Alternative phrasings:\s*(.+?)(?:\nTags:|$)", content, re.DOTALL)
    question = (q.group(1).strip() if q else "") or ""
    answer = (a.group(1).strip() if a else "") or ""
    alt_questions = []
    if alt:
        alt_questions = [x.strip() for x in re.split(r"\s*/\s*", alt.group(1).strip()) if x.strip()]
    return question, answer, alt_questions


def main(file_id: str):
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    fmeta = sb.table("knowledge_files").select("id,filename").eq("id", file_id).execute().data
    if not fmeta:
        print(f"file_id {file_id} not found"); return
    fname = fmeta[0]["filename"]
    chunks = sb.table("knowledge_chunks").select("id,content,chunk_index,section_title") \
        .eq("file_id", file_id).eq("is_active", True).execute().data
    if not chunks:
        print("no active chunks for this file"); return

    records = []
    for i, c in enumerate(chunks):
        question, answer, alt_questions = _parse_chunk(c.get("content") or "")
        if not question or not answer:
            print(f"  skip chunk {c['id']} — could not parse Question/Answer")
            continue
        # liquid FAQ -> widen alt_questions across the liquid-product category
        if "ของเหลว" in answer and ("ไม่สามารถนำเข้า" in answer or "ไม่รับนำเข้า" in answer):
            for term in _LIQUID_PRODUCT_TERMS:
                for form in (f"{term}นำเข้าได้ไหม", f"สั่ง{term}ได้ไหม", f"ฝากสั่ง{term}ได้ไหม"):
                    if form not in alt_questions:
                        alt_questions.append(form)
        records.append({
            "id": str(uuid.uuid4()),
            "knowledge_file_id": file_id,
            "chunk_id": c["id"],
            "question": question,
            "answer": answer,
            "sheet_name": "Quick_FAQ_Patch",
            "row_index": (c.get("chunk_index") if c.get("chunk_index") is not None else i) + 1,
            "tags": [],
            "alt_questions": alt_questions,
            "language": "th",
        })

    print(f"file: {fname}  ->  {len(records)} knowledge_items to write")
    for r in records:
        print(f"  Q: {r['question']}  | {len(r['alt_questions'])} alt(s)")

    sb.table("knowledge_items").delete().eq("knowledge_file_id", file_id).execute()
    for i in range(0, len(records), 100):
        sb.table("knowledge_items").insert(records[i:i + 100]).execute()
    print(f"done — {len(records)} knowledge_items created for {fname}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FILE_ID)
