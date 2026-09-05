# -*- coding: utf-8 -*-
"""CUSTOMER-RED-HOTFIX-2 -- seed/update the Purchase Withdrawal (CUS-S05)
and Shipping Withdrawal SP/FT (CUS-S12) Knowledge Base content.

Each answer is the customer-approved literal text from
tests/customer_uat/customer_uat_master.jsonl (CUS-S05 / CUS-S12),
inserted as an ordinary `knowledge_chunks` row via the SAME embedding
pipeline every other document uses (ingestion/embedder.py's
upsert_chunks / services/embedding_service.py) -- no second KB, no new
storage. Each row is tagged with a STABLE `intent` value
(PURCHASE_WITHDRAWAL / SHIPPING_WITHDRAWAL_SP / SHIPPING_WITHDRAWAL_FT)
that services/withdrawal_flow.py reads back with a direct tag lookup
(never vector similarity), so a superficially-similar but wrong chunk
(e.g. the existing order-cancellation / seller-refund FAQ) can never
win once the intent is already known -- and that existing FAQ is left
completely untouched.

Idempotent: re-running this deactivates and replaces ONLY the chunks
this script itself previously created (matched by `intent`), under one
dedicated knowledge_files row it registers once and reuses on every
later run (looked up by filename, never re-registered).

Run once against production:  python -m tools.add_withdrawal_kb_content
"""
from ingestion.embedder import (
    get_file_id, register_file, upsert_chunks, mark_file_synced,
)
from services.supabase_client import get_supabase

_FILE_NAME = "CUSTOMER_RED_HOTFIX_2_Withdrawal_FAQ"

# verbatim from tests/customer_uat/customer_uat_master.jsonl CUS-S05
_PURCHASE_WITHDRAWAL_ANSWER = (
    "คุณลูกค้าเข้าหน้าแอปหรือหน้าเว็บ จะมีเมนู รายการเติมเงินนะคะ\n"
    "จากนั้นเลือกปุ่มเครดิตสั่งซื้อนะคะ\n"
    "ขวามือจะมีปุ่มถอนเงินสีเหลือง คุณลูกค้ากดถอนและกรอกข้อมูลเลขบัญชีมาได้เลยค่ะ\n"
    "ระยะเวลาในการถอนประมาณ 3-5 วัน นับจากวันที่แจ้งถอนมานะคะ"
)

# verbatim from tests/customer_uat/customer_uat_master.jsonl CUS-S12 (SP branch)
_SHIPPING_WITHDRAWAL_SP_ANSWER = (
    "คุณลูกค้าสามารถกรอกข้อมูลรายละเอียดในแบบฟอร์มรูปภาพที่แอดมินส่งให้ "
    "และส่งเอกสารสำเนาบัตรประชาชนมาให้แอดมินได้เลยค่ะ"
)

# verbatim from tests/customer_uat/customer_uat_master.jsonl CUS-S12 (FT branch)
_SHIPPING_WITHDRAWAL_FT_ANSWER = (
    "คุณลูกค้าสามารถเข้าที่เมนู ประวัติการชำระเงินขนส่ง "
    "และขวามือจะมีปุ่ม ถอนเงินจากระบบ นะคะ สามารถกดถอนเข้ามาได้เลยค่ะ"
)

_CHUNKS = [
    {
        "intent": "PURCHASE_WITHDRAWAL",
        "text": _PURCHASE_WITHDRAWAL_ANSWER,
        "source": "customer_uat_master.jsonl:CUS-S05",
        "section_title": "Purchase Withdrawal (ถอนเงินสั่งซื้อ)",
        "tags": ["ถอนเงินสั่งซื้อ", "เครดิตสั่งซื้อ", "รายการเติมเงิน", "เงินที่ร้านคืนมา"],
    },
    {
        "intent": "SHIPPING_WITHDRAWAL_SP",
        "text": _SHIPPING_WITHDRAWAL_SP_ANSWER,
        "source": "customer_uat_master.jsonl:CUS-S12:SP",
        "section_title": "Shipping Withdrawal - SP brand (ถอนเงินขนส่ง SP)",
        "tags": ["ถอนเงินขนส่ง", "SP", "แบบฟอร์ม", "บัตรประชาชน"],
    },
    {
        "intent": "SHIPPING_WITHDRAWAL_FT",
        "text": _SHIPPING_WITHDRAWAL_FT_ANSWER,
        "source": "customer_uat_master.jsonl:CUS-S12:FT",
        "section_title": "Shipping Withdrawal - FT brand (ถอนเงินขนส่ง FT)",
        "tags": ["ถอนเงินขนส่ง", "FT", "ประวัติการชำระเงินขนส่ง", "ถอนเงินจากระบบ"],
    },
]


def main() -> None:
    sb = get_supabase()

    file_id = get_file_id(_FILE_NAME)
    if file_id is None:
        file_id = register_file(_FILE_NAME, size=sum(len(c["text"]) for c in _CHUNKS))
        print(f"registered new knowledge_files row {file_id} ({_FILE_NAME})")
    else:
        print(f"reusing existing knowledge_files row {file_id} ({_FILE_NAME})")

    intents = [c["intent"] for c in _CHUNKS]
    existing = (sb.table("knowledge_chunks").select("id,intent")
                .in_("intent", intents).eq("is_active", True).execute().data)
    if existing:
        ids = [row["id"] for row in existing]
        sb.table("knowledge_chunks").update({"is_active": False}).in_("id", ids).execute()
        print(f"deactivated {len(ids)} previous active chunk(s) for {intents} before replacing")

    to_insert = [
        {
            "file_id": file_id,
            "text": c["text"],
            "source": c["source"],
            "intent": c["intent"],
            "metadata": {
                "file_name": _FILE_NAME,
                "document_title": "Withdrawal FAQ",
                "section_title": c["section_title"],
                "tags": c["tags"],
                "scope": "General",
                "document_purpose": "faq",
            },
        }
        for c in _CHUNKS
    ]
    inserted = upsert_chunks(to_insert)
    mark_file_synced(file_id, inserted)
    print(f"done — {inserted}/{len(_CHUNKS)} withdrawal KB chunk(s) active")


if __name__ == "__main__":
    main()
