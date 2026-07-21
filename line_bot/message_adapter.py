"""LINE OA message-segmentation adapter (Part 9, P0 2026-07-21) — converts
one reply string into 1..N separate LINE TextMessage objects using
services/message_segmenter.py's already-tested split logic, never a
second, separately-maintained splitter and never an LLM call.

Kept in its own module (rather than inline in line_bot/webhook.py) so it
has no dependency on erp.bridge/pymysql or any other LINE-webhook-only
import — this makes it importable and unit-testable in isolation.
"""
from typing import List

from linebot.v3.messaging import TextMessage

from services.message_segmenter import segment_message

# LINE's Messaging API caps a single reply at 5 messages total (text +
# image combined together) and each TextMessage's real cap is 5000
# characters — a small safety margin is kept below that.
LINE_MAX_MESSAGES = 5
LINE_MAX_TEXT_LENGTH = 4900


def build_line_text_messages(reply_text: str, max_text_messages: int) -> List[TextMessage]:
    """Converts one reply string into 1..max_text_messages separate LINE
    TextMessage objects, reusing services/message_segmenter.py's already-
    tested split logic (paragraph/bullet/prose-marker boundaries only —
    never an arbitrary character cut, never inside a number/URL/phone/
    tracking code). If segmentation would produce MORE parts than the
    channel currently has room for (image attachments consume reply slots
    too), the least-important TRAILING parts are merged into the last
    kept part rather than silently dropped — order and content are always
    preserved, just combined. Each part is additionally capped to LINE's
    real character limit as a final safety net. Never sends reply_text as
    one message when it can be safely segmented into more than one."""
    max_text_messages = max(1, max_text_messages)
    reply_text = reply_text or ""
    segmented = segment_message(reply_text, reply_mode="auto", max_messages=min(3, max_text_messages))
    parts = segmented.message_parts or ([reply_text] if reply_text else [])
    if len(parts) > max_text_messages:
        head = parts[:max_text_messages - 1]
        tail = "\n\n".join(parts[max_text_messages - 1:])
        parts = head + [tail]
    return [TextMessage(text=p[:LINE_MAX_TEXT_LENGTH]) for p in parts if p]
