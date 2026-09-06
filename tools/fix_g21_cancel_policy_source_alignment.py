# -*- coding: utf-8 -*-
"""PHASE-3-RAG-COVERAGE-COMPLETION-1 / R5 — realign the active G21
cancellation-policy FAQ answer to the customer-approved source framing.

Chunk f2e2cf8f-8b1a-44fc-acef-c789e3faeb71  (intent "สต็อก", active,
source AI_Knowledge_Master_RAG_FINAL_CLEAN.xlsx, "Question: ยกเลิกคำสั่งซื้อได้ไหม")
currently frames cancellation eligibility on the SYSTEM STATUS
("ยกเลิกได้ก่อนสถานะในระบบเปลี่ยนเป็น สั่งซื้อสำเร็จ").

The customer-approved answer for CUS-G21
(tests/customer_uat/customer_uat_master.jsonl) frames it on PAYMENT:

    "สวัสดีค่ะ หากยังไม่ได้ชำระจะสามารถยกเลิกก่อนได้ค่ะ กรณีที่ชำระแล้ว
     แอดมินขอสอบถามร้านก่อนนะคะว่าจัดส่งสินค้าให้แล้วหรือยังค่ะ"

This replaces ONLY the Answer line of THIS ONE chunk with a wording that:
  - leads with the payment-based eligibility (ยังไม่ได้ชำระ -> ยกเลิกก่อนได้ / ชำระแล้ว -> แอดมินสอบถามร้านว่าจัดส่งแล้วหรือยัง),
  - keeps the shop/factory-consent condition (source: "แอดมินขอสอบถามร้านก่อน"),
  - KEEPS the website-backed refund clause ("เว็บไซต์ระบุประมาณ 3–7 วัน") —
    it is independently supported by customer source, so per the R5 rule it
    is NOT removed,
  - stays policy-only RAG: no per-order eligibility claim, no implication
    that the bot executes the cancellation.

Question / structure are preserved; Alternative phrasings + Tags are
widened with the payment-framed paraphrases for retrieval coverage.
The chunk is re-embedded through the EXISTING pipeline
(services.embedding_service.get_embedding_provider) at the production
model/dimension — no model or dimension change. The knowledge_items twin
answer is synced.

Idempotent — running it twice is a no-op. No other KB row is touched.
The embedding is generated BEFORE any DB write, so if the embedding
provider is unavailable the script aborts cleanly with NO partial change.

Run once against production:  python -m tools.fix_g21_cancel_policy_source_alignment
"""
import os
import re

from supabase import create_client

from services.embedding_service import get_embedding_provider

CHUNK_ID = "f2e2cf8f-8b1a-44fc-acef-c789e3faeb71"

# Source-aligned answer. Payment-framed eligibility first (verbatim intent
# of the CUS-G21 approved answer), shop-consent condition retained, and the
# website-backed 3–7 day refund clause kept unchanged.
SOURCE_ALIGNED_ANSWER = (
    "หากยังไม่ได้ชำระเงิน สามารถยกเลิกคำสั่งซื้อก่อนได้ค่ะ โดยแจ้งเจ้าหน้าที่เพื่อดำเนินการ "
    "กรณีที่ชำระเงินแล้ว แอดมินจะขอสอบถามทางร้านก่อนว่าจัดส่งสินค้าให้แล้วหรือยัง "
    "หากยังไม่จัดส่งและร้านยินยอม จึงจะยกเลิกให้ได้ค่ะ "
    "ทั้งนี้หากร้านคืนเงิน บริษัทจะคืนเงินให้หลังได้รับเงินจากร้านจีนแล้ว "
    "ซึ่งเว็บไซต์ระบุประมาณ 3–7 วันค่ะ"
)

NEW_ALT_PHRASINGS = (
    "ยกเลิกบิลได้ตอนไหน / ยังไม่ได้ชำระเงินยกเลิกได้ไหม / ชำระเงินแล้วยกเลิกได้ไหม / "
    "เปลี่ยนใจหลังสั่งได้หรือไม่ / ขอคืนเงินค่าสินค้าได้ไหม"
)
NEW_TAGS = "ยกเลิก, คืนเงิน, สถานะคำสั่งซื้อ, ชำระเงิน"

NEW_ITEM_TAGS = ["ยกเลิก", "คืนเงิน", "สถานะคำสั่งซื้อ", "ชำระเงิน", "ร้านจีน"]

# marker that identifies the OLD status-framed answer
_OLD_MARKER = "ยกเลิกได้ก่อนสถานะในระบบเปลี่ยนเป็น"
# marker that identifies the ALREADY-ALIGNED answer
_NEW_MARKER = "หากยังไม่ได้ชำระเงิน สามารถยกเลิกคำสั่งซื้อก่อนได้"

_ANSWER_LINE_RE = re.compile(r"(?m)^Answer:\s*.*$")
_ALT_LINE_RE = re.compile(r"(?m)^Alternative phrasings:\s*.*$")
_TAGS_LINE_RE = re.compile(r"(?m)^Tags:\s*.*$")


def _rewrite_content(content: str) -> str:
    out = _ANSWER_LINE_RE.sub("Answer: " + SOURCE_ALIGNED_ANSWER, content, count=1)
    out = _ALT_LINE_RE.sub("Alternative phrasings: " + NEW_ALT_PHRASINGS, out, count=1)
    out = _TAGS_LINE_RE.sub("Tags: " + NEW_TAGS, out, count=1)
    return out


def _rewrite_embedding_text(embedding_text: str) -> str:
    """The stored embedding_text embeds Question + Answer + alts + Tags in a
    'Content:' block. Replace the answer sentence and the alt/tags tails the
    same way, so the regenerated vector matches the new content."""
    t = embedding_text
    t = re.sub(r"Answer:\s*ยกเลิกได้ก่อนสถานะในระบบเปลี่ยนเป็น.*?(?=\nAlternative phrasings:|\Z)",
               "Answer: " + SOURCE_ALIGNED_ANSWER + "\n", t, count=1, flags=re.S)
    t = re.sub(r"Alternative phrasings:.*?(?=\nTags:|\Z)",
               "Alternative phrasings: " + NEW_ALT_PHRASINGS + "\n", t, count=1, flags=re.S)
    t = re.sub(r"Tags:.*$", "Tags: " + NEW_TAGS, t, count=1, flags=re.S)
    return t


def main() -> None:
    url = os.environ["SUPABASE_URL"]
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
    sb = create_client(url, key)

    rows = sb.table("knowledge_chunks").select(
        "id,content,is_active,metadata").eq("id", CHUNK_ID).execute().data
    if not rows:
        print(f"chunk {CHUNK_ID} not found — nothing to do")
        return
    r = rows[0]
    content = r["content"] or ""

    if _NEW_MARKER in content and _OLD_MARKER not in content:
        print("already source-aligned — no change")
        return
    if _OLD_MARKER not in content:
        print("expected old status-framed answer not found — aborting (no blind write)")
        return

    new_content = _rewrite_content(content)
    if new_content == content:
        print("content unchanged after rewrite — aborting (no blind write)")
        return

    meta = dict(r.get("metadata") or {})
    embed_src = new_content
    if meta.get("embedding_text"):
        meta["embedding_text"] = _rewrite_embedding_text(meta["embedding_text"])
        embed_src = meta["embedding_text"]

    # Embedding FIRST — if the provider is unavailable this raises and NOTHING
    # below runs, so the DB is never left half-updated.
    vector = get_embedding_provider().embed_documents([embed_src])[0]

    sb.table("knowledge_chunks").update({
        "content": new_content,
        "metadata": meta,
        "embedding": vector,
    }).eq("id", CHUNK_ID).execute()
    print(f"{CHUNK_ID[:8]}  Answer realigned to payment-framed source wording + re-embedded")
    print("  BEFORE:", content.splitlines()[1][:130])
    print("  AFTER :", new_content.splitlines()[1][:130])

    items = sb.table("knowledge_items").select("id,answer,tags").eq("chunk_id", CHUNK_ID).execute().data
    for it in items:
        if it.get("answer") != SOURCE_ALIGNED_ANSWER:
            sb.table("knowledge_items").update(
                {"answer": SOURCE_ALIGNED_ANSWER, "tags": NEW_ITEM_TAGS}).eq("id", it["id"]).execute()
            print(f"  knowledge_items {it['id'][:8]}  answer + tags synced")
    print("done")


if __name__ == "__main__":
    main()
