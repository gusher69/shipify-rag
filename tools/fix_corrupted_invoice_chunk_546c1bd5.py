# -*- coding: utf-8 -*-
"""INVOICE-PRODUCT-REGRESSION-2 (Problem A) — replace the corrupted
Invoice FAQ answer at its SOURCE.

Chunk 546c1bd5 (Quick_FAQ_Patch row 6, "ใบกำกับค่าสินค้าออกได้ไหม") is an
ACTIVE FAQ-exact match whose stored Answer is:

    "สวัสดีค่ะ ทางเราสามารถออกใบกำกับค่าสินค้า และใบเสร็จค่าขนส่งให้ได้นะคะ
     ไม่ทราบว่าสินค้าของลูกค้าเป็นอะไรคะ"

— it ends with an unnecessary product question and omits every real
condition (bill / payment condition, taxpayer-info requirement,
credit-card limitation). It is superseded by the complete, active,
customer-approved trusted rows ff288877 ("ออกใบกำกับได้ไหม") and
99390831 ("Shipify ออก e-Tax Invoice ได้ไหม"). Routing around it in
services/playground_orchestrator.py is not enough — any invoice phrasing
the deterministic branch does not catch can still surface this text.

This replaces ONLY the Answer line of THIS ONE chunk with the clean
approved wording (_INVOICE_ISSUANCE_ANSWER, composed verbatim from
ff288877 + 99390831), keeps the Question / Alternative phrasings / Tags,
re-embeds the chunk, and syncs its knowledge_items answer. Idempotent —
running it twice is a no-op. No other KB row is touched.

Run once against production:  python -m tools.fix_corrupted_invoice_chunk_546c1bd5
"""
import os
import re

from supabase import create_client

from services.embedding_service import get_embedding_provider

CHUNK_ID = "546c1bd5-63a4-4cf6-9dee-5bfdcb88e6c0"

# verbatim from services/playground_orchestrator.py::_INVOICE_ISSUANCE_ANSWER
CLEAN_ANSWER = (
    "ทางเราสามารถออกใบกำกับค่าสินค้าและใบเสร็จค่าขนส่งให้ได้ค่ะ ตามเงื่อนไขของบิลและวิธีชำระเงิน "
    "หากต้องการใบกำกับภาษี รบกวนแจ้งข้อมูลผู้เสียภาษีและเลขบิลให้เจ้าหน้าที่ตรวจสอบเงื่อนไขก่อนชำระเงินนะคะ "
    "ทั้งนี้ การชำระค่าสินค้าด้วยบัตรเครดิตจะไม่สามารถออกใบกำกับได้ตามข้อมูลปัจจุบันค่ะ")

_BAD_MARKER = "ไม่ทราบว่าสินค้าของลูกค้าเป็นอะไร"
_ANSWER_LINE_RE = re.compile(r"(?m)^Answer:\s*.*$")


def _rewrite_answer(content: str) -> str:
    return _ANSWER_LINE_RE.sub("Answer: " + CLEAN_ANSWER, content, count=1)


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
    if _BAD_MARKER not in content and CLEAN_ANSWER in content:
        print("already clean — no change")
        return

    new_content = _rewrite_answer(content)
    if new_content == content:
        print("answer line not found / unchanged — aborting (no blind write)")
        return

    meta = r.get("metadata") or {}
    embed_src = new_content
    if meta.get("embedding_text"):
        meta["embedding_text"] = re.sub(
            r"(?s)ไม่ทราบว่าสินค้าของลูกค้าเป็นอะไร.*$", "", meta["embedding_text"]).strip() \
            + " " + CLEAN_ANSWER
        embed_src = meta["embedding_text"]

    vector = get_embedding_provider().embed_documents([embed_src])[0]
    sb.table("knowledge_chunks").update({
        "content": new_content,
        "metadata": meta,
        "embedding": vector,
    }).eq("id", CHUNK_ID).execute()
    print(f"{CHUNK_ID[:8]}  Answer replaced with clean approved wording + re-embedded")
    print("  BEFORE:", content.splitlines()[1][:120])
    print("  AFTER :", new_content.splitlines()[1][:120])

    items = sb.table("knowledge_items").select("id,answer").eq("chunk_id", CHUNK_ID).execute().data
    for it in items:
        if _BAD_MARKER in (it.get("answer") or "") or it.get("answer") != CLEAN_ANSWER:
            sb.table("knowledge_items").update({"answer": CLEAN_ANSWER}).eq("id", it["id"]).execute()
            print(f"  knowledge_items {it['id'][:8]}  answer synced")
    print("done")


if __name__ == "__main__":
    main()
