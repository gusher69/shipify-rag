import asyncio
import requests
from typing import List
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
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
from services.session_service import (
    get_session_service, extract_conversation_fields, is_same_handoff_episode,
)
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


# ── LINE Multi-User Concurrency Fix (2026-08-27) ────────────────────────
# Confirmed live root cause: this process runs a single uvicorn worker with
# a fully synchronous handler chain — the OLD webhook() route called
# handler.handle(body, signature) directly, which parses AND dispatches to
# the registered callback (handle_message) INLINE on the async event loop
# thread. Since handle_message runs the entire Decision Engine/RAG/LLM/ERP
# pipeline synchronously, this blocked the ONE event loop for the full
# duration of every turn — for ANY user, not just the one being answered.
# Proved with two synthetic LINE users sent ~50ms apart against this exact
# endpoint: User A's own turn took 9.76s, but User B — despite arriving
# almost simultaneously — didn't finish until 21.13s total, meaning User
# B's real processing didn't even start until User A's was nearly done.
#
# Fix: PER-USER asyncio.Queue + a lazily-created background consumer task
# per user, with the actual (synchronous) processing offloaded to a shared
# bounded ThreadPoolExecutor. This guarantees, using only existing asyncio/
# threading primitives (no new architecture, no external queue/broker):
#   - strict FIFO order for the SAME user (asyncio.Queue is FIFO)
#   - true concurrency ACROSS users (independent queues/tasks — the event
#     loop is never blocked by one user's processing, so a second user's
#     turn can start immediately instead of waiting)
#   - zero changes to any RAG/routing/business logic — handle_message() /
#     _handle_message_via_decision_engine() / _handle_message_legacy() are
#     called exactly as before, only the DISPATCH layer changed.
_EXECUTOR = ThreadPoolExecutor(max_workers=20, thread_name_prefix="line-user-worker")
_user_queues: dict = {}
_user_tasks: dict = {}
_USER_WORKER_IDLE_TIMEOUT_SECONDS = 120.0  # self-terminate an idle per-user worker/queue

# Same-Question-Pending Collapse (2026-08-27) — a set of normalized message
# texts currently queued-or-processing per user. Populated the instant a
# message is accepted for real processing, cleared the instant that exact
# item finishes (success or failure) — so a LATER repeat of the same
# question, asked after the first one has genuinely completed, is never
# suppressed (only a duplicate arriving WHILE the first is still pending
# is collapsed). Confirmed live: without this, an impatient customer
# repeat of the identical question produced two independent ~10s
# executions and two separate replies minutes apart — exactly the
# production symptom this fix targets. Never touches RAG/Answerability/
# spell correction/routing — a duplicate is simply never handed to
# handle_message() at all; the ONE in-flight execution's own eventual
# reply is the customer's only answer.
_pending_texts_by_user: "dict[str, set]" = defaultdict(set)


def _normalize_for_dedup(text) -> str:
    return (text or "").strip()


async def _user_worker(user_id: str):
    """Background consumer for ONE LINE user — processes that user's own
    queued messages strictly in arrival order, one at a time, while other
    users' workers run fully independently. Self-terminates after a period
    of inactivity so a one-off/inactive user doesn't hold a queue/task
    forever."""
    import time as _time
    queue = _user_queues[user_id]
    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_USER_WORKER_IDLE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                break
            # TEMP LATENCY DIAGNOSTIC (2026-08-31) — remove once the
            # production slowness investigation is resolved.
            _t0 = _time.time()
            print(f"[LATENCY_DEBUG] dequeued for {user_id!r} at {_t0:.3f}, submitting to executor")
            try:
                await loop.run_in_executor(_EXECUTOR, handle_message, event)
            except Exception as e:
                print(f"[webhook] per-user worker error for {user_id!r}: {e}")
            finally:
                print(f"[LATENCY_DEBUG] executor call for {user_id!r} finished after {_time.time()-_t0:.3f}s")
                text = _normalize_for_dedup(getattr(event.message, "text", None))
                _pending_texts_by_user[user_id].discard(text)
                queue.task_done()
    finally:
        if _user_tasks.get(user_id) is asyncio.current_task():
            _user_tasks.pop(user_id, None)
            _user_queues.pop(user_id, None)
            _pending_texts_by_user.pop(user_id, None)


def _dispatch_event(event) -> None:
    """Fast, non-blocking dispatch — called synchronously from the async
    webhook() route on the event loop thread for every parsed MessageEvent/
    TextMessageContent. Creates (or reuses) this user's own queue +
    background consumer task, then either enqueues the event for normal
    serialized processing or — if this is a duplicate of an already
    pending message from the SAME user — skips it entirely (same user +
    same normalized text + first request still queued/processing)."""
    user_id = event.source.user_id if event.source else None
    if not user_id:
        # No isolable per-user identity (e.g. a group/room event) —
        # process directly via the shared executor; matches prior
        # behavior for such events (no per-user ordering was ever
        # meaningful for them anyway).
        asyncio.get_running_loop().run_in_executor(_EXECUTOR, handle_message, event)
        return

    text = _normalize_for_dedup(getattr(event.message, "text", None))
    if text and text in _pending_texts_by_user[user_id]:
        print(f"[webhook] duplicate pending message from {user_id!r} ({text!r}) — skipping duplicate execution")
        return

    # TEMP LATENCY DIAGNOSTIC (2026-08-31) — remove once the production
    # slowness investigation is resolved.
    import time as _time
    print(f"[LATENCY_DEBUG] dispatch_event enqueueing for {user_id!r} at {_time.time():.3f}, "
          f"existing_queue={_user_queues.get(user_id) is not None}, active_users={len(_user_queues)}")

    queue = _user_queues.get(user_id)
    if queue is None:
        queue = asyncio.Queue()
        _user_queues[user_id] = queue
        _user_tasks[user_id] = asyncio.create_task(_user_worker(user_id))

    if text:
        _pending_texts_by_user[user_id].add(text)
    queue.put_nowait(event)


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


@app.get("/debug/queue-status")
async def debug_queue_status():
    """TEMP diagnostic (2026-08-31) — introspects the live, in-process
    _EXECUTOR/_user_queues/_user_tasks state to find why real messages
    hang for minutes while every isolated reproduction (a fresh process,
    a fresh executor) completes in seconds. Remove once the investigation
    concludes. No side effects — pure read-only introspection of already-
    running state, using ThreadPoolExecutor's own internal attributes
    (undocumented but stable across CPython 3.x)."""
    import threading
    import sys
    import traceback
    threads_info = []
    for t in _EXECUTOR._threads:
        threads_info.append({"name": t.name, "alive": t.is_alive()})
    queue_info = {}
    for uid, q in _user_queues.items():
        task = _user_tasks.get(uid)
        queue_info[uid] = {
            "queue_size": q.qsize(),
            "task_done": task.done() if task else None,
            "task_cancelled": task.cancelled() if task else None,
        }
    all_threads = [{"name": t.name, "alive": t.is_alive(), "daemon": t.daemon}
                    for t in threading.enumerate()]

    # sys._current_frames() needs no ptrace/attach permission (unlike
    # py-spy, which this container's capabilities block) — it reads each
    # thread's live frame from inside this same process.
    frames = sys._current_frames()
    stacks = {}
    for t in threading.enumerate():
        frame = frames.get(t.ident)
        stacks[f"{t.name} (ident={t.ident})"] = (
            "".join(traceback.format_stack(frame)) if frame else None
        )

    return {
        "executor_work_queue_size": _EXECUTOR._work_queue.qsize(),
        "executor_threads": threads_info,
        "executor_max_workers": _EXECUTOR._max_workers,
        "user_queues": queue_info,
        "pending_texts_by_user": {k: list(v) for k, v in _pending_texts_by_user.items()},
        "all_process_threads": all_threads,
        "thread_stacks": stacks,
    }


@app.post("/webhook")
async def webhook(request: Request):
    """Signature verification and payload parsing are unchanged (still
    delegated to the LINE SDK's own WebhookParser, still returning the
    identical 400 on any invalid-signature/malformed-payload error) — only
    DISPATCH changed. Reusing handler.handle(...) here would parse AND
    invoke handle_message() synchronously, inline, on this coroutine's own
    event-loop thread — exactly the global-blocking bottleneck this fix
    exists for (see _dispatch_event's docstring above). Calling the SAME
    parser directly (handler.parser.parse — the exact method handler.handle()
    itself calls internally) keeps identical validation behavior while
    letting each event be handed to its own user's queue instead."""
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        payload = handler.parser.parse(body.decode(), signature, as_payload=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid signature")

    for event in payload.events:
        # Matches the exact narrow dispatch handler.handle() used to
        # perform via its internal registry (only MessageEvent +
        # TextMessageContent was ever registered here) — every other
        # event type is still silently ignored, unchanged from before.
        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            _dispatch_event(event)
    return {"status": "ok"}


def handle_message(event: MessageEvent):
    """Production Integration Sprint (2026-08-02), Phase 1 Step D/E —
    temporary rollout dispatch. `config.DECISION_ENGINE_LIVE_ROUTING`
    (default false) picks between the existing, untouched legacy adapter
    and the modern Decision Engine path. Both branches are isolated
    functions below — no shared mutable state, no interleaved logic — so
    removing the legacy branch later (once LINE OA UAT passes, per the
    sprint's explicit plan) is a clean deletion, not an untangling.

    Webhook Redelivery Dedup (Rapid-Message Concurrency investigation,
    2026-08-25) — checked here, once, before EITHER branch, so a
    redelivered event (LINE resending the same webhookEventId because it
    didn't get an HTTP response before its own timeout) never re-runs a
    full turn a second time. This is about DUPLICATE processing of the
    SAME event, distinct from the separate same-user/same-text pending
    collapse in _dispatch_event() above. Uses the database's own UNIQUE
    constraint (insert-then-detect-conflict, never check-then-insert), so
    it stays correct now that different users' events genuinely run
    concurrently (LINE Multi-User Concurrency Fix, 2026-08-27 — this
    process previously ran every event fully sequentially regardless of
    user; that global blocking is what that fix removed)."""
    from services.webhook_event_dedup_service import get_webhook_event_dedup_service
    import config as _config
    dedup = get_webhook_event_dedup_service()
    claimed = dedup.claim_event(
        tenant_id=_config.DEFAULT_TENANT_ID, channel="line",
        webhook_event_id=getattr(event, "webhook_event_id", None),
        conversation_key=event.source.user_id if event.source else None,
    )
    if not claimed:
        print(f"[webhook] duplicate webhookEventId {event.webhook_event_id} — skipping reprocessing")
        return

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


# ── LINE display-name capture (P3.1 data-integrity, 2026-09-01) ───────
# user_profiles.display_name was never populated for real webhook users:
# the post-reply bookkeeping only ever copied the name from the profile
# row back onto itself. A LINE webhook event identifies the user by
# source.userId only — the human-readable name needs one Profile API
# call. This runs exclusively inside the detached post-reply thread
# (never the reply path), at most once per user per cooldown window, and
# every failure is swallowed: the user stays identified by line_user_id
# and the name is retried on a later turn.
_DISPLAY_NAME_LOOKUP_COOLDOWN: dict = {}
_DISPLAY_NAME_LOOKUP_COOLDOWN_SEC = 3600.0


def _lookup_line_display_name(user_id: str):
    """Return the user's current LINE display name, or None. Cooldown-
    guarded (a blocked OA / transient 5xx must not trigger a retry
    storm) and never raises."""
    import time as _t
    now = _t.monotonic()
    last = _DISPLAY_NAME_LOOKUP_COOLDOWN.get(user_id)
    if last is not None and (now - last) < _DISPLAY_NAME_LOOKUP_COOLDOWN_SEC:
        return None
    _DISPLAY_NAME_LOOKUP_COOLDOWN[user_id] = now
    try:
        with ApiClient(configuration) as api_client:
            resp = MessagingApi(api_client).get_profile(user_id)
        return (getattr(resp, "display_name", "") or "").strip() or None
    except Exception as e:
        print(f"[webhook] LINE display-name lookup failed for {user_id[:12]}… (non-fatal): {e}")
        return None


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
    from services.customer_binding_service import get_customer_binding_service
    import config as _config

    user_id = event.source.user_id
    question = event.message.text
    profile = get_profile(user_id)

    engine = DecisionEngine()
    pending_service = get_pending_confirmation_service()
    tenant_id = _config.DEFAULT_TENANT_ID
    channel = "line"

    # Task 06B — the ONLY trusted source of a customer's CustCode for
    # this LINE user is a VERIFIED binding (services/
    # customer_binding_service.py), re-resolved fresh every turn — never
    # user_profiles.cust_code (Task 06's own root cause: a customer-typed
    # convenience cache with no ownership proof). A stale pre-Task-06
    # profile.cust_code value must never leak into customer_context, so
    # it is explicitly dropped here rather than merely "not written
    # anymore" (Task 06B Phase 22 legacy cleanup, code-level).
    verified_binding = get_customer_binding_service().get_verified_binding(
        tenant_id=tenant_id, channel=channel, external_user_id=user_id)
    _profile_cust_code = (profile or {}).get("cust_code")  # deprecated cache — provenance only
    customer_context = dict(profile or {})
    customer_context.pop("cust_code", None)
    if verified_binding:
        customer_context["cust_code"] = verified_binding["cust_code"]
        # P8.2 — a cached account-scoped identifier (last_shipment_code /
        # last_order_code / last_tracking) learned before this binding, or
        # under a DIFFERENT customer, must never satisfy a private ERP
        # slot alongside the verified CustCode. Fail closed: drop it so
        # dynamic collection asks the customer. Same reasoning as the
        # cust_code pop above, extended to the whole IDENTIFIER_MEMORY set.
        from services.action_selection_primitives import strip_cross_identity_identifier_memory
        customer_context = strip_cross_identity_identifier_memory(
            customer_context, profile_cust_code=_profile_cust_code,
            verified_cust_code=verified_binding["cust_code"])

    decide_context = {
        "channel": channel, "customer_context": customer_context, "developer_mode": True,
        # Task 06B — passed through untouched to services/
        # authorization_service.py via every exec_context construction in
        # decision_engine.py; both are server-derived (external_user_id
        # is the LINE webhook's own HMAC-verified user id), never from
        # message text.
        "tenant_id": tenant_id, "external_user_id": user_id,
    }

    # Context Continuity (Customer Intelligence V1, 2026-08-15) -- a
    # bounded recent-message window for THIS user's own active
    # conversation only (SessionService.get_recent_history scopes
    # strictly to one ai_sessions.id, itself one line_user_id -- another
    # customer's history can never appear here). Fetched once and reused
    # for both the Decision Engine call below and the Conversation
    # History / profile-stats recording at the end of this function.
    session_service = get_session_service()
    conversation = session_service.get_or_create_active_conversation(user_id)
    # max_turns raised from the function's own default (3 exchanges) --
    # see the identical fix and full rationale in admin/routes.py's Auto
    # Mode wrapper (Address Change Full UAT — Status Query fix,
    # 2026-08-24): a Dynamic Collection flow with more than 3 sequential
    # exchanges silently lost continuation-matching for EARLIER-collected
    # identifiers, on this exact same call site.
    recent_history = session_service.get_recent_history(conversation["id"], max_turns=20) if conversation else []
    # Fetched once, BEFORE any decide() call below -- reused by the
    # existing dedup check further down this function, so Active Handoff
    # Follow-up routing (services/decision_engine.py::decide()) and
    # duplicate-notification suppression read the exact same value,
    # never two separately-timed reads of the same row.
    # Fix-2.1 (2026-09-03) — read the FULL handoff state (status + reason
    # + notified_at) once here so the duplicate-notification check below
    # is ISSUE / EPISODE aware. decide()'s Active Handoff Follow-up
    # routing only needs the status string, so that is what it still gets.
    handoff_state_before = session_service.get_handoff_state(conversation["id"]) \
        if conversation else {"status": "NONE", "reason": None, "notified_at": None}
    handoff_status_before = handoff_state_before["status"]
    decide_context["handoff_status"] = handoff_status_before

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
            # Confirmation Continuation Correctness fix (2026-08-23) —
            # passes the pending row's OWN already-collected, already-
            # validated parameters straight through via
            # confirmed_action_id/confirmed_parameters, so
            # DecisionEngine.decide() executes directly instead of
            # re-deriving "collected" from a replayed history (which
            # loses any parameter given more than one turn before the
            # confirmation question — confirmed live for the Shipping
            # Address Change flow, where CustCode was given several
            # turns earlier than the address details). Real recent
            # history is passed through unchanged for everything else
            # decide() may want it for.
            result = engine.decide(question, history=recent_history,
                                    context={**decide_context, "confirmed": True,
                                             "confirmed_action_id": pending["pending_action_id"],
                                             "confirmed_parameters": pending.get("pending_parameters") or {}})
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
            # — no per-action revision logic. pending_action_id/
            # pending_parameters (Address Change Full UAT fix,
            # 2026-08-24) let decide() recognize this as a continuation
            # of the SAME still-pending action even when history-replay
            # alone can't reconstruct it (e.g. a field correction after
            # an Identifier-Memory-sourced parameter) — a genuine topical
            # diversion still overrides this via the Generic Continuation
            # Intent Guard.
            result = engine.decide(question, history=recent_history,
                                    context={**decide_context,
                                             "pending_action_id": pending["pending_action_id"],
                                             "pending_parameters": pending.get("pending_parameters") or {}})
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
            result = engine.decide(question, history=recent_history, context=decide_context)

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
    ics = dev.get("information_collection_status") or {}
    if gate and gate.get("required") and not gate.get("confirmed") and gate.get("action_id"):
        try:
            full_action = engine.registry.get_full(gate["action_id"], mask_secrets=False)
            collected = ics.get("collected_parameters") or {}
            pending_service.create(
                tenant_id=tenant_id, channel=channel, conversation_key=user_id,
                action=full_action or {"id": gate["action_id"], "action_key": gate.get("action_key")},
                parameters=collected, original_message=question, question_text=reply_text,
            )
        except Exception as e:
            print(f"[webhook] failed to persist pending confirmation (non-fatal): {e}")
    elif routing_type == "WORKFLOW" and ics.get("selected_action_id") and not ics.get("is_complete"):
        # Interrupted Workflow Auto-Resume fix (Task 02C, 2026-08-25) —
        # reuses the EXACT same pending_confirmations mechanism above,
        # just persisted at every MID-collection turn too (confirmation_
        # required=False), not only once the action is fully complete and
        # ready to confirm. Confirmed live: without this, a temporary
        # diversion (e.g. "พัสดุล่าสุดถึงไหนแล้ว" answered mid-address-
        # change-collection) leaves NO structural trace of the still-
        # incomplete action anywhere — _resolve_continuation_action can
        # only match by re-generating the LAST assistant turn's exact
        # question, which is now the diversion's own reply, not this
        # action's — so the customer's next answer (e.g. "ผู้รับชื่อสมชาย")
        # had nothing to bind against and fell through to RAG, losing an
        # otherwise valid, in-progress collection. get_active() already
        # returns this row on the very next turn regardless of
        # confirmation_required's value; _handle_dynamic_collection
        # already merges context["pending_parameters"] in whenever
        # context["pending_action_id"] matches (used today only for the
        # confirmation-stage correction case) — this is the same
        # mechanism, just reaching one stage earlier. A genuine topical
        # diversion still overrides it exactly as before (the Generic
        # Continuation Intent Guard runs unconditionally on whatever
        # continuation_action this resolves to, never bypassed here), and
        # a decisively different NEW multi-parameter workflow starting
        # afterward naturally supersedes this row the same way a fresh
        # confirmation-stage pending row already does (create() cancels
        # any prior active row for this conversation first).
        try:
            full_action = engine.registry.get_full(ics["selected_action_id"], mask_secrets=False)
            collected = ics.get("collected_parameters") or {}
            pending_service.create(
                tenant_id=tenant_id, channel=channel, conversation_key=user_id,
                action=full_action or {"id": ics["selected_action_id"],
                                         "action_key": ics.get("selected_business_action")},
                parameters=collected, original_message=question, question_text=reply_text,
                confirmation_required=False,
            )
        except Exception as e:
            print(f"[webhook] failed to persist mid-collection pending state (non-fatal): {e}")
    elif routing_type in ("API", "WEBHOOK", "TOOL", "NOTIFICATION", "HUMAN_HANDOFF"):
        # Terminal Pending Cleanup (Root Change 3, Final Systemic Routing
        # Fix, 2026-08-28) — closes the one asymmetry the forensic routing
        # audit found between the two continuation paths: pending_service.
        # create() already auto-supersedes any prior active pending_
        # confirmations row whenever a NEW one is persisted (either branch
        # above); but a turn whose OWN execution reached a genuinely
        # terminal outcome (that SAME action ran to success/error/denied,
        # or escalated to Human Handoff) neither branch above fires, so no
        # create() call ever runs to supersede a still-"pending" row left
        # over from an EARLIER turn for THAT action — it would otherwise
        # sit "pending" until it passively expires
        # (PENDING_CONFIRMATION_TIMEOUT_SECONDS, default 300s) and could be
        # re-adopted as a continuation by an unrelated later message.
        #
        # Deliberately narrowed to the SAME action_key only (confirmed
        # live: an unrelated one-shot status-query interruption — e.g.
        # "พัสดุล่าสุดถึงไหนแล้ว" mid-address-change — also executes as a
        # genuine, successful API action; that action completing must
        # never be mistaken for the DIFFERENT, still-pending address-
        # change action having concluded — the exact scenario the
        # Interrupted Workflow Auto-Resume fix above exists to protect).
        # A RAG/SAFE_FALLBACK/HYBRID/WORKFLOW(clarification) turn, or any
        # OTHER action's own execution, is never evidence that THIS
        # PENDING action itself concluded.
        try:
            stale_pending = pending_service.get_active(tenant_id=tenant_id, channel=channel, conversation_key=user_id)
            if stale_pending and stale_pending.get("pending_action_name") == dev.get("selected_business_action"):
                pending_service.mark_cancelled(stale_pending["id"], source="turn_reached_terminal_outcome")
        except Exception as e:
            print(f"[webhook] failed to resolve stale pending state (non-fatal): {e}")

    # Human Handoff (2026-08-13) — the escalation DECISION already
    # happened inside decide() (explicit human request, refusal, max-
    # retry, or AI Policies' own escalation verdict surfaced through the
    # RAG pipeline) — this adapter only reacts to it, never re-derives
    # its own trigger. Notifies CS through whichever real,
    # Credential-Store-backed NOTIFY/NOTIFICATION Business Action is
    # configured (e.g. SendLineNotiCS — services/human_handoff_service.py
    # resolves it generically, never hardcoded to that one action_key)
    # instead of the legacy LINE Notify token (send_line_notify, kept
    # only for the deprecated _handle_message_legacy path below).
    #
    # Duplicate-send protection reuses the SAME ai_sessions row every
    # other Conversation History write already targets (see
    # services/session_service.py's handoff-state helpers,
    # migrations/037_handoff_state.sql) — a repeated "ขอคุยกับเจ้าหน้าที่"
    # within the same active (24h) conversation notifies CS at most once,
    # mirroring the exact pattern pending_confirmations already uses for
    # conversation-scoped state.
    if is_handoff:
        handoff_payload = result.get("handoff_payload") or {}
        reason = handoff_payload.get("reason", "handoff")
        # Nothing between the pre-decide() read above and here can change
        # handoff_status (none of the confirm/cancel/fresh decide() paths
        # touch it; set_handoff_status is only ever called in the `else`
        # branch just below) -- reusing the same value avoids a second,
        # redundant read of the same row.
        handoff_status = handoff_status_before
        # Customer UAT Fix 2 (2026-09-02) — for the "unsupported company
        # fact" handoff, the reply decide() carried is the honest no-info
        # text with NO "staff will check" promise. Append the follow-up
        # clause ONLY when a real notification is on the books for this
        # conversation: either it succeeds on this turn, or it already
        # went out earlier (dedup path below) so CS is genuinely engaged.
        # If the send fails, the customer keeps the plain no-info wording
        # — never a fabricated promise.
        _unsupported_fact_handoff = reason == "unsupported_company_information"
        # Fix-2.2 (2026-09-03) — Service-Mind wording. The RAG pipeline's
        # P7.1 no-info text ends with a SELF-SERVICE line ("go ask staff
        # yourself"), which is wrong once the system has already taken
        # ownership via a Human CS handoff. For an unsupported-company
        # handoff, drop that line, and — ONLY when the handoff truth rule
        # is satisfied (a notification for THIS episode succeeded this
        # turn, or this same episode is already legitimately NOTIFIED/
        # PENDING) — add a "we will coordinate staff for you" line
        # instead. On notification failure the customer is left with the
        # plain neutral no-info wording — never a coordination promise.
        _SELF_SERVICE_ASK = "รบกวนสอบถามเจ้าหน้าที่เพื่อความชัดเจนอีกครั้งนะคะ"
        _STAFF_COORDINATION_CLAUSE = " เดี๋ยวทางเราประสานเจ้าหน้าที่ช่วยตรวจสอบเพิ่มเติมให้นะคะ"
        if _unsupported_fact_handoff:
            reply_text = reply_text.replace(" " + _SELF_SERVICE_ASK, "").replace(_SELF_SERVICE_ASK, "").strip()
        # Fix-2.1 (2026-09-03) — dedupe on the ISSUE / EPISODE, not on any
        # historical NOTIFIED. A stale or unrelated prior handoff (e.g. a
        # 3-day-old self_verification_failed) must NOT suppress a genuine
        # new episode (unsupported_company_information). Same reason class
        # within the active window (or an in-flight PENDING) is a real
        # duplicate and is skipped; anything else sends a fresh
        # notification and rewrites handoff_reason / handoff_notified_at
        # to the current episode.
        _same_episode = is_same_handoff_episode(
            reason, handoff_state_before.get("status"),
            handoff_state_before.get("reason"), handoff_state_before.get("notified_at"))
        if _same_episode:
            print(f"[webhook] Human Handoff already {handoff_status} for THIS issue/episode "
                  f"(reason class {reason!r}) — skipping duplicate notification")
            # Same episode is already legitimately NOTIFIED/PENDING — the
            # service-mind coordination line is truthful (CS is engaged
            # for THIS issue).
            if _unsupported_fact_handoff and _STAFF_COORDINATION_CLAUSE.strip() not in reply_text:
                reply_text = reply_text + _STAFF_COORDINATION_CLAUSE
        else:
            if conversation:
                session_service.set_handoff_status(conversation["id"], "PENDING", reason=reason)
            from services.human_handoff_service import send_handoff_notification
            from services.customer_tier_service import classify_message_stage
            collected = (dev.get("information_collection_status") or {}).get("collected_parameters") or {}
            # Handoff Context Package (Human Handoff V1, 2026-08-15) --
            # prefers THIS turn's own collected parameters (freshest),
            # falls back to the Customer Intelligence profile (what was
            # remembered from earlier turns) so a bare "ขอคุยกับเจ้าหน้าที่"
            # with no identifier of its own still gives CS useful context.
            # Never a credential: `profile` only ever carries the
            # customer_message-sourced identifier fields Customer
            # Intelligence V1 persists (cust_code/last_order_code/
            # last_shipment_code/last_tracking), never SecretCode.
            stage_now = classify_message_stage(question).get("stage")
            recent_summary = " | ".join(
                f"{h['role']}: {h['content']}" for h in (recent_history or []) if h.get("content")
            ) or None
            send_result = send_handoff_notification(
                reason=reason,
                customer_name=(profile or {}).get("display_name") or None,
                cust_code=collected.get("CustCode") or (profile or {}).get("cust_code"),
                line_user_id=user_id,
                customer_message=question,
                conversation_summary=recent_summary,
                customer_stage=stage_now or (profile or {}).get("conversation_tier"),
                primary_intent=(dev.get("intent") or {}).get("actionable_intent") or (profile or {}).get("primary_intent"),
                current_topic=(dev.get("intent") or {}).get("actionable_intent"),
                last_order_code=collected.get("OrderCode") or (profile or {}).get("last_order_code"),
                last_shipment_code=collected.get("ShipmentCode") or (profile or {}).get("last_shipment_code"),
                last_tracking=collected.get("Tracking") or (profile or {}).get("last_tracking"),
            )
            if send_result.get("sent") and _unsupported_fact_handoff \
                    and _STAFF_COORDINATION_CLAUSE.strip() not in reply_text:
                # Real notification went out — now it is true to tell the
                # customer that WE will coordinate a staff follow-up.
                reply_text = reply_text + _STAFF_COORDINATION_CLAUSE
            if conversation:
                if send_result.get("sent"):
                    session_service.set_handoff_status(conversation["id"], "NOTIFIED", reason=reason)
                else:
                    # Never claim success the customer already saw a
                    # confirming reply for if the real call failed — reset
                    # to NONE so the next handoff-triggering message can
                    # retry instead of being silently swallowed forever.
                    session_service.set_handoff_status(conversation["id"], "NONE", reason=reason)
                    print(f"[webhook] Human Handoff notification failed (non-fatal, will retry next trigger): {send_result.get('error')}")

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

    # Customer UAT Fix 2 — keep the persisted turn (record_conversation_
    # turn reads `result`) consistent with what the customer actually
    # received when the handoff block appended the follow-up clause.
    if is_handoff and isinstance(reply, dict) and reply.get("text") != reply_text:
        reply["text"] = reply_text

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

    # Phase 3.1 — Conversation History. Kept SYNCHRONOUS: the NEXT turn's
    # continuity (SessionService.get_recent_history) depends on this
    # turn's messages being persisted. It is now cheap (session dict
    # reused, no get_session hydration — see record_conversation_turn).
    is_new_conversation = bool(conversation) and (conversation.get("message_count") or 0) == 0
    try:
        if conversation:
            session_service.record_conversation_turn(
                conversation["id"], question, result, line_user_id=user_id,
                conversation_tier=(profile or {}).get("conversation_tier"),
                session=conversation)
    except Exception as e:
        print(f"[webhook] record_conversation_turn failed (non-fatal): {e}")

    # Phase 3.2-3.3 — profile 'notes', incremental profile stats, and
    # customer-tier rescoring. Pure analytics/enrichment: nothing here is
    # read for correctness or security by the next turn, so it runs on a
    # detached daemon thread AFTER the reply and never holds the per-user
    # worker (which was previously blocked ~10s by these writes).
    def _post_reply_bookkeeping():
        try:
            display_name = (profile.get("display_name", "") if profile else "") or ""
            if not display_name.strip():
                # First real turn (or a still-nameless profile): resolve the
                # LINE display name once, off the reply path. None on
                # failure/cooldown — the row is still written, name filled later.
                display_name = _lookup_line_display_name(user_id) or ""
            upsert_profile(user_id, {
                "display_name": display_name,
                "order_count":  profile.get("order_count", 0) if profile else 0,
                "total_spend":  profile.get("total_spend", 0) if profile else 0,
                "notes":        f"ถามเรื่อง: {routing_type}",
            })
            if conversation:
                conversation_fields = extract_conversation_fields(result)
                update_profile_from_turn(user_id, decide_result=result,
                                          conversation_fields=conversation_fields,
                                          is_new_conversation=is_new_conversation)
                update_tier_for_profile(user_id, message=question)
                # P4 — Cold/Warm/Hot lead analysis. Analytical side-channel:
                # reuses the SAME conversation_fields already extracted, adds
                # no network/LLM call, and nothing in the reply path reads
                # its output (separate lead_* columns, not conversation_tier).
                try:
                    from services.lead_stage_service import update_lead_stage_from_turn
                    update_lead_stage_from_turn(
                        user_id, decide_result=result,
                        conversation_fields=conversation_fields,
                        question=question, is_verified=bool(verified_binding),
                        history=recent_history)
                except Exception as e:
                    print(f"[webhook] lead-stage update failed (non-fatal): {e}")
                # P4.1 — negative-customer detection + one Admin LINE alert.
                # Same detached thread, analytical side-channel: the customer
                # reply is already sent and is not altered.
                try:
                    from services.sentiment_service import update_sentiment_from_turn
                    update_sentiment_from_turn(
                        user_id, question=question,
                        display_name=display_name or (profile or {}).get("display_name"),
                        cust_code=(verified_binding or {}).get("cust_code"),
                        # This turn already escalated to Human Handoff, which
                        # notifies CS through the SAME NOTIFY action — never
                        # double-alert.
                        handoff_active=(routing_type == "HUMAN_HANDOFF"))
                except Exception as e:
                    print(f"[webhook] sentiment update failed (non-fatal): {e}")
        except Exception as e:
            print(f"[webhook] post-reply bookkeeping failed (non-fatal): {e}")

    import threading as _threading
    _threading.Thread(target=_post_reply_bookkeeping, daemon=True,
                      name="line-post-reply-bookkeeping").start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
