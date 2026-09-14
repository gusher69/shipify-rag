"""Orchestrates one AI Playground turn end-to-end, calling ONLY the named
services (never OpenAI or Supabase directly) and recording a full
execution trace for the Pipeline tab.

Execution order here is the functionally-correct one (retrieval must
happen before prompt assembly, since the prompt needs the retrieved
context) — the Playground's Pipeline tab renders these stages in this
same order.
"""
import re
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional

from config import OPENAI_CHAT_MODEL
from rag.searcher import is_analytical
from rag.query_expansion import normalize_query
from rag.spell_correction import correct_query
from rag.query_resolution import resolve_conversation, requested_transport_modes
from rag.canonical_query import rewrite_canonical_query
from rag.query_understanding import classify_actionable_intent
from rag.confidence import compute_confidence, confidence_label as _confidence_label_from_score
# SEMANTIC-FIRST-2.1 — the central-interpreter families that are always a
# PUBLIC company-information question (never general chit-chat).
from services.conversation_semantics import PUBLIC_INFO_FAMILIES as _PUBLIC_INFO_COMPANY_FAMILIES
# PHASE-6-SLOT-CONSUMPTION — the SAME count-unit vocabulary already used
# for a bare quantity ANSWER, reused here (never re-declared) so a
# quantity+unit span is never mistaken for a product noun below.
from services.conversation_semantics import _USER_QTY_RE as _IMPORT_QTY_UNIT_RE
from services.conversation_semantics import _METHOD_WORD_RE as _IMPORT_METHOD_WORD_RE
from services.rag_service import get_rag_service
from services.policy_engine import evaluate as evaluate_policies, get_escalation_settings, get_messaging_settings, PolicyVerdict
from services.policy_studio_service import get_default_policy_set
from services.answer_planner import plan_answer
from services.attachment_planner import plan_attachments
from services.message_segmenter import segment_message
from services.prompt_builder import build_prompt, DEFAULT_TEMPLATE_ID
from services.llm_service import get_llm_service, estimate_cost_usd
from services.embedding_service import get_embedding_provider
from rag.evidence_classifier import classify_evidence, select_citation_sources
from rag.hybrid_scoring import has_strong_company_profile_evidence

# Zero-Evidence Fallback Tone Guard (Customer Journey UAT, 2026-08-27) —
# the Answerability Gate's deterministic "no information" fallback below
# never invokes the LLM, so it cannot pick up CS-03's own Human CS tone
# rules (urgency acknowledgement, complaint acknowledgement) the way an
# LLM-generated reply does. Deliberately separate from
# services/policy_engine.py::DISSATISFACTION_KEYWORDS (that list gates
# real ESCALATION to a human via the exact production handoff logic
# line_bot/webhook.py shares — this guard only prepends one short,
# neutral acknowledgement sentence to the SAME truthful "no information"
# text; it never escalates, never fabricates a status, never invents a
# cause). Narrowly scoped to this one fallback branch, not a new tone
# system.
_URGENCY_SIGNAL_RE = re.compile(r"รีบ|ด่วน|ตามมาหลาย|ตามอยู่|ไม่ทันใช้", re.IGNORECASE)
_COMPLAINT_SIGNAL_RE = re.compile(r"ของเก่า|มีรอย|ชำรุด|เสียหาย|ของผิด|ของขาด|ตกหล่น|ไม่ครบ", re.IGNORECASE)


def _g6c_promote_general_assistance(question, interpretation, history, *,
                                     rag_strong_direct=False, rag_faq_exact=False) -> bool:
    """PHASE-6C — Smart General Assistance. PROMOTE a turn onto the
    general-chat path (LLM answers from general knowledge, with the same
    no-fabrication guardrails in GENERAL_CHAT_GUIDANCE) when the Request
    Grounding Classifier decisively reads it as GENERAL_ASSISTANCE — a
    general packing / fragile-goods / logistics how-to question — that
    the existing company-topic gate does NOT already claim.

    STRICTLY ADDITIVE: it can only send a turn to general-chat that the
    keyword gate below would otherwise have sent to the SAME general-chat
    branch anyway (its `not (...)` arm). It defers entirely to the
    curated `_COMPANY_OPERATIONAL_TOPIC_RE` — a message carrying any
    company-topic term (CBM, ทางเรือ/ทางรถ, นำเข้า, ค่าส่ง, นโยบาย, …) is a
    Shipify question by project definition and stays on the company RAG
    path (that gate is deliberately broad; do not fight it here). It
    NEVER promotes a BUSINESS_TRUTH_REQUIRED / PRIVATE_OR_ERP_REQUIRED /
    MIXED / UNCLEAR turn, and never fires when the company RAG actually
    found strong/exact evidence (that answer is better than a general
    one). Its one job: an unseen PURE how-to paraphrase that _compose
    mislabelled as PRODUCT_POLICY ("ของแตกง่ายควรแพ็กยังไงดี") must not
    dead-end on "no confirmed info" + Human CS."""
    if rag_strong_direct or rag_faq_exact:
        return False
    if _COMPANY_OPERATIONAL_TOPIC_RE.search(question or ""):
        return False
    try:
        from services.request_grounding_classifier import classify_request_grounding
        g = classify_request_grounding(question or "", interpretation, history)
    except Exception:
        return False
    # g.cls == GENERAL_ASSISTANCE already REQUIRES an explicit general-
    # knowhow shape AND zero business / private marker — a strictly
    # stronger signal than the family label. It overrides a _compose
    # mislabel that bucketed a general how-to as PRODUCT_POLICY. A real
    # public-info family question ("โกดังจีนอยู่ไหน", "ขอเบอร์ติดต่อ") does
    # not carry that shape, so its grounding class is never
    # GENERAL_ASSISTANCE and it stays on the company RAG path here.
    return g.cls == "GENERAL_ASSISTANCE" and g.business_part is False and g.private_part is False

# P7.1 — a company GUARANTEE / WARRANTY / RESPONSIBILITY yes-no question
# ("Shipify รับประกันว่า…ไหม", "…รับผิดชอบเรื่อง…ไหม", "…การันตี…ไหม"). Such a
# question is a binary company-POLICY fact — when the KB actually holds a
# policy it is a curated (exact/near-exact) FAQ row; without one, synthesis
# would infer a policy (positive OR negative) from generic company chunks
# plus world knowledge — an invented company fact either way.
_COMPANY_GUARANTEE_Q_RE = re.compile(
    r"(รับประกัน|การันตี|การันตี|รับรองได้|รับผิดชอบ|ยืนยันได้ไหมว่า|guarantee|warrant)"
    r"[^\n]{0,45}?(ไหม|มั้ย|มัย|หรือไม่|รึเปล่า|หรือเปล่า|ได้ไหม|ได้มั้ย)",
    re.IGNORECASE)

# Deterministic Grounding Safety Net (P0 Final Blocker Closure, 2026-08-28)
# — confirmed live: for a "X คืออะไร"-shaped question where the trusted
# Context only mentions X in passing (e.g. RAG-035's own trusted answer
# names "Form E" only as "may help reduce duty under the ASEAN-China
# agreement", never explaining what Form E actually IS), the LLM
# persistently added real-world facts about X (its formal name, its
# certification mechanism) despite an explicit STRICT_GROUNDING_RULES
# instruction naming this EXACT term as a worked example of what never
# to add — prompt instructions alone proved insufficient for this one
# well-known-term class. This is a small, deliberately narrow watchlist
# of terms that are strong, unambiguous signals of exactly that failure
# mode (a formal international-trade-agreement name/acronym, or a
# certificate-of-origin description) — never a general "any added fact"
# detector, which would be too fragile/broad. If the model's own answer
# contains one of these AND it does not appear anywhere in the trusted
# Context actually supplied, the answer is deterministically discarded
# and replaced with the SAME safe-uncertainty phrase already used
# elsewhere in this module for genuine no-information cases — zero
# additional LLM calls, never a silent partial rewrite of the model's
# own wording.
_GROUNDING_RISK_TERMS = [
    "ACFTA", "ASEAN-China Free Trade Area", "AFTA", "ASEAN Free Trade Area",
    "หนังสือรับรองแหล่งกำเนิดสินค้า", "หนังสือรับรองถิ่นกำเนิดสินค้า", "certificate of origin",
]


def _find_ungrounded_risk_term(answer_text: str, context_text: str) -> Optional[str]:
    answer_l = (answer_text or "").lower()
    context_l = (context_text or "").lower()
    for term in _GROUNDING_RISK_TERMS:
        term_l = term.lower()
        if term_l in answer_l and term_l not in context_l:
            return term
    return None


# Phone-shaped run: 9–10 digits, optionally grouped with - or spaces.
_PHONE_RUN_RE = re.compile(r"\d[\d\-\s]{7,13}\d")

# Final category-verdict hotfix (2026-09-01) — for a MULTI-product
# eligibility answer, deterministically FIRM a soft/unconfirmed verdict
# when the answer's OWN text has already classified that item into a
# real-world category ("<x>เป็นของเหลว", "…เป็นอาหาร") that the trusted
# Context prohibits. This reads the model's own classification from its
# output — it is NOT a hardcoded product->category map. Same family as
# _restore_verbatim_scalar_values / _strip_contradictory_noinfo_hedge.
_PROHIBITED_CATEGORY_WORDS = (
    "ของเหลว", "อาหาร", "ของกิน", "เครื่องดื่ม", "เครื่องสำอาง", "วัตถุไวไฟ",
    "วัตถุอันตราย", "แบตเตอรี่", "แบตเตอร์รี่", "ยาเวชภัณฑ์", "ของมีคม",
    "สิ่งมีชีวิต", "พืช",
)
# "<item> (จัด)เป็น (สินค้าประเภท)? <CATEGORY>" — a firm classification of
# the item itself, not a conditional ("หาก…เป็น", "อาจมี…").
_ITEM_IS_CATEGORY_RE = re.compile(
    r"(?<!หาก)(?<!ถ้า)(?<!อาจมี)(?:จัดเป็น|เป็น|คือ)\s*(?:สินค้าประเภท)?\s*"
    r"(" + "|".join(_PROHIBITED_CATEGORY_WORDS) + r")")
# Exact soft-verdict phrases -> replaced 1:1 with a firm prohibition on a
# line that already carries a prohibited-category classification.
_SOFT_TO_FIRM = [
    ("จึงอาจเข้าข่ายสินค้าต้องห้ามเช่นกัน", "จึงไม่สามารถนำเข้าได้ค่ะ"),
    ("ซึ่งอาจเข้าข่ายสินค้าต้องห้ามเช่นกัน", "ซึ่งไม่สามารถนำเข้าได้ค่ะ"),
    ("จึงอาจเข้าข่ายสินค้าต้องห้าม", "จึงไม่สามารถนำเข้าได้ค่ะ"),
    ("ซึ่งอาจเข้าข่ายสินค้าต้องห้าม", "ซึ่งไม่สามารถนำเข้าได้ค่ะ"),
    ("อาจเข้าข่ายสินค้าต้องห้ามเช่นกัน", "ไม่สามารถนำเข้าได้ค่ะ"),
    ("อาจเข้าข่ายสินค้าต้องห้ามด้วย", "ไม่สามารถนำเข้าได้ค่ะ"),
    ("อาจเข้าข่ายสินค้าต้องห้าม", "ไม่สามารถนำเข้าได้ค่ะ"),
    ("อาจเข้าข่ายเช่นกัน", "ไม่สามารถนำเข้าได้ค่ะ"),
    ("อาจจะไม่สามารถนำเข้าได้", "ไม่สามารถนำเข้าได้"),
    ("น่าจะไม่สามารถนำเข้าได้", "ไม่สามารถนำเข้าได้"),
    ("อาจไม่สามารถนำเข้าได้", "ไม่สามารถนำเข้าได้"),
]


def _firm_prohibited_category_hedge(answer_text: str, context_text: str) -> str:
    """On a MULTI-product eligibility answer: if a line has classified an
    item into a category the trusted Context prohibits AND then softened
    that item's verdict, swap the soft phrase for a firm prohibition.
    Reads the model's OWN classification from its output — never a
    hardcoded product->category map. No effect on an unclassified line
    (e.g. bare 'ยังไม่มีข้อมูล') or a conditional ('หากมีของเหลว…')."""
    if not answer_text or not context_text:
        return answer_text
    if not any(k in context_text for k in ("ไม่รับนำเข้า", "ห้ามนำเข้า", "ไม่สามารถนำเข้า")):
        return answer_text
    prohibited_here = {c for c in _PROHIBITED_CATEGORY_WORDS if c in context_text}
    if not prohibited_here:
        return answer_text
    out = []
    for line in answer_text.split("\n"):
        m = _ITEM_IS_CATEGORY_RE.search(line)
        if m and m.group(1) in prohibited_here:
            for soft, firm in _SOFT_TO_FIRM:
                if soft in line:
                    line = line.replace(soft, firm)
        out.append(line)
    return "\n".join(out)


# P2A blocker — a single-product eligibility answer must not conclude
# "can be imported" without an AFFIRMATIVE permission in the Context;
# inferring "allowed" from mere absence in a prohibited-goods list is not
# grounded. Matched on the ANSWER text; single-product eligibility turns
# only. (a) a non-negated positive verdict for the item; (b) the classic
# "not on the list, therefore allowed" shape.
_POSITIVE_VERDICT_RE = re.compile(
    r"(?<!ไม่)สามารถนำเข้าได้|(?<!ไม่)นำเข้าได้(ค่ะ|ครับ|นะคะ|เลย|อยู่)")
_ABSENCE_ALLOW_RE = re.compile(
    r"(นำเข้าได้|สามารถนำเข้าได้)[^\n]{0,45}(ไม่ได้อยู่ใน|ไม่อยู่ใน|ไม่ได้ระบุ|ไม่พบใน|ไม่ปรากฏใน)"
    r"[^\n]{0,20}(รายการ|สินค้าต้องห้าม|ต้องห้าม|ระบบ)"
    r"|(ไม่ได้อยู่ใน|ไม่อยู่ใน|ไม่ปรากฏใน|ไม่พบใน)[^\n]{0,25}(รายการ|สินค้าต้องห้าม|ต้องห้าม)"
    r"[^\n]{0,35}(จึงนำเข้าได้|จึงสามารถนำเข้าได้|สามารถนำเข้าได้|นำเข้าได้)")
# An explicit POSITIVE-permission statement in the Context (never a
# negated one — "ไม่รับนำเข้า" / "ไม่สามารถนำเข้า" must not count).
_AFFIRMATIVE_PERMISSION_RE = re.compile(
    r"(?<!ไม่)รับนำเข้าสินค้า(?!ผิด)(?!ต้องห้าม)|นำเข้าได้ทุกประเภท|(?<!ไม่)อนุญาตให้นำเข้า"
    r"|สินค้าทั่วไป[^\n]{0,10}นำเข้าได้|รายการสินค้าที่รับนำเข้า")

# P2A — a finalized eligibility answer that STATES a prohibition verdict
# (and is not an "unconfirmed" answer). Used to gate the
# "offer_alternative_product" follow-up: it may only be appended after an
# actual prohibited verdict, never after an unconfirmed one.
_P2_PROHIBITED_VERDICT_RE = re.compile(
    r"(?<!ว่า)ไม่สามารถนำเข้า|นำเข้าไม่ได้|(?<!ว่า)ห้ามนำเข้า|เป็นสินค้าต้องห้าม|"
    r"จัดเป็นสินค้าต้องห้าม|อยู่ในรายการสินค้าที่ห้าม|อยู่ในรายการสินค้าต้องห้าม|(?<!ไม่)ไม่รับนำเข้า")
# Any "not confirmed / uncertain / check with staff" outcome — checked
# FIRST, so a phrase like "ยังไม่มีการยืนยันว่าห้ามนำเข้า" is treated as
# unconfirmed, never as a prohibition.
_P2_UNCONFIRMED_VERDICT_RE = re.compile(
    r"ยังไม่ยืนยัน|ไม่มีการยืนยัน|ยังไม่มีการยืนยัน|ยังไม่มีข้อมูลยืนยัน|ยังไม่มีการระบุ|"
    r"ไม่มีข้อมูลยืนยัน|ยังไม่มีข้อมูล|ยังไม่แน่ชัด|ยังไม่ยืนยันแน่ชัด|ควรตรวจสอบ.{0,6}เจ้าหน้าที่|"
    r"สอบถามเจ้าหน้าที่เพื่อความ|ไม่เข้าข่ายสินค้าที่ห้าม|ไม่อยู่ในหมวด|ไม่อยู่ในรายการสินค้าต้องห้าม|"
    r"ไม่ได้อยู่ในรายการสินค้าต้องห้าม")


def _apply_p2_followup(answer_text: str, followup: Optional[Dict]) -> "tuple[str, str]":
    """Deterministically append a P2 contextual follow-up question to a
    FINALIZED answer (FAQ-direct verbatim, or post-synthesis). Returns
    (answer_text, note). Rules:
      - never a duplicate (the answer already asks an equivalent question);
      - "offer_alternative_product" only after an actual PROHIBITED verdict,
        never after an "unconfirmed" one;
      - "elicit_product_type" on synthesis is already phrased by the LLM
        (prompt instruction) so it is skipped here via the duplicate check.
    """
    if not followup or not followup.get("needed"):
        return answer_text, "not-needed"
    purpose = followup.get("purpose")
    from services.answer_planner import render_followup_question, _FOLLOWUP_PURPOSE_MARKERS
    marker = _FOLLOWUP_PURPOSE_MARKERS.get(purpose)
    if marker and marker.search(answer_text or ""):
        return answer_text, "already-served"
    if purpose == "offer_alternative_product":
        if (not _P2_PROHIBITED_VERDICT_RE.search(answer_text or "")
                or _P2_UNCONFIRMED_VERDICT_RE.search(answer_text or "")):
            return answer_text, "suppressed-verdict-not-prohibited"
    q = render_followup_question(purpose)
    if not q or q in (answer_text or ""):
        return answer_text, "already-present"
    return (answer_text or "").rstrip() + "\n\n" + q, f"appended:{purpose}"


def _restore_verbatim_scalar_values(answer_text: str, context_text: str) -> str:
    """Deterministic evidence-value fidelity (2026-09-01). Synthesis may
    freely rephrase Thai prose but must NOT mutate a trusted factual
    value. Confirmed live: the Nonthaburi phone written "091-5050-775" in
    the Context came back "091-505-0775" (digits preserved, grouping
    changed) in the aggregated answer.

    This restores phone numbers only — the one value class with a proven
    live regression and an unambiguous shape. For every phone-shaped run
    in the answer whose digit string matches a phone-shaped run in the
    Context AND whose Context form carries explicit "-" grouping, the
    answer's token is replaced with the Context's exact string. A
    bare-digit Context number (no "-") has no canonical grouping to
    enforce, so the model's grouping of it is left alone. Other value
    types (prices, rates, durations, dates) are covered by the
    STRICT_GROUNDING_RULES "preserve VERBATIM" instruction; no heuristic
    rewrite is attempted for them here."""
    if not answer_text or not context_text:
        return answer_text
    ctx_forms: Dict[str, str] = {}
    for m in _PHONE_RUN_RE.finditer(context_text):
        raw = m.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        if 9 <= len(digits) <= 10 and "-" in raw:
            ctx_forms.setdefault(digits, raw)
    if not ctx_forms:
        return answer_text

    def _sub(mm):
        tok = mm.group(0)
        digits = re.sub(r"\D", "", tok)
        canon = ctx_forms.get(digits)
        if canon and canon != tok.strip():
            return tok.replace(tok.strip(), canon)
        return tok

    return _PHONE_RUN_RE.sub(_sub, answer_text)


_LEADING_NOINFO_HEDGE_RE = re.compile(
    r"^\s*(?:ตอนนี้|ขณะนี้|ในขณะนี้)?\s*(?:ยังไม่มีข้อมูลยืนยัน|ยังไม่มีข้อมูล|ไม่มีข้อมูลยืนยัน|ไม่มีข้อมูล)"
    r"[^\n。]*?(?:ในระบบ)?\s*(?:ค่ะ|ครับ|นะคะ|นะครับ)[\s,–\-]*"
)

_PRODUCT_ANSWER_NOUN_OK_RE = re.compile(r"^[฀-๿A-Za-z0-9 ]{2,25}$")

# A GENUINE "we cannot import this" verdict — narrower than
# _P2_PROHIBITED_VERDICT_RE, which also matches a NEGATED list reference
# ("ไม่อยู่ในรายการสินค้าที่ห้ามนำเข้า"). Used only to decide whether a
# product-answer continuation should keep an eligibility verdict.
_P51_FIRM_PROHIBITED_RE = re.compile(
    r"ทางเราไม่สามารถนำเข้า|ไม่สามารถนำเข้าสินค้า|ไม่สามารถนำเข้าได้|นำเข้าไม่ได้|"
    r"ไม่รับนำเข้า|เป็นสินค้าต้องห้าม|จัดเป็นสินค้าต้องห้าม|ห้ามนำเข้าเด็ดขาด")


def _product_answer_continuation_noun(followup_type: Optional[str], history, single_elig,
                                       request_spec) -> Optional[str]:
    """P5.1 — this turn is a bare product reply to the assistant's OWN
    immediately-preceding `elicit_product_type` question (Clarification
    State Engine resolved it), so it is a CONTEXTUAL PRODUCT ANSWER, not a
    standalone eligibility question. Returns the product noun, or None.
    Reuses the existing P2 purpose marker (`_purpose_recently_served`) and
    the eligibility RequestSpec — no new dialogue engine."""
    if followup_type != "clarification-answer":
        return None
    try:
        from services.answer_planner import _purpose_recently_served
        if not _purpose_recently_served("elicit_product_type", history):
            return None
    except Exception:
        return None
    noun = ""
    if single_elig:
        noun = (single_elig[0].split(" / ")[0] or "").strip()
    if not noun:
        ents = list(getattr(request_spec, "entities", []) or [])
        noun = (ents[0] if ents else "").strip()
    if noun and _PRODUCT_ANSWER_NOUN_OK_RE.match(noun):
        return noun
    return None


# ── FIX-2.3 — product / import interest is NOT a company-fact no-info ───
# "สนใจนำเข้ารองเท้า", "อยากนำเข้าเสื้อผ้า", "จะนำเข้าอะไหล่", "กำลังสนใจ
# สั่งของจากจีน" are EARLY SALES INTEREST, not an understood company-
# policy fact question. When retrieval finds no direct evidence, the
# Answerability Gate must NOT promote such a turn to
# `unsupported_company_fact` / Human CS merely because every retrieved
# chunk is RELATED_CONTEXT. Reuses the SAME P5.1 product-answer service
# continuation the elicit_product_type reply path already uses. A company
# GUARANTEE/POLICY question (_COMPANY_GUARANTEE_Q_RE) is explicitly
# excluded — that remains TRUE no-info -> Fix-2. A prohibited product
# retrieves its own firm policy evidence and never reaches this branch.
#
# PHASE-6-SLOT-CONSUMPTION — bare "สั่ง" (order) is now its own
# alternative, not only the compound "สั่งซื้อ/สั่งของ/สั่งสินค้า" forms.
# "อยากสั่งรองเท้าจากจีน" (order SHOES, a specific goods noun straight
# after the verb, no generic "ของ/สินค้า" placeholder) previously matched
# NEITHER this regex NOR _FIX23_STRIP_RE's own recognition of "สั่ง" as a
# filler verb consistently — the two regexes disagreed on what counts as
# an ordering verb, so a product-first opener with a real product noun
# fell through to plain RAG instead of opening the IMPORT_INTEREST
# journey at all. The compound alternatives are kept (harmless, already
# subsumed by bare "สั่ง") for readability / history.
_FIX23_IMPORT_VERB_RE = re.compile(
    r"นำเข้า|ฝากสั่ง|ฝากนำเข้า|สั่งซื้อ|สั่งของ|สั่งสินค้า|สั่ง|ชิปปิ้ง|ขนของ|นำสินค้าเข้า")
_FIX23_INTEREST_RE = re.compile(
    r"สนใจ|อยาก|ต้องการ|วางแผน|เล็ง|กำลังมองหา|กำลังสนใจ|มองหา")
_FIX23_ABOUT_TO_RE = re.compile(
    r"(จะ|กำลังจะ)\s*(นำเข้า|ฝากสั่ง|ฝากนำเข้า|สั่งซื้อ|สั่งของ|สั่งสินค้า)")
# stripped to expose the bare product noun; generic markers only, never a
# product dictionary.
#
# PHASE-6 (customer master pass) — "สินค้า"/"ของ" are deliberately NOT in
# this list. Both are also legitimate word-formants inside a real Thai
# compound product noun ("ชั้นวางของ", "กล่องใส่ของ", "ของเล่น", "ชั้นเก็บ
# สินค้า") — Thai script has no spaces between words, so a regex cannot
# tell "the generic placeholder noun used as filler" from "the same two/
# three syllables that happen to end/start/sit inside a real product
# name" by position alone. Stripping them unconditionally here corrupted
# compound nouns by prefix, suffix, AND infix (e.g. "ของเล่น" -> "เล่น",
# "ชั้นวางของ" -> "ชั้นวาง"). See _product_interest_noun below for how
# they ARE still recognised as a placeholder — only when nothing else
# survives the rest of this strip.
_FIX23_STRIP_RE = re.compile(
    r"(สนใจ|อยากจะ|อยากได้|อยาก|ต้องการ|กำลังจะ|กำลังสนใจ|กำลัง|วางแผนจะ|วางแผน|เล็งจะ|เล็ง|มองหา|จะ|เอา|ได้|"
    r"นำเข้า|ฝากสั่ง|ฝากนำเข้า|สั่งซื้อ|สั่งของ|สั่งสินค้า|สั่ง|ชิปปิ้ง|ขนของ|นำสินค้าเข้า|"
    r"จาก|เว็บ|จีน|taobao|1688|tmall|เถาเป่า|"
    r"ครับ|ค่ะ|คะ|นะ|หน่อย|ผม|ฉัน|เรา|\s)+", re.IGNORECASE)
# the bare generic placeholder noun ("อยากสั่งของจากจีน", "อยากได้สินค้า")
# with NOTHING else left after the strip above -> no real product was
# named at all.
_FIX23_BARE_PLACEHOLDER_RE = re.compile(r"^(?:สินค้า|ของ)+$")


def _is_product_import_interest(question: str) -> bool:
    q = question or ""
    if _COMPANY_GUARANTEE_Q_RE.search(q):
        return False
    if _FIX23_ABOUT_TO_RE.search(q):
        return True
    return bool(_FIX23_INTEREST_RE.search(q) and _FIX23_IMPORT_VERB_RE.search(q))


def _product_interest_noun(question: str) -> Optional[str]:
    """The bare product noun in a product/import-interest declarative,
    or None (-> a generic acknowledgement). Deterministic generic-token
    strip, never a product-name lookup.

    PHASE-6-SLOT-CONSUMPTION: a quantity+unit span ("20 คู่") is a
    QUANTITY, never a product noun, regardless of where in the sentence
    it appears ("20 คู่อยากสั่งของจากจีน" vs "อยากสั่งของจากจีน 20 คู่").
    It is stripped FIRST, using the SAME count-unit vocabulary already
    used for a bare quantity ANSWER (_USER_QTY_RE / here aliased
    _IMPORT_QTY_UNIT_RE) — never a second, duplicated unit list — so this
    generalizes to every unit that vocabulary already knows. Without this
    step a leading/trailing quantity either got returned AS the product
    (when nothing else was left after the generic filler strip) or got
    glued onto a real product noun ("30 ชิ้น...ชั้นวางของ" ->
    "30ชิ้นชั้นวาง"), corrupting both slots at once. A shipping-method
    mention ("ส่งเรือ") is stripped the same way and for the same reason
    ("รองเท้า...ส่งเรือ" -> "รองเท้าส่งเรือ" glued).

    PHASE-6 (customer master pass) — a real compound product noun must
    survive intact even when it contains "สินค้า"/"ของ" as a prefix,
    suffix, or infix ("ชั้นวางของ", "กล่องใส่ของ", "ของเล่น", "ชั้นเก็บ
    สินค้า"): _FIX23_STRIP_RE no longer touches those two tokens at all,
    so they are only ever treated as the GENERIC placeholder noun (no
    real product named) when the ENTIRE remnant is bare "สินค้า"/"ของ"
    with nothing else left — never when they are part of a longer
    surviving span."""
    q = _IMPORT_QTY_UNIT_RE.sub(" ", question or "")
    q = _IMPORT_METHOD_WORD_RE.sub(" ", q)
    remnant = _FIX23_STRIP_RE.sub("", q).strip()
    if remnant and _FIX23_BARE_PLACEHOLDER_RE.match(remnant):
        return None
    if remnant and _PRODUCT_ANSWER_NOUN_OK_RE.match(remnant):
        return remnant
    return None


def _product_answer_service_continuation(noun: str, *, lead_stage: Optional[str],
                                          sentiment_status: Optional[str], history,
                                          transport_known: bool) -> "tuple[str, str]":
    """Deterministic Branch-B reply: acknowledge the product, then ONE
    useful Shipify service next-step. Never invents an eligibility verdict;
    road/sea are trusted Shipify service facts. NEGATIVE -> acknowledge
    only (P5 suppression). HOT / known-transport / prohibited-category
    noun / already-asked -> no transport question."""
    stage = (lead_stage or "").upper()
    if (sentiment_status or "").upper() == "NEGATIVE":
        return f"รับทราบค่ะ เป็น{noun}นะคะ 😊 หากต้องการให้ช่วยตรวจสอบเพิ่มเติม แจ้งได้เลยค่ะ", "negative-ack-only"
    ack = (f"รับทราบค่ะ เป็น{noun}นะคะ 😊 หากต้องการนำเข้ากับ Shipify "
           f"มีบริการขนส่งทั้งทางรถและทางเรือค่ะ")
    from services.answer_planner import render_followup_question, _purpose_recently_served
    if (stage != "HOT" and not transport_known and noun not in _PROHIBITED_CATEGORY_WORDS
            and not _purpose_recently_served("elicit_transport_mode", history)):
        q = render_followup_question("elicit_transport_mode")
        return ack + "\n\n" + q, "appended:elicit_transport_mode"
    return ack, "ack-service-only"


def _strip_contradictory_noinfo_hedge(answer_text: str, answerability: str) -> str:
    """Deterministic false-hedge suppression (2026-09-01). Confirmed live:
    for the China-warehouse address/contact route, synthesis sometimes
    prepended "ตอนนี้ยังไม่มีข้อมูล…ในระบบค่ะ" and then immediately gave
    the trusted website/menu route — internally contradictory. When
    retrieval judged the turn answerable (answerability != no_information)
    and a substantive answer follows the hedge, drop just the leading
    hedge sentence. A genuine no-information turn (answerability ==
    no_information, or nothing of substance after the hedge) is left
    untouched."""
    if not answer_text or answerability == "no_information":
        return answer_text
    m = _LEADING_NOINFO_HEDGE_RE.match(answer_text)
    if not m:
        return answer_text
    rest = answer_text[m.end():].strip()
    return rest if len(rest) >= 15 else answer_text


# SEM-GEN-1 — retrieved-entity contamination guard. A retrieved chunk may
# supply FACTS; it must NOT introduce the customer's UNSTATED target. For
# "สั่งเยอะได้ไหม" the retrieved FAQ "สั่งแบตเตอรี่จำนวนเยอะได้ไหม" led
# synthesis to volunteer "…แต่ถ้าเป็นแบตเตอรี่ ทางเราไม่รับ…" — a caveat
# about a product the customer never mentioned. Deterministic, generic
# (the caveat SHAPE + the existing prohibited-category word list, never a
# battery-specific rule): drop a hedge-introduced prohibited-product
# caveat clause when its product/category word appears in NEITHER the
# current message NOR the recent user turns NOR the active frame.
_UNSOLICITED_CAVEAT_SPLIT_RE = re.compile(
    r"(?=(?:แต่ถ้า|อย่างไรก็ตาม\s*หากเป็น|อย่างไรก็ดี\s*หากเป็น|ทั้งนี้\s*หากเป็น|ยกเว้น|เว้นแต่)\S)")
_CAVEAT_PROHIBITION_RE = re.compile(r"ไม่รับ|ไม่สามารถ|ห้าม|ต้องห้าม|งดรับ")


def _strip_unsolicited_prohibited_caveat(answer_text: str, allowed_terms: str) -> str:
    if not answer_text or "แบตเตอรี่" not in answer_text and not any(
            w in answer_text for w in _PROHIBITED_CATEGORY_WORDS):
        return answer_text
    allowed = allowed_terms or ""
    parts = _UNSOLICITED_CAVEAT_SPLIT_RE.split(answer_text)
    if len(parts) < 2:
        return answer_text
    kept = [parts[0]]
    for seg in parts[1:]:
        cat = next((w for w in _PROHIBITED_CATEGORY_WORDS if w in seg), None)
        if cat and _CAVEAT_PROHIBITION_RE.search(seg) and cat not in allowed:
            continue  # drop this volunteered, off-target caveat
        kept.append(seg)
    out = "".join(kept).strip()
    return out if len(out) >= 15 else answer_text


# CUSTOMER-INVOICE-1 (2026-09-03) — a "can you issue a goods invoice?"
# yes/no policy question. Distinct from the download how-to
# ("โหลดใบกำกับยังไง"), which keeps its own FAQ answer.
_INVOICE_ISSUANCE_Q_RE = re.compile(
    r"(?:ออก|ขอ|มี|รับ|ได้)[^\n]{0,14}ใบกำกับ"
    r"|ใบกำกับ[^\n]{0,14}(?:ออก|ขอ|ได้)"
    r"|ไม่สามารถ[^\n]{0,10}ใบกำกับ")
_INVOICE_DOWNLOAD_RE = re.compile(
    r"โหลด|ดาวน์โหลด|download|ยังไง|อย่างไร|วิธี|ขั้นตอน|กดตรงไหน|เมนูไหน|หาได้ที่ไหน")
# Composed ONLY from clauses present verbatim in the trusted invoice FAQ
# rows ff288877 / 99390831 / a2618c6d (see the branch comment). No
# invented condition.
_INVOICE_ISSUANCE_ANSWER = (
    "ทางเราสามารถออกใบกำกับค่าสินค้าและใบเสร็จค่าขนส่งให้ได้ค่ะ ตามเงื่อนไขของบิลและวิธีชำระเงิน "
    "หากต้องการใบกำกับภาษี รบกวนแจ้งข้อมูลผู้เสียภาษีและเลขบิลให้เจ้าหน้าที่ตรวจสอบเงื่อนไขก่อนชำระเงินนะคะ "
    "ทั้งนี้ การชำระค่าสินค้าด้วยบัตรเครดิตจะไม่สามารถออกใบกำกับได้ตามข้อมูลปัจจุบันค่ะ")


def _is_invoice_issuance_question(question: str) -> bool:
    q = question or ""
    return bool(_INVOICE_ISSUANCE_Q_RE.search(q) and not _INVOICE_DOWNLOAD_RE.search(q))


def _invoice_issuance_branch_applies(question, actionable_intent, interpretation=None) -> bool:
    """INVOICE-REGRESSION-1 — the deterministic trusted-invoice branch
    applies when the turn is an invoice ISSUANCE/policy question (not the
    download how-to). Primary signal is the central SEMANTIC-FIRST-1
    INVOICE family (so every paraphrase interpret() resolves — "tax
    invoice", "ใบเสร็จค่าขนส่ง", "e-tax invoice" — is covered); the
    literal-phrase recogniser is kept as the signal for callers that pass
    no interpretation (Playground / benchmark)."""
    if actionable_intent != "invoice_policy":
        return False
    if _INVOICE_DOWNLOAD_RE.search(question or ""):
        return False
    if _is_invoice_issuance_question(question):
        return True
    # the central INVOICE family opens the branch ONLY when the
    # deterministic compositional tier resolved it (it requires an actual
    # invoice noun — ใบกำกับ / ใบเสร็จ / tax invoice). An LLM family GUESS
    # must not drive a deterministic trusted-answer branch: e.g.
    # "ชำระบัตรเครดิตได้ไหม" / "บิลขนส่งชำระได้เลยไหม" are payment-policy
    # questions the resolver sometimes labels INVOICE by association.
    return (getattr(interpretation, "intent_family", None) == "INVOICE"
            and getattr(interpretation, "source", None) == "deterministic")


# Company/Operational Topic Guard (Hybrid RAG + General AI Chat,
# 2026-08-27; broadened 2026-08-27 same day — Final Hybrid Stabilization;
# broadened again 2026-08-27 same day — Semantic RAG Retrieval fix) — a
# CLOSED, explicit deny-list, directly reusing the exact topic taxonomy
# this feature's own spec lists as never-to-be-guessed by a general LLM
# (company policy, shipping rate/duration, refund/warranty, order/shipment
# status, customer/wallet/coupon data, warehouse status, prices,
# promotions), plus the same self-reference markers ("ของผม" etc) already
# established elsewhere in this codebase for "this is about MY OWN
# account" detection, plus a few precise, low-collision domain terms
# (CBM, ทางรถ/ทางเรือ shipping modes, ขั้นต่ำ) added once this check
# started running BEFORE consulting RAG evidence quality at all (see the
# General Chat Fallback branch below) — without them, a genuinely
# company-specific question that doesn't happen to name "บริษัท" (e.g.
# "CBM คืออะไร", "ทางรถกี่วัน") would incorrectly skip its own real,
# well-grounded company answer in favor of general chat. Used to decide
# whether a message should even be CONSIDERED against RAG evidence at all
# (this matches — stays on the existing company/RAG path, unchanged) or
# is safe to answer as ordinary general conversation instead (this does
# NOT match). Deliberately conservative: matching this regex is the ONLY
# way to stay on the company path is to match it, so anything ambiguous
# or genuinely company-adjacent defaults to the existing safe/RAG
# behavior, never the other way around.
#
# Semantic RAG Retrieval fix (2026-08-27) — "สั่ง"/"ซื้อ" added: confirmed
# live that a natural order/purchase-intent message ("ผมต้องการสั่งซื้อ
# สินค้าจากจีนครับ", "อยากให้ช่วยสั่งของจากจีน", "ผมสั่งของกับร้านจีนเองแล้ว
# ต้องทำอะไรต่อ") already retrieves a genuinely relevant, high-confidence
# RAG answer (rag/confidence.py's Answerability Gate already correctly
# returns direct_answer/0.9 for these) but never REACHED that answer
# because this gate — checked BEFORE consulting RAG evidence at all — ran
# first and had no order/purchase vocabulary in its list, sending the
# message to General Chat instead (which then either falsely claimed "no
# information exists" despite a real answer being available, or, worse,
# fabricated generic international-shipping/customs advice). "สั่ง" (to
# order, with the ั vowel — confirmed distinct from "ส่ง"/"ขนส่ง", "to
# send"/"shipping", which do NOT contain this substring) and "ซื้อ" (to
# buy) are safe, low-collision bare additions: neither appears in any of
# this task's required General Chat test messages (verified: "จีนอยู่ทวีป
# อะไร", "ขนส่งระหว่างประเทศคืออะไร", "ช่วยคิดข้อความขายของ", etc. contain
# neither word).
_COMPANY_OPERATIONAL_TOPIC_RE = re.compile(
    r"shipify|บริษัท|ของผม|ของฉัน|ของดิฉัน|"
    r"นโยบาย|เงื่อนไข|ประกัน|เคลม|ค่าส่ง|ค่าขนส่ง|เรท|ราคา|"
    r"คืนเงิน|คืนสินค้า|รับประกัน|การชำระเงิน|ยกเลิก|"
    r"ออเดอร์|คำสั่งซื้อ|order|"
    r"shipment|พัสดุ|บิลขนส่ง|ติดตาม|tracking|"
    r"ข้อมูลลูกค้า|wallet|กระเป๋าเงิน|ยอดเงิน|"
    r"คูปอง|โกดัง|โปรโมชั่น|โปรโมชัน|บริการ|"
    r"cbm|ทางรถ|ทางเรือ|ระยะเวลาขนส่ง|ขั้นต่ำ|"
    r"นำเข้า|ฝากสั่ง|ฝากโอน|"
    r"สั่ง|ซื้อ|ที่อยู่|"
    # Missing Company-Topic Keywords fix (2026-08-31) — confirmed live via
    # a customer test-question spreadsheet: several genuinely company-
    # specific questions (does the company offer wooden crating, is a
    # named item on the prohibited-goods list, is air freight available,
    # can a tax invoice be issued, is doorstep delivery offered) carried
    # NONE of this list's existing terms, so they fell through to General
    # Chat Fallback (empty Context) instead of the real, correctly-
    # retrieved RAG answer that already existed for every one of them —
    # the LLM then correctly (per its own General Chat instructions) said
    # "no information," which looked identical to a genuine knowledge-base
    # gap but was actually a routing miss. Same low-collision, specific-
    # term convention as every prior addition to this list.
    r"ตีลังไม้|แบตเตอรี่|ใบกำกับ|จัดส่ง|ขนส่ง|เครื่องบิน|"
    # Final Systemic Routing Fix (2026-08-28) — "ที่อยู่" (shipping
    # address) was missing from this gate despite RequestShippingAddress
    # Change being a real, configured Business Action in this exact
    # registry — confirmed live via the Locked Acceptance Matrix:
    # "เปลี่ยนที่อยู่ในระบบยังไง" (a genuine Shipify how-to question) fell
    # through to General Chat Fallback purely because the raw text
    # matched none of this list's existing terms, exactly the same class
    # of gap the "form e" fix above already closed once.

    # P0 Final Blocker Closure (2026-08-28) — "Form E" is a term named
    # ONLY inside RAG-035's own trusted answer (a customs/tax FAQ record),
    # with no plausible general-chat meaning outside that exact context.
    # Confirmed live: "Form E คืออะไร" retrieved RAG-035 with a PERFECT
    # (1.0) confidence score, yet this text-only gate — checked BEFORE
    # consulting retrieval quality at all — still diverted it to General
    # Chat (empty context) purely because the raw question text matched
    # none of this list's existing terms, letting the LLM answer entirely
    # from its own real-world knowledge of ASEAN-China trade documents
    # instead of the one genuinely relevant, retrieved trusted chunk.
    r"form e|"
    # CUSTOMER-RAG-1 (2026-09-03) — pickup / receiving-point vocabulary.
    # Confirmed live: "สามารถรับสินค้าได้ที่ไหนหรอคะ" retrieved the trusted
    # "ขอที่อยู่โกดังหน่อย" FAQ (2 Thai warehouses + maps + phone) at
    # rerank 0.84 / raw_vector_rank 1, yet this text-only gate matched
    # none of its terms (only "โกดัง" was present, which the customer's
    # phrasing omits), so it was diverted to General Chat Fallback and
    # answered "no info about branch pickup location". Same closed, low-
    # collision domain-term convention as every prior addition; "ของผม…
    # ไปรับ" still routes PRIVATE upstream via the existing self-reference
    # check, and "รับสินค้าเองได้ไหม" stays on the RAG path where the
    # trusted chunk itself states the self-pickup option.
    r"รับสินค้า|จุดรับ|จุดส่ง|จุดรับของ|มารับสินค้า|เข้ารับสินค้า|ไปรับสินค้า|"
    r"คลังสินค้า|คลังไทย|สาขา",
    re.IGNORECASE,
)

# China-Sourced Action Guard (Semantic RAG Retrieval fix, 2026-08-27) —
# China named as the SOURCE of a shipping/fetch action ("ส่งของจากจีนมา
# ไทยทำยังไง", "สินค้าจีนส่งมาไทยใช้เวลากี่วัน", "อยากเอาของจากจีนเข้ามาไทย")
# is genuine Shipify service intent even when none of the topic words
# above are present. Deliberately NOT a bare "จีน" check: confirmed live
# that bare "จีน" alone is an unsafe signal on its own — rag/confidence.py's
# Answerability Gate already gives "จีนอยู่ทวีปอะไร" a literal_keyword_score
# up to 1.0 and answerability="direct_answer" purely from coincidental
# word overlap (จีน + อยู่) against the China-warehouse-address FAQ, which
# is exactly the spurious-match failure mode this whole topic gate exists
# to reject — so "จีน" must never be added to _COMPANY_OPERATIONAL_TOPIC_RE
# as a bare word. Requiring collocation with a real shipping/fetch verb
# (ส่ง/เอา) keeps a pure geography/general-knowledge mention of China
# ("จีนอยู่ทวีปอะไร", "จีนมีเมืองอะไรบ้าง" — neither has a nearby ส่ง/เอา)
# on the General Chat path, verified against every required General Chat
# test message for this task.
_CHINA_SOURCED_ACTION_RE = re.compile(r"(ส่ง|เอา).{0,20}จีน|จีน.{0,20}(ส่ง|เอา)")

# New-Topic Question Marker (Usage Lock fix, 2026-08-28) — a documented
# SUBSET of services/hybrid_question_classifier.py's own _QUESTION_MARKER_RE
# word list (never re-widened, never a new vocabulary), keeping only the
# WH-question words that ask FOR a specific new fact about some entity
# ("จีนอยู่ทวีปอะไร", "ทางรถกี่วัน") — genuine new-topic signals. Deliberately
# EXCLUDES the polar/yes-no markers (หรือไม่|หรือเปล่า|ไหม): in Thai these
# routinely function as a POLITE REQUEST form ("...ช่วยอธิบายง่ายๆได้ไหม" =
# "could you explain simply?"), not a fact-seeking question about a new
# subject — confirmed live: excluding them was necessary for "ผมไม่ค่อย
# เข้าใจ ช่วยอธิบายง่ายๆได้ไหม" (a genuine continuation request) to be
# correctly distinguished from "จีนอยู่ทวีปอะไร" (a genuine new topic),
# since both would otherwise look identical to a single, undifferentiated
# "has a question marker" check.
_NEW_TOPIC_QUESTION_MARKER_RE = re.compile(r"(ยังไง|อย่างไร|อะไร|ทำไม|เท่าไหร่|เท่าไร|กี่|ที่ไหน|แค่ไหน|เมื่อไหร่)")

# ERP-Clarification-In-Progress Guard (Final Two Blockers, 2026-08-28) —
# confirmed live: "ช่วยคิดข้อความขายสินค้านี้ให้หน่อย" (a General Chat
# creative-writing request, no self-contained WH-question marker) sent
# right after an ERP Shipment lookup asked for CustCode ("กรุณาแจ้ง
# รหัสลูกค้าค่ะ") was wrongly captured by THIS SAME continuity exception,
# because the most recent CUSTOMER turn ("...Shipment...") happened to
# match _COMPANY_OPERATIONAL_TOPIC_RE — even though the conversation's
# actual active thread had already moved from RAG to an (independently
# escaped, per services/decision_engine.py's own continuation-escape
# fix) ERP clarification exchange, not a RAG answer. Reuses the EXACT
# literal template services/decision_engine.py::_generate_parameter_
# question already renders for every single ERP follow-up question
# across the whole codebase ("กรุณาแจ้ง{display}ค่ะ") — never a new,
# invented pattern — as the signal that the conversation's immediately
# preceding turn is ERP-shaped, not RAG-shaped.
_ERP_CLARIFICATION_QUESTION_RE = re.compile(r"^กรุณาแจ้ง.*ค่ะ$")


def _is_ambiguous_rag_continuity_followup(question: str, history: Optional[List[Dict]]) -> bool:
    """Usage Lock root-cause fix (2026-08-28) — confirmed live: a plain
    declarative follow-up inside an active Shipify-informational
    conversation (e.g. "ผมมีงบประมาณประมาณ 10,000 บาทครับ" right after
    "ฝากสั่งกับฝากนำเข้าต่างกันยังไง") carries no _COMPANY_OPERATIONAL_
    TOPIC_RE/_CHINA_SOURCED_ACTION_RE term of its own, so it fell straight
    into General Chat Fallback and lost all connection to the ongoing
    conversation — the exact "context is not used naturally" complaint.

    Narrow, additive exception: keeps the turn on the company/RAG path
    (with history, letting the existing grounding rules decide what — if
    anything — can be said) ONLY when ALL of:
      (a) there IS prior conversation history (never the first turn);
      (b) the most recent CUSTOMER turn in that history was itself on a
          company topic (the SAME two regexes above, reused not
          duplicated) — proving this really is a continuation, not an
          assumption;
      (c) the CURRENT message carries no NEW-TOPIC WH-question marker of
          its own (_NEW_TOPIC_QUESTION_MARKER_RE) — a self-contained new
          question (e.g. "จีนอยู่ทวีปอะไร") is deliberately NOT covered by
          this exception and must still be evaluated independently, so
          General Chat Fallback still correctly wins for it even
          immediately after the same company conversation;
      (d) the immediately preceding turn overall (history[-1], typically
          the assistant's own last reply) is NOT itself an ERP
          clarification question in progress (_ERP_CLARIFICATION_
          QUESTION_RE) — the conversation's active thread has already
          moved on to an ERP exchange by then, so a RAG-topic mention
          from several turns earlier must not resurrect it.

    Never phrase-specific — no keyword list of its own beyond the shared,
    documented subset above — works for any declarative/request-shaped
    follow-up that merely supplies context (budget, quantity, shipping
    preference, experience level, a request to simplify), never for a
    new self-contained question."""
    if not history:
        return False
    if _NEW_TOPIC_QUESTION_MARKER_RE.search(question or ""):
        return False
    if _ERP_CLARIFICATION_QUESTION_RE.match((history[-1].get("content") or "").strip()):
        return False
    last_customer_turn = next(
        (t.get("content") or "" for t in reversed(history) if t.get("role") == "user"), "")
    return bool(_COMPANY_OPERATIONAL_TOPIC_RE.search(last_customer_turn)
                or _CHINA_SOURCED_ACTION_RE.search(last_customer_turn))


def _faq_row_overspecified_for_transport(faq_text: str, raw_question: str) -> bool:
    """True when a verbatim FAQ return would over-answer on transport mode:
    the customer's question names exactly ONE mode (requested_transport_
    modes — the shared transport SoT, negation-aware) but the matched FAQ
    row's answer covers BOTH road and sea. Happens when a mode-specific
    question near-matches a generic rate/duration FAQ ("ทางรถเรทเท่าไหร่"
    -> "เรทเท่าไหร่คะ"). Such a turn must go through synthesis so
    answer_plan's own transport narrowing applies; a zero-mode ("generic
    rate") or already-two-mode question is NOT over-specified and still
    returns verbatim."""
    modes = requested_transport_modes(raw_question or "")
    if len(modes) != 1:
        return False
    if not (("ทางรถ" in faq_text or "ทางบก" in faq_text) and "ทางเรือ" in faq_text):
        return False
    # Only narrow when the FAQ row actually carries a per-mode value for
    # the ONE mode the customer asked about (its canonical term appears in
    # the answer, e.g. "ทางรถ 35 บาท/กก / ทางเรือ 19 …"). An availability
    # answer that merely lists the modes we DO run in order to say another
    # mode is unavailable ("มีขนส่งทางเครื่องบินไหม" -> "…ทางรถและทางเรือ
    # เท่านั้น ยังไม่มี…เครื่องบิน…") is a complete answer that must be
    # returned verbatim, follow-up question and all.
    return modes[0] in faq_text


def _has_direct_structured_evidence(chunks: List[Dict]) -> bool:
    """True when the FINAL evidence-classified chunk list (rag/
    hybrid_scoring.py's `classification`, already computed by the time
    step 4a runs) contains at least one direct/structured answer — e.g. a
    log_event_time chunk with a real timestamp+startup match, or a
    deterministic calculation. Escalation must never override an answer
    that already has this kind of evidence, no matter how the confidence
    threshold happens to be configured."""
    return any(c.get("classification") in ("direct_evidence", "structured_deterministic") for c in chunks)


@dataclass
class Stage:
    name: str
    status: str          # success | failed | skipped
    duration_ms: float
    detail: str = ""


@dataclass
class PlaygroundResult:
    answer: str
    chunks: List[Dict]
    context: str
    prompt: "object"           # services.prompt_builder.BuiltPrompt
    policy: "object"           # services.policy_engine.PolicyResult
    stages: List[Stage]
    model: str
    embedding_model: str
    temperature: float
    input_tokens: int
    output_tokens: int
    latency_ms: float
    estimated_cost_usd: float
    confidence: float
    confidence_label: str      # High | Medium | Low
    answerability: str          # direct_answer | partial_answer | no_information (rag/confidence.py)
    raw_vector_similarity: Optional[float]   # top chunk's raw cosine score — shown SEPARATELY from confidence
    hybrid_retrieval_score: Optional[float]  # top chunk's hybrid_score
    embedding_provider: str
    embedding_dimensions: int
    embedding_version: str
    excluded_candidates: List[Dict]  # dropped weak_semantic/irrelevant chunks, admin-debug only (Part 20)
    query_expansion: Dict  # {original_query, expanded_queries, detected_language} — Explainability tab
    services_used: List[Dict]  # [{"name": "RAGService", "status": "success"}, ...]
    policy_set_name: str  # AI Policies (services/policy_studio_service.py) — Explainability tab
    # Phase 2 (AI Playground Intelligence Pipeline) — additive, never
    # changes `chunks`/`answer`/citations, only reported for Explainability.
    context_builder_summary: Dict   # rag/context_builder.py's dedup/merge/compress counts
    retrieval_confidence: float     # rag/retrieval_confidence.py — 0.0-1.0
    retrieval_confidence_components: Dict
    # Human-like Multi-Message Replies (services/message_segmenter.py) —
    # additive: `answer` above remains the full canonical string for
    # citations/logs/analytics/Copy Answer/backward compatibility.
    message_parts: List[str]
    message_delay_ms: List[int]
    attachment_order: List[str]
    reply_mode_used: str
    segmentation_applied: bool
    message_count: int
    segment_reasons: List[str]
    boundary_types: List[str]   # e.g. ["paragraph_break"], ["bullet_block"], ["prose_marker"] — Explainability tab
    # Conversation Intelligence (Unified Intent Classification + Answer
    # Planner + Attachment Planner) — additive: none of these change
    # `answer`/`chunks`/`confidence`/citations/message_parts/attachment
    # metadata that already existed; Copy Answer and every existing API
    # field are unaffected.
    broad_intent: str
    actionable_intent: str
    intent_confidence: float
    requested_attributes: List[str]
    intent_entities: Dict
    answer_plan: Dict
    attachment_plan: Dict
    selected_attachments: List[Dict]
    # Conversation Intelligence Phase 1 (rag/conversation_state.py) —
    # additive: normalized topic bucket + explicit transition decision,
    # displayed in its own Explainability panel (Part 9), before
    # Canonical Query Rewrite (Part 8). Never changes retrieval/answer.
    conversation_state: Dict
    # Information Collection Engine / Slot Filling Engine (services/
    # slot_filling_engine.py) — additive: None when no ERP-backed intent
    # was detected this turn; otherwise the full collection state
    # (required/collected/missing slots, follow-up question, retry
    # count, escalation decision) for Developer Mode.
    slot_filling_state: Optional[Dict]
    # Message Segmentation data contract (Part 6, P0 2026-07-21) —
    # `messages` is the SOURCE OF TRUTH for any channel supporting
    # multiple messages (Playground, LINE OA); `answer`/`reply_text`
    # remain only for backward compatibility, exports, logs, and
    # single-message channels. Built from the SAME message_parts above —
    # never a second, independently-computed split.
    messages: List[Dict]       # [{"type": "text", "content": str}, ...]
    reply_text: str            # alias of `answer` — explicit name per the new contract
    # Knowledge Gap Handling (Part 10) — Developer-Mode-only signal, never
    # shown to the customer, never blocks or alters the answer itself.
    # None unless actionable_intent is company_overview/company_summary
    # AND no chunk carries strong (exact-FAQ or profile-heading) company-
    # profile evidence.
    company_profile_warning: Optional[str]
    # Root Change 2 (Final Systemic Routing Fix, 2026-08-28) — the
    # forensic routing audit's Root Cause #2: the General Chat Fallback
    # branch below (no company/china/urgency/complaint evidence) answers
    # with zero retrieved-chunk grounding, using general LLM knowledge,
    # but was previously reported to every caller identically to a
    # grounded Shipify-KB answer (routing_type=="RAG"). Surfaced here —
    # additive only, never changes `answer`/`chunks`/anything else this
    # dataclass already returns — so services/decision_engine.py can
    # report a genuinely distinct "GENERAL" route instead of masking it
    # as "RAG". Defaults False so this field is a pure addition for every
    # other branch (slot-filling/no-information/normal-grounded-answer).
    general_chat_used: bool = False
    # Customer UAT Fix 2 (2026-09-02) — True ONLY when this turn is a
    # genuine COMPANY/Shipify fact question that the deterministic
    # Answerability Gate / P7.1 branch answered with the grounded
    # "no trusted information" wording (zero reliable evidence, no LLM
    # call, no invented yes/no). services/decision_engine.py reads this
    # as a structured "NEED HUMAN FOLLOW-UP" signal and routes the turn
    # through the SAME existing Human CS handoff mechanism. Never set for
    # a normal grounded answer, a clarification-needed turn, a product-
    # answer continuation, or a General-Chat-Fallback reply.
    unsupported_company_fact: bool = False


def run_playground_turn(
    question: str,
    *,
    template_id: Optional[str] = None,
    top_k: int = 3,
    temperature: float = 0.3,
    max_tokens: int = 500,
    history: Optional[List[Dict]] = None,
    lead_stage: Optional[str] = None,
    sentiment_status: Optional[str] = None,
    interpretation: Optional[object] = None,
) -> PlaygroundResult:
    stages: List[Stage] = []
    services_used: List[Dict] = []

    # -1. Normalize -> Spell Correction (rag/spell_correction.py) —
    #     deterministic, pure Python, no LLM/API call. Runs BEFORE
    #     Follow-up Resolution / Intent Detection / Query Rewrite /
    #     Synonym Expansion / FAQ Exact Match / Hybrid Retrieval, per the
    #     required pipeline order, so a typo never has to survive all the
    #     way to embedding/hybrid scoring to get a chance at being fixed.
    #     Protected entities (tracking numbers, URLs, emails, phone
    #     numbers, product codes, known English abbreviations, numbers/
    #     prices — see rag/spell_correction.py's _PROTECTED_PATTERNS) are
    #     never touched. The raw `question` is preserved as-is for
    #     citation selection/logging further down; every retrieval/prompt
    #     step from here on uses the (possibly) corrected text.
    t0 = time.time()
    normalized_question = normalize_query(question)
    # Entity Introduction Policy (Part 2.B, P0 2026-07-21) — a correction
    # may only introduce an entity that's either already in the current
    # wording OR legitimately carried from the previous USER turn (never
    # from the previous ASSISTANT answer). accumulate_entities() only
    # ever inspects role=="user" turns (rag/query_resolution.py), so this
    # is safe to compute before Follow-up/Entity Resolution proper runs.
    from rag.query_resolution import accumulate_entities
    carried_entities = accumulate_entities(history)
    spell_result = correct_query(normalized_question, carried_entities=carried_entities)
    corrected_question = spell_result["corrected_query"]
    stages.append(Stage("Spell Correction", "success", (time.time() - t0) * 1000,
                         f"{len(spell_result['corrections'])} correction(s): "
                         + ", ".join(f"{c['from']}->{c['to']}" for c in spell_result["corrections"])
                         if spell_result["corrections"] else "no corrections needed"))

    # 0-. Bare product-list continuation of an import-eligibility thread
    #     ("น้ำปลา นำเข้าได้ไหม" -> "น้ำเปล่า ละ น้ำมัน น้ำมันงา"): rebuild
    #     the full eligibility question so decomposition + multi-target
    #     retrieval + completeness treat every listed product. Fires ONLY
    #     when the immediately preceding USER turn was itself an eligibility
    #     question — a bare list with no such context is left untouched.
    from rag.query_resolution import reconstruct_product_list_continuation
    _list_cont = reconstruct_product_list_continuation(corrected_question, history)
    if _list_cont and _list_cont != corrected_question:
        stages.append(Stage("List Continuation", "success", 0.0,
                             f"{corrected_question!r} -> {_list_cont!r} (product list inherits eligibility goal)"))
        corrected_question = _list_cont


    # 0. Follow-up / Entity Resolution — Conversation Resolver 2.0 (rag/
    #    query_resolution.py::resolve_conversation()). Deterministic, no
    #    LLM call. Uses ONLY prior USER turns from `history` (never a
    #    prior ASSISTANT answer) — tracks lightweight conversation
    #    entities (topic/location/transport/attribute) across turns so a
    #    much wider range of short follow-ups resolve into a standalone
    #    question ("แล้วรถล่ะ" -> "ขอเรททางรถ", "กี่วัน" -> "ขอทราบระยะเวลา
    #    ขนส่งทางรถ", "แล้วจีน" -> "ขอที่อยู่โกดังจีน"), not just the original
    #    narrow "แล้ว...ล่ะ" wrapper shape. A question with no follow-up
    #    marker at all is returned completely unchanged — never inherits
    #    unrelated entities from an earlier, different topic.
    t0 = time.time()
    conversation = resolve_conversation(corrected_question, history)
    resolved_question = conversation["resolved_question"]
    stages.append(Stage("Query Resolution", "success", (time.time() - t0) * 1000,
                         f"resolved to: {resolved_question!r} ({conversation['followup_type']}, "
                         f"conf={conversation['confidence']:.2f})" if resolved_question != corrected_question
                         else "not a follow-up — used as-is"))

    # 0a1. Active Slot-Filling Flow (P0, 2026-07-22, rag/slot_filling_flow.py)
    #      — checked here, BEFORE intent classification/retrieval even
    #      run, so a bare reply like "4*6" supplying dimensions for an
    #      active shipping-cost-calculation flow is never misclassified
    #      as a standalone arithmetic question. Returns None (no-op) for
    #      every other turn: no active flow, cancellation, an explicit
    #      new topic, or an explicit math request — in every such case
    #      the rest of the pipeline runs completely unchanged.
    from rag.slot_filling_flow import resolve_slot_filling_turn, resolve_bare_math_expression
    dimension_slot_result = resolve_slot_filling_turn(corrected_question, history)
    # Data entry vs math — a bare arithmetic expression with NO active
    # slot-filling flow is still evaluated directly as math (Part "Data
    # Entry vs Math", test "explicit math still works when no active
    # flow exists") — never a general eval(), see rag/slot_filling_flow.py.
    bare_math_result = None if dimension_slot_result else resolve_bare_math_expression(corrected_question, history)
    if dimension_slot_result:
        stages.append(Stage("Active Slot-Filling Flow", "success", 0.0,
                             f"shipping_cost_calculation — captured={dimension_slot_result['captured_slots']}, "
                             f"missing={dimension_slot_result['missing_slots']}"))
        if dimension_slot_result["flow_complete"]:
            # All required slots collected — hand off to the EXISTING
            # deterministic Excel Calculation Engine (rag/calculator.py)
            # by synthesizing a complete analytical question from the
            # captured values, rather than reimplementing shipping-cost
            # math here. Never invents a value: every number came from
            # what the customer actually typed across this flow's turns.
            cs = dimension_slot_result["captured_slots"]

            def _fmt(v):
                return str(int(v)) if float(v).is_integer() else str(v)
            dims_str = "x".join(_fmt(v) for v in cs["dimension_values"])
            resolved_question = (f"คำนวณค่าขนส่งขนาด {dims_str} {cs['dimension_unit']} "
                                  f"น้ำหนัก {_fmt(cs['weight'])} {cs['weight_unit']}")
            corrected_question = resolved_question

    # 0a2. Conversation State Engine (Conversation Intelligence Phase 1,
    #      rag/conversation_state.py) — deterministic, no LLM call. Wraps
    #      Conversation Resolver 2.0 (never re-implements entity
    #      extraction) and adds a normalized topic BUCKET (warehouse/
    #      shipping/payment/...) plus an explicit topic-transition
    #      decision, so a genuine topic switch (e.g. "โกดังจีน" ->
    #      "โปรโมชั่นมีอะไร") never leaks the old topic's entities into
    #      Canonical Query Rewrite below.
    t0 = time.time()
    from rag.conversation_state import build_conversation_state
    conversation_state = build_conversation_state(corrected_question, history)
    stages.append(Stage("Conversation State", "success", (time.time() - t0) * 1000,
                         f"topic={conversation_state['topic']}, subtopic={conversation_state['subtopic']}, "
                         f"transition={conversation_state['transition']}"))

    # 0b. Canonical Query Rewrite (rag/canonical_query.py) — deterministic,
    #     no LLM call. Runs AFTER Follow-up/Entity Resolution and BEFORE
    #     Synonym Expansion/FAQ Exact Match/Hybrid Retrieval (all inside
    #     rag/searcher.py::search()). Standardizes a phrasing that's
    #     already spell-corrected/resolved but still not a clear
    #     standalone search query (e.g. "ส่งแผนที่ให้หน่อย" names no
    #     subject) — using the SAME entity extraction Conversation
    #     Resolver 2.0 uses, plus whichever entities it just carried
    #     forward. Only rewrites when confident; otherwise the resolved
    #     question is used as-is, unchanged.
    t0 = time.time()
    canonical_entities = {**conversation.get("entities_carried", {})}
    if conversation.get("prev_topic") and "topic" not in canonical_entities:
        canonical_entities["topic"] = conversation["prev_topic"]
    if conversation_state["transition"] == "switch_topic":
        # A genuine topic switch (Conversation State Engine, above) must
        # never let the PREVIOUS topic's entities leak into this turn's
        # canonical rewrite (Part 6: "location should not affect
        # retrieval") — even though resolve_conversation's own follow-up
        # detection already wouldn't carry them for an unrelated new
        # question, this is the explicit, labeled guarantee.
        canonical_entities.pop("location", None)
        canonical_entities.pop("transport", None)
        canonical_entities.pop("topic", None)
    canonical_result = rewrite_canonical_query(resolved_question, entities=canonical_entities)
    canonical_question = canonical_result["canonical_query"]
    stages.append(Stage("Canonical Query Rewrite", "success", (time.time() - t0) * 1000,
                         f"rewritten to: {canonical_question!r} ({canonical_result['reason']})"
                         if canonical_result["rewrite_applied"] else f"not rewritten — {canonical_result['reason']}"))

    # 0c. Unified Intent Classification (rag/query_understanding.py::
    #     classify_actionable_intent()) — CONSOLIDATES the existing broad
    #     intent classifier (unchanged, still feeds Explainability/
    #     metadata matching) and rag/intent_classifier.py's narrow
    #     purpose-boost classifier (also unchanged, still the only thing
    #     rag/hybrid_scoring.py's ranking boost reads) with a new, more
    #     precise `actionable_intent` for the Answer/Attachment Planners
    #     below. Reuses the SAME merged entities as Canonical Query
    #     Rewrite — current wording's own entities win, missing ones fall
    #     back to whatever Conversation Resolver 2.0 already carried
    #     forward — so intent classification never disagrees with query
    #     resolution about what "location"/"transport" means this turn.
    t0 = time.time()
    from rag.query_resolution import extract_entities as _extract_entities_for_intent
    current_entities = _extract_entities_for_intent(canonical_question)
    merged_entities = dict(canonical_entities)
    for key, value in current_entities.items():
        if value:
            merged_entities[key] = value
    intent_result = classify_actionable_intent(canonical_question, entities=merged_entities,
                                               interpretation=interpretation)
    stages.append(Stage("Intent Classification", "success", (time.time() - t0) * 1000,
                         f"broad={intent_result['broad_intent']}, actionable={intent_result['actionable_intent']} "
                         f"(conf={intent_result['confidence']:.2f})"))

    # 0c2. Request decomposition (P1.2A, rag/query_resolution.py) —
    #      deterministic, no LLM call. A minimal multi-valued view of the
    #      resolved/canonical question (products, facets, transport modes,
    #      sub-questions, comparison, in-message corrections). Drives
    #      multi-target retrieval + the Answer Planner's completeness
    #      instruction ONLY when there is genuinely more than one evidence
    #      target; a normal single-component question is a complete no-op.
    from rag.query_resolution import (decompose_request, build_request_components,
                                      single_eligibility_component, generic_process_component)
    request_spec = decompose_request(canonical_question, history, raw_question=question)
    request_components = build_request_components(request_spec)
    multi_component_request = len(request_components) >= 2
    # Single concrete product + eligibility question ("น้ำหอมนำเข้าได้ไหม")
    # — reuse the SAME eligibility-focused retrieval enrichment the
    # multi-product path already gives each entity (P2A blocker). Not
    # multi-component: single retrieval path + normal plan, only the
    # retrieval query is enriched and the one component label is passed so
    # the P1.2A classification / "unconfirmed only if no policy" clause
    # still governs an unknown product.
    single_elig = None if multi_component_request else single_eligibility_component(request_spec)
    # P5.3 — generic ordering/import PROCESS question: enrich retrieval only
    # (never a marketplace/link-specific one) so the process-journey FAQ
    # ranks above the "สั่งจากเว็บจีน / วางลิงก์" sub-flow chunk.
    process_comp = (None if (multi_component_request or single_elig)
                    else generic_process_component(question)
                    or generic_process_component(canonical_question))
    stages.append(Stage("Request Decomposition", "success", 0.0,
                         (f"{len(request_components)} components: "
                          + "; ".join(l for l, _ in request_components))
                         if multi_component_request
                         else (f"single eligibility: {single_elig[0]}" if single_elig
                               else ("generic process — retrieval enriched" if process_comp
                                     else "single-component — existing retrieval path"))))

    # For a multi-component turn the single-intent Canonical Query Rewrite
    # (e.g. it collapsed "รถกับเรืออันไหนถูกกว่า" -> "อัตราค่าขนส่งทางรถ
    # เท่าไหร่") is the WRONG question to hand synthesis — the Answer Plan
    # already carries every component. Use the customer's own wording for
    # the prompt/plan instead; retrieval is the multi-target union either
    # way, and the routing gates below still read canonical_question.
    synthesis_question = (question if (multi_component_request and canonical_result["rewrite_applied"])
                          else canonical_question)

    # Conversation State refinement — the FINAL, most precise
    # actionable_intent/attribute (computed just above, on the final
    # canonical_question) supersedes build_conversation_state()'s
    # same-turn approximation (computed earlier, before Canonical Query
    # Rewrite existed for this turn) — same pattern as everywhere else in
    # this pipeline: never a second, independently-computed intent.
    from rag.conversation_state import subtopic_for
    conversation_state["intent"] = intent_result["actionable_intent"]
    conversation_state["subtopic"] = subtopic_for(intent_result["actionable_intent"], merged_entities.get("attribute"))

    # 0d. Information Collection Engine / Slot Filling Engine (services/
    #     slot_filling_engine.py) — a NEW workflow layer, additive only.
    #     Never touches Retrieval/Hybrid Search/Prompt Builder/Prompt
    #     Studio/AI Policies/Grounding/Benchmark/Production Validation.
    #     Detects its OWN narrow ERP-intent vocabulary (tracking/order/
    #     customer/warranty/invoice/payment) independently of the frozen
    #     actionable_intent classifier above — this is a distinct
    #     workflow question ("do we need ERP information for this turn"),
    #     not a redesign of Unified Intent Classification.
    t0 = time.time()
    from services.slot_filling_engine import resolve_active_erp_intent, build_collection_state
    # Detects intent from the ORIGINAL raw `question`, not
    # corrected_question/canonical_question — Spell Correction (rag/
    # spell_correction.py) can mis-correct an ERP keyword it has no
    # reason to recognize (observed: "เคลม" (claim) -> "คลัง" (warehouse)),
    # which would otherwise silently hide a genuine ERP intent before
    # this new workflow layer ever saw it. Slot VALUE extraction below
    # still uses canonical_question as normal — tracking/order/serial
    # numbers are already protected patterns spell correction never touches.
    erp_intent = resolve_active_erp_intent(history, question)
    slot_state = build_collection_state(erp_intent, history, canonical_question) if erp_intent else None
    stages.append(Stage("Slot Filling", "success" if slot_state else "skipped", (time.time() - t0) * 1000,
                         (f"intent={erp_intent}, complete={slot_state['is_complete']}, "
                          f"missing={slot_state['missing_slots']}, escalate={slot_state['escalation_required']}")
                         if slot_state else "no ERP-backed intent detected this turn"))

    # 1. Intent detection (unrelated — is_analytical() gates the Excel
    #    calculation engine; unchanged, untouched by Unified Intent
    #    Classification above)
    t0 = time.time()
    analytical = is_analytical(canonical_question)
    stages.append(Stage("Intent Detection", "success", (time.time() - t0) * 1000,
                         "analytical/calculation" if analytical else "general knowledge"))

    # 2. Policy Engine (pre-check — dissatisfaction keywords, business/
    #    knowledge/attachment/channel rule notes). The confidence-based
    #    "no answer found" escalation check needs retrieval confidence,
    #    which isn't known yet at this point in the pipeline — see the
    #    supplementary check after step 4 below, which upgrades this same
    #    `policy` object in place rather than re-running evaluate_policies
    #    (so notes/verdicts stay a single consistent object throughout).
    t0 = time.time()
    policy_set = get_default_policy_set()
    policy = evaluate_policies(canonical_question, policy_set=policy_set)
    stages.append(Stage("Policy Engine", "triggered" if policy.escalate else "success",
                         (time.time() - t0) * 1000,
                         "; ".join(v.name for v in policy.verdicts if v.status == "triggered") or "no policy triggered"))
    services_used.append({"name": "PolicyEngine", "status": "success"})

    # 3. Retrieval (embedding + vector search + excel engine + attachments —
    #    each already individually traced inside RAGService/rag.searcher)
    rag = get_rag_service()
    retrieval_trace: list = []
    t0 = time.time()
    # Negation-aware retrieval filtering — terms this turn explicitly
    # excluded (e.g. location=จีน after "ที่ไม่ใช่จีน") flow straight from
    # Conversation Resolver 2.0's excluded_entities into a down-rank
    # penalty (never a hard removal — see rag/hybrid_scoring.py) on any
    # matching chunk, so a contrastive follow-up doesn't retrieve the
    # excluded side's evidence.
    excluded_terms = (conversation.get("excluded_entities", {}).get("location", [])
                       + conversation.get("excluded_entities", {}).get("transport", []))
    # Broad "summarize/overview" questions need more than the default
    # top_k so the LLM can merge several FAQ/company chunks into one
    # summary instead of answering from a single chunk (see rag/
    # searcher.py::is_broad_summary_query). Only widens — never narrows
    # an explicitly larger top_k the caller already passed.
    from rag.searcher import is_broad_summary_query
    effective_top_k = top_k
    if is_broad_summary_query(canonical_question):
        from config import RAG_SUMMARY_TOP_K
        effective_top_k = max(top_k, RAG_SUMMARY_TOP_K)

    try:
        if multi_component_request:
            # Multi-target retrieval (P1.2A) — one focused query per
            # component through the EXISTING retrieve(), then union +
            # dedup + a strict overall cap. No new LLM calls. The single
            # top-level canonical_question path is untouched for every
            # normal message.
            per_component_k = max(3, effective_top_k)
            overall_cap = min(10, 3 + 2 * len(request_components))
            merged_chunks: List[Dict] = []
            seen_keys = set()
            for _label, _cquery in request_components:
                try:
                    _cc = rag.retrieve(_cquery, top_k=per_component_k, trace=None,
                                        excluded_terms=excluded_terms or None,
                                        actionable_intent=intent_result["actionable_intent"],
                                        original_question=_cquery)
                except Exception:
                    _cc = []
                for _c in _cc:
                    _key = _c.get("chunk_id") or (_c.get("text") or "")[:160]
                    if _key in seen_keys:
                        for _ex in merged_chunks:
                            if (_ex.get("chunk_id") or (_ex.get("text") or "")[:160]) == _key:
                                _ex.setdefault("_components", [])
                                if _label not in _ex["_components"]:
                                    _ex["_components"].append(_label)
                                break
                        continue
                    seen_keys.add(_key)
                    _c.setdefault("_components", []).append(_label)
                    merged_chunks.append(_c)
            chunks = merged_chunks[:overall_cap]
            retrieval_trace.append({"stage": "multi_target_retrieval", "status": "success",
                                     "duration_ms": (time.time() - t0) * 1000,
                                     "detail": f"{len(request_components)} components -> {len(chunks)} unioned chunks"})
        else:
            _retrieval_query = (single_elig[1] if single_elig
                                else process_comp[1] if process_comp
                                else canonical_question)
            chunks = rag.retrieve(_retrieval_query, top_k=effective_top_k, trace=retrieval_trace,
                                   excluded_terms=excluded_terms or None,
                                   actionable_intent=intent_result["actionable_intent"],
                                   original_question=corrected_question)
        services_used.append({"name": "RAGService", "status": "success"})
    except Exception as e:
        chunks = []
        retrieval_trace.append({"stage": "retrieval", "status": "failed", "duration_ms": (time.time() - t0) * 1000, "detail": str(e)})
        services_used.append({"name": "RAGService", "status": "failed"})

    excluded_candidates: List[Dict] = []
    query_expansion_debug: Dict = {}
    for entry in retrieval_trace:
        if entry["stage"] == "query_expansion_detail":
            # Debug-only payload (original_query/expanded_queries/
            # detected_language) — not rendered as its own Pipeline stage
            # row, just captured for the Explainability tab.
            query_expansion_debug = entry.get("query_expansion", {})
            continue
        stages.append(Stage(entry["stage"].replace("_", " ").title(), entry["status"], entry["duration_ms"], entry.get("detail", "")))
        if entry["stage"] == "excluded_candidates":
            excluded_candidates = entry.get("excluded", [])

    # Spell Correction Explainability (requirement 6: Original Query,
    # Corrected Query, Corrections Applied, Correction Confidence) — the
    # SAME query_expansion object the Explainability tab already renders,
    # just with these additional keys merged in.
    query_expansion_debug["raw_query"] = question
    query_expansion_debug["spell_corrected_query"] = corrected_question
    query_expansion_debug["spell_corrections"] = spell_result["corrections"]
    query_expansion_debug["spell_correction_confidence"] = spell_result["correction_confidence"]
    # Semantic Invariant Guard observability (Part 15, P0 2026-07-21) —
    # Developer Mode only; a rejected proposal never reaches the customer
    # in any form (corrected_question above is already the SAFE value).
    query_expansion_debug["rejected_correction"] = spell_result.get("rejected_correction")
    query_expansion_debug["rejection_reason"] = spell_result.get("rejection_reason")

    # Conversation Resolver 2.0 + Canonical Query Rewrite Explainability
    # (Part 1 + Part 2's required fields) — same additive pattern.
    query_expansion_debug["resolved_query"] = resolved_question
    query_expansion_debug["prev_user_topic"] = conversation.get("prev_topic")
    query_expansion_debug["followup_type"] = conversation.get("followup_type")
    query_expansion_debug["entities_carried"] = conversation.get("entities_carried")
    query_expansion_debug["resolution_confidence"] = conversation.get("confidence")
    query_expansion_debug["canonical_query"] = canonical_question
    query_expansion_debug["rewrite_applied"] = canonical_result["rewrite_applied"]
    query_expansion_debug["rewrite_reason"] = canonical_result["reason"]
    query_expansion_debug["rewrite_confidence"] = canonical_result["confidence"]

    # Negation-aware / contrastive follow-up Explainability — current vs.
    # carried vs. replaced vs. excluded entities, plus the contrast type
    # and the final standalone query, so the Playground can show e.g.
    # "Previous location: จีน / Current location: ไทย / Replaced: จีน -> ไทย
    # / Excluded: จีน" without re-deriving any of this from raw text.
    query_expansion_debug["current_entities"] = current_entities
    query_expansion_debug["excluded_entities"] = conversation.get("excluded_entities")
    query_expansion_debug["replaced_entities"] = conversation.get("replaced_entities")
    query_expansion_debug["contrast_type"] = conversation.get("followup_type")
    query_expansion_debug["final_standalone_query"] = canonical_question
    query_expansion_debug["excluded_terms_applied"] = excluded_terms

    # Conversation State Engine Explainability (Conversation Intelligence
    # Phase 1) — displayed BEFORE Canonical Query Rewrite per spec, so
    # it's placed here alongside the other pre-rewrite fields even though
    # this dict itself is only assembled after retrieval runs.
    query_expansion_debug["conversation_state"] = conversation_state
    # Slot Filling Engine Explainability (Developer Mode) — intent,
    # required/collected/missing slots, next expected slot, retry count,
    # escalation decision. None when no ERP-backed intent was detected.
    query_expansion_debug["slot_filling_state"] = slot_state

    # Evidence classification (rag/evidence_classifier.py) — annotates
    # each chunk with WHETHER it directly answers the specific attribute
    # asked about, vs. merely being related background on the same
    # entity/document. Purely additive metadata: never reorders, drops,
    # or adds chunks, and runs on the SAME final chunk list retrieval
    # already produced — retrieval/ranking itself is untouched.
    classify_evidence(canonical_question, chunks, query_variants=query_expansion_debug.get("expanded_queries"))

    excel_used = any(c.get("category") == "excel" for c in chunks)
    services_used.append({"name": "ExcelCalculationService", "status": "success" if excel_used else "skipped"})
    attachments_present = any(c.get("attachments") for c in chunks)
    services_used.append({"name": "AttachmentService", "status": "success" if attachments_present else "skipped"})

    from storage.factory import get_storage_service
    try:
        get_storage_service()
        services_used.append({"name": "StorageService", "status": "success"})
    except Exception:
        services_used.append({"name": "StorageService", "status": "failed"})

    # 4. Confidence (rag/confidence.py) — a deterministic model over
    #    evidence quality (answerability, lexical-evidence tier, evidence
    #    count), NOT the raw vector score. raw_vector_similarity is still
    #    captured and returned separately (Part 19: never relabel a 27%
    #    cosine score as "27% answer confidence").
    #    is_continuity_followup is passed ONLY when this turn is already
    #    established as a RAG-continuation, so rag/confidence.py's strong-
    #    vector fallback never applies to an ordinary fresh question. Two
    #    EXISTING signals, ORed (never a new classifier): (1) followup_type
    #    is one of the meta-followup shapes query_resolution.py resolves to
    #    "<confirmed prior topic> + <this turn's modifier>", or (2)
    #    _is_ambiguous_rag_continuity_followup(question, history) — the
    #    SAME continuity check already gating General Chat Fallback above
    #    — since a message can legitimately keep the conversation on-topic
    #    (e.g. it names "นำเข้า" itself, so query_resolution's own
    #    fresh-question guard leaves followup_type=None) while still being
    #    the kind of vague/simplify-style follow-up this gate must cover.
    is_continuity_followup = conversation.get("followup_type") in (
        "meta-summary-followup", "meta-detail-followup",
        "meta-simplify-followup", "meta-partial-followup",
    ) or _is_ambiguous_rag_continuity_followup(question, history)
    conf_result = compute_confidence(chunks, is_continuity_followup=is_continuity_followup)
    confidence = conf_result.answer_confidence
    confidence_label = _confidence_label_from_score(confidence)

    # Strong-Retrieval Yield (RAG-vs-General routing fix, 2026-08-31) — the
    # General Chat Fallback gate below is a pure keyword allowlist checked
    # BEFORE retrieval quality; it deliberately rejects spurious lexical
    # matches (the "จีนอยู่ทวีปอะไร" class), but it ALSO drops genuinely
    # company-specific questions whose wording happens to carry none of the
    # allowlisted terms ("รถกับเรือใช้เวลากี่วัน" — no ทาง-/ขนส่ง-/นำเข้า
    # token) even when RAG returned a literally-grounded direct answer.
    # This is NOT "retrieval quality alone decides": it fires only when the
    # single top chunk is a direct-evidence hit whose has_literal_evidence
    # bar was cleared on the RAW question AND whose hybrid_score is in the
    # multi-signal band (>= 0.95 — vector + keyword + heading all agreeing;
    # confirmed against the trace, single-signal spurious matches like
    # "จีนอยู่ทวีปอะไร" sit at ~0.72, well below this). Not sentence-
    # specific: no literal string is matched, only the shared retrieval
    # signals every chunk already carries.
    _top_chunk = chunks[0] if chunks else {}
    _rag_strong_direct = bool(
        chunks
        and conf_result.answerability in ("direct_answer", "partial_answer")
        and _top_chunk.get("classification") == "direct_evidence"
        and _top_chunk.get("has_literal_evidence")
        and (_top_chunk.get("hybrid_score") or 0.0) >= 0.95
    )

    # RAG-topic continuity follow-up (context-priority fix, 2026-09-01) —
    # a short "แล้ว…ล่ะ"/contrastive follow-up ("แล้วจีนล่ะ" after a Thai-
    # warehouse answer, "แล้วทางเรือล่ะ" after a road-duration answer)
    # carries none of the keyword-gate terms itself, so it fell to General
    # Chat and answered "ยังไม่มีข้อมูล" — losing the topic the customer is
    # still on. Reuses the EXISTING deterministic resolver output, no new
    # classifier: resolve_conversation already flagged this turn as a
    # genuine follow-up (`followup_type` set) and rewrote it to its real
    # subject (`canonical_question`); if that rewritten subject is itself a
    # company/operational topic, the turn stays on the RAG path so the
    # already-retrieved evidence for the resolved question is used.
    _rag_topic_continuity_followup = bool(
        conversation.get("followup_type")
        and (_COMPANY_OPERATIONAL_TOPIC_RE.search(canonical_question or "")
             or _CHINA_SOURCED_ACTION_RE.search(canonical_question or ""))
    )

    # Curated FAQ row is always a company answer (routing fix, 2026-09-01)
    # — an exact/near-exact match against the reviewed FAQ index (rag/
    # faq_matcher.py, NEAR_EXACT_THRESHOLD 0.88 on the question + its
    # curated alt-phrasings) means a human already decided this wording is
    # a company/operational question and wrote its answer. Such a turn must
    # never fall through the keyword gate to General Chat just because its
    # surface words ("ขอเบอร์ติดต่อ", "ส่งต่อในไทยคิดค่าใช้จ่ายอะไรบ้าง",
    # "แนะนำเพื่อนได้ส่วนลดอะไร") are not in _COMPANY_OPERATIONAL_TOPIC_RE.
    # Genuine general-knowledge questions do not FAQ-exact match (verified:
    # "จีนอยู่ทวีปอะไร" -> no FAQ row).
    _rag_faq_exact = bool(chunks and chunks[0].get("is_faq_exact"))

    # 4a. Escalation Rules, part 2 — "send to human when no answer is
    #     found" needs real confidence, only known now. Upgrades the SAME
    #     `policy` object from step 2 (never re-evaluates from scratch),
    #     so a dissatisfaction-keyword escalation from step 2 is never
    #     silently overwritten by this check running "not escalated".
    if not policy.escalate:
        esc = get_escalation_settings(policy_set)
        if (esc["enabled"] and esc["escalate_on_no_answer"] and confidence < esc["confidence_threshold"]
                and not _has_direct_structured_evidence(chunks)):
            policy.escalate = True
            policy.escalation_message = esc["message"]
            policy.notes.append(f"This question triggered escalation — hand off to a human. "
                                 f"Use this message: {esc['message']!r}")
            for i, v in enumerate(policy.verdicts):
                if v.name == "Escalation Rules":
                    policy.verdicts[i] = PolicyVerdict(
                        "Escalation Rules", "triggered",
                        f"Confidence {confidence:.2f} is below the {esc['confidence_threshold']:.2f} "
                        f"threshold — no reliable answer found")
                    break

    # 4b. Metadata-aware retrieval (Phase 2 Part 1, rag/metadata_retrieval.py)
    #     — additive Explainability annotation ("Metadata Match" /
    #     "Document Purpose"). Actual ranking already happened during
    #     retrieval (rag/hybrid_scoring.py's purpose-aware boost); this
    #     never reorders `chunks`.
    t0 = time.time()
    from rag.metadata_retrieval import annotate_metadata_match, summarize_metadata_match
    detected_intent = query_expansion_debug.get("detected_intent")
    annotate_metadata_match(chunks, detected_intent)
    metadata_summary = summarize_metadata_match(chunks)
    stages.append(Stage("Metadata Match", "success", (time.time() - t0) * 1000,
                         f"{metadata_summary['matched_chunks']}/{metadata_summary['total_chunks']} chunk(s) "
                         f"matched intent '{detected_intent}'"))

    # 4c. Context Builder (Phase 2 Part 2, rag/context_builder.py) —
    #     dedup -> merge overlapping -> group by source -> compress
    #     repeated lines -> final context top-K. Only the PROMPT's
    #     context uses this compressed set; `chunks` (Retrieved Chunks
    #     tab, citation selection below) stays the original list so
    #     nothing already relied upon elsewhere changes shape.
    t0 = time.time()
    from rag.context_builder import build_context as build_compressed_context
    context_chunks, context_builder_summary = build_compressed_context(chunks, final_top_k=effective_top_k)
    stages.append(Stage("Context Builder", "success", (time.time() - t0) * 1000,
                         f"{context_builder_summary['original_count']} -> {context_builder_summary['final_count']} "
                         f"chunk(s) ({context_builder_summary['duplicates_removed']} dup, "
                         f"{context_builder_summary['chunks_merged']} merged)"))

    # 4d. Retrieval Confidence (Phase 2 Part 3, rag/retrieval_confidence.py)
    #     — computed on the SAME compressed chunk set that's actually
    #     going to the LLM. A different metric from confidence/
    #     answer_confidence above (see that module's docstring).
    t0 = time.time()
    from rag.retrieval_confidence import compute_retrieval_confidence
    retrieval_confidence_result = compute_retrieval_confidence(context_chunks)
    stages.append(Stage("Retrieval Confidence", "success", (time.time() - t0) * 1000,
                         f"{retrieval_confidence_result['retrieval_confidence']:.2f}"))

    # 4d2. Source-of-truth conflict guard (P1.2B, rag/fact_conflict.py) —
    #      deterministic, no LLM. Over the SAME final evidence set (plus a
    #      FAQ-exact chunk's equivalently-eligible duplicate answers, if
    #      any), detect a high-risk structured fact (transport rate /
    #      duration / an entity's phone) that two trusted sources give
    #      INCOMPATIBLE values for. A flagged component is threaded into
    #      the P1.2A answer plan so synthesis states it as unconfirmed
    #      instead of silently choosing a value; every other component is
    #      answered normally.
    t0 = time.time()
    from rag.fact_conflict import detect_conflicts_in_chunks, components_with_conflict
    fact_conflict_result = detect_conflicts_in_chunks(context_chunks)
    conflicting_components = components_with_conflict(
        request_components if multi_component_request else [], fact_conflict_result, request_spec)
    if fact_conflict_result.has_conflict():
        # Ensure synthesis actually SEES both sides — a FAQ-exact chunk
        # carries only its own chosen answer; append the conflicting
        # duplicate answers as extra evidence so the model cannot resolve
        # the disagreement by omission.
        _existing = {(c.get("text") or "") for c in context_chunks}
        for _c in list(context_chunks):
            for _j, _alt in enumerate(_c.get("faq_conflict_texts") or []):
                if _alt and _alt not in _existing:
                    context_chunks.append({"text": _alt, "source": f"FAQ-duplicate-{_j}",
                                            "citation": f"Source: FAQ duplicate row {_j}",
                                            "is_faq_exact": False, "category": "faq",
                                            "attachments": []})
                    _existing.add(_alt)
    stages.append(Stage("Fact Conflict", "success", (time.time() - t0) * 1000,
                         ("; ".join(c.key for c in fact_conflict_result.conflicts)
                          + f" -> {conflicting_components}")
                         if fact_conflict_result.has_conflict()
                         else "no source-of-truth conflict in evidence"))
    # Developer trace (P1.2B) — fact key, normalized + original conflicting
    # values, source ids. Never surfaced in the customer-facing reply.
    query_expansion_debug["fact_conflicts"] = fact_conflict_result.as_trace()
    query_expansion_debug["conflicting_components"] = conflicting_components

    # 4e. Answer Planner (services/answer_planner.py) — selects/organizes
    #     which FACT LABELS (never fact values) the LLM should focus on,
    #     from the actionable_intent/entities/final retrieved evidence.
    #     Runs AFTER Context Builder + Retrieval Confidence (needs the
    #     SAME final evidence set the prompt will actually use) and
    #     BEFORE Prompt Builder. Never invents a fact — only picks from
    #     what's already present in `context_chunks`.
    t0 = time.time()
    answer_plan = plan_answer(
        synthesis_question, intent_result["actionable_intent"], intent_result["requested_attributes"],
        intent_result["entities"], context_chunks, retrieval_confidence_result["retrieval_confidence"], policy_set,
        raw_question=question,
        requested_components=([l for l, _ in request_components] if multi_component_request
                               else ([single_elig[0]] if single_elig else None)),
        comparison=request_spec.comparison,
        conflicting_components=conflicting_components or None,
        history=history, request_spec=request_spec,
        answerability=conf_result.answerability,
        lead_stage=lead_stage, sentiment_status=sentiment_status,
    )
    _fu = answer_plan.get("followup") or {}
    stages.append(Stage("Answer Planner", "success", (time.time() - t0) * 1000,
                         f"goal={answer_plan['answer_goal']!r}, shape={answer_plan['response_shape']}"
                         + (", clarification requested" if answer_plan["clarification_required"] else "")
                         + (f", followup={_fu['purpose']}" if _fu.get("needed") else "")))
    query_expansion_debug["followup"] = answer_plan.get("followup")

    # P5.1 — is THIS turn a bare product reply to the assistant's own
    # elicit_product_type question? (Clarification State Engine already
    # resolved it.) Computed once; consulted by BOTH the deterministic
    # no_information branch and the synthesis "leaned-allowed but
    # unproven" rewrite below, so a contextual product answer with no
    # trusted policy continues the service conversation instead of a
    # "ไม่มีข้อมูลยืนยัน" reply. A prohibited product (น้ำหอม -> liquid)
    # keeps its prohibited verdict and never reaches either path.
    _pac_noun = _product_answer_continuation_noun(
        conversation.get("followup_type"), history, single_elig, request_spec)
    _pac_transport_known = bool(merged_entities.get("transport")
                                 or (request_spec.transport_modes or []))

    # 5. Prompt Builder — needs the retrieved context, so it runs AFTER retrieval.
    # retrieval_confidence (Phase 2 Part 3, computed just above) is passed
    # through so build_prompt() can suppress/summarize conversation
    # history instead of injecting it verbatim (see services/
    # prompt_builder.py's module docstring — the fix for conversation
    # history contaminating RAG answers).
    t0 = time.time()
    context = rag.build_context(context_chunks)
    built_prompt = build_prompt(synthesis_question, context, template_id=template_id, policy_notes=policy.notes,
                                 history=history,
                                 retrieval_confidence=retrieval_confidence_result["retrieval_confidence"],
                                 answer_plan=answer_plan)
    stages.append(Stage("Prompt Builder", "success", (time.time() - t0) * 1000,
                         f"template={built_prompt.template.id} v{built_prompt.template.version}"))
    services_used.append({"name": "PromptBuilderService", "status": "success"})

    # 6. LLM — SKIPPED entirely when the Answer Planner requested
    #    clarification: "do not call the LLM just to generate
    #    clarification text; use deterministic clarification templates."
    #    The clarification question itself is a fixed string already
    #    chosen by services/answer_planner.py from retrieved evidence
    #    structure alone (e.g. both a Thai and a China warehouse were
    #    plausible) — never LLM-generated.
    t0 = time.time()
    slot_filling_active = slot_state is not None and not slot_state["is_complete"] and not slot_state["escalation_required"]
    slot_filling_escalation = slot_state is not None and slot_state["escalation_required"]
    slot_filling_complete = slot_state is not None and slot_state["is_complete"]
    general_chat_used = False
    unsupported_company_fact = False

    if bare_math_result:
        value = bare_math_result["value"]
        value_str = str(int(value)) if float(value).is_integer() else str(round(value, 4))
        answer_text = f"{bare_math_result['expression']} = {value_str}"
        stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                             "bare arithmetic expression, no active flow — evaluated directly, no LLM call"))
        services_used.append({"name": "LLMService", "status": "skipped"})
        input_tokens = output_tokens = 0
        llm_latency = 0.0
        llm_failed = False
    elif dimension_slot_result and not dimension_slot_result["flow_complete"]:
        # Active Slot-Filling Flow (shipping_cost_calculation) still has
        # missing slots — the deterministic, template-built message asks
        # only for what's actually missing; never an LLM call, never a
        # standalone arithmetic evaluation of the customer's dimension
        # values (the exact bug this fix exists for).
        answer_text = dimension_slot_result["message"]
        stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                             "active slot-filling flow incomplete — deterministic missing-slot message used, "
                             "no LLM call, no standalone math"))
        services_used.append({"name": "LLMService", "status": "skipped"})
        input_tokens = output_tokens = 0
        llm_latency = 0.0
        llm_failed = False
    elif intent_result["actionable_intent"] == "self_pickup_permission":
        # CUSTOMER-RAG-1.1 — a SELF-PICKUP permission / how-to question
        # ("รับสินค้าเองได้ไหม", "ไปรับของเองได้ไหม"). NOT a warehouse-
        # LOCATION question — must not ask ไทย/จีน. The trusted evidence
        # is one sentence in the "ขอที่อยู่โกดังหน่อย" FAQ (chunk
        # 5fdffb90-b922-4aef-847c-74e73fa941ec): "สำหรับลูกค้าต้องการ
        # เข้ารับสาขานี้ สามารถเลือกเปลี่ยนมารับสินค้าได้ที่หน้าที่อยู่
        # จัดส่งในระบบนะคะ". Answered deterministically from that trusted
        # wording — no LLM, no ไทย/จีน clarification, no invented
        # conditions. (The FAQ-exact matcher short-circuits retrieval to
        # the country-specific "ขอที่อยู่โกดังจีน" row for any pickup-ish
        # query, so a retrieval-based path cannot reach this sentence.)
        answer_text = ("ได้ค่ะ สามารถเลือกเปลี่ยนมารับสินค้าเองได้ที่หน้าที่อยู่จัดส่งในระบบค่ะ "
                       "หากต้องการทราบที่อยู่จุดรับสินค้า แจ้งได้เลยนะคะ")
        stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                             "self-pickup permission — deterministic answer from the trusted "
                             "warehouse FAQ's self-pickup sentence, no LLM call"))
        services_used.append({"name": "LLMService", "status": "skipped"})
        input_tokens = output_tokens = 0
        llm_latency = 0.0
        llm_failed = False
    elif _invoice_issuance_branch_applies(question, intent_result["actionable_intent"], interpretation):
        # INVOICE-REGRESSION-1 — the issuance branch now keys on the
        # CENTRAL INVOICE family (SEMANTIC-FIRST-1), not only the literal
        # "ใบกำกับ + ออก/ขอ/ได้" phrase shape. So "ออก tax invoice ให้ไหม",
        # "ขอใบเสร็จค่าขนส่งได้ไหม", "มี e-tax invoice ไหม" — every
        # paraphrase interpret() resolves to INVOICE — reaches the trusted
        # answer instead of falling through retrieval to the Answerability
        # Gate and Fix-2 Human Handoff. The download how-to
        # ("โหลดใบกำกับยังไง") is still excluded (_INVOICE_DOWNLOAD_RE) and
        # keeps its own 5-step FAQ (389645f9). No _INVOICE_RE phrase
        # dictionary is grown — the family is the primary signal.
        #
        # CUSTOMER-INVOICE-1 — "can you issue a goods invoice?"
        # (ใบกำกับค่าสินค้าออกได้ไหม / …ไม่ได้หรอ / ขอ…ไม่ได้หรอ). The
        # FAQ-exact matcher returns the Quick_FAQ_Patch row 546c1bd5
        # verbatim, whose Answer ends with an unrelated product question
        # ("…ไม่ทราบว่าสินค้าของลูกค้าเป็นอะไรคะ") and omits the actual
        # conditions. Answer deterministically from the COMPLETE trusted
        # invoice facts that already exist in the KB:
        #   ff288877 ("ออกใบกำกับได้ไหม") — issuance + bill/payment
        #            condition + taxpayer-info requirement + credit-card
        #            limitation.
        #   99390831 ("Shipify ออก e-Tax Invoice ได้ไหม") — send taxpayer
        #            info + bill number to staff.
        #   a2618c6d ("ชำระบัตรเครดิตได้ไหม") — credit card => no invoice.
        # No invented conditions; the download flow (โหลดใบกำกับยังไง) is
        # excluded and keeps its own FAQ answer.
        answer_text = _INVOICE_ISSUANCE_ANSWER
        stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                             "invoice issuance policy — deterministic answer composed from the "
                             "complete trusted invoice FAQ conditions, no LLM call"))
        services_used.append({"name": "LLMService", "status": "skipped"})
        input_tokens = output_tokens = 0
        llm_latency = 0.0
        llm_failed = False
    elif answer_plan["clarification_required"]:
        answer_text = answer_plan["clarification_question"]
        stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                             "clarification requested — deterministic template used, no LLM call"))
        services_used.append({"name": "LLMService", "status": "skipped"})
        input_tokens = output_tokens = 0
        llm_latency = 0.0
        llm_failed = False
    elif slot_filling_escalation:
        # Escalation ONLY per the task's own rule: explicit human
        # request, policy-required, information cannot be collected, or
        # the user refuses/exhausts retries — never merely because ERP
        # is unavailable (that case is slot_filling_complete below,
        # which is NOT an escalation).
        answer_text = slot_state["escalation_message"]
        policy.escalate = True
        policy.escalation_message = slot_state["escalation_message"]
        policy.notes.append(f"Slot Filling Engine escalation ({slot_state['escalation_reason']}) for "
                             f"intent={slot_state['intent']!r} — missing {slot_state['missing_slots']}")
        stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                             f"slot filling escalation ({slot_state['escalation_reason']}) — no LLM call"))
        services_used.append({"name": "LLMService", "status": "skipped"})
        input_tokens = output_tokens = 0
        llm_latency = 0.0
        llm_failed = False
    elif slot_filling_active:
        # Missing required ERP parameters — ask the deterministic
        # follow-up question instead of escalating or guessing. Never an
        # LLM call: the question text is a fixed template from
        # services/slot_filling_engine.py's INTENT_SCHEMAS (Admin
        # Configuration), never invented.
        answer_text = slot_state["follow_up_question"]
        stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                             f"slot filling incomplete (missing {slot_state['missing_slots']}) — "
                             f"follow-up question used, no LLM call"))
        services_used.append({"name": "LLMService", "status": "skipped"})
        input_tokens = output_tokens = 0
        llm_latency = 0.0
        llm_failed = False
    elif slot_filling_complete:
        # All required information collected — ERP is not implemented
        # yet (services/erp_adapter.py::MockERPAdapter), so the
        # acknowledgement message is used as-is. This is explicitly NOT
        # an escalation ("Do NOT escalate simply because ERP is
        # unavailable").
        from services.erp_adapter import get_erp_adapter, execute_erp_intent
        erp_result = execute_erp_intent(get_erp_adapter(), slot_state["intent"], slot_state["collected_slots"])
        answer_text = erp_result["message"]
        stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                             f"slot filling complete for intent={slot_state['intent']!r} — "
                             f"mock ERP acknowledgement used, no LLM call"))
        services_used.append({"name": "LLMService", "status": "skipped"})
        services_used.append({"name": "ERPAdapter", "status": erp_result["status"]})
        input_tokens = output_tokens = 0
        llm_latency = 0.0
        llm_failed = False
    elif _g6c_promote_general_assistance(question, interpretation, history,
                                          rag_strong_direct=_rag_strong_direct,
                                          rag_faq_exact=_rag_faq_exact) \
            or not (_COMPANY_OPERATIONAL_TOPIC_RE.search(question or "")
              or _CHINA_SOURCED_ACTION_RE.search(question or "")
              or _URGENCY_SIGNAL_RE.search(question or "")
              or _COMPLAINT_SIGNAL_RE.search(question or "")
              or _rag_strong_direct
              or _rag_topic_continuity_followup
              or _rag_faq_exact
              or multi_component_request or (single_elig is not None) or (process_comp is not None)
              or _is_ambiguous_rag_continuity_followup(question, history)
              # SEMANTIC-FIRST-2.1 — the ONE central interpreter named this
              # a PUBLIC company-information family (warehouse / pickup,
              # self-pickup, coupon USAGE, prohibited-goods, charter
              # service, invoice / document). It is a company-knowledge
              # question by MEANING, not general chit-chat — keep it on
              # the company RAG path so an unseen paraphrase with no
              # trusted evidence returns the honest company "no
              # information" answer (Answerability Gate), never a general-
              # chat guess.
              or getattr(interpretation, "intent_family", None) in _PUBLIC_INFO_COMPANY_FAMILIES):
        # General Chat Fallback (Hybrid RAG + General AI Chat, 2026-08-27;
        # moved ahead of the Answerability Gate 2026-08-27 same day — Final
        # Hybrid Stabilization) — confirmed live: "จีนอยู่ทวีปอะไร" (a pure
        # general-knowledge question) still got answered with the
        # company's China-warehouse-address FAQ, because that chunk's
        # ONLY shared word with the question is the generic noun "จีน" —
        # enough for rag/confidence.py's has_literal_evidence check to
        # call it "reliable," so conf_result.answerability came back
        # direct_answer/partial_answer, never no_information, and this
        # branch (originally gated on == "no_information") never ran.
        # Checking topic BEFORE consulting answerability at all — the
        # question's own nature decides, never retrieval-quality alone —
        # is what the task itself requires ("even if irrelevant company
        # RAG happens to retrieve lexical matches"). _COMPANY_OPERATIONAL_
        # TOPIC_RE was extended with a few precise, low-collision domain
        # terms (CBM, ทางรถ, ทางเรือ, ขั้นต่ำ) specifically so genuinely
        # company-specific questions that happen not to name "บริษัท"
        # (e.g. "CBM คืออะไร", "ทางรถกี่วัน") are never pulled into this
        # branch — matching examples from BOTH sides of this exact
        # distinction were verified live before this reordering shipped.
        general_chat_used = True
        try:
            general_chat_prompt = build_prompt(
                canonical_question, "", template_id=template_id, policy_notes=policy.notes,
                history=history, retrieval_confidence=retrieval_confidence_result["retrieval_confidence"],
                answer_plan=answer_plan, general_chat_mode=True)
            llm = get_llm_service()
            llm_response = llm.generate(general_chat_prompt.messages, model=OPENAI_CHAT_MODEL,
                                         temperature=temperature, max_tokens=max_tokens)
            stages.append(Stage("LLM", "success", (time.time() - t0) * 1000,
                                 f"model={llm_response.model} (general chat mode — no company evidence used)"))
            services_used.append({"name": "LLMService", "status": "success"})
            answer_text = llm_response.text
            input_tokens, output_tokens = llm_response.input_tokens, llm_response.output_tokens
            llm_latency = llm_response.latency_ms
            llm_failed = False
        except Exception as e:
            stages.append(Stage("LLM", "failed", (time.time() - t0) * 1000, str(e)))
            services_used.append({"name": "LLMService", "status": "failed"})
            answer_text = "ขออภัยค่ะ ระบบขัดข้องชั่วคราว กรุณาลองใหม่อีกครั้งนะคะ"
            input_tokens = output_tokens = 0
            llm_latency = 0.0
            llm_failed = True
    elif conf_result.answerability == "no_information" and not (
            multi_component_request and any(
                c.get("has_literal_evidence") or c.get("is_faq_exact")
                or c.get("classification") in ("direct_evidence", "structured_deterministic")
                for c in chunks)) and not (
            # FIX-2.3 / SEM-GEN-1 — a product/import-interest turn that
            # retrieved a chunk firmly classifying SOME product into a
            # prohibited category goes to grounded synthesis instead, so
            # the LLM classifies the CUSTOMER's actual product against
            # that policy ("กล่องพลาสติก" is not a liquid; "น้ำยาปรับผ้านุ่ม"
            # is). Surfacing the retrieved FAQ verbatim mis-answered the
            # unfamiliar product.
            _is_product_import_interest(question) and any(
                _ITEM_IS_CATEGORY_RE.search(c.get("text") or "")
                and _P51_FIRM_PROHIBITED_RE.search(c.get("text") or "")
                for c in (chunks or []))):
        # Partial answerability (P1.2A) — a MULTI-COMPONENT request with at
        # least one component that has literal evidence goes to synthesis
        # (the Answer Plan tells the LLM to answer the supported components
        # and mark only the unsupported ones as unconfirmed), instead of a
        # single blanket "no information" reply. A single-component
        # question, or a multi-component one with zero literal evidence
        # anywhere, still takes the deterministic safe-fallback unchanged —
        # no global confidence threshold is touched.
        #
        # Every message reaching this branch already matched the company/
        # operational/urgency/complaint check above (the General Chat
        # Fallback branch, immediately above, is what catches everything
        # else) — so this stays the SAME deterministic safe-fallback for a
        # genuinely company-specific question with zero reliable evidence,
        # completely unchanged from before this reordering.
        #
        # Answerability Gate (Task 04B, 2026-08-26) — retrieval returning
        # a non-empty Top-K is never the same thing as the question being
        # answerable (confirmed live: "บริษัทมีนโยบายเรื่องการรีไซเคิล
        # กล่องพัสดุอย่างไร" — genuinely absent from the knowledge base —
        # retrieved several shipping-related FAQ chunks that scored a
        # "perfect" keyword match purely via generic company-intent-
        # expansion terms or a bare Tags-line word, none of which actually
        # answers the question asked; the LLM then answered confidently
        # using that unrelated pricing/service content). Once
        # rag/confidence.py's stricter _classify_answerability determines
        # no chunk carries reliable (literal/intent/structured) evidence,
        # reject BEFORE the LLM ever sees the chunks — never rely on the
        # system prompt's own "don't answer if irrelevant" instruction
        # alone when deterministic retrieval evidence can reject it first
        # (Phase 11). The exact wording already exists as the product's
        # own approved fallback phrasing (services/prompt_builder.py's
        # "## กรณีไม่มีข้อมูล (Fallback Tone)" section) — reused verbatim so
        # this deterministic path sounds identical to what the LLM would
        # have said anyway, never an infrastructure-sounding message.
        #
        # Customer Journey UAT (2026-08-27) — this deterministic path
        # bypasses the LLM/Prompt Studio entirely, so CS-03's
        # unknown_information_wording rule (which forbids the internal-
        # sounding term "ฐานความรู้" in customer-facing replies) could
        # never reach it. Confirmed live for real LINE traffic
        # (channel="line") via this same Answerability Gate. Updated to
        # the same natural wording CS-03 already established elsewhere —
        # no business fact changed, still an honest "no information"
        # answer.
        #
        # Zero-Evidence Fallback Tone Guard (Customer Journey UAT,
        # 2026-08-27) — a bare "no information" reply is not sufficient
        # Human CS behavior when the customer's own message carries an
        # urgency or complaint signal (confirmed live: "ตามมาหลายวันแล้ว
        # ครับ ของรีบใช้..." and "...เหมือนเป็นของเก่าครับ กล่องก็มีรอย" both
        # produced only the bare fallback, with no acknowledgement at
        # all). Matched against the RAW customer message (never the
        # canonicalized/rewritten query, which can lose the emotional
        # signal) — checked in order (complaint takes precedence when a
        # message carries both, since the customer report is the more
        # specific of the two). Prepends one short, neutral acknowledgement
        # sentence reusing the SAME safe phrasing families CS-02/CS-03
        # already established elsewhere in the prompt (never invents a
        # cause, never claims a status, never escalates).
        # P5.1 — a bare product reply to the assistant's own
        # elicit_product_type question that carries NO trusted prohibited-
        # policy evidence is a CONTEXTUAL PRODUCT ANSWER, not a standalone
        # eligibility question — it must continue the Shipify service
        # conversation, never answer "no information". (A prohibited product
        # like น้ำหอม surfaces its liquid-policy evidence and never reaches
        # this no_information branch.)
        if _pac_noun:
            answer_text, _pac_note = _product_answer_service_continuation(
                _pac_noun, lead_stage=lead_stage, sentiment_status=sentiment_status,
                history=history, transport_known=_pac_transport_known)
            stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                                 f"P5.1 product-answer continuation ({_pac_note}) — no LLM call"))
        elif _is_product_import_interest(question):
            # FIX-2.3 — a product / import interest declarative that
            # retrieved no direct evidence is EARLY SALES INTEREST, not a
            # company-fact no-info. NEVER set `unsupported_company_fact`,
            # never Human CS, never claim an eligibility guarantee. Full
            # product recognition -> policy cross-check -> quantity/weight/
            # route collection is CONV-SELL.
            #
            # (A product-interest turn whose retrieval carried a firm
            # prohibited-CATEGORY chunk was already diverted to grounded
            # synthesis by the outer guard above, so here there is no
            # trusted prohibition to state — only the safe service ack.)
            _pi_noun = _product_interest_noun(question)
            if _pi_noun:
                answer_text, _pi_note = _product_answer_service_continuation(
                    _pi_noun, lead_stage=lead_stage, sentiment_status=sentiment_status,
                    history=history, transport_known=_pac_transport_known)
            else:
                answer_text = ("รับทราบค่ะ สนใจนำเข้าสินค้ากับ Shipify นะคะ 😊 "
                               "มีบริการขนส่งทั้งทางรถและทางเรือค่ะ "
                               "รบกวนขอรายละเอียดสินค้าเพิ่มเติมสักนิด เช่น ประเภทสินค้า จำนวน "
                               "หรือน้ำหนักโดยประมาณ จะได้แนะนำบริการที่เหมาะสมให้ค่ะ")
                _pi_note = "generic-import-interest-ack"
            stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                                 f"FIX-2.3 product/import-interest continuation ({_pi_note}) — "
                                 "not a company-fact no-info, no LLM call, no Human CS"))
        elif (getattr(interpretation, "intent_family", None) == "PRODUCT_POLICY"
              and (getattr(interpretation, "entities", {}) or {}).get("product")):
            # INVOICE-PRODUCT-REGRESSION-2 (Problem B) — the CENTRAL semantic
            # layer recognised this turn as a PRODUCT entity (a product name
            # the customer gave, generically — no product dictionary). An
            # ORDINARY product whose exact noun is unseen carries no trusted
            # prohibited-policy evidence, but that is NOT "Shipify has no
            # company information" — it must continue the service
            # conversation, never Fix-2 / Human CS. (A prohibited product
            # surfaces its own policy evidence via retrieval and never
            # reaches this no_information branch.)
            _sf_noun = (interpretation.entities or {}).get("product")
            answer_text, _sf_note = _product_answer_service_continuation(
                _sf_noun, lead_stage=lead_stage, sentiment_status=sentiment_status,
                history=history, transport_known=_pac_transport_known)
            stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                                 f"SEMANTIC-FIRST product entity ({_sf_note}) — novel product noun is "
                                 "not a company-fact no-info, no LLM call, no Human CS"))
        else:
            answer_text = "ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ"
            if _COMPLAINT_SIGNAL_RE.search(question or ""):
                answer_text = "รับทราบเรื่องที่แจ้งมาค่ะ " + answer_text + " หากมีเลขที่คำสั่งซื้อหรือรายละเอียดเพิ่มเติม รบกวนแจ้งเพิ่มเติมได้เลยค่ะ จะช่วยตรวจสอบให้ค่ะ"
            elif _URGENCY_SIGNAL_RE.search(question or ""):
                answer_text = "เข้าใจว่าเรื่องนี้เร่งด่วนสำหรับคุณค่ะ " + answer_text + " จะติดตามและแจ้งความคืบหน้าให้เร็วที่สุดค่ะ"
            # Customer UAT Fix 2 — a genuine company-fact question with zero
            # reliable evidence is a structured "need human follow-up"
            # case; the Decision Engine routes it through the existing
            # Human CS handoff mechanism (no wording match, no new
            # infra).
            unsupported_company_fact = True
            stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                                 "no chunk carries reliable evidence for this question (Answerability Gate) — "
                                 "deterministic safe-fallback used, no LLM call, no chunks used as evidence"))
        services_used.append({"name": "LLMService", "status": "skipped"})
        input_tokens = output_tokens = 0
        llm_latency = 0.0
        llm_failed = False
    elif (answer_plan.get("response_shape") == "faq_direct" and chunks and chunks[0].get("is_faq_exact")
          and not _faq_row_overspecified_for_transport(chunks[0].get("text") or "", question)
          and not fact_conflict_result.has_conflict()):
        # Direct FAQ Fidelity — deterministic return (2026-09-01). An
        # exact/near-exact FAQ row IS a human-reviewed, customer-approved
        # answer (rag/faq_matcher.py + the knowledge_items index). Sending
        # it through the grounded-synthesis LLM was silently dropping its
        # own trailing follow-up question and, on the raw Thai, lightly
        # rewording it (spell-corrector artefacts, "ครีม" -> "ฟรีม"). The
        # FAQ-exact chunk's text is always exactly
        # "Question: <q>\nAnswer: <a>" (rag/searcher.py) — return <a>
        # verbatim, no LLM call, same pattern as the other deterministic
        # branches above. Non-FAQ retrieval still goes through synthesis
        # unchanged. EXCEPTION (_faq_row_overspecified_for_transport): a
        # generic multi-mode rate/duration FAQ row matched by a mode-
        # specific question ("ทางรถเรทเท่าไหร่" near-matches "เรทเท่าไหร่คะ")
        # must still be narrowed by the LLM using answer_plan — returning
        # every mode verbatim there is the wrong answer.
        _faq_text = chunks[0].get("text") or ""
        answer_text = _faq_text.split("\nAnswer: ", 1)[1].strip() if "\nAnswer: " in _faq_text else _faq_text.strip()
        # P2 contextual follow-up on the verbatim FAQ path — appended
        # deterministically (no synthesis call, FAQ wording untouched),
        # gated by _apply_p2_followup (no duplicate; offer only after a
        # real prohibited verdict).
        answer_text, _p2_note = _apply_p2_followup(answer_text, answer_plan.get("followup"))
        stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                             "exact FAQ row — customer-approved answer returned verbatim, no LLM call"
                             + (f" (P2 {_p2_note})" if _p2_note != "not-needed" else "")))
        services_used.append({"name": "LLMService", "status": "skipped"})
        input_tokens = output_tokens = 0
        llm_latency = 0.0
        llm_failed = False
    elif (_COMPANY_GUARANTEE_Q_RE.search(question or "")
          and not any(c.get("is_faq_exact") for c in (chunks or []))):
        # P7.1 — a company guarantee/responsibility yes-no question with NO
        # exact-FAQ policy row behind it. Do NOT let synthesis manufacture a
        # positive or negative company policy from generic company chunks +
        # world knowledge — use the existing grounded no-information wording.
        # (Ordinary category-semantics -> trusted-prohibited-policy is a
        # DIFFERENT class and keeps its own path; a supported guarantee fact
        # is a curated FAQ row and answers normally.)
        answer_text = ("ตอนนี้ยังไม่มีข้อมูลยืนยันนโยบายเรื่องนี้ในระบบค่ะ "
                       "รบกวนสอบถามเจ้าหน้าที่เพื่อความชัดเจนอีกครั้งนะคะ")
        # Customer UAT Fix 2 — same structured "need human follow-up"
        # signal as the Answerability-Gate branch above.
        unsupported_company_fact = True
        stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                             "P7.1 — company guarantee/responsibility question, no exact-FAQ policy "
                             "evidence — grounded no-information, no LLM call"))
        services_used.append({"name": "LLMService", "status": "skipped"})
        input_tokens = output_tokens = 0
        llm_latency = 0.0
        llm_failed = False
    else:
        try:
            llm = get_llm_service()
            # Import-eligibility synthesis is a strict deterministic
            # classify->apply-policy task — run it at temperature 0 so the
            # per-component category verdict is stable across identical
            # turns (the multi-product REAL LINE variance). Not a new call.
            _synth_temp = (0.0 if (intent_result["actionable_intent"] == "prohibited_goods"
                                    or all(str(c).endswith("eligibility")
                                           for c in (answer_plan.get("requested_components") or ["x"])))
                            else temperature)
            llm_response = llm.generate(built_prompt.messages, model=OPENAI_CHAT_MODEL,
                                         temperature=_synth_temp, max_tokens=max_tokens)
            answer_text = llm_response.text
            _risk_term = _find_ungrounded_risk_term(answer_text, context)
            if _risk_term:
                stage_detail = (f"model={llm_response.model} — answer discarded: contained "
                                 f"ungrounded term {_risk_term!r} not present in the supplied Context "
                                 f"(Deterministic Grounding Safety Net)")
                answer_text = "ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ในระบบค่ะ"
            else:
                # Deterministic evidence-value fidelity + false-hedge
                # suppression (2026-09-01) — pure post-processing on the
                # model's own text, no extra LLM call. Applied only on
                # this genuine-synthesis path (the deterministic
                # branches above never mutate a value or hedge).
                answer_text = _restore_verbatim_scalar_values(answer_text, context)
                answer_text = _strip_contradictory_noinfo_hedge(answer_text, conf_result.answerability)
                # SEM-GEN-1 — a retrieved chunk must not inject an unstated
                # prohibited-product caveat (see helper). allowed = the
                # current message + the recent user turns.
                _sg1_allowed = (question or "") + " " + " ".join(
                    (t.get("content") or "") for t in (history or [])[-4:]
                    if t.get("role") == "user")
                answer_text = _strip_unsolicited_prohibited_caveat(answer_text, _sg1_allowed)
                # SEM-GEN-1 — for a product/import-interest turn, synthesis
                # must not answer ABOUT a different product it only saw in a
                # RELATED_CONTEXT chunk ("สนใจนำเข้ากล่องพลาสติก" ->
                # "ครีมอาบน้ำจัดเป็นของเหลว…"). If the answer firmly
                # classifies an item whose name is neither the customer's
                # product nor in the message, it is mis-grounded — fall
                # back to the safe P5.1 service ack for the real product.
                if _is_product_import_interest(question):
                    _tgt = _product_interest_noun(question)
                    _wrong = re.search(r"(?P<x>[ก-๙A-Za-z ]{2,20}?)\s*(?:จัดเป็น|เป็น)\s*สินค้าประเภท", answer_text)
                    if (_tgt and _wrong and _wrong.group("x").strip()
                            and _wrong.group("x").strip() not in (question or "")
                            and _wrong.group("x").strip() != _tgt):
                        answer_text, _ = _product_answer_service_continuation(
                            _tgt, lead_stage=lead_stage, sentiment_status=sentiment_status,
                            history=history, transport_known=_pac_transport_known)
                # Final category-verdict hotfix — for a multi-product
                # eligibility answer, firm a hedge on an item the answer
                # itself classified into a Context-prohibited category
                # ("<x>เป็นของเหลว … อาจเข้าข่าย" -> "<x> … ไม่สามารถนำเข้าได้").
                if len(answer_plan.get("requested_components") or []) >= 2 and all(
                        str(c).endswith("eligibility") for c in answer_plan["requested_components"]):
                    answer_text = _firm_prohibited_category_hedge(answer_text, context)
                # P2A blocker — a single-product eligibility answer must not
                # infer "ALLOWED" merely because the product is absent from a
                # prohibited-goods list. Deterministic, no extra LLM call —
                # same family as the two guards above. Replaces only the
                # invented verdict; any P2 follow-up already lives on a
                # separate line and is re-appended.
                if single_elig:
                    _prod = single_elig[0].split(" / ")[0]
                    _pos_verdict = bool(
                        _ABSENCE_ALLOW_RE.search(answer_text)
                        or re.search(rf"{re.escape(_prod)}[^\n]{{0,25}}(?<!ไม่)สามารถนำเข้าได้", answer_text)
                        or re.search(rf"{re.escape(_prod)}[^\n]{{0,20}}(?<!ไม่)นำเข้าได้(ค่ะ|ครับ|นะคะ|\s|$)", answer_text)
                        or re.match(rf"\s*{re.escape(_prod)}\s*(?:จัดเป็น[^\n]{{0,15}})?(?<!ไม่)สามารถนำเข้าได้", answer_text))
                    if _pos_verdict and not _AFFIRMATIVE_PERMISSION_RE.search(context):
                        # No affirmative permission anywhere in the trusted
                        # Context — a positive verdict for this product can
                        # only be an "absent from the list, therefore
                        # allowed" inference. Replace with an honest
                        # unconfirmed answer (perfume/shampoo say
                        # "ไม่สามารถนำเข้า" and never reach here).
                        answer_text = (f"ตอนนี้ยังไม่มีข้อมูลยืนยันว่า{_prod}นำเข้าได้หรือไม่ค่ะ "
                                        f"รบกวนสอบถามเจ้าหน้าที่เพื่อความชัดเจนอีกครั้งนะคะ")
                # P5.1 — this turn is a bare product ANSWER to the
                # assistant's own elicit_product_type question, NOT an
                # explicit "<x>นำเข้าได้ไหม". Unless trusted evidence firmly
                # says the product is PROHIBITED (น้ำหอม -> ของเหลว ->
                # "ไม่สามารถนำเข้า"), continue the Shipify service
                # conversation instead of any eligibility verdict —
                # positive ("สามารถนำเข้าได้"), unconfirmed
                # ("ยังไม่มีข้อมูลยืนยัน") or "not on the list" are all wrong
                # here because the customer never asked about eligibility.
                if _pac_noun:
                    _is_prohibited_verdict = bool(
                        _P51_FIRM_PROHIBITED_RE.search(answer_text or "")
                        and not _P2_UNCONFIRMED_VERDICT_RE.search(answer_text or ""))
                    if not _is_prohibited_verdict:
                        answer_text, _ = _product_answer_service_continuation(
                            _pac_noun, lead_stage=lead_stage, sentiment_status=sentiment_status,
                            history=history, transport_known=_pac_transport_known)
                # P2 follow-up on the synthesis path — "elicit_product_type"
                # was already phrased by the LLM (prompt); this only appends
                # "offer_alternative_product" AFTER a real prohibited verdict
                # (never after the unconfirmed rewrite above).
                answer_text, _p2_note = _apply_p2_followup(answer_text, answer_plan.get("followup"))
                stage_detail = f"model={llm_response.model}" + (
                    f" (P2 {_p2_note})" if _p2_note not in ("not-needed",) else "")
            stages.append(Stage("LLM", "success", (time.time() - t0) * 1000, stage_detail))
            services_used.append({"name": "LLMService", "status": "success"})
            input_tokens, output_tokens = llm_response.input_tokens, llm_response.output_tokens
            llm_latency = llm_response.latency_ms
            llm_failed = False
        except Exception as e:
            stages.append(Stage("LLM", "failed", (time.time() - t0) * 1000, str(e)))
            services_used.append({"name": "LLMService", "status": "failed"})
            answer_text = "ขออภัยค่ะ ระบบขัดข้องชั่วคราว กรุณาลองใหม่อีกครั้งนะคะ"
            input_tokens = output_tokens = 0
            llm_latency = 0.0
            llm_failed = True

    # 6a1. Attachment Planner (services/attachment_planner.py) — decides
    #      whether/which/how-many attachments to send, from the SAME
    #      final evidence set (`context_chunks`) already used for the
    #      prompt — never a separate attachment lookup, never invents a
    #      URL. Runs BEFORE Message Segmentation so the response can be
    #      ordered correctly (text/attachment/instruction). A
    #      clarification turn never sends an attachment — "no irrelevant
    #      attachment" (Part 7's clarification requirement).
    t0 = time.time()
    if dimension_slot_result and not dimension_slot_result["flow_complete"]:
        attachment_plan = {"should_send": False, "selected_attachments": [], "attachment_order": [],
                            "selection_reason": "active slot-filling flow — no attachment until dimensions/weight are complete",
                            "omitted_attachments": []}
    elif answer_plan["clarification_required"]:
        attachment_plan = {"should_send": False, "selected_attachments": [], "attachment_order": [],
                            "selection_reason": "clarification turn — no attachment until the customer answers",
                            "omitted_attachments": []}
    elif slot_filling_active or slot_filling_escalation or slot_filling_complete:
        attachment_plan = {"should_send": False, "selected_attachments": [], "attachment_order": [],
                            "selection_reason": "slot filling turn (follow-up question, escalation, or pending "
                                                 "ERP acknowledgement) — never an evidence-chunk attachment",
                            "omitted_attachments": []}
    elif not (_COMPANY_OPERATIONAL_TOPIC_RE.search(question or "")
              or _CHINA_SOURCED_ACTION_RE.search(question or "")
              or _URGENCY_SIGNAL_RE.search(question or "")
              or _COMPLAINT_SIGNAL_RE.search(question or "")
              or _rag_strong_direct
              or _rag_topic_continuity_followup
              or _rag_faq_exact
              or multi_component_request or (single_elig is not None) or (process_comp is not None)
              or _is_ambiguous_rag_continuity_followup(question, history)
              # SEMANTIC-FIRST-2.1 — the ONE central interpreter named this
              # a PUBLIC company-information family (warehouse / pickup,
              # self-pickup, coupon USAGE, prohibited-goods, charter
              # service, invoice / document). It is a company-knowledge
              # question by MEANING, not general chit-chat — keep it on
              # the company RAG path so an unseen paraphrase with no
              # trusted evidence returns the honest company "no
              # information" answer (Answerability Gate), never a general-
              # chat guess.
              or getattr(interpretation, "intent_family", None) in _PUBLIC_INFO_COMPANY_FAMILIES):
        # General Chat Fallback (see the matching branch above) answers
        # from the LLM's own general knowledge with empty context — the
        # retrieved context_chunks here are whatever (possibly irrelevant)
        # evidence the company RAG pipeline happened to return and were
        # never actually shown to the LLM; attaching one of them (e.g. a
        # company image/file) to a general-chat answer would be exactly
        # as wrong as using it as text evidence.
        attachment_plan = {"should_send": False, "selected_attachments": [], "attachment_order": [],
                            "selection_reason": "general chat mode — no company evidence-chunk attachment",
                            "omitted_attachments": []}
    elif conf_result.answerability == "no_information":
        # Answerability Gate (Task 04B, 2026-08-26) — a safe-fallback
        # answer must never carry an attachment sourced from the very
        # chunks just judged insufficient to answer the question.
        attachment_plan = {"should_send": False, "selected_attachments": [], "attachment_order": [],
                            "selection_reason": "no reliable evidence for this question (Answerability Gate) — "
                                                 "safe fallback, no evidence-chunk attachment",
                            "omitted_attachments": []}
    else:
        attachment_plan = plan_attachments(
            intent_result["actionable_intent"], intent_result["requested_attributes"],
            answer_plan, context_chunks, channel="playground", policy_set=policy_set,
            validated_entities=merged_entities,
        )
    stages.append(Stage("Attachment Planner", "success", (time.time() - t0) * 1000,
                         f"should_send={attachment_plan['should_send']}, "
                         f"{len(attachment_plan['selected_attachments'])} selected — {attachment_plan['selection_reason']}"))

    # 6a2. Message Segmentation (services/message_segmenter.py) — runs
    #     AFTER the LLM answer exists and AFTER policy/escalation is
    #     already fully decided (steps 2/4a above), per the required
    #     order: LLM Answer -> Answer Validation -> Policy Enforcement ->
    #     Attachment Planner -> Message Segmentation -> Channel Sender.
    #     Deterministic, pure Python, no LLM call. `answer_text` itself is
    #     NEVER modified — message_parts is a second, additive field. A
    #     clarification response is always exactly one bubble (never
    #     escalation, per Part 7) regardless of reply_mode.
    t0 = time.time()
    messaging_settings = get_messaging_settings(policy_set)
    is_fallback_answer = llm_failed or conf_result.answerability == "no_information"
    is_clarification = answer_plan["clarification_required"] or (
        dimension_slot_result is not None and not dimension_slot_result["flow_complete"])
    is_slot_filling_turn = slot_filling_active or slot_filling_escalation or slot_filling_complete
    segmented = segment_message(
        answer_text,
        reply_mode=("single" if (is_clarification or is_slot_filling_turn) else messaging_settings["reply_mode"]),
        max_messages=messaging_settings["max_messages"],
        message_delay=messaging_settings["message_delay"],
        is_escalation=(policy.escalate and not is_clarification),
        is_fallback=is_fallback_answer,
        has_attachments=attachment_plan["should_send"],
    )
    stages.append(Stage("Message Segmentation", "success", (time.time() - t0) * 1000,
                         f"{segmented.message_count} part(s) — " + "; ".join(segmented.segment_reasons)))

    # 6a3. Knowledge Gap Handling (Part 10) — Developer-Mode-only signal.
    #      Never shown to the customer, never blocks or rewrites the
    #      answer — this only flags that a company_overview/company_summary
    #      answer is a synthesis from generic service FAQ rows rather than
    #      resting on an explicit company-profile FAQ/heading, so an admin
    #      knows to add one for more stable answers going forward.
    company_profile_warning: Optional[str] = None
    if intent_result["actionable_intent"] in ("company_overview", "company_summary"):
        if not has_strong_company_profile_evidence(chunks):
            company_profile_warning = (
                "Company profile evidence is incomplete. Add an approved company-profile "
                "knowledge item for stable company-overview answers."
            )

    # 7. Formatter — selects which chunks qualify as citation Sources
    #    (rag/evidence_classifier.py), now that the actual answer text
    #    exists to check sentence-level support against. This ONLY
    #    filters which chunks are marked "cited" for display — it never
    #    alters `chunks`, `context`, or anything already sent to the LLM.
    t0 = time.time()
    citation_sources = select_citation_sources(canonical_question, answer_text, chunks)
    # select_citation_sources returns references to the SAME chunk dicts
    # (never copies), so identity comparison works even for
    # structured/calculated chunks that have no chunk_id.
    cited_object_ids = {id(c) for c in citation_sources}
    for c in chunks:
        c["cited"] = id(c) in cited_object_ids
    stages.append(Stage("Formatter", "success", (time.time() - t0) * 1000,
                         f"{len(citation_sources)}/{len(chunks)} chunk(s) selected as citation sources"))

    total_latency = sum(s.duration_ms for s in stages)
    cost = estimate_cost_usd(OPENAI_CHAT_MODEL, input_tokens, output_tokens)

    provider = get_embedding_provider()

    return PlaygroundResult(
        answer=answer_text, chunks=chunks, context=context, prompt=built_prompt, policy=policy,
        stages=stages, model=OPENAI_CHAT_MODEL, embedding_model=provider.model_name(), temperature=temperature,
        input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=total_latency,
        estimated_cost_usd=cost, confidence=confidence, confidence_label=confidence_label,
        answerability=conf_result.answerability,
        raw_vector_similarity=conf_result.raw_vector_similarity,
        hybrid_retrieval_score=conf_result.hybrid_retrieval_score,
        embedding_provider=provider.provider_name, embedding_dimensions=provider.dimensions(),
        embedding_version=provider.version(),
        excluded_candidates=excluded_candidates,
        query_expansion=query_expansion_debug,
        services_used=services_used,
        policy_set_name=policy.policy_set_name,
        context_builder_summary=context_builder_summary,
        retrieval_confidence=retrieval_confidence_result["retrieval_confidence"],
        retrieval_confidence_components=retrieval_confidence_result["components"],
        message_parts=segmented.message_parts,
        message_delay_ms=segmented.delay_ms,
        attachment_order=segmented.attachment_order,
        reply_mode_used=segmented.reply_mode_used,
        segmentation_applied=segmented.segmentation_applied,
        message_count=segmented.message_count,
        segment_reasons=segmented.segment_reasons,
        boundary_types=segmented.boundary_types,
        broad_intent=intent_result["broad_intent"],
        actionable_intent=intent_result["actionable_intent"],
        intent_confidence=intent_result["confidence"],
        requested_attributes=intent_result["requested_attributes"],
        intent_entities=intent_result["entities"],
        answer_plan=answer_plan,
        attachment_plan=attachment_plan,
        selected_attachments=attachment_plan["selected_attachments"],
        conversation_state=conversation_state,
        slot_filling_state=slot_state,
        messages=[{"type": "text", "content": part} for part in segmented.message_parts],
        reply_text=answer_text,
        company_profile_warning=company_profile_warning,
        general_chat_used=general_chat_used,
        unsupported_company_fact=unsupported_company_fact,
    )
