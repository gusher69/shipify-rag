"""PromptBuilderService — the only place that assembles the final prompt
sent to an LLM. Centralizing this (instead of inlining string
concatenation at each call site, like line_bot/tone.py used to) is what
lets the AI Playground show "Prompt Template / System Prompt / Final
Prompt" as first-class, inspectable objects, and is the seam Prompt
Studio (services/prompt_studio_service.py — versioned, admin-editable
templates backed by ai_prompt_templates/ai_prompt_assignments) plugs
into.

Templates are DB-backed (migrations/017_prompt_studio.sql) with a small
in-memory fallback registry so the app never hard-crashes before that
migration is run, or if the query fails for any reason — same graceful-
degrade convention used everywhere else in this app.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional

from config import RAG_GROUNDING_MODE

# ── Strict grounding (generic — no domain/topic-specific wording) ──────
# Fixes the class of failure where the LLM fills a gap in the retrieved
# context with plausible-sounding general/external knowledge (e.g.
# inventing a minimum Python runtime version because the context only
# listed PACKAGE version constraints). This block is deliberately generic
# — "package version vs runtime version" is one example of a broader
# "don't substitute a related-but-different attribute for the one asked
# about" rule, and it applies the same way to any product/version/API/
# model/database question, not just this one.
STRICT_GROUNDING_RULES = (
    "\n\nSTRICT GROUNDING RULES (must follow exactly):\n"
    "- Answer only from the provided Context. Do not use general/external technical "
    "knowledge, best practices, or common conventions unless a policy note explicitly allows it.\n"
    "- Identify exactly which attribute the question asks about (e.g. a runtime/platform "
    "version vs. a package/library/dependency version vs. a product/API/SDK version vs. an "
    "operating system version). Never answer with a DIFFERENT attribute than the one asked, "
    "even if it is closely related (a package version is NOT a runtime version).\n"
    "- Do not infer or invent a version, requirement, or compatibility constraint that is not "
    "written verbatim in the Context — including phrases like 'should generally be at least "
    "version X' or any specific version number not present in the Context.\n"
    "- If the exact requested attribute is absent from the Context, say so plainly first. Then, "
    "if the Context contains genuinely related information (e.g. package versions when a runtime "
    "version was asked), state that related information separately and label it as such — never "
    "blend it into a claim about the attribute that was actually asked about.\n"
    "- Do not use hedging/general-advice phrases (\"โดยทั่วไป\", \"แนะนำให้ใช้\", \"ควรใช้\", "
    "\"generally\", \"it is recommended\", \"you should use\") to fill a gap in the Context.\n"
    "- Do not add an escalation/handoff sentence (e.g. offering to check with a team) unless an "
    "active policy note actually triggered one."
)


# ── General Chat Guidance (Hybrid RAG + General AI Chat, 2026-08-27) ───
# The counterpart to STRICT_GROUNDING_RULES above, for the ONE case the
# Decision Engine/RAG pipeline has already determined is NOT a company-
# specific/operational question (services/playground_orchestrator.py's
# own deny-list check, run BEFORE deciding to pass general_chat_mode=True
# here — never decided by this module). Swapped in for that exact turn
# only; STRICT_GROUNDING_RULES remains the default for every other turn,
# so this never weakens factual safety for a genuinely company-specific
# question. `context` is passed empty for this call (see the caller) so
# there is no retrieved-but-irrelevant chunk for the LLM to be tempted
# into forcing an answer from.
GENERAL_CHAT_GUIDANCE = (
    "\n\nGENERAL CHAT GUIDANCE (this question was already determined to be "
    "NOT about company-specific data, policy, or operations — must follow exactly):\n"
    "- You may answer this question naturally and helpfully using your own general "
    "knowledge, like a normal AI assistant would — this is not a company data lookup.\n"
    "- Never state or imply that a general fact, opinion, or piece of advice is Shipify's "
    "own official policy, process, rate, or data unless it is explicitly present in the "
    "Context below (which will normally be empty for this kind of question).\n"
    "- If your answer could sound like it describes this company's own specific process "
    "(e.g. general advice about importing or reselling goods), frame it as general "
    "information (\"โดยทั่วไปแล้ว...\", \"ถ้าพูดในภาพรวม...\") rather than as Shipify's own "
    "steps, unless the Context explicitly supports that.\n"
    "- Do not invent or guess this company's prices, shipping rates/duration, order or "
    "shipment status, refund/warranty policy, customer/wallet/coupon data, warehouse "
    "status, or current promotions — those still require confirmed information; say so "
    "honestly if asked and unavailable, instead of guessing."
)


# ── Base Conversation Rules (platform standard, read-only) ─────────────
# Requirement: prompt authors should never have to re-write basic
# conversation hygiene (greet-once, don't repeat prior answers, don't
# blindly tack on a support-line sign-off, etc.) in every single Prompt
# Studio template. This block is a FIXED, platform-wide constant — never
# stored per-template, never editable from Prompt Studio, and injected
# into EVERY built prompt automatically (including prompts created before
# this feature existed — see build_prompt()'s system_content assembly).
# It intentionally overlaps with nothing in Tone Guidance (which only
# ever changes phrasing/formality/warmth, never these rules) or AI
# Policies (business/escalation rules, evaluated separately) — this is
# the one and only place these specific rules live.
BASE_CONVERSATION_RULES = (
    "## กฎการสนทนา\n"
    "\n"
    "- กล่าวสวัสดีลูกค้าเฉพาะข้อความแรกของบทสนทนาเท่านั้น\n"
    "- หากเป็นบทสนทนาต่อเนื่อง ห้ามกล่าวสวัสดีซ้ำ\n"
    "- ให้ตอบต่อจากบริบทเดิมทันที\n"
    "- ถือว่าคำถามสั้น เช่น \"แล้วทางรถล่ะ\" เป็นคำถามต่อจากบทสนทนาก่อนหน้า\n"
    "- ใช้น้ำเสียงเหมือนเจ้าหน้าที่คนเดิมกำลังคุยต่อ\n"
    "\n"
    "## รูปแบบการตอบ\n"
    "\n"
    "- ตอบคำถามก่อน\n"
    "- ไม่ต้องเกริ่นนำ\n"
    "- ไม่ต้องกล่าวสวัสดีซ้ำ\n"
    "- ไม่ต้องลงท้ายเหมือนเดิมทุกครั้ง\n"
    "- ตอบสั้น กระชับ เว้นแต่ลูกค้าขอรายละเอียดเพิ่มเติม\n"
    "\n"
    "## การอ้างอิงข้อมูล\n"
    "\n"
    "- ตอบเฉพาะข้อมูลจาก Knowledge Base, ERP หรือข้อมูลที่ระบบอนุมัติ\n"
    "- หากไม่มีข้อมูล ให้แจ้งตามความจริง\n"
    "- ห้ามเดา\n"
    "- ห้ามสร้างข้อมูลขึ้นเอง\n"
    "\n"
    "## กรณีไม่มีข้อมูล (Fallback Tone)\n"
    "\n"
    "- ห้ามขึ้นต้นด้วย \"ขออภัย\" / \"ต้องขออภัย\" / \"ขออภัยในความไม่สะดวก\" เพียงเพราะข้อมูลบางส่วนไม่ครบ "
    "— ใช้คำกลางๆ แทน เช่น \"ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ\" หรือ \"ข้อมูลส่วนนี้ยังไม่มีอยู่ในระบบค่ะ\" "
    "ห้ามพูดคำว่า \"ฐานความรู้\" หรือคำศัพท์ระบบภายในอื่นๆ กับลูกค้าโดยเด็ดขาด\n"
    "- ใช้ \"ขออภัย\" เฉพาะเมื่อมีนโยบายที่ Active กำหนดไว้ชัดเจน หรือเกิดความผิดพลาดของระบบ/บริการจริง "
    "(เช่น API ภายนอกล้มเหลว) — ไม่ใช่เพียงเพราะไม่มีข้อมูลบางอย่าง\n"
    "- ห้ามต่อท้ายด้วย \"หากต้องการข้อมูลเพิ่มเติม...\" / \"แนะนำให้ติดต่อเจ้าหน้าที่...\" / \"กรุณาตรวจสอบ...\" "
    "โดยอัตโนมัติ — ใช้เฉพาะเมื่อ FAQ ที่ดึงมาระบุไว้ตรงๆ, มีนโยบาย Active กำหนดไว้ หรือมีการ Escalate จริง\n"
    "- หาก Retrieved Context มีคำแนะนำ/ขั้นตอน/วิธีการที่เกี่ยวข้องอยู่แล้ว (เช่น บอกให้เข้าเมนูในหน้าเว็บเพื่อดูข้อมูล) "
    "ให้ตอบตามขั้นตอนนั้นตรงๆ — ห้ามตอบว่า \"ไม่มีข้อมูล\" เพียงเพราะคำตอบไม่ได้ระบุค่าที่ต้องการโดยตรง แต่บอกวิธีไปหาแทน\n"
    "\n"
    "## การใช้บริบท\n"
    "\n"
    "- ใช้บทสนทนาก่อนหน้าเพื่อช่วยตีความคำถามต่อเนื่องเท่านั้น\n"
    "- ห้ามใช้คำตอบเก่าของ AI มาเป็นข้อมูลอ้างอิง\n"
    "- คำถามล่าสุดของลูกค้าต้องมีความสำคัญสูงสุดเสมอ\n"
    "\n"
    "## การส่งต่อเจ้าหน้าที่\n"
    "\n"
    "กล่าวถึงเจ้าหน้าที่เฉพาะเมื่อ\n"
    "\n"
    "- ลูกค้าขอคุยกับเจ้าหน้าที่\n"
    "- AI Policies กำหนดให้ Escalate\n"
    "- ไม่มีข้อมูลที่เชื่อถือได้\n"
    "\n"
    "ห้ามปิดท้ายทุกข้อความด้วย\n"
    "\n"
    "\"หากมีคำถามเพิ่มเติมสามารถติดต่อเจ้าหน้าที่...\"\n"
    "\n"
    "เว้นแต่เข้าเงื่อนไขด้านบน\n"
    "\n"
    "## รูปแบบข้อความ\n"
    "\n"
    "- เขียนให้เป็นธรรมชาติ เหมือนคุยกับลูกค้าจริง\n"
    "- หลีกเลี่ยงการรวมทุกอย่างเป็นย่อหน้าเดียวยาวๆ เมื่อคำตอบมีหลายประเด็นที่แยกจากกันชัดเจน\n"
    "- ไม่จำเป็นต้องแบ่งทุกคำตอบเป็นหลายข้อความ\n"
    "- แต่ละส่วนของคำตอบต้องสมบูรณ์และเข้าใจได้ในตัวเอง\n"
    "- ห้ามกล่าวสวัสดีหรือประโยคปิดท้ายซ้ำในแต่ละส่วน\n"
    "- เมื่อคำตอบมีหลายหัวข้อที่แยกจากกันชัดเจน (เช่น ทางเรือ / ทางรถ / สรุป) ให้เว้นบรรทัดว่างคั่นระหว่างแต่ละหัวข้อเสมอ "
    "และให้แต่ละหัวข้อจบในตัวเอง — อย่าสร้างหัวข้อย่อยเล็กเกินไปจนเกินความจำเป็น "
    "(กฎข้อนี้ช่วยเสริมเท่านั้น — คำตอบสั้นทั่วไปยังคงเป็นข้อความเดียวตามปกติ)"
)
# Human-like Multi-Message Replies (services/message_segmenter.py): the
# "## รูปแบบข้อความ" block above only guides HOW the LLM writes the answer
# (natural, not one giant paragraph) — it never decides how many message
# bubbles are actually sent. That decision is made deterministically,
# after the LLM response exists, by services/message_segmenter.py — this
# prompt guidance must never replace or duplicate that backend step, and
# is intentionally NOT repeated inside Tone Guidance (a separate concern:
# tone only ever changes phrasing/formality/warmth, never message count).


@dataclass
class PromptTemplate:
    id: str
    name: str
    version: str
    system_prompt: str
    description: Optional[str] = None
    channel: Optional[str] = None
    language: str = "th"
    tone: Optional[str] = None
    response_rules: Dict = field(default_factory=dict)
    fallback_rules: Dict = field(default_factory=dict)
    safety_rules: Dict = field(default_factory=dict)
    is_active: bool = True
    is_default: bool = False
    parent_id: Optional[str] = None


# In-memory fallback — used only if ai_prompt_templates can't be reached
# (migration not run yet, or a transient DB error). Keeps the Playground
# and LINE OA usable even in that degraded state.
_FALLBACK_TEMPLATES: Dict[str, PromptTemplate] = {
    "line_oa_default": PromptTemplate(
        id="line_oa_default",
        name="LINE OA Default",
        version="1",
        system_prompt=(
            "คุณคือ AI ผู้ช่วยของ Shipify แพลตฟอร์มนำเข้าสินค้าจากจีน\n"
            "ตอบภาษาไทย สุภาพ กระชับ เป็นกันเอง ใช้คำลงท้าย 'ค่ะ' หรือ 'นะคะ'\n"
            "ถ้าไม่มีข้อมูลพอ บอกตรงๆ ว่าไม่ทราบ ดีกว่าเดาผิด\n"
            "สำหรับคำถามเชิงตัวเลข/การเงินจากไฟล์ Excel: ถ้า context มีบรรทัดที่ขึ้นต้นด้วย\n"
            "\"CALCULATED RESULT\" ให้ใช้ตัวเลขนั้นตรงๆ ห้ามคำนวณเองใหม่หรือเดาตัวเลขอื่น"
        ),
        is_default=True,
    ),
    "playground_neutral": PromptTemplate(
        id="playground_neutral",
        name="Playground Neutral (English debug)",
        version="1",
        system_prompt=(
            "You are an AI assistant for Shipify, a China-import shipping platform.\n"
            "Answer concisely and only from the given context. If the context doesn't "
            "contain the answer, say so plainly instead of guessing.\n"
            "If the context contains a line starting with \"CALCULATED RESULT\", state "
            "that number exactly as given — never recompute or guess a different number."
        ),
    ),
}

DEFAULT_TEMPLATE_ID = "line_oa_default"


def _row_to_template(row: Dict) -> PromptTemplate:
    return PromptTemplate(
        id=row["id"], name=row["name"], version=str(row.get("version") or 1),
        system_prompt=row["system_prompt"], description=row.get("description"),
        channel=row.get("channel"), language=row.get("language") or "th",
        tone=row.get("tone"), response_rules=row.get("response_rules") or {},
        fallback_rules=row.get("fallback_rules") or {}, safety_rules=row.get("safety_rules") or {},
        is_active=bool(row.get("is_active")), is_default=bool(row.get("is_default")),
        parent_id=row.get("parent_id"),
    )


def _get_sb():
    from services.prompt_studio_service import _get_sb as _sb
    return _sb()


def list_templates() -> List[PromptTemplate]:
    try:
        res = _get_sb().table("ai_prompt_templates").select("*").is_("deleted_at", "null") \
            .order("created_at", desc=True).execute()
        rows = res.data or []
        if rows:
            return [_row_to_template(r) for r in rows]
    except Exception as e:
        print(f"[prompt_builder] list_templates DB query failed, using fallback: {e}")
    return list(_FALLBACK_TEMPLATES.values())


def get_template(template_id: Optional[str]) -> PromptTemplate:
    if template_id:
        try:
            res = _get_sb().table("ai_prompt_templates").select("*").eq("id", template_id) \
                .is_("deleted_at", "null").execute()
            if res.data:
                return _row_to_template(res.data[0])
        except Exception as e:
            print(f"[prompt_builder] get_template({template_id}) DB query failed: {e}")
        # template_id might be a legacy in-memory key (e.g. "line_oa_default")
        if template_id in _FALLBACK_TEMPLATES:
            return _FALLBACK_TEMPLATES[template_id]
    return get_default_template()


def get_default_template() -> PromptTemplate:
    try:
        res = _get_sb().table("ai_prompt_templates").select("*").eq("is_default", True) \
            .is_("deleted_at", "null").limit(1).execute()
        if res.data:
            return _row_to_template(res.data[0])
    except Exception as e:
        print(f"[prompt_builder] get_default_template DB query failed, using fallback: {e}")
    return _FALLBACK_TEMPLATES[DEFAULT_TEMPLATE_ID]


# CS-02 (2026-08-26) — confirmed live: the real LINE webhook's own runtime
# channel VALUE (line_bot/webhook.py: `channel = "line"`, propagated through
# services/decision_engine.py's context plumbing and ALSO used for
# authorization/session/customer-binding scoping — see Task 06/06B) does not
# match the admin-facing Prompt Studio channel LABEL
# (services/prompt_studio_service.py::CHANNELS, e.g. "LINE OA") that an admin
# actually picks when assigning a prompt template. Before this fix, an
# admin's "LINE OA" assignment could never take effect for real production
# LINE traffic — get_active_prompt_for_channel("line") queried assignments
# for channel="line", a value the admin UI can never create (assign_channel()
# validates against CHANNELS, which only contains "LINE OA"). This mapping is
# deliberately local to PROMPT RESOLUTION ONLY — it must never be reused for
# authorization/session/binding scoping, which correctly keep using the raw
# internal channel value ("line") unchanged.
_INTERNAL_CHANNEL_TO_PROMPT_STUDIO_LABEL = {"line": "LINE OA"}


def get_active_prompt_for_channel(channel: str) -> PromptTemplate:
    """The routing rule the whole feature is built around: real customer
    traffic (LINE OA, Website, etc.) always resolves its system prompt
    through here — never a hardcoded string. Falls back to Global Default,
    then to the in-memory fallback, so a misconfigured/missing assignment
    never breaks the channel."""
    prompt_studio_channel = _INTERNAL_CHANNEL_TO_PROMPT_STUDIO_LABEL.get(channel, channel)
    try:
        ares = _get_sb().table("ai_prompt_assignments").select("prompt_template_id") \
            .eq("channel", prompt_studio_channel).eq("is_active", True).limit(1).execute()
        if ares.data:
            tres = _get_sb().table("ai_prompt_templates").select("*") \
                .eq("id", ares.data[0]["prompt_template_id"]).is_("deleted_at", "null").execute()
            if tres.data:
                return _row_to_template(tres.data[0])
    except Exception as e:
        print(f"[prompt_builder] get_active_prompt_for_channel({channel}) failed, falling back: {e}")
    return get_default_template()


def get_active_prompt_for_tier(tier: str) -> Optional[PromptTemplate]:
    """Phase 3.4 (2026-08-05, Conversation Intelligence sprint) — Prompt
    Studio's "Customer Tier Prompt" section. Mirrors
    get_active_prompt_for_channel() exactly, but keyed by
    services.customer_tier_service.py's conversation_tier
    (cold/warm/hot/negative) instead of channel. Returns None (not a
    fallback template) when no assignment is configured for this tier —
    callers must fall back to channel/global resolution themselves, since
    "no tier prompt configured" is a valid, common state (tier prompts are
    opt-in), unlike a missing channel assignment."""
    try:
        ares = _get_sb().table("ai_prompt_tier_assignments").select("prompt_template_id") \
            .eq("tier", tier).eq("is_active", True).limit(1).execute()
        if ares.data:
            tres = _get_sb().table("ai_prompt_templates").select("*") \
                .eq("id", ares.data[0]["prompt_template_id"]).is_("deleted_at", "null").execute()
            if tres.data:
                return _row_to_template(tres.data[0])
    except Exception as e:
        print(f"[prompt_builder] get_active_prompt_for_tier({tier}) failed: {e}")
    return None


def get_active_prompt(*, channel: Optional[str] = None, tier: Optional[str] = None) -> PromptTemplate:
    """The single resolution entry point Decision Engine uses: Customer
    Tier Prompt (if one is configured for this tier) takes priority over
    the channel's own assignment, which takes priority over the Global
    Default — never a manual per-conversation choice (users cannot select
    a prompt directly; see CLAUDE.md Phase 3.4). Falling through this
    chain is the ONLY way a prompt gets selected for real traffic."""
    if tier:
        tier_prompt = get_active_prompt_for_tier(tier)
        if tier_prompt:
            return tier_prompt
    if channel:
        return get_active_prompt_for_channel(channel)
    return get_default_template()


@dataclass
class BuiltPrompt:
    template: PromptTemplate
    context: str
    question: str
    messages: List[Dict]          # what actually gets sent to the LLM
    final_prompt_text: str        # human-readable flattened view for the Prompt tab


def _rules_block(label: str, rules: Dict) -> str:
    if not rules:
        return ""
    lines = "\n".join(f"- {k}: {v}" for k, v in rules.items())
    return f"\n\n{label}:\n{lines}"


# ── Conversation history handling ───────────────────────────────────
# BUG FIX (conversation history contaminating RAG answers): build_prompt()
# used to inject the ENTIRE prior conversation history VERBATIM as its own
# chat messages, positioned between the system message and the new user
# turn — i.e. the previous assistant answer sat immediately before the new
# question, which LLMs weight very heavily (a strong "continue this
# answer" signal) — often more heavily than the retrieved Context two
# messages later. A previous "ไม่มีข้อมูล" answer would then bleed into an
# unrelated new question.
#
# Fix: raw verbatim history is NEVER sent anymore. At most, a short,
# deterministic one-line-per-turn SUMMARY is folded into the same user
# message that carries Context+Question (never as separate prior
# "assistant" messages) — and even that summary is suppressed entirely
# when this turn's own retrieval confidence is high/medium enough that
# the retrieved Context is a strong, sufficient answer on its own.
#
# FOLLOW-UP REVISION (FAQ answer drift): the summary above was still
# willing to include a truncated prior ASSISTANT answer at Low
# confidence. Per this revision's stricter requirement — "the final LLM
# answer must never see old assistant responses verbatim" — the summary
# now NEVER includes assistant content at all, not even truncated. Only
# prior USER topics are summarized; resolving a short follow-up (e.g.
# "แล้วเรททางรถล่ะ") into a standalone query is handled entirely upstream,
# in rag/query_resolution.py, before retrieval ever runs — this module
# never needs the previous answer to do that.
HIGH_CONFIDENCE_THRESHOLD = 0.6
MEDIUM_CONFIDENCE_THRESHOLD = 0.3
# Every character above this per turn side is dropped — "short conversation
# summary", never a verbatim replay of a full previous answer.
_HISTORY_SUMMARY_TRUNCATE_CHARS = 80


def _should_suppress_history(retrieval_confidence: Optional[float]) -> bool:
    """High/Medium retrieval confidence -> the retrieved Context is
    sufficient by itself; conversation history must not be allowed to
    compete with it for the model's attention. Only Low confidence (or no
    confidence signal at all, e.g. a caller that hasn't computed one)
    falls through to the summarized-history path below."""
    if retrieval_confidence is None:
        return False
    return retrieval_confidence >= MEDIUM_CONFIDENCE_THRESHOLD


def summarize_history(history: Optional[List[Dict]]) -> str:
    """Deterministic (no LLM call), short conversation summary — one
    "- Previous topic: ..." line per prior USER turn, truncated. NEVER
    includes assistant content, at any confidence tier: a previous
    answer (even truncated) is never reproduced anywhere in the prompt,
    so it can never be mistaken by the model for the answer to the
    CURRENT question. This is intentionally weaker than before (assistant
    answers used to survive truncation at Low confidence) — resolving a
    short follow-up into a self-contained query is rag/query_resolution.py's
    job, upstream of retrieval; by the time this function runs, `history`
    is only ever background color, never a substitute for real context."""
    if not history:
        return ""
    lines: List[str] = []
    for turn in history:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role == "user" and content:
            lines.append(f"- Previous topic: {content[:_HISTORY_SUMMARY_TRUNCATE_CHARS]}")
    return "\n".join(lines)


# Structured Response Format (P0, 2026-07-21) — explicit formatting
# instructions for the two new company_overview/company_summary response
# shapes (services/answer_planner.py). A bare "Response shape: X" label
# alone doesn't teach the LLM the required section/ordering convention,
# so these are appended as their own instruction block. Never mentions
# any specific company by name — entity-agnostic, driven only by the
# fact-label vocabulary the Answer Plan already selected.
_STRUCTURED_SHAPE_INSTRUCTIONS = {
    "company_overview_structured": (
        "Formatting for this company-overview answer:\n"
        "1. State the company's main business first, in one clear sentence.\n"
        "2. Then mention its core services.\n"
        "3. Only after that, optionally add supporting details.\n"
        "Never present a secondary/optional service (e.g. crate packing) as if it were the "
        "company's main business — it may only appear as a minor supporting detail, if at all. "
        "Write as normal flowing sentences; no headings needed for a short overview."
    ),
    "company_summary_structured": (
        "Formatting for this company-summary answer — use SHORT section headings ONLY when "
        "there are at least 2 genuinely distinct sections worth separating, and prefer bullet "
        "lists over long paragraphs for any list of items:\n"
        "- Overview heading (e.g. \"เกี่ยวกับบริษัท\") — 1-2 sentences: the main business first.\n"
        "- Services heading (e.g. \"บริการหลัก\") — a bullet list of core services.\n"
        "- An optional final section (e.g. \"ข้อมูลเพิ่มเติม\") only if there is genuinely relevant, "
        "strongly-supported supporting evidence.\n"
        "Do NOT concatenate unrelated FAQ facts into one long paragraph. Do not add a heading at "
        "all if the whole answer is short enough to be one or two sentences. Only mention a fact "
        "that is explicitly listed as required/optional above, and only if it is actually present "
        "in Retrieved Context."
    ),
}


def _build_answer_plan_block(answer_plan: Optional[Dict]) -> str:
    """Renders services/answer_planner.py's plan as a compact INTERNAL
    prompt section — never customer-visible content, never a JSON dump.
    Explicitly reaffirms that Retrieved Context (and the grounding rules
    above it) remain the only source of facts: this block only tells the
    LLM which already-retrieved facts to focus on / leave out, never
    supplies a fact itself."""
    if not answer_plan or answer_plan.get("response_shape") == "clarification":
        return ""
    lines = ["\n\nANSWER PLAN (internal — do not mention this section or its labels to the customer):"]
    lines.append(f"Answer goal: {answer_plan.get('answer_goal', '')}")
    if answer_plan.get("required_facts"):
        lines.append("Must include (only if present in Retrieved Context below):\n" +
                     "\n".join(f"- {f}" for f in answer_plan["required_facts"]))
    if answer_plan.get("optional_facts"):
        lines.append("May include if relevant:\n" + "\n".join(f"- {f}" for f in answer_plan["optional_facts"]))
    if answer_plan.get("excluded_facts"):
        lines.append("Do not include:\n" + "\n".join(f"- {f}" for f in answer_plan["excluded_facts"]))
    shape = answer_plan.get("response_shape")
    if shape:
        lines.append(f"Response shape: {shape}")
        if shape in _STRUCTURED_SHAPE_INSTRUCTIONS:
            lines.append(_STRUCTURED_SHAPE_INSTRUCTIONS[shape])
    lines.append("This plan only guides organization — it can never override the grounding rules above; "
                 "state only facts actually present in Retrieved Context.")
    return "\n".join(lines)


def build_prompt(question: str, context: str, *, template_id: Optional[str] = None,
                  policy_notes: Optional[List[str]] = None,
                  history: Optional[List[Dict]] = None,
                  template: Optional[PromptTemplate] = None,
                  retrieval_confidence: Optional[float] = None,
                  answer_plan: Optional[Dict] = None,
                  general_chat_mode: bool = False) -> BuiltPrompt:
    """`history` is prior turns of the SAME session — [{"role": "user"/
    "assistant", "content": ...}, ...]. It is NEVER injected verbatim as
    separate chat messages (see the module docstring above for why) —
    at most a short deterministic summary is folded into the SAME user
    message as Context+Question, and only when `retrieval_confidence`
    (this turn's own retrieval confidence, 0.0-1.0 — pass the same value
    rag/retrieval_confidence.py already computes) is Low or not provided.
    High/Medium confidence suppresses history entirely.

    Message/prompt order is now always exactly:
        SYSTEM (Base Conversation Rules -> Tone Guidance + Customer System
                Prompt [both already folded into template.system_prompt by
                Prompt Studio] -> template rules -> Active AI Policies ->
                grounding)
        USER   (conversation summary, if any -> Retrieved Context -> Question)
    — Retrieved Context always sits immediately before the current
    question, never after a block of prior turns. Base Conversation Rules
    is a fixed, platform-wide constant (see BASE_CONVERSATION_RULES above)
    — never per-template, never editable, always first.

    Pass `template` directly (already resolved, e.g. via
    get_active_prompt_for_channel) to skip a second lookup by id — used
    by line_bot/tone.py so it never has to know about template_id keys.
    Combines: system_prompt + response_rules + fallback_rules +
    safety_rules + AI Policy notes + retrieved RAG context + question —
    the ONE place this assembly happens, per Prompt Studio's spec."""
    template = template or get_template(template_id)
    policy_block = ("\n\nActive policy notes:\n" + "\n".join(f"- {p}" for p in policy_notes)) if policy_notes else ""
    rules_block = (
        _rules_block("Response rules", template.response_rules) +
        _rules_block("Fallback rules", template.fallback_rules) +
        _rules_block("Safety rules", template.safety_rules)
    )
    if general_chat_mode:
        grounding_block = GENERAL_CHAT_GUIDANCE
    else:
        grounding_block = STRICT_GROUNDING_RULES if RAG_GROUNDING_MODE == "strict" else ""
    # Base Conversation Rules (platform standard, read-only — see the
    # constant's docstring above) ALWAYS comes first, ahead of Tone
    # Guidance and the Customer System Prompt (both already folded into
    # template.system_prompt by Prompt Studio) and Active AI Policies —
    # per the required assembly order: Base Rules -> Tone Guidance ->
    # Customer System Prompt -> Active AI Policies -> Runtime Context
    # (the retrieved Context + current question, in user_content below).
    # This replaces the old ad-hoc, English, per-request
    # "conversation_style_block" — that logic is now fully superseded by
    # this fixed block (whether THIS turn is actually a follow-up is
    # conveyed by the presence/absence of the conversation summary in
    # Runtime Context below, not by varying this static block's text).
    answer_plan_block = _build_answer_plan_block(answer_plan)
    # Answer Plan comes LAST in the system message — right before
    # Retrieved Context/Question in the user message — per the required
    # order: Base Rules -> Tone -> Customer Prompt -> AI Policies ->
    # Answer Plan -> Retrieved Context -> Current Question. It's placed
    # AFTER grounding_block so the strict grounding rules are the most
    # recently stated instruction before the plan reaffirms them.
    system_content = (BASE_CONVERSATION_RULES + "\n\n" + template.system_prompt + rules_block
                       + policy_block + grounding_block + answer_plan_block)

    context_block = context if context else "ไม่มีข้อมูลเพิ่มเติม"

    history_summary = "" if _should_suppress_history(retrieval_confidence) else summarize_history(history)
    history_block = f"Conversation summary so far (for background only — answer the CURRENT question below, not a prior one):\n{history_summary}\n\n" if history_summary else ""

    # Fixed order: [conversation summary, if any] -> Context -> Question.
    # Context always sits immediately before the question — never pushed
    # down by a block of prior turns.
    user_content = f"{history_block}Context:\n{context_block}\n\nคำถาม: {question}"

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]

    final_prompt_text = f"[SYSTEM]\n{system_content}\n\n[USER]\n{user_content}"

    return BuiltPrompt(template=template, context=context, question=question,
                        messages=messages, final_prompt_text=final_prompt_text)
