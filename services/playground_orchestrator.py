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
from rag.query_resolution import resolve_conversation
from rag.canonical_query import rewrite_canonical_query
from rag.query_understanding import classify_actionable_intent
from rag.confidence import compute_confidence, confidence_label as _confidence_label_from_score
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
    r"สั่ง|ซื้อ|"
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
    r"form e",
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


def run_playground_turn(
    question: str,
    *,
    template_id: Optional[str] = None,
    top_k: int = 3,
    temperature: float = 0.3,
    max_tokens: int = 500,
    history: Optional[List[Dict]] = None,
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
    intent_result = classify_actionable_intent(canonical_question, entities=merged_entities)
    stages.append(Stage("Intent Classification", "success", (time.time() - t0) * 1000,
                         f"broad={intent_result['broad_intent']}, actionable={intent_result['actionable_intent']} "
                         f"(conf={intent_result['confidence']:.2f})"))

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
        chunks = rag.retrieve(canonical_question, top_k=effective_top_k, trace=retrieval_trace,
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
    conf_result = compute_confidence(chunks)
    confidence = conf_result.answer_confidence
    confidence_label = _confidence_label_from_score(confidence)

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

    # 4e. Answer Planner (services/answer_planner.py) — selects/organizes
    #     which FACT LABELS (never fact values) the LLM should focus on,
    #     from the actionable_intent/entities/final retrieved evidence.
    #     Runs AFTER Context Builder + Retrieval Confidence (needs the
    #     SAME final evidence set the prompt will actually use) and
    #     BEFORE Prompt Builder. Never invents a fact — only picks from
    #     what's already present in `context_chunks`.
    t0 = time.time()
    answer_plan = plan_answer(
        canonical_question, intent_result["actionable_intent"], intent_result["requested_attributes"],
        intent_result["entities"], context_chunks, retrieval_confidence_result["retrieval_confidence"], policy_set,
    )
    stages.append(Stage("Answer Planner", "success", (time.time() - t0) * 1000,
                         f"goal={answer_plan['answer_goal']!r}, shape={answer_plan['response_shape']}"
                         + (", clarification requested" if answer_plan["clarification_required"] else "")))

    # 5. Prompt Builder — needs the retrieved context, so it runs AFTER retrieval.
    # retrieval_confidence (Phase 2 Part 3, computed just above) is passed
    # through so build_prompt() can suppress/summarize conversation
    # history instead of injecting it verbatim (see services/
    # prompt_builder.py's module docstring — the fix for conversation
    # history contaminating RAG answers).
    t0 = time.time()
    context = rag.build_context(context_chunks)
    built_prompt = build_prompt(canonical_question, context, template_id=template_id, policy_notes=policy.notes,
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
    elif not (_COMPANY_OPERATIONAL_TOPIC_RE.search(question or "")
              or _CHINA_SOURCED_ACTION_RE.search(question or "")
              or _URGENCY_SIGNAL_RE.search(question or "")
              or _COMPLAINT_SIGNAL_RE.search(question or "")):
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
    elif conf_result.answerability == "no_information":
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
        answer_text = "ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ"
        if _COMPLAINT_SIGNAL_RE.search(question or ""):
            answer_text = "รับทราบเรื่องที่แจ้งมาค่ะ " + answer_text + " หากมีเลขที่คำสั่งซื้อหรือรายละเอียดเพิ่มเติม รบกวนแจ้งเพิ่มเติมได้เลยค่ะ จะช่วยตรวจสอบให้ค่ะ"
        elif _URGENCY_SIGNAL_RE.search(question or ""):
            answer_text = "เข้าใจว่าเรื่องนี้เร่งด่วนสำหรับคุณค่ะ " + answer_text + " จะติดตามและแจ้งความคืบหน้าให้เร็วที่สุดค่ะ"
        stages.append(Stage("LLM", "skipped", (time.time() - t0) * 1000,
                             "no chunk carries reliable evidence for this question (Answerability Gate) — "
                             "deterministic safe-fallback used, no LLM call, no chunks used as evidence"))
        services_used.append({"name": "LLMService", "status": "skipped"})
        input_tokens = output_tokens = 0
        llm_latency = 0.0
        llm_failed = False
    else:
        try:
            llm = get_llm_service()
            llm_response = llm.generate(built_prompt.messages, model=OPENAI_CHAT_MODEL,
                                         temperature=temperature, max_tokens=max_tokens)
            answer_text = llm_response.text
            _risk_term = _find_ungrounded_risk_term(answer_text, context)
            if _risk_term:
                stage_detail = (f"model={llm_response.model} — answer discarded: contained "
                                 f"ungrounded term {_risk_term!r} not present in the supplied Context "
                                 f"(Deterministic Grounding Safety Net)")
                answer_text = "ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ในระบบค่ะ"
            else:
                stage_detail = f"model={llm_response.model}"
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
              or _COMPLAINT_SIGNAL_RE.search(question or "")):
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
    )
