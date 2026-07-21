import requests
from typing import List
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, ImageMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from config import LINE_CHANNEL_SECRET, LINE_CHANNEL_TOKEN, LINE_NOTIFY_TOKEN, CONFIDENCE_THRESHOLD, AUTO_MODE
from line_bot.intent import classify
from line_bot.tone import generate_reply
from rag.searcher import search, format_context
from erp.bridge import get_stock, get_order
from profiles.manager import get_profile, upsert_profile
from line_bot.message_adapter import build_line_text_messages

app = FastAPI()
handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_TOKEN)

# session memory (ใช้ dict ง่ายๆ ก่อน — เปลี่ยนเป็น Redis ทีหลังได้)
sessions: dict = {}


def send_line_notify(message: str):
    """แจ้ง CS ผ่าน LINE Notify"""
    try:
        requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {LINE_NOTIFY_TOKEN}"},
            data={"message": message},
            timeout=5,
        )
    except Exception as e:
        print(f"❌ LINE Notify ล้มเหลว: {e}")


@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode(), signature)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return {"status": "ok"}


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    user_id = event.source.user_id
    question = event.message.text

    # 1. ดึง user profile
    profile = get_profile(user_id)

    # 2. classify intent
    intent = classify(question)

    # 3. ดึงข้อมูลตาม intent
    rag_context = ""
    erp_data    = None
    chunks      = []

    if intent in ["นโยบาย", "ทั่วไป"]:
        chunks      = search(question)
        rag_context = format_context(chunks)

    if intent in ["สต็อก", "ออเดอร์"]:
        # ดึง order/stock จาก ERP — TODO: extract SKU/order_id จาก question
        erp_data = {"note": "TODO: extract entity จาก question แล้วดึงจาก ERP"}

    # 4. สร้างคำตอบ
    result     = generate_reply(question, rag_context, erp_data, profile)
    reply_text = result["reply"]
    confidence = result["confidence"]

    # 5. smart handoff — resolved from the Default AI Policy set
    # (services/policy_studio_service.py / AI Policies UI) rather than a
    # hardcoded threshold/message, so an admin's escalation settings
    # actually take effect on real LINE OA traffic, not just in the
    # Playground. Falls back to the existing CONFIDENCE_THRESHOLD/keyword
    # list if the policy set is unreachable (get_escalation_settings()
    # already degrades to DEFAULT_CONFIG internally).
    from services.policy_engine import get_escalation_settings, DISSATISFACTION_KEYWORDS
    esc = get_escalation_settings()
    dissatisfaction_hit = any(kw in question for kw in DISSATISFACTION_KEYWORDS)
    is_handoff = esc["enabled"] and (
        (esc["escalate_on_dissatisfaction"] and dissatisfaction_hit)
        or (esc["escalate_on_no_answer"] and confidence < esc["confidence_threshold"])
    )

    if is_handoff:
        send_line_notify(f"🚨 Smart Handoff\nUser: {user_id}\nคำถาม: {question}\nConfidence: {confidence:.2f}")
        reply_text = esc["message"]

    # 6. รวบรวม attachments จาก RAG results — images ส่งเป็น ImageMessage
    # แยกก้อน, ไฟล์อื่น (PDF ฯลฯ) แนบลิงก์ดาวน์โหลดไว้ในข้อความ text แทน
    # (LINE Messaging API ไม่มี "file message" สำหรับ push/reply ทั่วไป)
    # Gated by the Default AI Policy set's Attachment Rules — "Text first"
    # is already structural (text message is always built/sent first,
    # images appended after; see step 7 below), "skip missing/broken" is
    # always enforced (a URL-less attachment is never useful regardless
    # of the toggle), and send_image/send_file_link are real on/off switches.
    from services.policy_studio_service import get_default_policy_set
    att_rules = (get_default_policy_set().get("config") or {}).get("attachment_rules", {})
    send_images = att_rules.get("send_image_if_available", True)
    send_file_links = att_rules.get("send_file_link_if_available", True)

    image_messages = []
    file_links: List[str] = []
    if not is_handoff and chunks:
        seen_urls: set = set()
        for chunk in chunks:
            for att in (chunk.get("attachments") or []):
                pub_url = att.get("public_url")
                if not pub_url or pub_url in seen_urls:
                    continue
                atype = (att.get("attachment_type") or "").lower()
                mime  = att.get("mime_type") or ""
                is_image = atype == "image" or (
                    not atype and (mime.startswith("image/") or
                                   pub_url.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")))
                )
                if is_image and send_images:
                    seen_urls.add(pub_url)
                    # LINE supports max 5 messages per reply; reserve 1 for text
                    if len(image_messages) < 4:
                        image_messages.append(ImageMessage(
                            original_content_url=pub_url,
                            preview_image_url=pub_url,
                        ))
                elif not is_image and send_file_links:
                    # PDF, Office documents, archives, video, and anything
                    # else all become a download link in the text reply —
                    # LINE's Messaging API has no generic "file message" for
                    # push/reply. Video getting its own native message type
                    # is explicitly future work (per the smart-importer
                    # spec), not implemented here.
                    seen_urls.add(pub_url)
                    icon = {
                        "pdf": "📄", "document": "📝", "spreadsheet": "📊",
                        "presentation": "📽️", "archive": "🗜️", "video": "🎬",
                    }.get(atype, "📎")
                    fname = att.get("filename") or att.get("original_filename") or "file"
                    file_links.append(f"{icon} {fname}: {pub_url}")

    # 7. ส่งคำตอบกลับ LINE (text ก่อนเสมอ — ไฟล์แนบที่ไม่ใช่รูปจะถูกใส่เป็น
    # ลิงก์ดาวน์โหลดต่อท้ายข้อความ text นี้ แล้วค่อยตามด้วยรูปภาพแยกข้อความ)
    full_reply_text = reply_text
    if file_links:
        full_reply_text = reply_text + "\n\n" + "\n".join(file_links)
    # Message Segmentation for LINE (Part 9, P0 2026-07-21) — every text
    # segment becomes its own LINE TextMessage instead of one giant reply,
    # using the SAME deterministic split logic (services/message_segmenter.py)
    # the AI Playground already uses — never a second, separately-
    # maintained splitter. Never sends reply_text as one message when it
    # can be safely segmented; respects LINE's 5-message reply cap by
    # merging the least-important trailing text segments when images
    # already occupy some of that budget. Attachment ordering (text
    # first, then images) is unchanged from before this fix.
    text_messages = build_line_text_messages(full_reply_text, max(1, 5 - len(image_messages)))
    messages = text_messages + image_messages
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=messages,
        ))

    # 7. อัพเดท profile
    upsert_profile(user_id, {
        "display_name": profile.get("display_name", "") if profile else "",
        "order_count":  profile.get("order_count", 0) if profile else 0,
        "total_spend":  profile.get("total_spend", 0) if profile else 0,
        "notes":        f"ถามเรื่อง: {intent}",
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
