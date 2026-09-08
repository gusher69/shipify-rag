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
    "- HIGHEST PRIORITY: if the question asks what a term/document/code IS or HOW something is "
    "calculated/works (e.g. \"X คืออะไร\", \"X ทำงานอย่างไร\", \"X คิดยังไง\"), and the Context only "
    "MENTIONS that term in passing (e.g. as part of a different answer) WITHOUT actually defining "
    "it or explaining its calculation, you must NOT produce a definition or calculation method from "
    "your own knowledge, even though you know what the term commonly means in the real world. "
    "Instead say only what the Context actually states about it, then explicitly add that the "
    "definition/calculation itself is not confirmed in the system (e.g. \"ระบบมีข้อมูลเพียงว่า "
    "[restate the Context's own words] แต่ยังไม่มีคำอธิบายความหมาย/วิธีคำนวณโดยละเอียดในระบบค่ะ\"). "
    "This applies even to well-known real-world terms (VAT, Form E, HS Code, ฯลฯ) — being a common, "
    "well-known concept is never a reason to supply its real-world definition here. Concrete "
    "example: if a customer asks \"Form E คืออะไร\" and the Context only mentions, in a DIFFERENT "
    "answer, that \"เอกสาร Form E อาจช่วยลดอากรภายใต้ความตกลงอาเซียน–จีน\" (with no further "
    "explanation of what Form E actually is or certifies), the correct answer is ONLY that "
    "sentence restated plainly, followed by an honest \"แต่ยังไม่มีคำอธิบายเพิ่มเติมว่า Form E คือ"
    "เอกสารอะไรโดยเฉพาะในระบบค่ะ\" — NEVER add real-world facts such as \"Form E is a certificate "
    "of origin\", \"ASEAN-China Free Trade Area\", or \"ACFTA\", even though these are all true and "
    "well-known — none of them were written in the Context.\n"
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
    "- Do not state a specific number, rate, percentage, or fee that is not written verbatim in "
    "the Context, even when the Context discusses the same general topic — if the Context itself "
    "avoids committing to an exact figure (e.g. says rates 'vary' or 'differ by category'), never "
    "supply a specific figure from outside/general knowledge to fill that gap.\n"
    "- Preserve every exact factual value from the Context VERBATIM: phone numbers, prices, "
    "rates, durations, addresses, URLs, tracking/order/reference codes, percentages, dates/times, "
    "coupon values, IDs. Rephrase the Thai prose around a value if you like, but never reformat, "
    "regroup, round, or otherwise change the value itself (e.g. never turn \"091-5050-775\" into "
    "\"091-505-0775\", or \"6,900 บาท/CBM\" into \"6900\").\n"
    "- Never begin an answer with a \"ยังไม่มีข้อมูล\" / \"ไม่มีข้อมูลยืนยัน\" hedge and then go on "
    "to give a usable answer from the Context (including a website/menu route). If the Context "
    "gives a usable answer, state it directly; use a no-information reply only when the Context "
    "has nothing usable for what was asked.\n"
    "- Do not explain what a named term, document, code, or requirement technically means, "
    "certifies, or legally requires beyond what is explicitly stated in the Context — if the "
    "Context only names something (e.g. a form, code, or document) and says what it may help "
    "with, without explaining its underlying legal mechanism, do not add that mechanism from "
    "outside/general knowledge. Concrete example: if the Context says a document 'may help "
    "reduce duty under a trade agreement' without saying what the document certifies or its "
    "formal name, do NOT add 'this document certifies the product's country of origin' or spell "
    "out the agreement's formal name/acronym (e.g. 'ACFTA') — both are real-world facts about "
    "that document supplied from outside knowledge, not from the Context. Likewise, if the "
    "Context says a tax 'must be considered' without stating how it is calculated, do NOT add "
    "the calculation method (e.g. 'based on goods value plus shipping plus duty') or a rate — "
    "that is genuine tax knowledge, not something the Context said.\n"
    "- Do not add an escalation/handoff sentence (e.g. offering to check with a team) unless an "
    "active policy note actually triggered one.\n"
    "- When the question asks about a PROCESS/PROCEDURE (steps, how something is done), use ONLY "
    "the steps/facts actually written in the Context, in that order — never add a step, "
    "requirement, fee, timeline, or legal/compliance detail (e.g. supplier vetting, negotiation, "
    "invoices/certificates, customs clearance, duties/taxes, HS codes, import licenses/permits) "
    "just because it is standard practice for that general topic elsewhere in the world. A process "
    "described in the Context is treated as COMPLETE as given — you may summarize, reorder for "
    "readability, and make the wording natural, but never complete an apparently missing step using "
    "outside/industry knowledge. If the Context only covers PART of what was asked, answer that part "
    "and say the rest is not confirmed, rather than filling the gap with plausible-sounding steps.\n"
    "- Refusing to answer a question the Context already answers directly is ALSO a grounding "
    "failure, not a safety measure. When a Context entry's own Question line is clearly the same "
    "question the customer asked (or a listed Alternative phrasing of it) and its Answer states a "
    "plain fact, yes/no, or availability (e.g. \"ทางเรามีบริการ... ให้นะคะ\", \"ยังไม่มีบริการ...\"), "
    "give that answer directly and confidently — do not say \"ไม่มีข้อมูล\"/refuse merely because the "
    "SAME Answer also defers ONE secondary detail elsewhere (e.g. \"เงื่อนไขตามรูปภาพที่แอดมินส่งให้\", "
    "\"ตรวจสอบเรทล่าสุดกับเจ้าหน้าที่ก่อน\", \"กรุณาแจ้งรายละเอียดเพิ่มเติม\"). State the confirmed part "
    "exactly as the Context gives it, then mention the deferred detail exactly as the Context itself "
    "phrases it — never invent what that deferred detail actually is, and never treat its mere "
    "presence as a reason the whole answer is unconfirmed."
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


# ── Base Conversation Rules — Core Conversation Invariants ─────────────
# (a.k.a. "Base Conversation Rules"): a FIXED, platform-wide constant —
# never stored per-template, never editable from Prompt Studio, injected
# into EVERY built prompt automatically BEFORE Tone Guidance and the
# per-template System Prompt (see build_prompt()'s system_content
# assembly). This is the ONE place these invariants live; they overlap
# with nothing in Tone Guidance (phrasing/formality/warmth only) or AI
# Policies (business/escalation rules, evaluated separately).
#
# PROMPT-STUDIO-BASE-RULES-CLEANUP — trimmed to the core invariants and
# hidden from the Prompt Studio admin UI (runtime injection unchanged).
BASE_CONVERSATION_RULES = (
    "## Core Conversation Rules\n"
    "\n"
    "### Conversation Context\n"
    "- ให้ความสำคัญกับข้อความล่าสุดของลูกค้าสูงสุด\n"
    "- ใช้บริบทก่อนหน้าเฉพาะเมื่อเกี่ยวข้องกับคำถามปัจจุบัน\n"
    "- คำถามสั้นหรือข้อความต่อเนื่อง ให้ตีความจากหัวข้อที่กำลังคุยอยู่\n"
    "- หากลูกค้าเปลี่ยนเรื่อง แก้ข้อมูล หรือปฏิเสธคำตอบเดิม ให้ประเมิน Intent ใหม่ทันที\n"
    "- ห้ามให้ workflow หรือข้อมูลเก่าที่จบไปแล้วกลับมาควบคุมคำถามใหม่\n"
    "- ห้ามใช้คำตอบเก่าของ AI เป็นแหล่งข้อมูลอ้างอิง\n"
    "\n"
    "### Intent & Data Source\n"
    "- ต้องเข้าใจ Intent และ Conversation Context ก่อนเลือกแหล่งข้อมูล\n"
    "- ข้อมูลทั่วไปของบริษัทให้ใช้ Trusted Knowledge\n"
    "- ข้อมูลเฉพาะลูกค้าให้ใช้ Authorization + ERP\n"
    "- การคำนวณให้ใช้ Calculator\n"
    "- คำขอดำเนินการให้ใช้ Workflow / Business Action ที่ได้รับอนุญาต\n"
    "- ห้ามใช้ RAG หรือ ERP เพียงเพราะพบ keyword โดยยังไม่เข้าใจ Intent\n"
    "\n"
    "### Business Truth\n"
    "- ราคา นโยบาย เงื่อนไขบริษัท สถานะบิล ETA และข้อมูลลูกค้า ต้องอ้างอิงจาก Knowledge Base, ERP หรือ Trusted System เท่านั้น\n"
    "- ห้ามเดา ห้ามสร้างข้อมูล และห้ามอ้างว่าดำเนินการสำเร็จถ้ายังไม่มีผลจากระบบจริง\n"
    "- คำถามทั่วไปที่ไม่สร้างข้อมูลเฉพาะของ Shipify สามารถตอบด้วยความรู้ทั่วไปได้ แต่ห้ามนำเสนอว่าเป็นนโยบายของบริษัท\n"
    "\n"
    "### Public & Private Information\n"
    "- ข้อมูลสาธารณะ เช่น เว็บไซต์ ช่องทางติดต่อ บริการทั่วไป และข้อมูล FAQ ไม่ต้องยืนยันตัวตน\n"
    "- ข้อมูลส่วนตัวหรือข้อมูลธุรกรรมต้องผ่าน Authorization ตามระบบ\n"
    "- ห้ามใช้ข้อมูลเก่าหรือ profile legacy เพื่อข้าม authorization\n"
    "\n"
    "### Fallback\n"
    "- KB_NOT_FOUND ไม่ได้หมายความว่าบทสนทนาต้องจบ\n"
    "- หากลูกค้ากำลังแสดงความต้องการใช้บริการ ให้ถามต่อเพื่อทำความเข้าใจและพาไปขั้นตอนถัดไป\n"
    "- หากเป็นข้อมูลเฉพาะบริษัทที่ไม่มีข้อมูลยืนยัน ให้แจ้งตามจริงโดยไม่เดา\n"
    "- กล่าวถึงเจ้าหน้าที่เฉพาะเมื่อมีการ handoff จริง, ลูกค้าขอเจ้าหน้าที่ หรือข้อมูลนั้นจำเป็นต้องให้เจ้าหน้าที่ตรวจสอบ\n"
    "\n"
    "### Response Safety\n"
    "- ห้ามเปิดเผยข้อมูลส่วนตัวก่อนผ่าน Authorization\n"
    "- ห้ามทำ destructive action โดยไม่มีขั้นตอนยืนยันที่ถูกต้อง\n"
    "- ห้ามให้ stale state, pending confirmation หรือ workflow เก่ามีผลกับคำถามใหม่ที่ไม่เกี่ยวข้อง"
)
# Human-like Multi-Message Replies (services/message_segmenter.py): how
# many message bubbles are actually sent is decided deterministically,
# AFTER the LLM response exists, by services/message_segmenter.py — never
# by these rules or by Tone Guidance. (The former "## รูปแบบข้อความ" block
# was removed from the Core Conversation Rules in
# PROMPT-STUDIO-BASE-RULES-CLEANUP; the segmenter is unaffected.)


# ── Response Style Guidance (PHASE-6E) ────────────────────────────────
# PHRASING ONLY. Appended LAST to the system prompt, after grounding and
# the answer plan, so it can shape HOW a reply reads without ever
# changing WHAT is true, which source was chosen, or which action runs
# (all decided upstream, before this text is built). Complements the
# per-persona Response Rules from Prompt Studio — always present even if
# the DB template carries none.
RESPONSE_STYLE_GUIDANCE = (
    "\n\nRESPONSE STYLE (phrasing only — never changes what is true, which "
    "source is used, or what action is taken):\n"
    "- Shape each reply as: (1) answer or acknowledge the point directly, "
    "(2) add only the context that is actually needed, (3) end with the one "
    "useful next step or question — nothing more.\n"
    "- Sound like a warm, competent Thai customer-service person: polite, "
    "friendly, concise, professional; never stiff, childish, or salesy. Keep "
    "the ค่ะ/คะ ending consistent with the configured persona. At most one "
    "emoji, only when it truly fits — usually none.\n"
    "- Do not repeat a greeting or an apology already given earlier in the "
    "conversation, and do not read the customer's whole question back to "
    "them before answering.\n"
    "- Never show internal or system wording to the customer — e.g. "
    "SAFE_FALLBACK, KB_NOT_FOUND, RAG, ERP, intent, workflow, pending "
    "state, classifier, tool or action names, \"ฐานความรู้\", \"ในระบบ\", "
    "\"ไม่พบข้อมูลในระบบ\". Say it the way a person would.\n"
    "- When confirmed business information is genuinely unavailable, say it "
    "plainly and naturally — \"เรื่องนี้ตอนนี้ยังไม่มีข้อมูลที่ยืนยันได้ค่ะ\" — "
    "not a system phrase. Do NOT say staff will check or follow up unless a "
    "real handoff is actually happening this turn.\n"
    "- If the customer already gave a detail (a weight, a size, a brand, a "
    "link, a destination), use it — never ask for it again. If only one "
    "detail is still missing, ask for just that one.\n"
    "- Keep replies to 1–3 short paragraphs. Use short numbered steps only "
    "when the instructions genuinely need an order. Do not paste long "
    "policy text unless the customer asked for that level of detail.\n"
    "- For a general how-to question, answer it naturally from general "
    "knowledge; do not mention knowledge bases, databases, retrieval, or AI "
    "limitations.\n"
    "- When the customer is showing interest in a service, keep the "
    "conversation moving with a natural next question instead of ending on "
    "generic information — and ask only for details the next step really "
    "needs, never for a private identifier unless it is actually required."
)


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


# ── Prompt-config read cache (latency P0, 2026-08-31) ──────────────────
# ai_prompt_assignments / ai_prompt_templates change only on an admin
# edit, but a single LINE turn resolved the same template 3x from the DB
# (get_active_prompt + get_template x2). 60s TTL — an admin edit shows up
# within a minute; the identical row is returned in between.
_PROMPT_CACHE: Dict[str, tuple] = {}
_PROMPT_CACHE_TTL = 60.0


def _pc_get(key: str, producer):
    import time as _t
    hit = _PROMPT_CACHE.get(key)
    if hit is not None and hit[0] > _t.time():
        return hit[1]
    val = producer()
    _PROMPT_CACHE[key] = (_t.time() + _PROMPT_CACHE_TTL, val)
    return val


def clear_prompt_cache():
    """Called by the Prompt Studio save/assign routes so an admin edit
    takes effect immediately rather than waiting out the TTL."""
    _PROMPT_CACHE.clear()


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
        def _fetch():
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
            return None
        cached = _pc_get(f"tpl:{template_id}", _fetch)
        if cached is not None:
            return cached
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
    def _resolve():
        if tier:
            tier_prompt = get_active_prompt_for_tier(tier)
            if tier_prompt:
                return tier_prompt
        if channel:
            return get_active_prompt_for_channel(channel)
        return get_default_template()
    return _pc_get(f"active:{channel}:{tier}", _resolve)


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
    if answer_plan.get("requested_components"):
        # P1.2A — the customer asked several things in one message. Answer
        # EVERY component the Retrieved Context supports; for any it does
        # not support, say ONLY that one component is unconfirmed — never a
        # blanket "no information" reply for the whole question.
        lines.append("Requested components — give EVERY one an explicit verdict:\n"
                     + "\n".join(f"- {c}" for c in answer_plan["requested_components"]))
        lines.append(
            "Before writing the reply, run this checklist SILENTLY for EACH requested item "
            "(do not show the checklist to the customer — only the concise result):\n"
            "  1. CATEGORY: from ordinary real-world knowledge, name the item's general product "
            "category (liquid, beverage, food / sauce / condiment, cosmetic, medicine, battery, "
            "flammable, sharp object, plant / living thing, counterfeit goods, …). If you cannot "
            "confidently place it in any such category, skip to step 4B.\n"
            "  2. POLICY: search the Retrieved Context for a rule about the item itself OR about "
            "that category (or a broader category it clearly falls under).\n"
            "  3. APPLY: if such a rule PROHIBITS it, the verdict is PROHIBITED — state it plainly, "
            "the SAME as for an item named word-for-word in the Context. The item NOT appearing "
            "literally in the Context is IRRELEVANT and is NEVER a reason to withhold this verdict. "
            "If the rule PERMITS it, the verdict is allowed.\n"
            "  4. UNCONFIRMED (ยังไม่ยืนยัน) is permitted ONLY in these two cases:\n"
            "     4A. you cannot confidently classify the item into any relevant category, or\n"
            "     4B. no Context rule covers the item or any category it clearly belongs to.\n"
            "Worked example: 'น้ำยาซักผ้า' -> category = liquid; Context = 'ของเหลวไม่สามารถนำเข้าได้' "
            "-> verdict = PROHIBITED (do NOT say 'ยังไม่ยืนยัน' just because 'น้ำยาซักผ้า' is not a "
            "literal Context row). Contrast: 'แก้วน้ำ' -> category = drinking glass / glassware, "
            "NOT a liquid (the 'น้ำ' in its name is irrelevant); if nothing in the Context covers "
            "glassware -> verdict = UNCONFIRMED (case 4B). Never invent a verdict, and never "
            "reply 'no information' for the whole question.")
        lines.append(
            "The verdict itself must always come from a policy actually present in the Retrieved "
            "Context (on the item or a category) — never from outside knowledge. A prohibited-goods "
            "LIST only tells you what is NOT allowed — it can NEVER be used to conclude that an "
            "item merely absent from it IS allowed.")
    if answer_plan.get("conflicting_components"):
        # P1.2B — the Retrieved Context carries INCOMPATIBLE trusted values
        # for these. Do not choose one; say plainly it is not confirmed.
        lines.append(
            "SOURCE CONFLICT — the Retrieved Context contains DIFFERENT, incompatible values for "
            "the following. Do NOT state or choose any single value for these; say plainly that "
            "the trusted information is currently inconsistent / not yet confirmed and (if "
            "appropriate) suggest confirming with staff:\n"
            + "\n".join(f"- {c}" for c in answer_plan["conflicting_components"]))
        lines.append(
            "Answer every OTHER requested part normally from the Context — a conflict on one part "
            "never blocks the rest of the answer, and never triggers a blanket no-information reply.")
    _fu = answer_plan.get("followup") or {}
    # Only the "elicit_product_type" purpose needs the LLM to phrase a
    # question — it is unconditionally useful for an import-interest turn.
    # "offer_alternative_product" is verdict-conditional (only after an
    # actual PROHIBITED answer) and is appended deterministically by
    # services/playground_orchestrator.py, never through this prompt.
    if _fu.get("needed") and _fu.get("question_goal") and _fu.get("purpose") == "elicit_product_type":
        # P2 — the ONLY sanctioned exception to BASE_CONVERSATION_RULES'
        # no-auto-trailing-question rule. Narrow, plan-gated, single question.
        lines.append(
            "CONTEXTUAL FOLLOW-UP — after the factual answer above, ask EXACTLY ONE short, "
            f"natural question whose goal is: {_fu['question_goal']}. Ask only this one question "
            "and nothing else — no other closing line, no generic 'มีอะไรให้ช่วยอีกไหม' / "
            "'สอบถามเพิ่มเติมได้'. If the factual answer already fully resolves the customer's "
            "need, ask nothing.")
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
                       + policy_block + grounding_block + answer_plan_block
                       + RESPONSE_STYLE_GUIDANCE)

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
