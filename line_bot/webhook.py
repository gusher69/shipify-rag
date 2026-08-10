import requests
from typing import List
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, ImageMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from config import LINE_CHANNEL_SECRET, LINE_CHANNEL_TOKEN, LINE_NOTIFY_TOKEN, CONFIDENCE_THRESHOLD, DECISION_ENGINE_LIVE_ROUTING
from line_bot.intent import classify
from line_bot.tone import generate_reply
from rag.searcher import search, format_context
from profiles.manager import get_profile, upsert_profile, update_profile_from_turn
from line_bot.message_adapter import build_line_text_messages
from services.session_service import get_session_service, extract_conversation_fields
from services.customer_tier_service import update_tier_for_profile

app = FastAPI()
handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_TOKEN)


@app.on_event("startup")
async def _validate_embedding_configuration_at_startup():
    """2026-08-01 configuration-hardening pass — this process is the one
    that actually calls rag/searcher.py::search() for real customer
    messages, so it gets the same startup guard as admin.routes:app: log
    the active embedding model/dimension and the database's expected
    dimension once, at boot, and refuse to finish starting if they're
    CONFIRMED to differ (see
    services/embedding_service.py::report_and_validate_embedding_configuration).
    An unreachable DB / unconfigured provider is logged, not fatal — only
    a genuine, confirmed mismatch stops this webhook from ever serving a
    customer message against the wrong vector space."""
    from services.embedding_service import report_and_validate_embedding_configuration
    report_and_validate_embedding_configuration()

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


def _synthetic_result(text: str) -> dict:
    """Shapes a locally-decided reply (cancellation / expiry) exactly
    like a real DecisionEngine.decide() return value, so every
    downstream consumer of `result` in _handle_message_via_decision_engine
    (reply building, Phase 3 conversation-history recording) works
    unchanged — never calls the Decision Engine or the Action Executor,
    so this can never itself trigger any real execution."""
    return {
        "reply": {"text": text, "images": [], "files": []},
        "routing": {"type": "WORKFLOW"}, "handoff_payload": None, "alert": None, "error": None,
        "developer": None,
    }


def _cancelled_result() -> dict:
    return _synthetic_result("รับทราบค่ะ ยกเลิกการดำเนินการแล้ว")


def _expired_result() -> dict:
    return _synthetic_result("คำขอก่อนหน้าหมดเวลายืนยันแล้วค่ะ รบกวนแจ้งคำขอใหม่อีกครั้งนะคะ")


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
    """Production Integration Sprint (2026-08-02), Phase 1 Step D/E —
    temporary rollout dispatch. `config.DECISION_ENGINE_LIVE_ROUTING`
    (default false) picks between the existing, untouched legacy adapter
    and the modern Decision Engine path. Both branches are isolated
    functions below — no shared mutable state, no interleaved logic — so
    removing the legacy branch later (once LINE OA UAT passes, per the
    sprint's explicit plan) is a clean deletion, not an untangling."""
    if DECISION_ENGINE_LIVE_ROUTING:
        _handle_message_via_decision_engine(event)
    else:
        _handle_message_legacy(event)


# ── Legacy adapter (temporary — kept ONLY as an isolated fallback while
# DECISION_ENGINE_LIVE_ROUTING=false; scheduled for removal, together with
# the flag itself, once LINE OA UAT against the Decision Engine passes) ──

def _handle_message_legacy(event: MessageEvent):
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


# ── Decision Engine adapter (Production Integration Sprint, 2026-08-02,
# Phase 1 Step E/F) — active only when DECISION_ENGINE_LIVE_ROUTING=true.
# Routes through services/decision_engine.py::DecisionEngine.decide(),
# the SAME orchestrator (Business Action selection, Hybrid Question
# Classifier, the shared production RAG pipeline, AI Policies, Prompt
# Studio) the AI Playground already exercises — this function only
# adapts its generic reply shape into LINE-specific messages; it
# implements no routing/escalation/attachment-classification logic of
# its own (all of that already happened inside `decide()`, per
# "no duplicate routing logic"). ──

def _handle_message_via_decision_engine(event: MessageEvent):
    from services.decision_engine import DecisionEngine
    from services.pending_confirmation_service import get_pending_confirmation_service, classify_confirmation_reply
    import config as _config

    user_id = event.source.user_id
    question = event.message.text
    profile = get_profile(user_id)

    engine = DecisionEngine()
    pending_service = get_pending_confirmation_service()
    tenant_id = _config.DEFAULT_TENANT_ID
    channel = "line"
    decide_context = {"channel": channel, "customer_context": profile or {}, "developer_mode": True}

    # LINE Confirmation Flow (2026-08-10) — a customer-typed reply like
    # "ยืนยัน"/"yes" only means anything in the context of a PENDING
    # confirmation for THIS exact tenant/channel/user (never another
    # user's — see PendingConfirmationService.get_active's own .eq()
    # scoping). No pending row -> this turn is handled as a completely
    # ordinary fresh message, unchanged from before this sprint.
    pending = pending_service.get_active(tenant_id=tenant_id, channel=channel, conversation_key=user_id)
    result = None

    if pending:
        reply_kind = classify_confirmation_reply(question)
        if reply_kind == "confirm":
            # Replays the EXACT 2-turn history that produced this pending
            # confirmation, so the Decision Engine's own conversation-
            # continuation matching (_resolve_continuation_action)
            # re-selects the SAME action with the SAME already-collected
            # parameters — never re-derived from this short "ยืนยัน" reply.
            history = [
                {"role": "user", "content": pending.get("original_message") or ""},
                {"role": "assistant", "content": pending.get("question_text") or ""},
            ]
            result = engine.decide(question, history=history,
                                    context={**decide_context, "confirmed": True})
            # Resolve the pending row REGARDLESS of the execution outcome
            # (success or ERP error) — duplicate-send protection: once
            # consumed, get_active() can never return it again, so a
            # repeated "ยืนยัน" cannot trigger a second real execution.
            pending_service.mark_confirmed(pending["id"], source="line_text_reply")
            pending_service.mark_executed(pending["id"])
        elif reply_kind == "cancel":
            pending_service.mark_cancelled(pending["id"], source="line_text_reply")
            result = _cancelled_result()
        else:
            # Not a recognized confirm/cancel phrase — treat it as the
            # customer revising their request (e.g. a corrected message)
            # rather than answering the confirmation question. The stale
            # pending row is superseded (PendingConfirmationService.create
            # below cancels it) and this message runs as a fresh turn,
            # reusing the SAME selection/collection/confirmation pipeline
            # — no per-action revision logic.
            result = engine.decide(question, history=[], context=decide_context)
    else:
        reply_kind_for_expired_check = classify_confirmation_reply(question)
        if reply_kind_for_expired_check in ("confirm", "cancel"):
            # A confirm/cancel-shaped reply with NOTHING currently
            # pending — most likely the customer replied after their
            # confirmation window (config.PENDING_CONFIRMATION_TIMEOUT_SECONDS)
            # expired. Give a clear "please start again" reply instead of
            # silently routing "ยืนยัน" through fresh keyword search.
            most_recent = pending_service.get_most_recent(tenant_id=tenant_id, channel=channel,
                                                            conversation_key=user_id)
            if most_recent and most_recent.get("status") == "expired":
                result = _expired_result()
        if result is None:
            result = engine.decide(question, history=[], context=decide_context)

    reply = result.get("reply") or {}
    reply_text = reply.get("text") or ""
    routing_type = (result.get("routing") or {}).get("type")
    is_handoff = routing_type == "HUMAN_HANDOFF"

    # Persist a NEW pending confirmation whenever THIS turn's result is
    # itself a confirmation-required response (works for any COMMAND-type
    # Business Action, not just SendLineNotiCS — driven entirely by
    # services/decision_engine.py::_requires_confirmation's own generic
    # classification).
    dev = result.get("developer") or {}
    gate = dev.get("confirmation_gate")
    if gate and gate.get("required") and not gate.get("confirmed") and gate.get("action_id"):
        try:
            full_action = engine.registry.get_full(gate["action_id"], mask_secrets=False)
            collected = (dev.get("information_collection_status") or {}).get("collected_parameters") or {}
            pending_service.create(
                tenant_id=tenant_id, channel=channel, conversation_key=user_id,
                action=full_action or {"id": gate["action_id"], "action_key": gate.get("action_key")},
                parameters=collected, original_message=question, question_text=reply_text,
            )
        except Exception as e:
            print(f"[webhook] failed to persist pending confirmation (non-fatal): {e}")

    # Smart Handoff — the escalation DECISION already happened inside
    # decide() (explicit human request, refusal, max-retry, or AI
    # Policies' own escalation verdict surfaced through the RAG pipeline)
    # — this adapter only reacts to it, never re-derives its own trigger.
    if is_handoff:
        handoff_payload = result.get("handoff_payload") or {}
        reason = handoff_payload.get("reason", "handoff")
        send_line_notify(f"🚨 Smart Handoff\nUser: {user_id}\nคำถาม: {question}\nReason: {reason}")

    # Attachments — decide() already classified images vs. other files
    # and applied the Attachment Rules toggle (services/decision_engine.py
    # ::_extract_reply_attachments); this adapter only converts the
    # generic URL lists into LINE-specific message objects/text links.
    image_messages = []
    file_links: List[str] = []
    if not is_handoff:
        for pub_url in (reply.get("images") or [])[:4]:  # LINE's 5-message reply cap, 1 reserved for text
            image_messages.append(ImageMessage(original_content_url=pub_url, preview_image_url=pub_url))
        icon_by_type = {"pdf": "📄", "document": "📝", "spreadsheet": "📊",
                        "presentation": "📽️", "archive": "🗜️", "video": "🎬"}
        for f in (reply.get("files") or []):
            icon = icon_by_type.get((f.get("attachment_type") or "").lower(), "📎")
            file_links.append(f"{icon} {f.get('filename') or 'file'}: {f.get('url')}")

    full_reply_text = reply_text
    if file_links:
        full_reply_text = reply_text + "\n\n" + "\n".join(file_links)
    text_messages = build_line_text_messages(full_reply_text, max(1, 5 - len(image_messages)))
    messages = text_messages + image_messages

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=messages,
        ))

    upsert_profile(user_id, {
        "display_name": profile.get("display_name", "") if profile else "",
        "order_count":  profile.get("order_count", 0) if profile else 0,
        "total_spend":  profile.get("total_spend", 0) if profile else 0,
        "notes":        f"ถามเรื่อง: {routing_type}",
    })

    # Phase 3.1-3.3 (2026-08-05, Conversation Intelligence sprint) — record
    # this turn into Conversation History, update the customer's
    # incremental profile stats, then re-score their Customer Tier. Runs
    # AFTER the LINE reply is already sent, and every step degrades
    # gracefully on its own (never raises) — analytics/tier persistence
    # must never delay or break the customer-facing reply above.
    try:
        session_service = get_session_service()
        conversation = session_service.get_or_create_active_conversation(user_id)
        is_new_conversation = bool(conversation) and (conversation.get("message_count") or 0) == 0
        conversation_fields = None
        if conversation:
            session_service.record_conversation_turn(
                conversation["id"], question, result, line_user_id=user_id,
                conversation_tier=(profile or {}).get("conversation_tier"))
            conversation_fields = extract_conversation_fields(result)

        if conversation_fields is not None:
            update_profile_from_turn(user_id, decide_result=result, conversation_fields=conversation_fields,
                                      is_new_conversation=is_new_conversation)
            update_tier_for_profile(user_id)
    except Exception as e:
        print(f"[webhook] Phase 3 conversation-intelligence recording failed (non-fatal): {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
