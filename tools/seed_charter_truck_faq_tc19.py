"""CUSTOMER-RAG-2 — add the customer-approved CHARTER-TRUCK (เหมารถ)
service FAQ to Production RAG.

Provenance (source-confirmed, NOT invented):
  * Ai.xlsx  sheet '1.thameuangton'  row 19.0  ("TC19" / customer PDF
    p5 #8), UAT master cases CUS-G19 / CUS-SC1.
  * customer_provided_expected_answer (status SOURCE_CONFIRMED):
      "สวัสดีค่ะ ทางเรามีบริการเหมารถให้ได้นะคะ คุณลูกค้าแจ้งเลขบิล
       และโลเคชั่นปลายทาง พร้อมกับชื่อผู้รับ และเบอร์โทรผู้รับมาได้เลยนะคะ"
  * Customer feedback: "บริการเหมารถ ไม่ควรตอบว่าไม่มีในระบบ ต้องดู TC19".

Production RAG audit: the only related row is RAG-036 (chunk 1e0f7f00,
"ส่งต่อในไทยคิดค่าใช้จ่ายอะไรบ้าง") which is a COST breakdown and asks for
different inputs (จังหวัด/น้ำหนัก/ขนาด/บริษัท). The direct service-
existence question ("มีบริการเหมารถไหม") had NO coverage -> no-info /
Human CS. This adds ONE clean FAQ row via the EXISTING Quick_FAQ_Patch
mechanism (a knowledge_chunks row + its knowledge_items index row),
using the SAME shape tools/backfill_faq_index_for_patch.py produced.

Idempotent: re-running updates the same chunk/item in place, never
duplicates. No re-ingest, no unrelated chunk touched, no code path.

    python -m tools.seed_charter_truck_faq_tc19
"""
import io
import sys
import uuid

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

sys.path.insert(0, ".")
from config import SUPABASE_URL, SUPABASE_KEY
from supabase import create_client
from services.embedding_service import get_embedding_provider

FILE_ID = "40519066-2685-4d12-a7e1-92686eeeb0d7"  # Quick_FAQ_Patch_2026-08-31.xlsx
FILE_NAME = "Quick_FAQ_Patch_2026-08-31.xlsx"

QUESTION = "มีบริการเหมารถไหม"
# verbatim from the source-confirmed customer_provided_expected_answer
# (Ai.xlsx 1.thameuangton row 19 / TC19 / CUS-G19).
ANSWER = ("สวัสดีค่ะ ทางเรามีบริการเหมารถให้ได้นะคะ "
          "คุณลูกค้าแจ้งเลขบิล และโลเคชั่นปลายทาง พร้อมกับชื่อผู้รับ "
          "และเบอร์โทรผู้รับมาได้เลยนะคะ")
ALT_QUESTIONS = [
    "มีบริการเหมารถไหมคะ",
    "เหมารถให้ได้ไหม",
    "เรียกรถให้ได้ไหม",
    "สามารถเหมารถได้ไหม",
    "รับเหมารถส่งของไหม",
    "เหมารถส่งต่อในไทยได้ไหม",
    "มีบริการเรียกรถส่งของไหม",
    "เหมารถส่งของให้หน่อยได้ไหม",
]
TAGS = ["เหมารถ", "เรียกรถ", "ขนส่งในไทย", "บริการเหมารถ"]
ROW_INDEX = 19  # TC19 / Ai.xlsx sheet '1.thameuangton' row 19

_CONTENT = (
    f"Question: {QUESTION}\n"
    f"Answer: {ANSWER}\n"
    f"Alternative phrasings: {' / '.join(ALT_QUESTIONS)}\n"
    f"Tags: {', '.join(TAGS)}"
)
_EMBEDDING_TEXT = f"Document: {FILE_NAME}\nContent:\n{_CONTENT}"


def main():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    provider = get_embedding_provider()

    existing = sb.table("knowledge_chunks").select("id,content").eq("file_id", FILE_ID) \
        .ilike("content", f"Question: {QUESTION}%").execute().data
    chunk_id = existing[0]["id"] if existing else str(uuid.uuid4())
    vector = provider.embed_documents([_EMBEDDING_TEXT])[0]

    meta = {
        "tags": TAGS, "scope": "General", "source": "uploaded_file", "file_id": FILE_ID,
        "version": 1, "category": "quick_faq_patch", "chunk_id": chunk_id, "language": "th",
        "platform": "Website", "file_name": FILE_NAME, "file_type": "xlsx", "is_active": True,
        "page_number": 1,
        "uploaded_by": "claude_code_customer_rag2_tc19",
        "provenance": "Ai.xlsx sheet '1.thameuangton' row 19 (TC19) / CUS-G19 / CUS-SC1",
        "embedding_text": _EMBEDDING_TEXT,
        "embedding_model": "text-embedding-3-large", "embedding_version": "openai-te3l-v1",
        "embedding_provider": "openai", "embedding_dimensions": 3072,
    }
    chunk_row = {
        "id": chunk_id, "content": _CONTENT, "source": FILE_NAME, "intent": "ขนส่ง",
        "file_id": FILE_ID, "metadata": meta, "is_active": True, "version": 1,
        "embedding": vector,
    }
    if existing:
        sb.table("knowledge_chunks").update(chunk_row).eq("id", chunk_id).execute()
        print(f"updated knowledge_chunks {chunk_id}")
    else:
        sb.table("knowledge_chunks").insert(chunk_row).execute()
        print(f"inserted knowledge_chunks {chunk_id}")

    sb.table("knowledge_items").delete().eq("chunk_id", chunk_id).execute()
    sb.table("knowledge_items").insert({
        "id": str(uuid.uuid4()), "knowledge_file_id": FILE_ID, "chunk_id": chunk_id,
        "question": QUESTION, "answer": ANSWER, "sheet_name": "Quick_FAQ_Patch",
        "row_index": ROW_INDEX, "tags": TAGS, "alt_questions": ALT_QUESTIONS, "language": "th",
    }).execute()
    print(f"knowledge_items row written for chunk {chunk_id}")
    print("done — charter-truck (เหมารถ / TC19) FAQ is now in Production RAG")


if __name__ == "__main__":
    main()
