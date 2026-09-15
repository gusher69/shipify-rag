# -*- coding: utf-8 -*-
"""SEM-GEN-1 — conversational semantic backbone.

A small, reusable layer that lets the Decision Engine understand a SHORT
follow-up inside an ONGOING import-interest conversation without a
product dictionary or per-phrase rules:

    "สนใจนำเข้ารองเท้า"      -> frame: IMPORT_INTEREST, product=รองเท้า
    "ถ้าเป็นกางเกงล่ะ"       -> CHANGE_TARGET  product -> กางเกง
    "เสื้อยืดล่ะ"            -> CHANGE_TARGET  product -> เสื้อยืด
    "200 ตัว"               -> SET_QUANTITY   quantity = 200
    "ไม่ใช่ 200 เอา 300"     -> CORRECT_QUANTITY quantity -> 300
    "ถ้าทางเรือล่ะ"          -> CHANGE_METHOD  method = sea (product/qty kept)
    "งั้นถามเรื่องคูปองดีกว่า" -> CHANGE_TOPIC   topic = coupon (frame dropped)

Split of responsibilities:
  * THIS module classifies MEANING / STATE only. It never authorizes
    private data and never invents a policy fact.
  * A CHANGE_TARGET is rewritten to the canonical "สนใจนำเข้า<product>"
    and handed back to the normal pipeline, so the existing FIX-2.3 /
    prohibited-goods eligibility path decides the business answer — the
    prohibited-product policy is NOT baked into target recognition.

The open-vocabulary step (what product/topic a bare follow-up names) is
a single, tightly-gated structured call to the EXISTING LLM service
(services/llm_service.py) — no new framework, no per-turn LLM call: it
runs only when a deterministic gate already established (a) an active
IMPORT_INTEREST frame in recent history and (b) the current message has
a follow-up shape. On any error / unparseable output it degrades to
op=UNKNOWN and the caller falls back to ordinary routing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional

from services.link_conversion_flow import (
    is_link_conversion_signal as _is_link_conversion_signal,
    classify_link_request as _classify_link_request,
)

_RESOLVER_MODEL = "gpt-4o-mini"

# ── deterministic frame derivation ────────────────────────────────────
# An assistant product-acknowledgement reply ("รับทราบค่ะ เป็น<X>นะคะ",
# "รับทราบค่ะ สนใจนำเข้า<X>") is how the frame's product is carried across
# turns (same "reply wording IS the state" pattern the P2 purpose markers
# use). Never a persisted column, so no migration and no stale-state bug.
# product carriers in the assistant's own reply. Deliberately no digits
# in the class and a keyword veto (`_NOT_A_PRODUCT_RE`) so the frame-
# acknowledgement's own "…จำนวนประมาณ 200 ชิ้น…" wording is never read
# back as the product.
_ASSIST_PRODUCT_RES = (
    re.compile(r"รับทราบค่ะ\s*เป็น(?P<p>[ก-๙A-Za-z ]{2,28}?)นะคะ"),
    re.compile(r"\(สินค้า\s*(?P<p>[ก-๙A-Za-z ]{2,28}?)\s*(?:•|\)|จำนวน|ขนส่ง)"),
    re.compile(r"สนใจนำเข้า(?P<p>[ก-๙A-Za-z ]{2,28}?)(?:นะคะ|ค่ะ|$)"),
    # OWNER-REAL-LINE-FIX-06 — natural-prose frame_ack_reply carriers
    # (no state-token parenthetical). Lazy, and anchored so ordinary
    # sentences ("ขอบคุณสำหรับข้อมูลนะคะ") never match.
    re.compile(r"ต้องการนำเข้า(?P<p>[ก-๙A-Za-z ]{2,28}?)(?=\s*(?:จำนวน|ขนส่ง|นะคะ|ค่ะ|😊|$))"),
    re.compile(r"เปลี่ยนเป็น(?P<p>[ก-๙A-Za-z ]{2,28}?)ได้เลย(?:ค่ะ|เลย)?"),
    re.compile(r"(?:ชิ้น|ทางรถ|ทางเรือ|ทางอากาศ)\s*สำหรับ(?P<p>[ก-๙A-Za-z ]{2,28}?)นะคะ"),
)
_NOT_A_PRODUCT_RE = re.compile(
    r"จำนวน|ชิ้น|ตัว|ประมาณ|ขนส่ง|ทางรถ|ทางเรือ|ทางอากาศ|กิโล|น้ำหนัก|ราคา|บริการ|Shipify", re.IGNORECASE)
# LANGGRAPH UPGRADE (cross-turn continuity) — a product carrier above can
# also match the assistant's own REQUEST for the product, because the ask
# ("รบกวนแจ้งชื่อหรือประเภทสินค้าที่สนใจนำเข้าด้วยนะคะ") ends in the same
# "สนใจนำเข้า…นะคะ" shape as the acknowledgement it was modelled on. That
# made the NEXT turn read product="ด้วย" — the frame remembering a
# particle out of its own question, which then looked like a filled slot
# and skipped the product ask entirely. A captured span that is nothing
# but grammatical particles is never a product name; this is the same
# "only when the WHOLE remnant is filler" rule the two product-noun
# extractors already apply, stated for the frame reader.
_PARTICLE_ONLY_PRODUCT_RE = re.compile(
    r"^(?:ด้วย|หน่อย|เลย|อีก|นะ|น่ะ|ค่ะ|คะ|ครับ|คับ|จ้า|เพิ่มเติม|โดยประมาณ|"
    r"ที่สนใจ|ที่ต้องการ|อะไร|ไหน|บ้าง|ๆ|\s)+$")


def _is_product_carrier_span(span: str) -> bool:
    """True when a captured product-carrier span is a real product noun
    rather than the assistant's own phrasing."""
    s = (span or "").strip()
    return bool(s) and not _NOT_A_PRODUCT_RE.search(s) \
        and not _PARTICLE_ONLY_PRODUCT_RE.match(s)
# PHASE 6 closure gate D/H — ONE count-unit vocabulary for the whole
# module (see the PHASE-6-SLOT-CONSUMPTION note below for why). Defined
# here, ahead of the first regex that substitutes it.
_COUNT_UNIT_ALT = ("ตัว|ชิ้น|ชิน|อัน|ใบ|คู่|ชุด|กล่อง|ขวด|โหล|แพ็ค|แพก|แพค|ลัง|ผืน|"
                   "หลัง|เครื่อง|พาเลท|pcs?")

# PHASE 6 POST-DEPLOY (defect class C) — also capture the UNIT the
# acknowledgement carried, so the typed quantity survives a round trip
# through the conversation exactly like the product and method already do
# ("reply wording IS the state"). The unit group is optional: an older
# acknowledgement with no unit still parses, and the digits group is
# unchanged, so every existing read-back keeps working.
_ASSIST_QTY_RE = re.compile(r"จำนวน(?:ประมาณ)?\s*(?P<q>\d{1,7})\s*(?P<u>"
                            + _COUNT_UNIT_ALT + r")?")
_ASSIST_METHOD_RE = re.compile(r"ขนส่งทาง(?P<m>รถ|เรือ|อากาศ)")

#
# PHASE-6-SLOT-CONSUMPTION — generalized the unit vocabulary (added
# ขวด/พาเลท) and dropped the trailing `\b`: a Thai count-unit that ends in
# a bare combining tone/vowel mark ("คู่" = ค + ู + ่, the LAST character a
# non-word Mn mark) never satisfied `\b` when followed by end-of-string
# OR a space, because a word boundary needs a transition INTO/OUT OF a
# `\w` character and Python's `\w` does not count standalone combining
# marks — so "20 คู่" as the very LAST word, or followed only by a space
# ("20 คู่ ส่งเรือ"), silently failed to match at all, while "20 คู่อยาก…"
# (more text glued right on, no space) happened to work (the boundary
# exists between the mark and the very next real letter). No trailing
# anchor is needed here in the first place: this pattern only ever fires
# on an explicit DIGIT + unit-word span, so even the one case a trailing
# boundary might have guarded against — a longer, unrelated compound word
# starting right after the unit ("20 คู่กางเกง") — is itself the CORRECT
# split (quantity=20/unit=คู่, remnant "กางเกง" left for the product noun),
# never a false positive.
# PHASE 6 closure gate D/H — ONE count-unit vocabulary for the whole
# module. Four separate copies of this alternation had drifted apart
# (only this one carried "ขวด"/"พาเลท"), so a bottle quantity was
# recognised by the opener and invisible to the bare-slot-answer
# matchers. Every site below now substitutes this constant, so a new
# unit can only ever be added in one place.

_USER_QTY_RE = re.compile(
    r"(?:ประมาณ\s*)?(?P<q>\d{1,7})\s*(" + _COUNT_UNIT_ALT + r")",
    re.IGNORECASE)
# PHASE-6-SLOT-CONSUMPTION — added the bare "ส่ง<mode>"/"โดย<mode>"
# phrasings ("ส่งเรือ", "ส่งรถ") alongside the existing "ทาง<mode>" ones;
# same vocabulary P1's own services/conversation_resolution.py::_METHOD_RE
# recognizes (kept as a separate literal here, not an import, because
# conversation_resolution.py itself imports FROM this module — importing
# back would be circular).
# LANGGRAPH UPGRADE — ONE shipping-method vocabulary for the module, the
# same consolidation _COUNT_UNIT_ALT already applies to count units. The
# bare-method ANSWER matcher below is built from this same alternation,
# so a phrasing the opener understands can never be one the slot answer
# does not ("ส่งเรือครับ" was recognised as frame material by
# derive_active_frame and NOT as a follow-up by is_frame_followup).
_METHOD_WORD_ALT = ("ทางรถ|ทางเรือ|ทางอากาศ|ทางเครื่องบิน|โดยรถ|โดยเรือ|"
                    "โดยเครื่องบิน|ส่งเรือ|ส่งรถ")
_METHOD_WORD_RE = re.compile(_METHOD_WORD_ALT)
# a bare method ANSWER: an optional verb, a method from the ONE
# vocabulary above (or the bare mode noun), an optional polite particle.
# THAI-HUMAN-LANGUAGE — the tail also accepts a feasibility question
# ("ส่งเรือได้ปะ", "ทางเรือได้ไหมครับ"): in reply to the assistant's own
# "ทางรถหรือทางเรือ", naming a mode and asking whether it is possible IS
# choosing that mode. The tail is a sequence so stacked particles work.
_BARE_METHOD_ANSWER_RE = re.compile(
    r"^\s*(?:ส่ง|เอา|ขอ|ไป|เป็น|ใช้)?\s*(?:" + _METHOD_WORD_ALT
    + r"|รถ|เรือ|เครื่องบิน)\s*"
    r"(?:ก็ได้|ได้(?:ไหม|มั้ย|ปะ|ป่ะ|หรอ|เหรอ)?|ล่ะ|มั้ย|ไหม|ครับ|ค่ะ|คะ|คับ|นะ|เลย|จ้า|\s)*$",
    re.IGNORECASE)

# ── ENTITY SPAN vs QUESTION / ACTION SPAN ─────────────────────────────
# PHASE 6 POST-DEPLOY (defect class A — entity boundary / multi-intent).
# A Thai turn routinely declares an ENTITY and asks an INTERROGATIVE /
# ACTION question in one breath ("อยากสั่งรองเท้าจากจีน 30 คู่ ส่งเรือ
# ราคาเท่าไหร่"). Thai has no word spaces, so a product-noun extractor
# that only strips filler words returns "รองเท้าราคาเท่าไหร่" — the
# question welded onto the product, which is then echoed back to the
# customer.
#
# The separation implemented here is STRUCTURAL, not a phrase table. A
# question/action clause is a CONTIGUOUS RUN at one end of the message
# built ENTIRELY out of closed-class material — interrogatives, generic
# predicates/auxiliaries, generic attribute/measure/place nouns,
# transport-mode spans, conjunctions, particles, digits, punctuation —
# that contains at least one interrogative. Any CONTENT word terminates
# the run, and a product name is by definition a content word, so the
# rule generalises to wordings nobody enumerated and can never eat a
# product name. The run must reach the very end (or start) of the
# message, which is what makes a product noun sitting at the boundary
# self-protecting.
#
# Deliberately ABSENT from both lists:
#   * "ของ" / "สินค้า" — legitimate formants inside real compound product
#     nouns ("ชั้นวางของ", "กล่องใส่ของ"); same reason _FIX23_STRIP_RE
#     leaves them alone.
#   * every COUNT UNIT in _COUNT_UNIT_ALT — a run must not be able to
#     swallow a quantity span.
#   * bare "รถ" / "เรือ" — each can be a product in its own right, so
#     only the full transport-MODE spans ("ทางรถ", "โดยเรือ") qualify.
_QC_INTERROGATIVE_ALT = (
    "เท่าไหร่|เท่าไร|เท่าใด|กี่|ยังไง|ยังงัย|อย่างไร|"
    # THAI-HUMAN-LANGUAGE — the colloquial yes/no particles the permission
    # recogniser (_ACT_PERMIT) already accepts are question markers here
    # too, so "ส่งเรือได้ปะ" and "ส่งเรือได้ไหม" split identically.
    "ไหม|มั้ย|มัย|หรือเปล่า|รึเปล่า|หรือไม่|ป่าว|ปะ|ป่ะ|หรอ|เหรอ|หรือยัง|รึยัง|"
    "เมื่อไหร่|เมื่อไร|ที่ไหน|ตรงไหน|อันไหน|แบบไหน|ไหน|อะไร|ทำไม")
_QC_CLAUSE_FILLER_ALT = (
    # generic predicates / auxiliaries
    "คำนวณ|คำนวน|ประเมิน|ตีราคา|คิด|เสีย|จ่าย|ชำระ|"
    "จัดส่ง|ขนส่ง|ส่ง|ถึง|ใช้|มี|เป็น|ได้|ต้อง|ควร|รับ|ทำ|นับ|รวม|"
    # generic attribute / measure / place nouns
    "ค่าใช้จ่าย|ค่าส่ง|ค่าขนส่ง|ค่านำเข้า|ค่า|ราคา|เรท|"
    "ระยะเวลา|เวลา|วัน|นาน|บาท|กิโลกรัม|กิโล|กก|โล|kg|น้ำหนัก|คิว|cbm|"
    "ประเทศไทย|ไทย|จีน|ปลายทาง|หน้าบ้าน|บ้าน|"
    # transport MODE spans only (never a bare "รถ"/"เรือ")
    "ทางรถ|ทางเรือ|ทางอากาศ|ทางเครื่องบิน|โดยรถ|โดยเรือ|โดยเครื่องบิน|เครื่องบิน|"
    # conjunctions / particles
    "และ|กับ|หรือ|แล้ว|บ้าง|เลย|ด้วย|หน่อย|อีก|คือ|"
    "ครับ|ค่ะ|คะ|คับ|ขอรับ|นะ|น่ะ|อ่ะ|จ๊ะ|จ้า|หรอ|เหรอ|ล่ะ|ละ|ๆ")


def _qc_alt(alt: str) -> str:
    """Longest-alternative-first, so "ทางเรือ" is never matched as the
    shorter "ทาง…" prefix of something else."""
    return "|".join(sorted(alt.split("|"), key=len, reverse=True))


_QC_INTERROGATIVE_RE = re.compile("(?:" + _qc_alt(_QC_INTERROGATIVE_ALT) + ")", re.IGNORECASE)
_QC_TOKEN_RE = re.compile(
    "(?:" + _qc_alt(_QC_INTERROGATIVE_ALT) + "|" + _qc_alt(_QC_CLAUSE_FILLER_ALT)
    + r"|\d+(?:[.,]\d+)?|[\s,.!?ฯ\-–_]" + ")", re.IGNORECASE)


def _qc_run_end(s: str, i: int) -> "tuple[int, bool]":
    """Consume a maximal run of closed-class clause tokens from i.
    Returns (end_index, saw_interrogative)."""
    seen_q = False
    while i < len(s):
        m = _QC_TOKEN_RE.match(s, i)
        if not m:
            break
        if _QC_INTERROGATIVE_RE.fullmatch(m.group(0)):
            seen_q = True
        i = m.end()
    return i, seen_q


def split_question_clause(text: str) -> "tuple[str, str]":
    """Split a turn into (entity_span, question_action_span).

    The question/action span is the maximal closed-class run containing
    an interrogative at the END of the message (the overwhelmingly
    common Thai order), or failing that at the START. When the whole
    message is question material there is no entity to protect and the
    text is returned unsplit.
    """
    s = text or ""
    n = len(s)
    if not s.strip():
        return s, ""
    for i in range(1, n):                       # earliest cut = longest clause
        if s[i].isspace():
            continue
        end, seen_q = _qc_run_end(s, i)
        if end >= n and seen_q and s[:i].strip():
            return s[:i], s[i:]
    end, seen_q = _qc_run_end(s, 0)             # leading question clause
    if seen_q and 0 < end < n and s[end:].strip():
        return s[end:], s[:end]
    return s, ""

# import / product interest reused from the FIX-2.3 recognizer so the
# frame opens on exactly the turns FIX-2.3 already treats as import
# interest — no second definition.
#
# PHASE-6B — this MUST be a lazy import. `services.playground_orchestrator`
# imports `PUBLIC_INFO_FAMILIES` from THIS module at its own load time, so
# a module-level `from services.playground_orchestrator import …` here is
# circular: when conversation_semantics is imported first (the normal
# order — decision_engine imports it before playground_orchestrator) the
# import raised ImportError and silently fell back to a stub that always
# returned False, killing deterministic IMPORT_INTEREST recognition in
# production (Part-2 T03/T04). Resolving the recognizers on first CALL —
# by which time playground_orchestrator has finished loading — fixes that
# without touching either module's public surface.
_IMPORT_RECOGNIZERS: Dict[str, object] = {}


def _import_recognizers():
    if not _IMPORT_RECOGNIZERS:
        try:  # pragma: no cover - import guard
            from services.playground_orchestrator import (
                _is_product_import_interest as _p,
                _product_interest_noun as _n,
            )
            _IMPORT_RECOGNIZERS["is"] = _p
            _IMPORT_RECOGNIZERS["noun"] = _n
        except Exception:  # pragma: no cover
            _IMPORT_RECOGNIZERS["is"] = lambda _q: False
            _IMPORT_RECOGNIZERS["noun"] = lambda _q: None
    return _IMPORT_RECOGNIZERS


def _is_import_interest(q: str) -> bool:
    return bool(_import_recognizers()["is"](q or ""))


def _classify_cancellation(q: str) -> Optional[str]:
    """Lazy bridge to the ONE cancellation discriminator (see
    services/operational_change_flow.py::classify_cancellation). Lazy for
    the same reason as _import_recognizers: import order safety, never a
    second copy of the rule."""
    try:
        from services.operational_change_flow import classify_cancellation
    except Exception:  # pragma: no cover - import guard
        return None
    return classify_cancellation(q or "")


def _import_noun(q: str):
    return _import_recognizers()["noun"](q or "")

# a turn that clearly leaves the import frame (the customer is now asking
# about something unrelated). Used only to EXPIRE a stale frame, never to
# route.
_FRAME_EXIT_RE = re.compile(
    r"คูปอง|coupon|วอลเล็ท|wallet|ยอดเงิน|ติดตาม|แทรค|แทร็ก|เลขพัสดุ|บิลขนส่ง|สถานะ|"
    r"ที่อยู่โกดัง|เบอร์ติดต่อ|สมัคร|ลงทะเบียน|ยกเลิก|คืนเงิน|ร้องเรียน|"
    # OWNER-REAL-LINE-FIX-05 — a bare abandon / "never mind" also expires
    # the active import frame, so the NEXT turn is not treated as in-frame.
    r"ไม่เอาแล้ว|ไม่เอาละ|ไม่เอาล่ะ|ไม่ต้องแล้ว|พอแล้ว|ไม่สนแล้ว|เลิกแล้ว", re.IGNORECASE)

_FRAME_MAX_LOOKBACK = 14
_FRAME_MAX_GAP = 3  # off-topic-weight after the frame opened -> it is stale


@dataclass
class Frame:
    intent: str = "IMPORT_INTEREST"
    product: Optional[str] = None
    quantity: Optional[int] = None
    # PHASE 6 POST-DEPLOY (defect class C — slot unit preservation) — the
    # COUNT UNIT the customer supplied alongside the quantity. It used to
    # be recognised by the quantity parser and then thrown away, so every
    # acknowledgement rendered the generic fallback ("20 คู่" -> "20 ชิ้น")
    # and the customer saw their own words replaced. A quantity is a
    # TYPED value: number + unit.
    unit: Optional[str] = None
    weight: Optional[str] = None
    dimensions: Optional[str] = None
    method: Optional[str] = None

    def as_dict(self) -> Dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _method_label(word: str) -> Optional[str]:
    if "เรือ" in word:
        return "sea"
    if "รถ" in word:
        return "road"
    if "อากาศ" in word or "เครื่องบิน" in word:
        return "air"
    return None


def derive_active_frame(history: Optional[List[Dict]]) -> Optional[Frame]:
    """Deterministically reconstruct the current IMPORT_INTEREST frame
    from recent conversation, or None when there is no active frame (or
    it has gone stale behind unrelated turns). No LLM."""
    turns = list(history or [])[-_FRAME_MAX_LOOKBACK:]
    if not turns:
        return None

    # 1. find the most recent frame-open: a user import-interest turn OR
    #    an assistant product-acknowledgement.
    open_idx = None
    open_product = None
    for i in range(len(turns) - 1, -1, -1):
        t = turns[i]
        content = t.get("content") or ""
        if t.get("role") == "user" and _is_import_interest(content):
            open_idx, open_product = i, _import_noun(content)
            break
        if t.get("role") == "assistant":
            for rx in _ASSIST_PRODUCT_RES:
                m = rx.search(content)
                if m and _is_product_carrier_span(m.group("p")):
                    open_idx, open_product = i, m.group("p").strip()
                    break
            # THAI-HUMAN-LANGUAGE (cross-turn continuity) — the platform's
            # own QUANTITY / METHOD acknowledgement ("รับทราบ จำนวนประมาณ
            # 200 ลังนะคะ … รบกวนแจ้งชื่อสินค้า") opens the frame exactly
            # like a product acknowledgement does: reply wording IS the
            # state, and a frame that only opened on a product ack lost a
            # confidently acknowledged quantity the moment the customer
            # answered the product question ("กล่องพลาสติกครับ" -> re-ask).
            if open_idx is None and (_ASSIST_QTY_RE.search(content)
                                     or _ASSIST_METHOD_RE.search(content))                     and _assistant_asked_for_product([t]):
                open_idx = i
            if open_idx is not None:
                break
    if open_idx is None:
        return None

    # 2. staleness — user turns AFTER the open that are neither a follow-up
    #    shape nor frame material; a hard subject switch weighs double, and
    #    the immediately-preceding user turn being a hard switch expires it.
    drift = 0
    last_user = None
    for t in turns[open_idx + 1:]:
        if t.get("role") != "user":
            continue
        c = (t.get("content") or "").strip()
        last_user = c
        if is_frame_followup(c) or _USER_QTY_RE.search(c) or _METHOD_WORD_RE.search(c):
            continue
        drift += 2 if _FRAME_EXIT_RE.search(c) else 1
    if drift >= _FRAME_MAX_GAP:
        return None
    if last_user and _FRAME_EXIT_RE.search(last_user):
        return None

    # 3. accumulate product / quantity / method across the whole window
    #    (newest wins).
    product = open_product
    quantity = method = unit = None
    for t in reversed(turns):
        c = t.get("content") or ""
        role = t.get("role")
        if product is None and role == "assistant":
            for rx in _ASSIST_PRODUCT_RES:
                m = rx.search(c)
                if m and _is_product_carrier_span(m.group("p")):
                    product = m.group("p").strip()
                    break
        if quantity is None:
            mq = (_ASSIST_QTY_RE.search(c) if role == "assistant" else _USER_QTY_RE.search(c))
            if mq:
                quantity = int(mq.group("q"))
                unit = (mq.group("u") if role == "assistant" else mq.group(2)) or None
        if method is None:
            mm = (_ASSIST_METHOD_RE.search(c) if role == "assistant" else None)
            if mm:
                method = _method_label(mm.group("m"))

    # LANGGRAPH UPGRADE (cross-turn continuity) — an import journey that
    # has a QUANTITY or a METHOD but not yet a product is still an ACTIVE
    # frame. Requiring a product here is what broke the customer's own
    # reported sequence:
    #
    #     "20 คู่อยากสั่งของจากจีน"   -> ack + "which product?"
    #     "รองเท้าครับ"               -> no frame existed, so the answer
    #                                    attached to nothing and the turn
    #                                    fell through to "ตอนนี้ยังไม่มี
    #                                    ข้อมูลยืนยันเรื่องนี้ค่ะ"
    #
    # i.e. exactly "เปลี่ยนบริบท AI ตอบไม่ได้". The frame now survives with
    # product=None, so the quantity the customer already gave is still
    # known on the next turn and the product answer has a journey to join.
    # Every existing consumer guards on `frame.product` before using the
    # frame as a product context, so a product-less frame changes no
    # routing decision — it only stops the KNOWN slots being forgotten.
    if product is None and quantity is None and method is None:
        return None
    return Frame(product=product, quantity=quantity, unit=unit, method=method)


# ── deterministic follow-up-shape gate ────────────────────────────────
_FOLLOWUP_SHAPE_RES = (
    re.compile(r"(ล่ะ|หละ|ละ)\s*(คะ|ครับ|ค่ะ)?\s*$"),
    re.compile(r"^\s*ถ้า(เป็น)?"),
    re.compile(r"^\s*แล้ว(ถ้า|เป็น)?"),
    re.compile(r"^\s*(ประมาณ\s*)?\d{1,7}\s*(" + _COUNT_UNIT_ALT
               + r"|กก\.?|กิโล|kg)?\s*$", re.IGNORECASE),
    re.compile(r"ไม่ใช่\s*\S+\s*(เอา|เป็น)\s*\S+"),
    re.compile(r"งั้น.*(ดีกว่า|แทน|แล้วกัน)"),
    re.compile(r"เปลี่ยน(ไป|เป็น)?(ถาม|เรื่อง)"),
    _BARE_METHOD_ANSWER_RE,
    # OWNER-REAL-LINE-FIX-05 — a slot-CORRECTION shape ("เปลี่ยนเป็น X",
    # "เอา X แทน", "เอ้ย <n>", "แก้เป็น X"). Typo-tolerant: เปลี่ยน~เปลียน,
    # เป็น~เปน~เป้น. Deliberately NOT matched: a fresh "สนใจนำเข้า X"
    # statement, a topic switch ("ขอเบอร์ติดต่อ"), a policy ask.
    re.compile(r"เปล[ีิ]?[่้]?ย?น\S{0,3}(?:เป[็้]?น|ไป|ของ|มัน)"
               r"|(?<![ก-๙])เอา.{1,20}?แทน(?![ก-๙])"
               r"|^\s*เอ[้๊่]?ย[\s\d]"
               r"|(?:^|[\s,]|ขอ|ช่วย)แก้\S{0,3}เป[็้]?น"),
)
_FOLLOWUP_MAX_LEN = 36


def is_frame_followup(message: str) -> bool:
    t = (message or "").strip()
    if not t or len(t) > _FOLLOWUP_MAX_LEN:
        return False
    return any(rx.search(t) for rx in _FOLLOWUP_SHAPE_RES)


# ── gated structured LLM resolver ─────────────────────────────────────
_OPS = ("CHANGE_TARGET", "SET_QUANTITY", "CORRECT_QUANTITY",
        "CHANGE_METHOD", "CHANGE_TOPIC", "CONTINUE", "UNKNOWN")

_SYS_PROMPT = (
    "You classify ONE short Thai follow-up message inside an ongoing "
    "product-import conversation. Output STRICT JSON only, no prose.\n"
    "Schema: {\"op\": <one of "
    + "|".join(_OPS) + ">, \"product\": <string|null>, "
    "\"quantity\": <integer|null>, \"method\": <\"road\"|\"sea\"|\"air\"|null>, "
    "\"topic\": <string|null>}\n"
    "Rules:\n"
    "- CHANGE_TARGET: the message names a DIFFERENT product/goods to import "
    "(e.g. \"ถ้าเป็นกางเกงล่ะ\", \"เสื้อยืดล่ะ\", \"หมวกล่ะ\"). Put the bare "
    "product noun in \"product\". Any goods noun is valid — never refuse an "
    "unfamiliar product.\n"
    "- SET_QUANTITY: a first quantity (\"200 ตัว\"). CORRECT_QUANTITY: replaces "
    "a stated quantity (\"ไม่ใช่ 200 เอา 300\"). Put the NEW number in \"quantity\".\n"
    "- CHANGE_METHOD: names a shipping method (\"ถ้าทางเรือล่ะ\"). Set \"method\".\n"
    "- CHANGE_TOPIC: the customer switches to an unrelated subject "
    "(\"งั้นถามเรื่องคูปองดีกว่า\"). Put a short topic word in \"topic\".\n"
    "- CONTINUE: stays in the frame but adds no new slot.\n"
    "- UNKNOWN: cannot tell.\n"
    "Classify MEANING ONLY. Never output a policy statement, an eligibility "
    "verdict, or any personal data."
)


def resolve_followup(message: str, frame: Frame) -> Dict:
    """Single gated LLM call. Returns {op, product, quantity, method,
    topic}. Never raises — degrades to op=UNKNOWN."""
    out = {"op": "UNKNOWN", "product": None, "quantity": None, "method": None, "topic": None}
    try:
        from services.llm_service import get_llm_service
        user = ("active_frame=" + json.dumps(frame.as_dict(), ensure_ascii=False)
                + "\nfollow_up=" + json.dumps(message or "", ensure_ascii=False))
        resp = get_llm_service().generate(
            [{"role": "system", "content": _SYS_PROMPT},
             {"role": "user", "content": user}],
            model=_RESOLVER_MODEL, temperature=0.0, max_tokens=120)
        raw = (resp.text or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return out
        data = json.loads(raw[start:end + 1])
    except Exception as e:  # pragma: no cover - network/parse degradation
        print(f"[conversation_semantics] resolver degraded ({e!r}) -> UNKNOWN")
        return out

    op = str(data.get("op") or "").strip().upper()
    if op not in _OPS:
        return out
    out["op"] = op
    prod = data.get("product")
    if isinstance(prod, str) and 1 < len(prod.strip()) <= 30:
        out["product"] = prod.strip()
    q = data.get("quantity")
    if isinstance(q, (int, float)) and 0 < int(q) < 10_000_000:
        out["quantity"] = int(q)
    meth = str(data.get("method") or "").strip().lower()
    if meth in ("road", "sea", "air"):
        out["method"] = meth
    topic = data.get("topic")
    if isinstance(topic, str) and topic.strip():
        out["topic"] = topic.strip()[:40]

    # deterministic safety net: a CHANGE_TARGET with no usable product is
    # not actionable.
    if out["op"] == "CHANGE_TARGET" and not out["product"]:
        out["op"] = "UNKNOWN"
    if out["op"] in ("SET_QUANTITY", "CORRECT_QUANTITY") and out["quantity"] is None:
        mq = _USER_QTY_RE.search(message or "") or re.search(r"(\d{1,7})", message or "")
        if mq:
            out["quantity"] = int(mq.group(1))
        else:
            out["op"] = "UNKNOWN"
    return out


_METHOD_TH = {"road": "ทางรถ", "sea": "ทางเรือ", "air": "ทางอากาศ"}


# PHASE 6 POST-DEPLOY — ONE wording for "what the rate actually depends
# on", shared by the frame slot ladder and by the RAG product-interest
# continuation, so a customer who asked about price is moved toward a
# real answer with the same sentence either way.
ASK_WEIGHT_FOR_RATE = "รบกวนแจ้งน้ำหนักโดยประมาณเพิ่มเติมได้ไหมคะ"
# LANGGRAPH UPGRADE — the product ask is now a NAMED constant so the
# detector that recognises the customer's answer to it
# (_ASSISTANT_ASKED_PRODUCT_RE) can be parity-tested against the exact
# sentence the platform actually sends. The two had silently drifted.
ASK_PRODUCT_SLOT = "รบกวนแจ้งชื่อหรือประเภทสินค้าที่สนใจนำเข้าด้วยนะคะ"


def frame_ack_reply(frame: Frame, *, changed: str) -> str:
    """Deterministic acknowledgement that re-states the frame IN NATURAL
    PROSE so the next turn's derive_active_frame() can read it back.
    OWNER-REAL-LINE-FIX-06 — no "(สินค้า X)" / state-token parenthetical
    is ever shown to the customer; the product / quantity / method are
    carried in plain sentence form that _ASSIST_PRODUCT_RES /
    _ASSIST_QTY_RE / _ASSIST_METHOD_RE parse. Never a policy claim, never
    an eligibility verdict."""
    p = frame.product or "สินค้า"
    # the supplied unit wins; "ชิ้น" is the fallback ONLY when the
    # customer never stated one.
    _u = frame.unit or "ชิ้น"
    qty = f" จำนวนประมาณ {frame.quantity} {_u}" if frame.quantity else ""
    # "ขนส่งทาง…" (NOT "ส่งทาง…") so the trailing "สนใจส่งทางรถหรือทางเรือ"
    # ask clause is never mis-read as a chosen method.
    mth = f" ขนส่ง{_METHOD_TH.get(frame.method, frame.method)}" if frame.method else ""
    # EVERY variant re-states ALL known slots in plain prose so
    # derive_active_frame() can read product / quantity / method back
    # without any "(สินค้า X)" state token.
    if changed == "quantity":
        head = f"รับทราบค่ะ ปรับเป็นจำนวนประมาณ {frame.quantity} {_u} สำหรับ{p}นะคะ"
        if mth:
            head += f"{mth} ตามเดิมค่ะ"
    elif changed == "method":
        head = f"รับทราบค่ะ เปลี่ยนเป็นขนส่ง{_METHOD_TH.get(frame.method, frame.method)} สำหรับ{p}นะคะ"
        if qty:
            head += f"{qty} ตามเดิมค่ะ"
    elif changed == "product":
        head = f"ได้ค่ะ เปลี่ยนเป็น{p}ได้เลยค่ะ 😊"
        if qty or mth:
            head += f"{qty}{mth} ตามเดิมนะคะ"
    elif changed == "none" and not frame.product:
        # PHASE-6-SLOT-CONSUMPTION — a quantity/method-only opener with NO
        # product yet must never claim "ต้องการนำเข้า<placeholder>" — the
        # "สินค้า" placeholder would otherwise satisfy _ASSIST_PRODUCT_RES
        # on the NEXT turn and derive_active_frame() would read the
        # generic word back as if it were a real, confirmed product name.
        # Acknowledge only what IS known (quantity/method); the ask below
        # covers product.
        detail = f"{qty}{mth}"
        head = f"ได้ค่ะ รับทราบ{detail}นะคะ 😊" if detail else "ได้ค่ะ 😊"
    else:  # "none"
        head = f"ได้ค่ะ รับทราบว่าต้องการนำเข้า{p}{qty}{mth}นะคะ 😊"
    # PHASE-6-SLOT-CONSUMPTION — product is the FIRST missing slot to ask
    # for, ahead of quantity/method/weight. A correction call (changed in
    # ("quantity", "method", "product")) always already has frame.product
    # set by the time it reaches here (the frame it corrects already had
    # an open product) — this only changes behaviour for the "none"
    # (fresh-turn) case, where a quantity-only opener must not re-ask a
    # quantity it just gave while product is still genuinely unknown.
    ask = ""
    if not frame.product:
        ask = " " + ASK_PRODUCT_SLOT
    elif not frame.quantity:
        ask = " รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"
    elif not frame.method:
        ask = " สนใจส่งทางรถหรือทางเรือคะ"
    elif not frame.weight:
        ask = " " + ASK_WEIGHT_FOR_RATE
    return f"{head}{ask}"


# ── OWNER-REAL-LINE-FIX-05 — deterministic active-frame correction ────
# A short CORRECTION / REJECTION / AMBIGUOUS turn inside an active
# IMPORT_INTEREST journey must be resolved against the FRAME before any
# generic RAG / general knowledge. This is the offline / degraded / typo
# path (the gated LLM resolve_followup() is tried first in production).
# Typo-tolerant, per PHASE-6D (no fuzzy-fix at intent time):
#   เปลี่ยน ~ เปลียน ~ เปลี่ยน   เป็น ~ เปน ~ เป้น   ใช่ ~ ไช่
_FZ_CHANGE_VB = r"เปล[ีิ]?[่้]?ย?น"
_FZ_BE_TH = r"เป[็้]?น"

# any substitution / correction SHAPE (a swap of a slot value).
_FRAME_CORRECTION_SHAPE_RE = re.compile(
    _FZ_CHANGE_VB
    + r"|(?<![ก-๙])เอา\S{0,20}?แทน(?![ก-๙])"
    + r"|(?<![ก-๙])เอา\s*" + _FZ_BE_TH + r"\s*[ก-๙]"     # "เอาเป็น X" (take-as, no แทน)
    + r"|(?:^|[\s,])เอ[้๊่]?ย(?:[\s\d,]|$)"
    + r"|(?:^|[\s,])แก้\S{0,3}" + _FZ_BE_TH
    + r"|(?:^|\s)ไม[่้]?(?:ใช่|ไช่)\s*\S+.{0,4}(?:" + _FZ_BE_TH + r"|เอา)",
    re.IGNORECASE)

# a bare abandon / "never mind" — cancels the active journey.
_FRAME_CANCEL_RE = re.compile(
    r"^\s*(?:ไม่เอา(?:แล้ว|ละ|ล่ะ)?|ไม่ต้อง(?:แล้ว|ละ)?|พอ(?:แล้ว|ก่อน)|"
    r"ยกเลิก(?:เลย|ก่อน|รายการ)?|เลิก(?:แล้ว|ก่อน)|หยุด(?:ก่อน)?|ไม่สนแล้ว|"
    r"ช่างมัน|ไม่อยากได้แล้ว)\s*(?:ค่ะ|คะ|ครับ|คับ|นะ|น่ะ|จ้า|จ้ะ)?\s*$",
    re.IGNORECASE)

# a change request with NO stated target ("เปลี่ยนได้ไหม", "ขอเปลี่ยนหน่อย").
_FRAME_CORRECTION_NO_TARGET_RE = re.compile(
    r"^\s*(?:ขอ|อยาก|จะ|ช่วย)?\s*" + _FZ_CHANGE_VB + r"\S{0,3}(?:ได้|ไหว)?\S{0,3}"
    r"(?:ไหม|มั้ย|มัย|ป่าว|บ่|หรอ|เหรอ|หน่อย|ด้วย)?\s*(?:ค่ะ|คะ|ครับ|คับ|นะ)?\s*$",
    re.IGNORECASE)

# nouns after a change marker that are NOT an import product (they belong
# to address / account / logistics changes, handled by their own flows).
_NOT_A_CORRECTION_PRODUCT_RE = re.compile(
    r"^(?:ที่อยู่|ที่ส่ง|ปลายทาง|ผู้รับ|เบอร์|ชื่อ|บัญชี|รหัส|โกดัง|วิธี|บิล|ออเดอร์|"
    r"ที่รับ|จุดรับ|สาขา|ธนาคาร|พาสเวิร์ด|รหัสผ่าน|การจัดส่ง|การชำระ)")

_BRAND_TOKEN_RE = re.compile(r"(?<![A-Za-z])(SP|FT)(?![A-Za-z])|เอสพี|เอฟที", re.IGNORECASE)
_TH_BRAND_WORD = {"เอสพี": "SP", "เอฟที": "FT"}


def _frame_correction_product(t: str, current: Optional[str]) -> Optional[str]:
    """The bare goods noun a substitution turn points to, or None.
    Takes the span AFTER the last change / be / take marker."""
    s = (t or "").strip()
    markers = list(re.finditer(
        _FZ_CHANGE_VB + r"(?:ของ|สินค้า|มัน)?\s*(?:" + _FZ_BE_TH + r")?"
        + r"|(?<![ก-๙])เอา\s*(?:" + _FZ_BE_TH + r")?"
        + r"|(?<![ก-๙])" + _FZ_BE_TH
        + r"|^\s*เอ[้๊่]?ย",
        s))
    if not markers:
        return None
    tail = s[markers[-1].end():].strip()
    for _ in range(4):
        t2 = re.sub(
            r"[\s,]*(?:แทน|ได้ไหม|ได้มั้ย|ได้ป่าว|ได้บ่|มั้ย|ไหม|ดีกว่า|หน่อย|ด้วย|"
            r"เลย|ค่ะ|คะ|ครับ|คับ|นะคะ|นะ|น่ะ|จ้า|จ้ะ|แล้ว)\s*$", "", tail).strip()
        if t2 == tail:
            break
        tail = t2
    tail = re.sub(r"\s+", "", tail)
    if not (2 <= len(tail) <= 26) or not _THAI_CHAR_RE.search(tail):
        return None
    if (any(c.isdigit() for c in tail) or _NEGATION_STOPWORD_RE.search(tail)
            or _METHOD_WORD_RE.search(tail) or _NOT_A_CORRECTION_PRODUCT_RE.search(tail)):
        return None
    if current and tail == current:
        return None
    return tail


def resolve_frame_correction(message: str, frame: Optional["Frame"]) -> Dict:
    """Deterministic resolution of a correction / rejection / ambiguous
    turn against an active frame. Returns {op, product, quantity, method,
    brand}; op in CHANGE_TARGET / CORRECT_QUANTITY / CHANGE_METHOD /
    CHANGE_BRAND / REJECT / AMBIGUOUS / UNKNOWN. Never raises."""
    out = {"op": "UNKNOWN", "product": None, "quantity": None, "unit": None,
           "method": None, "brand": None, "method_was_unset": False}
    t = (message or "").strip()
    if not t or len(t) > 48 or frame is None or not getattr(frame, "product", None):
        return out
    if _FRAME_CANCEL_RE.match(t):
        out["op"] = "REJECT"
        return out
    # a bare quantity answer ("20 คู่", "ประมาณ 300 ชิ้น") fills the slot.
    # COUNT units only — a bare weight ("10 กิโล") is not a quantity.
    _bareq = re.fullmatch(r"\s*(?:ประมาณ\s*)?(\d{1,7})\s*"
                          r"(" + _COUNT_UNIT_ALT + r")?\s*"
                          r"(?:ค่ะ|คะ|ครับ|คับ|นะ)?\s*", t, re.IGNORECASE)
    if _bareq:
        out["op"], out["quantity"] = "SET_QUANTITY", int(_bareq.group(1))
        # PHASE 6 POST-DEPLOY (defect class C) — a corrected quantity
        # brings its OWN unit with it ("เอา 5 ลัง" after "20 คู่"), so the
        # acknowledgement echoes the unit the customer just used, not the
        # one from the superseded value.
        out["unit"] = _bareq.group(2) or None
        return out
    # LANGGRAPH UPGRADE — a bare method ANSWER ("ส่งเรือครับ", "ทางรถค่ะ")
    # when no method is set yet is a SET, exactly parallel to the bare
    # quantity answer above. Only the SWAP case existed, so answering the
    # assistant's own "สนใจส่งทางรถหรือทางเรือคะ" resolved to nothing and
    # the journey dead-ended on a no-information reply.
    if not getattr(frame, "method", None) and _BARE_METHOD_ANSWER_RE.match(t):
        _lab = _method_label(t)
        if _lab:
            out["op"], out["method"] = "CHANGE_METHOD", _lab
            # the op name stays CHANGE_METHOD so every existing consumer
            # keeps working; this flag lets the acknowledgement say
            # "noted" rather than "changed to" for a FIRST-time answer.
            out["method_was_unset"] = True
            return out
    # OWNER P1 — a shipping-method the customer already picked, re-named
    # to a DIFFERENT one ("ทางเรือดีกว่า", "เอาทางเรือ", "ไม่เอา เอาทางรถ"),
    # is a method correction even without an explicit "เปลี่ยน" verb. Only
    # a short, non-question turn (never "ส่งทางเรือกี่วัน").
    if (getattr(frame, "method", None) and len(t) <= 32
            and not re.search(r"กี่|เท่าไหร่|ไหม|มั้ย|ยังไง|อย่างไร|\?|วัน|บาท|กิโล|นาน", t)):
        _m2 = re.search(r"ทางรถ|ทางเรือ|ทางอากาศ|ทางเครื่องบิน|โดยรถ|โดยเรือ|โดยเครื่องบิน", t)
        if _m2:
            _lab = _method_label(_m2.group(0)) or (
                "sea" if "เรือ" in _m2.group(0) else "road" if "รถ" in _m2.group(0)
                else "air" if ("อากาศ" in _m2.group(0) or "บิน" in _m2.group(0)) else None)
            if _lab and _lab != frame.method:
                out["op"], out["method"] = "CHANGE_METHOD", _lab
                return out
    if not _FRAME_CORRECTION_SHAPE_RE.search(t):
        return out
    cur_product = frame.product if frame else None
    # shipping-method swap ("เปลี่ยนเป็นทางเรือ", "เอาทางรถแทน")
    if _METHOD_WORD_RE.search(t) or re.search(
            r"(?<![ก-๙])(?:ทางรถ|ทางเรือ|ทางอากาศ|ทางเครื่องบิน)(?![ก-๙])", t):
        mth = _method_label(t)
        if mth:
            out["op"], out["method"] = "CHANGE_METHOD", mth
            return out
    prod = _frame_correction_product(t, cur_product)
    # quantity swap ("เอ้ย 20", "แก้เป็น 8 อัน", "ไม่ใช่ 5 เป็น 8") — only
    # when no product noun. For an "A เป็น B" shape the NEW value is the
    # number AFTER the last "เป็น"/"เอา" marker, else the last number.
    if not prod and not re.search(r"\d\s*(?:กิโล|กก\.?|โล|กรัม|ตัน|kg|g\b|ซม|cm|มม|mm|เมตร|นิ้ว)", t):
        _nums = list(re.finditer(r"(\d{1,7})", t))
        if _nums:
            _split = None
            for _mm in re.finditer(r"เป[็้]?น|เอา", t):
                _split = _mm.end()
            _after = [m for m in _nums if _split is not None and m.start() >= _split]
            _pick = (_after[-1] if _after else _nums[-1] if _split is not None else _nums[0])
            out["op"], out["quantity"] = "CORRECT_QUANTITY", int(_pick.group(1))
            return out
        # brand swap ("เอ้ย FT", "ไม่ใช่ SP เป็น FT") — the LAST brand
        # token is the correction target.
        mbs = list(_BRAND_TOKEN_RE.finditer(t))
        if mbs:
            b = (mbs[-1].group(1) or mbs[-1].group(0)).upper()
            out["op"], out["brand"] = "CHANGE_BRAND", _TH_BRAND_WORD.get(b.lower(), b)
            return out
    if prod:
        out["op"], out["product"] = "CHANGE_TARGET", prod
        return out
    if _FRAME_CORRECTION_NO_TARGET_RE.match(t):
        out["op"] = "AMBIGUOUS"
    return out


# ═════════════════════════════════════════════════════════════════════
# SEMANTIC-FIRST-1 — central conversational semantic interpretation
# ═════════════════════════════════════════════════════════════════════
# GENERALISES this module from an import-interest follow-up backbone into
# the ONE semantic interpretation layer the Decision Engine consults
# FIRST, before any workflow-local text handling. Every existing
# conversational business flow (shipment status, invoice, warehouse /
# pickup, self-pickup, coupon, product policy, charter truck, shipping
# estimate, address change, import interest, public/private, short
# follow-ups / corrections / comparisons / topic changes) consumes the
# normalised {intent_family, entities, is_private, follow_up_op} contract
# `interpret()` produces — they no longer each re-classify the raw Thai.
#
# It is a COMPOSITIONAL meaning model, NOT a phrase list: a small set of
# orthogonal marker dimensions (OBJECT / ACTION / ROLE / FOLLOW-UP) is
# detected independently and COMPOSED into a family, so paraphrases that
# share meaning collapse to the same family without an exact-phrase rule.
# A single gated LLM call (same pattern as resolve_followup) only
# disambiguates genuinely novel / ambiguous phrasing and always degrades
# back to the deterministic result. Genuinely structural inputs (bare
# ids, URLs, numeric-only, empty, platform postbacks) skip semantics.
#
# Security / authorization / business truth stay deterministic: is_private
# is a pure self-reference test and NO policy verdict or eligibility fact
# is ever produced here.

INTENT_FAMILIES = (
    "SHIPMENT_STATUS", "INVOICE", "PICKUP_LOCATION", "SELF_PICKUP",
    "COUPON_USAGE", "MY_COUPONS", "PRODUCT_POLICY", "CHARTER_TRUCK",
    "SHIPPING_ESTIMATE", "ADDRESS_CHANGE", "IMPORT_INTEREST",
    "LINK_CONVERSION", "PURCHASE_WITHDRAWAL", "SHIPPING_WITHDRAWAL",
    # PHASE 6 POST-DEPLOY (defect class B) — cancellation is TWO
    # families, never one and never the withdrawal family.
    "CANCELLATION_POLICY", "CANCELLATION_OPERATION",
    # PHASE-6B — pre-RAG conversational / service-intent families. These
    # are NEVER a knowledge-base fact question, so a "KB has no chunk"
    # result must never end the journey for one (customer "แก้ไขเคส
    # Shipify Part 2", Acceptance Criteria: KB_NOT_FOUND ≠ conversation
    # cannot continue).
    "HELP_INTENT", "SERVICE_DISCOVERY", "MONEY_TRANSFER_INTEREST",
    "WEBSITE_LINK_REQUEST", "CONTACT_INFO", "WAREHOUSE_INBOUND_JOURNEY",
    "GENERAL", "UNKNOWN",
)

FOLLOWUP_OPS = ("CORRECTION", "COMPARISON", "TOPIC_CHANGE", "CONTINUE",
                "SET_VALUE", "NONE")

# PHASE-6B — a conversation ACT the CURRENT message performs on the
# dialogue itself, orthogonal to its intent family. REJECT covers the
# customer rejecting / correcting / clarifying the assistant's previous
# answer ("ไม่ใช่อันนี้", "ไม่ได้ถามแบบนั้น", "คุณไม่เข้าใจ", "หมายถึง…").
# The Decision Engine uses it to re-evaluate the turn and BREAK a
# repeated-answer loop instead of resending the same reply.
CONVERSATION_ACTS = ("NONE", "REJECT")


@dataclass
class Interpretation:
    """The normalised semantic contract every conversational flow reads."""
    intent_family: str = "UNKNOWN"
    entities: Dict[str, object] = field(default_factory=dict)
    is_private: bool = False
    follow_up_op: str = "NONE"
    confidence: float = 0.0
    source: str = "none"          # structural | deterministic | llm | degraded
    conversation_act: str = "NONE"  # NONE | REJECT  (PHASE-6B)

    def as_dict(self) -> Dict:
        d = asdict(self)
        d["entities"] = {k: v for k, v in (self.entities or {}).items() if v not in (None, "", [])}
        return d


# ── orthogonal meaning markers ───────────────────────────────────────
# OBJECT — what the message is ABOUT.
_OBJ_INVOICE = re.compile(r"ใบกำกับ|ใบเสร็จ|ใบแจ้งหนี้|tax\s*invoice|ภาษีมูลค่าเพิ่ม|\bvat\b|ออกบิลภาษี|เอกสารภาษี", re.IGNORECASE)
_OBJ_WAREHOUSE = re.compile(r"โกดัง|คลังสินค้า|คลังไทย|คลังจีน|จุดรับ|จุดรับของ|จุดส่ง|ที่รับของ|ที่รับสินค้า|โรงพัก|สาขา|warehouse", re.IGNORECASE)
_OBJ_COUPON = re.compile(r"คูปอง|ส่วนลด|โค้ดส่วนลด|coupon|voucher|โปรโมชั่นส่วนลด", re.IGNORECASE)
_OBJ_TRUCK = re.compile(r"เหมารถ|เหมา\s*รถ|รถเหมา|เรียกรถ|จ้างรถ|รถส่งต่อ|เหมาคันรถ|charter\s*truck", re.IGNORECASE)
_OBJ_COST = re.compile(r"ค่าส่ง|ค่าขนส่ง|ค่านำเข้า|ค่าจัดส่ง|เรทส่ง|เรทนำเข้า|ราคาส่ง|ค่าระวาง|shipping\s*cost", re.IGNORECASE)
_OBJ_PARCEL = re.compile(r"พัสดุ|ออเดอร์|order|ล็อตสินค้า|กล่องสินค้า|ของที่สั่ง|ของที่ส่ง|สินค้าที่สั่ง|ของผม|ของฉัน|สินค้าผม|บิลผม|บิลฉัน|เลขบิลผม", re.IGNORECASE)
_OBJ_ADDRESS = re.compile(r"ที่อยู่จัดส่ง|ที่อยู่ผู้รับ|ที่อยู่ในการจัดส่ง|ปลายทางจัดส่ง|delivery\s*address|ที่อยู่ส่งของ|ที่อยู่|ปลายทาง|ผู้รับ|เบอร์ผู้รับ", re.IGNORECASE)

# CUSTOMER-RED-REAL-FAIL-1 (RED-5/RED-6) — Ai.xlsx CUS-S05/CUS-S12: a
# withdraw-money request against the customer's own account. Two
# DISTINCT workflows (purchase-order credit vs. shipping-payment
# credit) that must never collapse into each other or into an
# unrelated add_vat / self-pickup / cancellation flow. Checked as its
# own decisive verb+object composite, same shape as every other family
# here — never a bare keyword-in-message check.
_WITHDRAWAL_VERB_RE = re.compile(
    r"ถอนเงิน|ถอนเครดิต|ถอนยอด|จะถอนยังไง|จะถอนมายังไง|ถอนได้ไหม|ถอนยังไง|ถอนออกมา|ขอถอน",
    re.IGNORECASE)
_PURCHASE_WITHDRAWAL_OBJ_RE = re.compile(r"สั่งซื้อ|ร้าน.{0,6}คืน|เครดิตสั่งซื้อ", re.IGNORECASE)
_SHIPPING_WITHDRAWAL_OBJ_RE = re.compile(r"ขนส่ง|ค่าส่ง|ค่าขนส่ง|เครดิตขนส่ง", re.IGNORECASE)

# ── PHASE-6B pre-RAG conversational / service-intent markers ──────────
# Compositional & anchored, never a bare keyword scan. They fire only
# when the deterministic tier found NO other actionable family, so an
# ordinary "ใช้คูปองยังไง" / "บิลผมถึงไหน" is untouched.
#
# HELP_INTENT — a generic "I need help / want to ask something" with no
# concrete subject yet. Must be OPENED (ask what they need), never sent
# to KB_NOT_FOUND.
_HELP_INTENT_RE = re.compile(
    r"^\s*(?:อยากได้|ต้องการ|ขอ|อยาก)?\s*(?:ความ)?ช่วยเหลือ(?:หน่อย|ด้วย|ที)?\s*(?:ครับ|ค่ะ|คะ|ด้วย|หน่อย)?\s*$"
    r"|ขอความช่วยเหลือ|ต้องการความช่วยเหลือ|อยากได้ความช่วยเหลือ"
    r"|^\s*ช่วย(?:หน่อย|ที|ด้วย)\S*\s*$"
    r"|สอบถามหน่อย|สอบถามหน่อยครับ|สอบถามหน่อยค่ะ|อยากสอบถาม|รบกวนสอบถาม|มีเรื่องอยากสอบถาม"
    r"|มีเรื่อง(?:อยาก|จะ)?(?:ถาม|สอบถาม|ปรึกษา)|มีคำถาม(?:อยาก|จะ)?ถาม|มีอะไร(?:อยาก|จะ)?ถาม"
    r"|ปรึกษาหน่อย|มีอะไรให้ช่วย(?:ไหม|มั้ย)?"
    r"|(?:อยากได้|ขอ)คำแนะนำ|ขอถามอะไร(?:หน่อย)?|ขอถามหน่อย|มีอะไรจะถาม",
    re.IGNORECASE)

# SERVICE_DISCOVERY — "what do you offer" / broad "import-export" topic /
# generic "interested in using the service".
_SERVICE_DISCOVERY_RE = re.compile(
    r"มีบริการอะไร|บริการอะไรบ้าง|ให้บริการอะไร|รับทำอะไรบ้าง|ทำอะไรได้บ้าง|บริการมีอะไร"
    r"|มีบริการไหนบ้าง|ช่วยอะไรได้บ้าง|บริการของ\S{0,10}มีอะไร"
    r"|(?:บริการ|ที่นี่|ที่นี้)\S{0,6}มีไร(?:มั่ง|บ้าง)|มีไรมั่ง|ทำอะไรได้(?:บ้าง|มั่ง)|ทำอะไรมั่ง"
    # colloquial "อะไร" -> "ไร" contraction on the two discovery shapes
    r"|(?:มีบริการ|บริการ)\S{0,3}ไร\S{0,3}(?:บ้าง|มั่ง)|ช่วย(?:อะ)?ไร\S{0,3}ได้\S{0,3}บ้าง"
    r"|สนใจใช้บริการ|อยากใช้บริการ|สนใจบริการ"
    r"|การนำเข้าส่งออก|นำเข้าส่งออก|นำเข้า-ส่งออก|นำเข้าและส่งออก|import\s*[/-]?\s*export",
    re.IGNORECASE)

# MONEY_TRANSFER_INTEREST — wants to SEND / pay money to a China shop
# (the OPPOSITE direction of a withdrawal). "ฝากโอน" is Shipify's own
# name for this service. Never matches "ถอนเงิน".
_MONEY_TRANSFER_RE = re.compile(
    r"โอนเงิน(?:ให้|ไป(?:ให้)?|ไปที่|ไปยัง)?\s*(?:ร้าน|โรงงาน|เจ้าของร้าน|ผู้ขาย|ร้านค้า|supplier|ซัพพลายเออร์)"
    r"|โอน(?:เงิน)?(?:ให้)?ร้าน(?:ค้า)?(?:ที่)?จีน"
    r"|ฝากโอน(?:เงิน)?"
    r"|จ่ายเงินให้ร้าน(?:ค้า)?(?:ที่)?จีน|ชำระเงินให้ร้าน(?:ค้า)?(?:ที่)?จีน"
    r"|โอนเงินไป(?:ที่)?จีน|โอนหยวน(?:ให้)?(?:ร้าน)?|เติมเงินร้านจีน|โอนค่าสินค้าให้ร้าน",
    re.IGNORECASE)

# WEBSITE_LINK_REQUEST — the customer wants a platform's WEBSITE /
# homepage / browse URL, NOT a product-link conversion. Decisive
# difference: no actual https?:// URL present and no explicit "แปลงลิงก์"
# verb (either of those is always a conversion, handled elsewhere).
_WEBSITE_LINK_RE = re.compile(
    r"ลิงก์(?:เว็บ(?:ไซต์)?|หน้าเว็บ|เข้าเว็บ|หน้าหลัก|หน้าแรก)"
    r"|(?:ขอ|เอา|มี|ส่ง|อยากได้|ต้องการ|ขอดู)\s*(?:ลิงก์|ลิงค์|link|url)\s*(?:ของ\s*)?(?:เว็บ|เว็บไซต์|หน้าเว็บ)"
    r"|ลิงก์(?:ที่จะ)?(?:เข้าไป)?(?:ดู|เลือก|ช้อป|เปิด)(?:ของ|สินค้า|เว็บ)?"
    r"|ขอ(?:ที่อยู่)?เว็บไซต์(?:ของ)?\s*(?:taobao|tmall|1688|เถาเป่า|ทีมอลล์|อาลีบาบา)?"
    r"|(?:เว็บไซต์|เว็บ|url)(?:ของ)?\s*(?:taobao|tmall|1688|เถาเป่า|ทีมอลล์)(?![ก-๙\w])"
    r"|ลิงก์\s*(?:เว็บ\s*)?(?:taobao|tmall|1688|เถาเป่า|ทีมอลล์)\s*(?:และ|กับ|,|/)"
    r"|เว็บ(?:ไซต์)?\s*(?:taobao|tmall|1688|เถาเป่า|ทีมอลล์)\S{0,14}(?:เข้า(?:ยังไง|ไง)|ขอลิงก์|ขอ url)"
    r"|(?:เข้าเว็บ|ลิงก์เว็บ)\s*(?:taobao|tmall|1688|เถาเป่า|ทีมอลล์)",
    re.IGNORECASE)

# CONTACT_INFO — a PUBLIC company contact request (channels / phone /
# email / LINE / company website / company address). Never an
# identity-gated ERP lookup.
_CONTACT_INFO_RE = re.compile(
    r"ติดต่อ(?:ได้)?(?:ทาง|ช่องทาง|ยัง|ที่|ตรง)?ไหน|ช่องทาง(?:การ)?ติดต่อ|ติดต่อ\S{0,6}ช่องทางไหน"
    r"|ติดต่อ\s*(?:shipify|บริษัท|แอดมิน|เจ้าหน้าที่|ฝ่าย\S{0,10})?\s*(?:ยังไง|อย่างไร|ทางไหน|ช่องทางไหน)"
    r"|ขอ\s*(?:เบอร์(?:โทร)?|โทรศัพท์|อีเมล|อีเมล์|เมล|e-?mail|ไลน์|line\s*id|line|ไอดีไลน์|ไอดี\s*line|เว็บไซต์บริษัท|ที่อยู่บริษัท|แฟนเพจ|เพจ|ช่องทางติดต่อ)"
    r"|มี(?:ไลน์|line|เพจ|แฟนเพจ)\s*(?:ไหม|มั้ย|หรือเปล่า)"
    r"|เบอร์(?:โทร)?(?:ติดต่อ|บริษัท|แอดมิน|ฝ่าย\S{0,10})|อีเมล(?:ติดต่อ|บริษัท|ของบริษัท)"
    r"|(?:อยาก|ต้องการ)ติดต่อ(?:แอดมิน|เจ้าหน้าที่|บริษัท|shipify)\S{0,8}(?:ทำไง|ยังไง|ทำยังไง|อย่างไร)?",
    re.IGNORECASE)

# WAREHOUSE_INBOUND_JOURNEY — the "my supplier will ship to your China
# warehouse" journey (identification of whose parcel it is / pre-arrival
# notification requirement / arrival contact-back). KB has no chunk for
# any of these today, so the honest answer is "no confirmed info + Human
# CS" — NOT a nearby FAQ (warehouse address, MOQ) and NOT a notification
# ACTION.
_WAREHOUSE_INBOUND_JOURNEY_RE = re.compile(
    r"(?:ร้าน|โรงงาน|ผู้ขาย|ซัพพลายเออร์|supplier)\S{0,18}ส่ง(?:ของ|สินค้า|พัสดุ)?\S{0,14}(?:ไป|เข้า|มา)?(?:ที่)?(?:คลัง|โกดัง|warehouse)"
    r"|ส่ง(?:ของ|สินค้า|พัสดุ)?\S{0,10}(?:ไป|เข้า)(?:ที่)?(?:คลัง|โกดัง)(?:จีน|ไทย)?"
    r"|มีของ\S{0,10}(?:จะ)?(?:ไป|เข้า)?ส่ง(?:ที่)?(?:คลัง|โกดัง)"
    r"|(?:ของ|พัสดุ|สินค้า)\S{0,8}(?:จะ|ไป)?ถึง(?:ที่)?(?:คลัง|โกดัง)"
    r"|จะมีของ\S{0,10}(?:ไป|เข้า)(?:ส่ง)?(?:ที่)?(?:คลัง|โกดัง)",
    re.IGNORECASE)
_WH_WHOSE_PARCEL_RE = re.compile(
    r"รู้ได้(?:ยัง)?ไง|รู้ได้อย่างไร|ระบุ(?:ได้)?(?:ยังไง|อย่างไร)?|เป็นของใคร|ของใคร"
    r"|ลูกค้าคนไหน|เป็นลูกค้าคนไหน|แยก(?:ของ)?(?:ยังไง|ออกยังไง)|ของลูกค้าคนไหน|เป็นพัสดุของใคร",
    re.IGNORECASE)
_WH_PRE_NOTIFY_RE = re.compile(
    r"ต้องแจ้ง|ต้องบอก|แจ้งอะไร(?:ไหม|บ้าง|มั้ย)?|ต้องลงทะเบียน|ต้องแจ้งล่วงหน้า|แจ้งล่วงหน้า"
    r"|ต้องทำอะไร(?:ก่อน)?(?:ไหม)?\S{0,6}(?:ส่ง|มีของ)",
    re.IGNORECASE)
_WH_ARRIVAL_CONTACT_RE = re.compile(
    r"(?:ถึง|มาถึง|ของถึง|ส่งถึง|ถึงคลัง|ถึงโกดัง)[^\n]{0,24}?(?:ติดต่อกลับ|แจ้งกลับ|ติดต่อผม|แจ้งผม|"
    r"ติดต่อ\S{0,4}(?:ไหม|มั้ย|หรือเปล่า|รึเปล่า)|แจ้ง\S{0,4}(?:ไหม|มั้ย|หรือเปล่า|รึเปล่า)|บอก\S{0,4}(?:ไหม|มั้ย))"
    r"|(?:ติดต่อกลับ|แจ้งกลับ)[^\n]{0,10}?(?:ไหม|มั้ย|หรือเปล่า)"
    r"|(?:ติดต่อ|แจ้ง|บอก)(?:กลับ)?[^\n]{0,10}?(?:ไหม|มั้ย|หรือเปล่า)[^\n]{0,12}?(?:ของ|พัสดุ)?\s*ถึง"
    r"|จะรู้(?:ได้)?(?:ยัง)?ไง[^\n]{0,10}?(?:ว่า)?(?:ของ|พัสดุ)?\s*ถึง",
    re.IGNORECASE)

# REJECT act — the customer pushes back on / clarifies the previous
# answer. Deliberately broad on MEANING but anchored so it does not fire
# on an ordinary "ไม่" answer to a yes/no question that is itself the
# task (a REJECT only matters when there IS a previous assistant answer —
# the Decision Engine gates on that).
_REJECT_ACT_RE = re.compile(
    r"^\s*ไม่ใช่(?:อันนี้|แบบนี้|แบบนั้น|อันนั้น|ค่ะ|ครับ|คับ|สิ|เลย|นะ|น่ะ)?\s*(?:[,\.]|\s|$|ขอ|เอา|หมายถึง|ที่|จะ)"
    r"|^\s*ไม่(?:ค่ะ|ค่า|ครับ|คับ|นะ)\b"
    r"|ไม่ได้(?:ถาม|หมายความ|จะถาม|อยากถาม|ต้องการ)\s*(?:แบบ|อย่าง|อัน)?(?:นั้น|นี้|งั้น)?"
    r"|(?:คุณ|เอไอ|ai|บอท|ระบบ)?\s*(?:เข้าใจผิด|ไม่เข้าใจ(?:ลูกค้า|คำถาม|ที่ถาม|ที่พิมพ์)?|ฟังไม่เข้าใจ|ตอบไม่ตรง(?:คำถาม)?|ไม่ตรงคำถาม|ตอบผิด|ผิดประเด็น|คนละเรื่อง|ตอบไม่ตรงที่ถาม)"
    r"|หมายถึง(?:ว่า)?\s*\S"
    r"|ที่(?:ถาม|หมายถึง|จะถาม)(?:ก็)?คือ",
    re.IGNORECASE)

# ACTION — what they want DONE.
_ACT_LOCATE = re.compile(r"ที่ไหน|ตรงไหน|อยู่ไหน|ที่ใด|ที่ตั้ง|แผนที่|พิกัด|เส้นทางไป|ไปยังไง|แถวไหน|ย่านไหน|โซนไหน|เขตไหน|อยู่แถว|\bwhere\b", re.IGNORECASE)
_ACT_STATUS = re.compile(r"ถึงไหน|ถึงหรือยัง|ถึงไทย|ถึงจีน|มาถึงยัง|ไปถึงไหน|สถานะ|คืบหน้า|อัปเดต|อัพเดท|เป็น(?:ยัง)?ไงบ้าง|ออกจากจีนยัง|ส่งของ(?:ให้)?\S{0,6}(?:หรือ)?ยัง|เช็ก\S{0,4}สถานะ|เช็คสถานะ|ตรวจสอบสถานะ|ติดตามพัสดุ|ติดตามสินค้า", re.IGNORECASE)
_ACT_ISSUE_GET = re.compile(r"ออก\S{0,6}(?:ได้ไหม|ให้|หรือเปล่า|หรือไม่)|ออกให้ได้|ขอ\S{0,3}(?:ใบ|เอกสาร)|มี\S{0,10}(?:ไหม|มั้ย)|ให้\S{0,6}(?:หรือเปล่า|ไหม|มั้ย)|issue|provide", re.IGNORECASE)
_ACT_HOWTO = re.compile(r"ใช้\S{0,6}(?:ยังไง|อย่างไร|ตรงไหน|ที่ไหน)|วิธีใช้|กดตรงไหน|กดยังไง|กดใช้\S{0,4}(?:ตรงไหน|ยังไง)|ทำยังไง|ขั้นตอน\S{0,6}ใช้|how\s*to\s*use", re.IGNORECASE)
_ACT_LIST_MINE = re.compile(r"มี\S{0,10}อะไรบ้าง|มี\S{0,6}(?:กี่|เท่าไหร่)|เหลือ\S{0,6}(?:ไหม|เท่าไหร่|กี่)|ของผม\S{0,12}(?:มี|เหลือ)|บัญชีผม|บัญชีฉัน|ในระบบผม", re.IGNORECASE)
# LANGGRAPH UPGRADE — the colloquial question particles "หรอ" / "เหรอ" /
# "ป่ะ" are the spoken-Thai equivalents of "หรือเปล่า" and appear
# constantly in real LINE ("กระต่ายนำเข้าได้หรอ"). Completing the particle
# vocabulary in the ONE place a permission question is recognised
# generalises to every family that reads it — the opposite of adding a
# rule per phrase.
_ACT_PERMIT = re.compile(
    r"ได้ไหม|ได้มั้ย|ได้มัย|ได้ป่าว|ได้ป่ะ|ได้ปะ|ได้บ่|ได้หรือเปล่า|ได้รึเปล่า|"
    r"ได้หรือไม่|ได้หรอ|ได้เหรอ|สามารถ\S{0,24}ได้|\bcan\s+i\b|allowed",
    re.IGNORECASE)
# an explicit CALCULATE / ESTIMATE verb — distinct from a bare "how much"
# price question (that stays a rate FAQ, per CUSTOMER-CALC-1).
_ACT_CALC_VERB = re.compile(r"คำนวณ|คำนวน|ประเมิน|ช่วยคิด|คิดค่า|คิดราคา|ตีราคา|estimate|calculate|quote", re.IGNORECASE)
_PRICE_Q_RE = re.compile(r"เท่าไหร่|เท่าไร|กี่บาท|ราคาเท่า|ราวๆ\s*กี่|แพงไหม|แพงมั้ย|แพงรึเปล่า|แพงไหมคะ|ราคาสูงไหม", re.IGNORECASE)

# PHASE-6B-REAL — a DIMENSIONS TRIPLE: three x/×/*-separated numbers, each
# with an optional glued unit (mm/cm/m/นิ้ว/มม/ซม). With or without a
# weight or a transport method, this is structurally a shipping-cost
# CALCULATOR input and nothing else. Recognised deterministically so a
# stale conversation context can never let the gated LLM reclassify it —
# real LINE: "520mm x 220mm x 110mm ส่งทางเรือ หนัก 2 กิโล" sent right
# after a link conversion was read as LINK_CONVERSION and dead-ended in
# SAFE_FALLBACK instead of computing ~57 บาท.
_DIMS_TRIPLE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:มม\.?|mm|ซม\.?|cm|เซน\S*|ม\.?|m|นิ้ว|inch(?:es)?)?\s*[x×*]\s*"
    r"\d+(?:\.\d+)?\s*(?:มม\.?|mm|ซม\.?|cm|เซน\S*|ม\.?|m|นิ้ว|inch(?:es)?)?\s*[x×*]\s*"
    r"\d+(?:\.\d+)?"
    # spelled-out: "กว้าง 40 ยาว 40 สูง 40"
    r"|กว้าง\s*\d+(?:\.\d+)?\s*\S{0,4}\s*ยาว\s*\d+(?:\.\d+)?\s*\S{0,4}\s*สูง\s*\d+(?:\.\d+)?",
    re.IGNORECASE)
# a description of a SPECIFIC parcel — turns a price question into a
# calculation request.
_PARCEL_DESC_RE = re.compile(
    r"กล่อง|ลัง|ชิ้นนี้|ของชิ้นนี้|ชิ้นเดียว|ของเท่านี้|เท่านี้|"
    r"ขนาด\s*(?:ประมาณ|นี้|เท่านี้)|น้ำหนัก\s*\d|\d+\s*(?:กิโล|กก|โล|ชิ้น|กล่อง|ลัง)", re.IGNORECASE)
_ACT_ESTIMATE = _ACT_CALC_VERB   # back-compat alias for _worth_llm_disambiguation
_ACT_CHANGE = re.compile(r"เปลี่ยน|เปเลี่ยน|เปลี่ยนแปลง|สลับ|แก้ไข|เเก้ไข|แก้\s*(?:เป็น|ให้|ที่อยู่|ที่ส่ง|ปลายทาง|ชื่อ|เบอร์|ข้อมูล|ผู้รับ)|ย้าย|ปรับ\S{0,4}(?:เป็น|ที่)|ขอเปลี่ยน|ขอเปเลี่ยน|ขอแก้|ขอสลับ|modify|\bchange\b", re.IGNORECASE)
_ACT_SELF_DO = re.compile(r"เอง|ด้วยตัวเอง|ตัวเอง|มารับเอง|ไปรับเอง|มาเอาเอง|ไปเอาเอง|self\s*pick", re.IGNORECASE)
_ACT_CHARTER = re.compile(r"เหมา|เรียก|จ้าง|ใช้บริการเหมา|\bhire\b", re.IGNORECASE)
_PICKUP_VERB = re.compile(r"รับของ|รับสินค้า|มารับ|ไปรับ|เข้ารับ|มาเอา|ไปเอา|รับเอง|รับพัสดุ", re.IGNORECASE)
_SHIP_VERB = re.compile(r"ส่งได้|ส่งไหว|นำเข้า|เอาเข้า|ขนส่งได้|ส่งไป(?:ได้)?|ส่งเข้า(?:มา)?|ส่งมา(?:ไทย)?|เข้ามาได้|ฝากส่ง|ฝากนำเข้า", re.IGNORECASE)
# PHASE-5 D15 / F05 — "สั่ง(ซื้อ) <goods> (จำนวนเยอะ) ได้ไหม" is the SAME
# can-this-kind-of-goods-be-brought-in question as "นำเข้า <goods> ได้ไหม"
# for prohibited-goods purposes (the customer-approved F05 answer is the
# prohibited-goods answer). Used ONLY inside the PRODUCT_POLICY branch and
# ONLY with a real goods noun extracted from AFTER the verb — never widens
# the shared _SHIP_VERB. A leading "สั่ง"/"ซื้อ" alone is just an order
# verb; the noun after it is what makes it a goods-policy question.
_ORDER_GOODS_VERB = re.compile(r"^(?:อยาก|ขอ|จะ|ต้องการ)?\s*(?:สั่งซื้อ|สั่ง|ซื้อ)(?=\S{0,1}[ก-๙])", re.IGNORECASE)
# the order verb AFTER a fronted topic noun ("<goods>สั่งจากจีนได้ไหม")
_ORDER_VERB_FRONTED_RE = re.compile(r"(?<=[ก-๙])\s*(?:สั่งซื้อ|สั่ง|ซื้อ)(?=\s*(?:จาก|ของ|ได้|มา|เข้า|ผ่าน))")

_ROLE_SELF = re.compile(r"ของผม|ของฉัน|ของดิฉัน|ของหนู|ของเรา|ของกระผม|บิลผม|บิลฉัน|ออเดอร์ผม|ออเดอร์ฉัน|พัสดุผม|พัสดุฉัน|บัญชีผม|บัญชีฉัน|เลขบิลผม|ผมสั่ง|ฉันสั่ง|ที่ผมสั่ง|ที่ฉันสั่ง", re.IGNORECASE)
# a record-identifying private marker — excludes SELF_PICKUP (that stays a
# public how-to per CUSTOMER-RAG-1.1) but not a bare first-person pronoun.
_PRIV_RECORD_RE = re.compile(r"เลขบิล|เลขที่บิล|บิลผม|บิลฉัน|ออเดอร์ผม|พัสดุผม|order\s*id", re.IGNORECASE)

_STATUS_STRONG = re.compile(r"ถึงไหน(?:แล้ว)?|ถึง(?:ไทย|จีน|โกดัง)?(?:แล้ว)?(?:หรือ|รึ)?ยัง|มาถึงยัง|ไปถึงไหน|ออกจาก(?:จีน|โกดัง|ไทย)?(?:แล้ว)?(?:หรือ)?ยัง|ของถึงยัง|เช็ก\S{0,4}สถานะ|เช็คสถานะ|ตรวจสอบสถานะ|ติดตามพัสดุ|ส่งของให้\S{0,6}(?:หรือ)?ยัง", re.IGNORECASE)
_EST_COMPOSITE = re.compile(
    r"เสีย(?:เงิน|ค่า)?\S{0,8}(?:เท่าไหร่|เท่าไร|กี่บาท)"
    r"|(?:คิด|ประเมิน|คำนวณ|คำนวน|ตี)\S{0,4}(?:ค่าส่ง|ค่าขนส่ง|ค่านำเข้า|ราคาค่าส่ง)", re.IGNORECASE)
_MEASURE_RE = re.compile(r"\d+\s*(?:กิโล|กก\.?|kg|โล|ตัน)|\d+\s*[x×*]\s*\d+|กว้าง\s*\d+|ยาว\s*\d+|สูง\s*\d+", re.IGNORECASE)
_DEST_MARKER_RE = re.compile(r"(?:ไป|ปลายทาง|ส่งไปที่|ส่งไป|ที่)\s*([ก-๙A-Za-z][ก-๙A-Za-z0-9 .\-]{1,20})", re.IGNORECASE)

# a follow-up that clearly changes the CURRENT calculation / frame.
_CORR_RE = re.compile(r"ไม่ใช่\s*\S+.{0,12}(?:เป็น|เอา)\s*\S")
_CMP_RE = re.compile(r"^\s*(?:ถ้า|แล้วถ้า|หากเป็น|สมมติ|งั้นถ้า).{0,28}(?:ล่ะ|ล้ะ|หละ|มั้ย|ไหม)\s*(?:คะ|ครับ|ค่ะ)?\s*$|แล้ว\S{0,18}(?:ล่ะ|หละ)\s*(?:คะ|ครับ|ค่ะ)?\s*$")
_TOPIC_RE = re.compile(
    r"งั้น.{0,24}(?:ดีกว่า|แทน|แล้วกัน)|เปลี่ยนไป(?:ถาม|เรื่อง)|ขอถามเรื่อง|เอาเป็นว่าถาม"
    r"|ไม่เอาแล้ว\s*(?:ขอ)?ถาม|ไม่เอาแล้ว\S{0,10}(?:ขอถาม|เปลี่ยนไป|ถามเรื่อง)"
    r"|พอแล้ว\S{0,10}(?:ขอถาม|เปลี่ยน)|กลับมา(?:เรื่อง|ถาม)")

# genuinely structural (non-conversational) inputs — skip semantics.
_STRUCT_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_STRUCT_ID_RE = re.compile(r"^[A-Za-z]{1,4}\d{5,}[A-Za-z0-9\-]*$")
_STRUCT_NUMERIC_RE = re.compile(r"^[\d\s.,:x×*/\-]+$", re.IGNORECASE)
_STRUCT_POSTBACK_RE = re.compile(r"^(?:action=|postback[:=]|__|/[a-z_]+$)", re.IGNORECASE)
_THAI_CHAR_RE = re.compile(r"[ก-๙]")


def _structural_kind(t: str) -> Optional[str]:
    if not t:
        return "empty"
    if _STRUCT_URL_RE.match(t):
        return "url"
    if _STRUCT_POSTBACK_RE.match(t):
        return "postback"
    if _STRUCT_ID_RE.match(t) and " " not in t:
        return "identifier"
    if _STRUCT_NUMERIC_RE.match(t) and not _THAI_CHAR_RE.search(t):
        return "numeric"
    return None


def _conversation_act(t: str) -> str:
    """PHASE-6B — the dialogue act the CURRENT message performs, separate
    from its intent family. Only REJECT is modelled for now."""
    return "REJECT" if _REJECT_ACT_RE.search(t or "") else "NONE"


def _followup_op(t: str) -> str:
    if _CORR_RE.search(t):
        return "CORRECTION"
    if _CMP_RE.search(t):
        return "COMPARISON"
    if _TOPIC_RE.search(t):
        return "TOPIC_CHANGE"
    if is_frame_followup(t):
        if _USER_QTY_RE.search(t) or re.match(r"^\s*(?:ประมาณ\s*)?\d", t):
            return "SET_VALUE"
        return "CONTINUE"
    return "NONE"


# ── PHASE 6 POST-DEPLOY — multi-intent fact preservation ─────────────
# CURRENT_TURN_ENTITY_CANNOT_BE_DROPPED. One turn may carry an intent, a
# product, a quantity+unit, a shipping method AND a question
# ("อยากนำเข้าชั้นวางของ 10 ชิ้น คิดค่าส่งยังไง"). Exactly one family wins
# the routing decision, and before this block whichever family that was
# (SHIPPING_ESTIMATE, or no family at all) simply DISCARDED the product
# and quantity the customer had just supplied, so the next turn asked
# for them again.
#
# This runs after every deterministic branch and only ever `setdefault`s
# — the winning family's own extraction always takes precedence. The
# product is carried only when the turn genuinely IS an import-interest
# declaration (the same recogniser the IMPORT_INTEREST branch itself
# gates on), so no other family gains a spurious product.
_QUESTION_KIND_RES = (
    ("PRICE", re.compile(r"ราคา|ค่าส่ง|ค่าขนส่ง|ค่านำเข้า|ค่าใช้จ่าย|เรท|กี่บาท|เท่าไหร่|เท่าไร")),
    ("DURATION", re.compile(r"กี่วัน|นานไหม|นานแค่ไหน|ระยะเวลา|ใช้เวลา|เมื่อไหร่|เมื่อไร")),
    ("METHOD_ELIGIBILITY", re.compile(r"ทางรถ|ทางเรือ|ทางอากาศ|ทางเครื่องบิน|ขนส่ง|จัดส่ง|ส่ง")),
    ("HOWTO", re.compile(r"ยังไง|ยังงัย|อย่างไร|วิธี")),
)


def _classify_question_span(span: str) -> Optional[str]:
    s = (span or "").strip()
    if not s:
        return None
    for kind, rx in _QUESTION_KIND_RES:
        if rx.search(s):
            return kind
    return "OTHER"


def _carry_current_turn_entities(t: str, ent: Dict) -> None:
    """Preserve every fact THIS turn stated, whichever family won."""
    text = t or ""
    entity_span, question_span = split_question_clause(text)
    if question_span.strip():
        ent.setdefault("question_span", question_span.strip())
        _qk = _classify_question_span(question_span)
        if _qk:
            ent.setdefault("question_kind", _qk)
    qm = _USER_QTY_RE.search(text)
    if qm:
        ent.setdefault("quantity", int(qm.group("q")))
        # PHASE 6 POST-DEPLOY (defect class C) — the TYPED quantity. The
        # unit the customer actually supplied is a fact of the turn, not
        # a parsing by-product: it must survive into the acknowledgement
        # and the frame instead of being replaced by a generic "ชิ้น".
        ent.setdefault("quantity_unit", qm.group(2))
        ent.setdefault("quantity_raw", qm.group(0).strip())
    mm = _METHOD_WORD_RE.search(text)
    if mm:
        _meth = _method_label(mm.group(0))
        if _meth:
            ent.setdefault("method", _meth)
    if not ent.get("product") and _is_import_interest(text):
        p = _import_noun(text)
        if p:
            ent["product"] = p


def _compose(t: str) -> "tuple[str, float, Dict]":
    """Deterministic compositional classification — see _compose_family.
    Every fact the CURRENT turn stated is preserved on the way out, no
    matter which family won (multi-intent invariant)."""
    fam, conf, ent = _compose_family(t)
    try:
        _carry_current_turn_entities(t, ent)
    except Exception:          # never let fact-carrying break routing
        pass
    return fam, conf, ent


def _compose_family(t: str) -> "tuple[str, float, Dict]":
    """Deterministic compositional classification. Returns
    (intent_family, confidence, entities). Order = most distinctive
    composite first."""
    ent: Dict[str, object] = {}
    obj_inv = bool(_OBJ_INVOICE.search(t))
    obj_wh = bool(_OBJ_WAREHOUSE.search(t))
    obj_cp = bool(_OBJ_COUPON.search(t))
    obj_tk = bool(_OBJ_TRUCK.search(t))
    obj_cost = bool(_OBJ_COST.search(t))
    obj_parcel = bool(_OBJ_PARCEL.search(t))
    obj_addr = bool(_OBJ_ADDRESS.search(t))

    a_locate = bool(_ACT_LOCATE.search(t))
    a_status = bool(_ACT_STATUS.search(t)) or bool(_STATUS_STRONG.search(t))
    a_issue = bool(_ACT_ISSUE_GET.search(t))
    a_howto = bool(_ACT_HOWTO.search(t))
    a_listmine = bool(_ACT_LIST_MINE.search(t))
    a_permit = bool(_ACT_PERMIT.search(t))
    a_calc = bool(_ACT_CALC_VERB.search(t))
    a_price_q = bool(_PRICE_Q_RE.search(t))
    a_parcel_desc = bool(_PARCEL_DESC_RE.search(t))
    a_est_composite = bool(_EST_COMPOSITE.search(t))
    a_change = bool(_ACT_CHANGE.search(t))
    a_self = bool(_ACT_SELF_DO.search(t))
    a_charter = bool(_ACT_CHARTER.search(t))
    v_pickup = bool(_PICKUP_VERB.search(t))
    v_ship = bool(_SHIP_VERB.search(t))
    has_measure = bool(_MEASURE_RE.search(t))
    m = _method_label(t) if _METHOD_WORD_RE.search(t) else None
    if m:
        ent["method"] = m

    # PHASE-6B — WEBSITE_LINK_REQUEST vs PRODUCT_LINK_CONVERSION. "ขอลิงก์
    # เว็บ Taobao และ Tmall" / "ขอลิงก์ที่จะเข้าไปดูของ" asks for the
    # platform's HOMEPAGE / browse URL, not "convert my product link".
    # Checked BEFORE the link-conversion signal (which fires on
    # "ลิงก์" + a platform word). Decisive guard: no actual https?:// URL
    # in the message and no explicit "แปลงลิงก์" verb — either of those
    # is always a genuine conversion and falls through untouched.
    if (_WEBSITE_LINK_RE.search(t) and "http" not in t.lower()
            and not re.search(r"แปลง\S{0,6}(?:ลิงก์|ลิงค์|link)", t, re.IGNORECASE)):
        return "WEBSITE_LINK_REQUEST", 0.8, ent

    # LINK CONVERSION (CUSTOMER-LINK-1) — an explicit conversion verb, or
    # an actual https?:// URL anywhere in the message, is itself decisive
    # (checked first: a link-conversion ask naming a warehouse/coupon/
    # invoice word incidentally in surrounding text must never be
    # mis-routed by a later, less specific branch). A bare textual
    # mention of a platform name with no verb and no URL is deliberately
    # NOT a signal (Case 6: "ผมซื้อของใน 1688" is not a conversion ask).
    if _is_link_conversion_signal(t):
        req = _classify_link_request(t)
        if req.get("url"):
            ent["url"] = req["url"]
        if req.get("platform"):
            ent["platform"] = req["platform"]
        return "LINK_CONVERSION", 0.85, ent

    # WITHDRAWAL (CUSTOMER-RED-REAL-FAIL-1 RED-5/RED-6) — an explicit
    # withdraw-money verb is itself decisive; the OBJECT tells purchase
    # vs. shipping apart. Checked before CHARTER_TRUCK/COUPON/etc so an
    # incidental object word never steals it, and before the generic
    # operational "add_vat"/change-request flow so a withdrawal is never
    # mistaken for a VAT request.
    # CANCELLATION (PHASE 6 POST-DEPLOY, defect class B) — named
    # DETERMINISTICALLY and confidently, BEFORE withdrawal, because the
    # cancel verb governs: "ยกเลิกการถอนเงินได้ไหม" is a cancellation of a
    # withdrawal, not a withdrawal request. Naming it here is the whole
    # fix for root cause G: at confidence 0.85 interpret() never consults
    # the gated LLM for these turns, so the LLM can no longer answer
    # "PURCHASE_WITHDRAWAL" for a question about cancelling a bill. The
    # POLICY/OPERATION discriminator itself is NOT duplicated here — it
    # is the one in services/operational_change_flow.py that the
    # operational collection path already obeys.
    _cancel_kind = _classify_cancellation(t)
    if _cancel_kind == "POLICY":
        return "CANCELLATION_POLICY", 0.85, ent
    if _cancel_kind == "OPERATION":
        return "CANCELLATION_OPERATION", 0.85, ent

    if _WITHDRAWAL_VERB_RE.search(t):
        if _SHIPPING_WITHDRAWAL_OBJ_RE.search(t):
            return "SHIPPING_WITHDRAWAL", 0.85, ent
        if _PURCHASE_WITHDRAWAL_OBJ_RE.search(t):
            return "PURCHASE_WITHDRAWAL", 0.85, ent
        # PHASE 6 POST-DEPLOY (defect class B) — a bare withdraw verb with
        # no distinguishing object ("อยากถอนเงิน", "ถอนเครดิตยังไง",
        # "ขอถอนยอดในระบบ") is the purchase-credit wallet, the default the
        # production LLM tier was already returning. Naming it here makes
        # the withdrawal side deterministic too, so BOTH directions of the
        # cancellation/withdrawal boundary are decided without an LLM.
        return "PURCHASE_WITHDRAWAL", 0.7, ent

    # PHASE-6B — MONEY_TRANSFER_INTEREST: wants to SEND / pay money to a
    # China shop ("ฝากโอน", "โอนเงินให้ร้านที่จีน"). Opposite direction
    # from a withdrawal (checked after, so "ถอนเงิน" never lands here) —
    # this must NOT be routed to a withdrawal how-to (T05).
    if _MONEY_TRANSFER_RE.search(t):
        return "MONEY_TRANSFER_INTEREST", 0.8, ent

    # PHASE-6B — CONTACT_INFO: a PUBLIC company contact request. Decisive
    # on its own so it can never be pulled into an identity-gated ERP /
    # customer lookup ("ขออีเมล และเว็บไซต์" must NOT ask for a CustCode).
    if _CONTACT_INFO_RE.search(t):
        return "CONTACT_INFO", 0.8, ent

    # PHASE-6B — WAREHOUSE_INBOUND_JOURNEY: "my supplier will ship to your
    # China warehouse — how do you know it is mine / do I notify first /
    # will you contact me on arrival". Checked before PICKUP_LOCATION so a
    # "โกดัง" + where-ish phrasing does not answer the warehouse-ADDRESS
    # FAQ instead. Sub-kind kept in entities for the honest-fallback
    # reply.
    _wh_inbound = bool(_WAREHOUSE_INBOUND_JOURNEY_RE.search(t))
    if _wh_inbound or (
            (obj_wh or v_ship) and (_WH_WHOSE_PARCEL_RE.search(t) or _WH_PRE_NOTIFY_RE.search(t)
                                    or _WH_ARRIVAL_CONTACT_RE.search(t))):
        if _WH_WHOSE_PARCEL_RE.search(t):
            ent["wh_kind"] = "whose_parcel"
        elif _WH_ARRIVAL_CONTACT_RE.search(t):
            ent["wh_kind"] = "arrival_contact"
        elif _WH_PRE_NOTIFY_RE.search(t):
            ent["wh_kind"] = "pre_notify"
        if ent.get("wh_kind"):
            return "WAREHOUSE_INBOUND_JOURNEY", 0.78, ent
    # a bare "will you contact me back once it arrives" — no parcel
    # identifier, no status verb of its own — is the arrival-contact
    # question in this journey even with no "โกดัง"/"ร้านส่ง" wording.
    if (_WH_ARRIVAL_CONTACT_RE.search(t) and not _PRIV_RECORD_RE.search(t)
            and not obj_parcel and not _MEASURE_RE.search(t)):
        ent["wh_kind"] = "arrival_contact"
        return "WAREHOUSE_INBOUND_JOURNEY", 0.7, ent

    # CHARTER TRUCK — a charter-truck object is itself decisive (a hire /
    # request move is implied by naming it).
    if obj_tk:
        md = _DEST_MARKER_RE.search(t)
        if md:
            ent["destination"] = md.group(1).strip()
        return "CHARTER_TRUCK", 0.85, ent

    # SHIPPING ESTIMATE — an explicit calculate/estimate verb, a
    # "how-much-will-it-cost-me" phrasing, or a price question about a
    # SPECIFIC parcel. A bare generic rate question ("ค่านำเข้าเท่าไหร่",
    # no parcel, no verb) is NOT this — it stays a rate FAQ
    # (CUSTOMER-CALC-1).
    if a_calc and (obj_cost or obj_parcel or has_measure or a_price_q or a_parcel_desc):
        return "SHIPPING_ESTIMATE", 0.85, ent
    # PHASE-6B-REAL — a bare dimensions triple (optionally with a weight /
    # method) is a calculator input, deterministically, so a stale
    # link-conversion / import-frame context can never let the LLM
    # reclassify it. Excludes a tax-document / coupon message (those keep
    # their own family). A real https?:// URL still wins LINK_CONVERSION
    # above.
    if _DIMS_TRIPLE_RE.search(t) and not obj_inv and not obj_cp and not obj_tk:
        return "SHIPPING_ESTIMATE", 0.85, ent
    if a_est_composite:
        return "SHIPPING_ESTIMATE", 0.8, ent
    if a_price_q and a_parcel_desc and (v_ship or obj_cost or re.search(r"ส่งมา|มาไทย|ส่งของ", t)):
        return "SHIPPING_ESTIMATE", 0.75, ent
    if obj_cost and (has_measure or m):
        return "SHIPPING_ESTIMATE", 0.7, ent
    # PHASE-6B-REAL (D) — a shipping-cost object + an EVALUATIVE price
    # question ("แพงไหม" / "แพงมั้ย" / "คิดราคายังไง"), with or without a
    # named item, is a shipping-cost intent. Route it to the estimate
    # flow (which explains it needs weight + dimensions and asks for
    # them) rather than letting a history-poisoned GENERAL read dead-end
    # in an "unsupported company fact" Human-CS handoff. A BARE
    # how-much rate question ("ค่านำเข้าเท่าไหร่", only "เท่าไหร่"/"กี่บาท")
    # is deliberately NOT included — it stays a rate FAQ (CUSTOMER-CALC-1).
    if obj_cost and re.search(
            r"แพงไหม|แพงมั้ย|แพงรึเปล่า|แพงมั๊ย|แพงมาก(?:ไหม|มั้ย)?|ราคาสูงไหม|แพงหรือเปล่า|แพงป่าว|แพงปะ|"
            r"แรง(?:ไหม|มั้ย|ป่าว)|(?:เยอะ|สูง|โหด)(?:ไหม|มั้ย|ป่าว)",
            t, re.IGNORECASE):
        return "SHIPPING_ESTIMATE", 0.7, ent

    # INVOICE — a tax-document object with an issue / permit / how-to move.
    if obj_inv and (a_issue or a_permit or a_howto or a_locate):
        return "INVOICE", 0.85, ent
    if obj_inv:
        return "INVOICE", 0.55, ent

    # SELF PICKUP — a self / personally marker with a pickup verb and NO
    # where-question (a public how-to, per CUSTOMER-RAG-1.1: "เอง" + a
    # where-question is a LOCATION question, handled next).
    if a_self and (v_pickup or obj_wh) and not _PRIV_RECORD_RE.search(t) and not a_locate:
        return "SELF_PICKUP", 0.8, ent

    # PICKUP LOCATION — a warehouse / pickup-point object (or a pickup
    # verb) with a where-question. A "เอง" marker does NOT block this
    # (CUSTOMER-RAG-1.1: the where-question dominates).
    if (obj_wh or v_pickup) and a_locate:
        return "PICKUP_LOCATION", 0.85, ent

    # COUPON — how-to vs. list-mine.
    if obj_cp and a_howto:
        return "COUPON_USAGE", 0.85, ent
    if obj_cp and a_listmine:
        return "MY_COUPONS", 0.8, ent
    if obj_cp and (a_permit or a_issue):
        return "COUPON_USAGE", 0.6, ent
    if obj_cp:
        return "COUPON_USAGE", 0.5, ent

    # PRODUCT POLICY — a can-I-ship / can-I-import / can-I-order move on
    # some goods, without a cost / warehouse / coupon / invoice object.
    # PHASE-5 D15 — "สั่ง(ซื้อ) <goods> (จำนวนเยอะ) ได้ไหม" counts too, but
    # ONLY when a real product noun precedes the verb (so a bare "สั่งได้
    # ไหม" / "ซื้อได้ไหม" with no goods still falls through).
    _v_order_goods = bool(_ORDER_GOODS_VERB.search(t))
    # THAI-HUMAN-LANGUAGE — topic-fronted order ("กระต่ายสั่งจากจีนได้ไหม"):
    # the goods noun BEFORE the order verb. The same shared extractor reads
    # the pre-verb span; a quantity, filler, demonstrative or record-object
    # span is not a goods noun, so "20 คู่สั่งได้ไหม", "อยากสั่งได้ไหม" and
    # "บิลที่สั่งยกเลิกได้ไหม" keep their own paths.
    _fronted_noun = None
    if a_permit and not _v_order_goods:
        _fm = _ORDER_VERB_FRONTED_RE.search(t)
        if _fm and _fm.start() > 0:
            _pre = t[:_fm.start()]
            if not re.search(r"\d|บิล|ออเดอร์|order|พัสดุ|ที่อยู่|ของผม|ของฉัน"
                             r"|(?:ฝาก|ช่วย|รับ|กำลัง|อยาก|จะ|ขอ)\s*$", _pre, re.IGNORECASE):
                _pn = _bare_product_noun(_pre)
                if (_pn and not _opener_filler_only(_pn)
                        and not _DEMONSTRATIVE_REF_RE.match(_pn)):
                    _fronted_noun = _pn
                    _v_order_goods = True
    if a_permit and (v_ship or _v_order_goods) and not (obj_cost or obj_wh or obj_cp or obj_inv
                                                          or (a_change and obj_addr)):
        # the product noun is the phrase BEFORE the ship / order / permit
        # verb. A run-on Thai phrase ("กล่องพลาสติกนำเข้าได้ไหมครับ") has no
        # spaces, so a bare split(" ")[0] would capture the WHOLE
        # sentence and echo it back downstream (REAL LINE regression).
        _cut = len(t)
        for _rx in (_SHIP_VERB, _ACT_PERMIT):
            _mm = _rx.search(t)
            if _mm and _mm.start() < _cut:
                _cut = _mm.start()
        # PHASE 6 POST-DEPLOY (defect class A) — the last resort used to
        # be "the last whitespace-separated token before the verb", which
        # for the Thai VERB-noun order ("อยากนำเข้ารองเท้า 10 ชุด …") is the
        # INTEREST WORD, not goods, and produced product="อยาก". The ONE
        # shared extractor is consulted first now; the old positional
        # fallback is kept last, for the spaced pre-verb shapes it was
        # written for.
        _noun = (_bare_product_noun(t[:_cut])
                 or _import_noun(t)
                 or (re.split(r"\s+", t[:_cut].strip())[-1] if _cut else ""))
        if _noun and _DEMONSTRATIVE_REF_RE.match(re.sub(r"\s+", "", _noun)):
            _noun = ""        # "ของแบบนี้นำเข้าได้ไหม" refers to the known product
        _noun_ok = bool(_noun and 2 <= len(_noun) <= 30 and _THAI_CHAR_RE.search(_noun))
        # PHASE-5 D15 — Thai "VERB noun" order ("สั่งแบตเตอรี่...ได้ไหม"):
        # the goods noun sits AFTER a leading order verb (never use the
        # pre-verb text here — it is the verb itself). Take the span
        # between the order verb and the permit marker, drop any quantity
        # qualifier ("จำนวนเยอะ", "เยอะ ๆ", "หลายชิ้น", "มาก"), and require
        # a genuine ≥3-char Thai noun — so a bare "สั่งได้ไหม" / "ซื้อได้ไหม"
        # (no goods) still falls through to the rest of _compose.
        _order_noun_ok = False
        if _fronted_noun:
            _noun, _noun_ok, _order_noun_ok = _fronted_noun, True, True
        elif _v_order_goods:
            _ov = _ORDER_GOODS_VERB.search(t)
            _pm = _ACT_PERMIT.search(t)
            if _ov is not None and _pm is not None and _pm.start() > _ov.end():
                # PHASE 6 POST-DEPLOY (defect class A) — this branch used
                # to run its OWN ad-hoc strip over the raw span between
                # the order verb and the permission marker, a THIRD
                # product extractor carrying the same contamination bug
                # ("อยากสั่งรองเท้าจากจีน 10 ชุด ส่งทางรถได้ไหม" ->
                # product "รองเท้าจากจีน10ชุดส่งทางรถ"). The positional
                # SHAPE gate is kept — a goods noun must sit between the
                # order verb and the permission marker — but the
                # EXTRACTION is delegated to the ONE shared extractor, so
                # there is no third copy of the rule to drift.
                _span = re.sub(r"\s+", "", t[_ov.end():_pm.start()])
                _cand = _import_noun(t)
                if _cand and _cand in _span:
                    _noun, _noun_ok, _order_noun_ok = _cand, True, True
        # A shipping verb ("นำเข้า") is decisive on its own; an order verb
        # counts only once a real goods noun was extracted from after it.
        if v_ship or _order_noun_ok:
            if _noun_ok:
                ent["product"] = _noun
            return "PRODUCT_POLICY", 0.7, ent

    # SHIPMENT STATUS — a parcel object (or a strong status phrasing)
    # asking about progress.
    if (obj_parcel and a_status) or _STATUS_STRONG.search(t):
        return "SHIPMENT_STATUS", 0.8, ent

    # ADDRESS CHANGE — a change move on a delivery-address object, or a
    # "send it somewhere else instead" phrasing (the "instead" IS the
    # change signal).
    if (a_change and obj_addr) or re.search(
            r"ส่งไป\S{0,12}(?:อีกที่|ที่อื่น|ที่ใหม่)|เปลี่ยนที่ส่ง|ส่ง(?:ของ)?ไป(?:ที่|ยัง)\S{1,20}แทน|"
            r"ส่ง\S{0,8}ที่อื่น\S{0,4}แทน|จัดส่ง\S{0,8}ที่อื่นแทน", t):
        return "ADDRESS_CHANGE", 0.8, ent

    # IMPORT INTEREST — reuse the FIX-2.3 recogniser verbatim.
    if _is_import_interest(t):
        p = _import_noun(t)
        if p:
            ent["product"] = p
        # PHASE-6-SLOT-CONSUMPTION — a FRESH opener may already state
        # quantity and/or shipping method in the SAME turn ("20 คู่อยาก
        # สั่งของจากจีน", "อยากสั่งรองเท้าจากจีน 20 คู่ ส่งเรือ"). Extract them
        # with the SAME regexes already used for a bare quantity/method
        # ANSWER (_USER_QTY_RE / _METHOD_WORD_RE) so a slot confidently
        # supplied in the opening turn is never re-asked one turn later.
        _qm = _USER_QTY_RE.search(t)
        if _qm:
            ent["quantity"] = int(_qm.group("q"))
        _mm = _METHOD_WORD_RE.search(t)
        if _mm:
            _meth = _method_label(_mm.group(0))
            if _meth:
                ent["method"] = _meth
        return "IMPORT_INTEREST", 0.7, ent

    # PHASE-6B — SERVICE_DISCOVERY: "what services do you offer" / a broad
    # "import-export" topic / "interested in using the service". Checked
    # after every specific family so a concrete request is never
    # swallowed. Answered by a pre-RAG discovery reply, never
    # KB_NOT_FOUND.
    if _SERVICE_DISCOVERY_RE.search(t):
        return "SERVICE_DISCOVERY", 0.72, ent

    # PHASE-6B — HELP_INTENT: a generic "I need help / want to ask
    # something" with NO concrete subject yet, and no recognised object.
    # The conversation must be OPENED (ask what they need), not routed to
    # KB_NOT_FOUND.
    if _HELP_INTENT_RE.search(t) and not (
            obj_inv or obj_wh or obj_cp or obj_tk or obj_cost or obj_parcel or obj_addr):
        return "HELP_INTENT", 0.7, ent

    # a recognised OBJECT but no actionable move -> a general FAQ turn
    # (keeps it OUT of the LLM-disambiguation tier and lets the ordinary
    # RAG / regex path answer it).
    if obj_cost or obj_inv or obj_wh or obj_cp or obj_parcel or obj_addr:
        return "GENERAL", 0.5, ent

    return "UNKNOWN", 0.0, ent


# ── gated LLM family disambiguation ──────────────────────────────────
_FAMILY_SYS_PROMPT = (
    "You label ONE Thai customer message for an import / logistics support "
    "bot with its INTENT FAMILY. Output STRICT JSON only, no prose.\n"
    "Schema: {\"family\": <one of " + "|".join(INTENT_FAMILIES) + ">, "
    "\"is_private\": <true|false>, \"product\": <string|null>, "
    "\"destination\": <string|null>, \"method\": <\"road\"|\"sea\"|\"air\"|null>}\n"
    "Families: SHIPMENT_STATUS=asking progress/where-is-it of a shipment/order; "
    "INVOICE=tax invoice / receipt issuance; PICKUP_LOCATION=where is the "
    "warehouse / pickup point; SELF_PICKUP=may I collect it myself / how; "
    "COUPON_USAGE=how to use a coupon/discount; MY_COUPONS=what coupons do I "
    "have (private); PRODUCT_POLICY=can this kind of goods be shipped/imported; "
    "CHARTER_TRUCK=hire / charter a whole truck for local delivery; "
    "SHIPPING_ESTIMATE=estimate/quote a shipping cost; ADDRESS_CHANGE=change "
    "the delivery address/recipient; PURCHASE_WITHDRAWAL=withdraw money from "
    "a purchase-order credit/refund wallet; SHIPPING_WITHDRAWAL=withdraw "
    "money from a shipping-payment wallet; IMPORT_INTEREST=wants to import "
    "some product (early sales interest); "
    "HELP_INTENT=a generic 'I need help / want to ask something' with no "
    "concrete subject yet; SERVICE_DISCOVERY='what services do you offer' / "
    "a broad import-export topic / 'interested in using the service'; "
    "MONEY_TRANSFER_INTEREST=wants to SEND / pay money to a China shop "
    "(ฝากโอน — opposite of a withdrawal); WEBSITE_LINK_REQUEST=wants a "
    "platform's website / homepage / browse URL, NOT a product-link "
    "conversion; CONTACT_INFO=a public company contact request (channels / "
    "phone / email / LINE / company website); WAREHOUSE_INBOUND_JOURNEY=my "
    "supplier will ship to your China warehouse — how do you know it is "
    "mine / must I notify first / will you contact me on arrival; "
    "GENERAL=any other FAQ; UNKNOWN=cannot tell.\n"
    "is_private=true only when it refers to the customer's OWN specific "
    "record/account. Classify MEANING ONLY — never a policy verdict, an "
    "eligibility answer, or personal data."
)


# once the resolver LLM is proven unreachable in this process, stop
# calling it — every caller then just keeps the deterministic family.
_LLM_RESOLVER_DOWN = False


def _llm_family(message: str, history: Optional[List[Dict]]) -> Optional[Dict]:
    """Single gated LLM call -> {family, is_private, product, destination,
    method}. Returns None on any failure so the caller marks the result
    'degraded' and keeps the deterministic family."""
    global _LLM_RESOLVER_DOWN
    if _LLM_RESOLVER_DOWN:
        return None
    try:
        from services.llm_service import get_llm_service
        recent = " | ".join(
            (t.get("content") or "")[:80] for t in (history or [])[-4:] if t.get("role") == "user")
        user = "recent_user_turns=" + json.dumps(recent, ensure_ascii=False) + \
               "\nmessage=" + json.dumps(message or "", ensure_ascii=False)
        resp = get_llm_service().generate(
            [{"role": "system", "content": _FAMILY_SYS_PROMPT},
             {"role": "user", "content": user}],
            model=_RESOLVER_MODEL, temperature=0.0, max_tokens=90)
        raw = (resp.text or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        s, e = raw.find("{"), raw.rfind("}")
        if s == -1 or e == -1:
            return None
        data = json.loads(raw[s:e + 1])
    except Exception as ex:  # pragma: no cover - network/parse degradation
        name = type(ex).__name__
        if any(k in name for k in ("Connection", "Timeout", "Auth", "APIError", "RateLimit")):
            _LLM_RESOLVER_DOWN = True
        print(f"[conversation_semantics] family resolver degraded ({ex!r})")
        return None
    fam = str(data.get("family") or "").strip().upper()
    if fam not in INTENT_FAMILIES:
        return None
    out = {"family": fam, "is_private": bool(data.get("is_private")),
           "product": None, "destination": None, "method": None}
    for k in ("product", "destination"):
        v = data.get(k)
        if isinstance(v, str) and 1 < len(v.strip()) <= 40:
            out[k] = v.strip()
    mth = str(data.get("method") or "").strip().lower()
    if mth in ("road", "sea", "air"):
        out["method"] = mth
    return out


# a message that is ONLY a social greeting / acknowledgement / bare
# confirmation (optionally with trailing politeness particles) — no task
# content. Anchored both ends so "ตกลงราคาได้ไหม" is NOT a match.
_GREET_CONFIRM_RE = re.compile(
    r"^(?:สวัสดี\S*|หวัดดี\S*|ดีครับ|ดีค่ะ|ดีจ้า|ขอบคุณ\S*|ขอบใจ\S*|โอเค\S*|โอเต\S*|"
    r"okay|ok|k|thx|thanks?|thank\s*you|"
    r"ยืนยัน|ตกลง|รับทราบ|เข้าใจแล้ว|เข้าใจ|ได้ครับ|ได้ค่ะ|ได้เลย)"
    r"[\s\.!,~ครับคับค่ะคะจ้าาๆนะฮะฮ่ะผมโว้ยว้อยเลย]*$", re.IGNORECASE)
_INTENT_SHAPE_RES = (_OBJ_INVOICE, _OBJ_WAREHOUSE, _OBJ_COUPON, _OBJ_TRUCK, _OBJ_COST,
                     _OBJ_PARCEL, _OBJ_ADDRESS, _ACT_LOCATE, _ACT_STATUS, _ACT_ISSUE_GET,
                     _ACT_HOWTO, _ACT_LIST_MINE, _ACT_ESTIMATE, _ACT_CHANGE, _ACT_CHARTER,
                     _PICKUP_VERB, _SHIP_VERB)


def _worth_llm_disambiguation(t: str) -> bool:
    """SEMANTIC-FIRST-2 — the gated LLM family call fires for ANY genuinely
    conversational Thai message the deterministic tier could not resolve.
    It is NOT pre-gated on a deterministic topic marker matching first —
    that made novel/short phrasings ("ร้านส่งหรือยังคะ", "ต้นทางส่งมาหรือ
    ยัง") fall straight through to no-info / Fix-2. Still excluded:
    greetings, confirmations, bare structural values, empty / single-char
    or over-long noise. Still one gated call, still degrades safely, still
    self-disables for the process on the first unreachable-LLM error."""
    s = (t or "").strip()
    if not _THAI_CHAR_RE.search(s) or not (4 <= len(s) <= 120):
        return False
    if _GREET_CONFIRM_RE.match(s):
        return False
    if _structural_kind(s) is not None:
        return False
    if _MEASURE_RE.fullmatch(s):   # a bare weight / dimensions value
        return False
    return True


# INVOICE-PRODUCT-REGRESSION-2 (Problem B) — the assistant asked the
# customer WHICH product (a legitimate product-elicitation turn). A small
# structural marker set, never a product list.
_ASSISTANT_ASKED_PRODUCT_RE = re.compile(
    r"สินค้า\S{0,4}(?:อะไร|ชนิดไหน|ประเภทไหน|แบบไหน|อะไรบ้าง)"
    r"|(?:นำเข้า|สั่ง|ฝากสั่ง|ฝากนำเข้า)\S{0,4}อะไร"
    r"|สินค้าที่(?:ต้องการ|จะ|อยาก)\S{0,20}(?:คืออะไร|อะไร)"
    r"|เป็นสินค้าอะไร|สินค้าของลูกค้าเป็นอะไร|ประเภทสินค้า\S{0,4}(?:คือ|อะไร)"
    # LANGGRAPH UPGRADE — a REQUEST VERB followed by the product-slot noun,
    # with no character budget between them. The previous alternative used
    # `รบกวน\S{0,10}(?:ประเภท|ชนิด)สินค้า`, and the platform's OWN ask
    # ("รบกวนแจ้งชื่อหรือประเภทสินค้าที่สนใจนำเข้าด้วยนะคะ") has eleven
    # characters in that gap — so the renderer asked for the product and
    # this detector did not believe it had, which is why a perfectly
    # ordinary "รองเท้าครับ" on the next turn was not read as the answer.
    # Structure (request verb -> slot noun) instead of a length budget.
    r"|(?:รบกวน|กรุณา|ขอทราบ|ช่วย|แจ้ง|บอก|ระบุ).{0,24}?(?:ชื่อ|ประเภท|ชนิด)สินค้า"
    r"|(?:รบกวน|กรุณา|ขอทราบ|ช่วย).{0,24}?สินค้าที่(?:สนใจ|ต้องการ|จะ)")
# a Thai question particle that would make the reply itself a question,
# not a bare answer.
_REPLY_IS_QUESTION_RE = re.compile(r"ไหม|มั้ย|หรือเปล่า|หรือไม่|ยังไง|อย่างไร|เท่าไหร่|กี่|ที่ไหน|\?")
# PHASE-6 (customer master pass) — "สินค้า"/"ของ" were removed from the
# PREFIX group and "ใส่ของ"/"ใส่ของได้" from the SUFFIX group: all three
# are also legitimate word-formants inside a real Thai compound product
# noun ("ของเล่น", "กล่องใส่ของ") — Thai script has no word spaces, so a
# blanket strip corrupted these by prefix ("ของเล่น" -> "เล่น") or suffix
# ("กล่องใส่ของ" -> "กล่อง"). See _bare_product_noun below for how they
# are still recognised as filler, just no longer unconditionally.
# THAI-HUMAN-LANGUAGE (2026-09) — the leading alternation also covers the
# other ways a customer ANSWERS "which product?": "เอา<noun>" (take/want),
# "อยาก(สั่ง|นำเข้า)<noun>", and the placeholder+copula openers
# "สินค้าเป็น<noun>" / "ของคือ<noun>". The placeholder is stripped ONLY when
# a copula follows it (lookahead), so a product whose name begins with
# "สินค้า"/"ของ" ("ของเล่น", "สินค้ามือสอง") keeps its first syllable.
# The placeholder+copula opener is tone-mark tolerant ("สินค่าเป็น", "สินคาคือ"):
# the shape "<placeholder><copula>" has no other reading, and the spelling
# layer deliberately never rewrites a run of real words ("สิน|ค่า") on its own.
_BARE_PRODUCT_STRIP_RE = re.compile(
    r"^(?:สินค[้่]?า(?=เป็น|คือ)|ของ(?=เป็น|คือ)|เป็น|คือ|ก็|น่าจะ|ประมาณ|พวก|เป็นพวก|จำพวก|ชนิด|ประเภท|"
    r"อยากได้|อยากสั่ง|อยากนำเข้า|อยาก|ต้องการ|จะเอา|เอา|ฝากสั่ง|ฝากนำเข้า|สั่งซื้อ|สั่ง|ซื้อ|นำเข้า|"
    # the verb+placeholder compounds "ส่งของ"/"ขนของ" name no product
    r"ส่ง(?=ของ|สินค้า)|ขน(?=ของ))\s*"
    r"|\s*(?:ครับ|ค่ะ|คะ|ค่า|นะ|น่ะ|จ้า|จ้ะ|เลย|อ่ะ|อะ|ล่ะ|หน่อย|ด้วย|ค่ะๆ|ครับๆ)+\s*$")
# the bare generic placeholder with nothing else left ("เป็นของครับ" ->
# no real product named at all).
_BARE_PRODUCT_PLACEHOLDER_RE = re.compile(r"^(?:สินค้า|ของ)+$")
# THAI-HUMAN-LANGUAGE — a DEMONSTRATIVE reference ("ของแบบนี้", "อันนี้",
# "สินค้าพวกนี้", "ตัวนั้น") points at goods already under discussion; it
# is not a product name and must never overwrite the remembered one.
_DEMONSTRATIVE_REF_RE = re.compile(
    r"^(?:สินค้า|ของ|อัน|ตัว|ชิ้น)?(?:แบบ|พวก|ประเภท|ชนิด|เหล่า)?(?:นี้|นั้น|นี่|นั่น|ดังกล่าว|โน้น)+$")
# NOTE (PHASE 6, system-wide pass): an earlier revision ALSO stripped a
# trailing "ใส่ของ(ได้)?" when what preceded it was "long enough" to look
# like a self-sufficient noun. That length threshold was brittle by
# construction and — checked against the actual customer source
# (docs/customer_uat_sources/INVOICE_PRODUCT_REGRESSION_2.md) — it was
# defending an assertion no customer ever made: the source only ever
# carries the bare "กล่องพลาสติกค่ะ". A purpose clause the customer chose
# to say IS part of how they named their goods, so nothing strips it now;
# the one rule below is the whole mechanism.


def _assistant_asked_for_product(history: Optional[List[Dict]]) -> bool:
    for t in reversed(list(history or [])[-4:]):
        if t.get("role") == "assistant":
            return bool(_ASSISTANT_ASKED_PRODUCT_RE.search(t.get("content") or ""))
        if t.get("role") == "user":
            continue
    return False


_QUANTITY_REPLY_RE = re.compile(
    r"\d+\s*(?:ชิ้น|อัน|ใบ|กล่อง|ลัง|ตัว|ชุด|คู่|โหล|แพ็ค|แพ็ก|pcs?|kg|กิโล|กก|โล|ตัน|บาท|หยวน)",
    re.IGNORECASE)
# OWNER-REAL-LINE-FIX-04 — a refusal / cancellation / stop is never a
# product name, even in reply to a "which product?" question. Keeps
# "ไม่เอาแล้ว" / "ยกเลิก" / "ไม่ใช่" out of the bare-product slot path so
# a topic switch or rejection still wins.
_NEGATION_STOPWORD_RE = re.compile(
    r"^(?:ไม่|ยัง)\S{0,3}(?:เอา|ต้อง|ใช่|อยาก|สน)|^ยกเลิก|^เปลี่ยนใจ|^หยุด|^พอแล้ว|^เลิก")


def _looks_like_bare_product(t: str) -> bool:
    s = (t or "").strip()
    if not (1 <= len(s) <= 42) or not _THAI_CHAR_RE.search(s):
        return False
    if _GREET_CONFIRM_RE.match(s) or _REPLY_IS_QUESTION_RE.search(s):
        return False
    if _NEGATION_STOPWORD_RE.search(s):
        return False
    # not a structural value — weight / dimensions / bare number /
    # a quantity ("500 ชิ้น"), a price, a URL/id.
    if (_MEASURE_RE.search(s) or _QUANTITY_REPLY_RE.search(s)
            or re.fullmatch(r"[\d\s.,x×*/\-]+", s)
            or _STRUCT_URL_RE.match(s) or _STRUCT_ID_RE.match(s)):
        return False
    # too digit-heavy to be a product noun
    if sum(ch.isdigit() for ch in s) > len(s) / 3:
        return False
    # not one of the other intent families (a real request, not a noun)
    if any(rx.search(s) for rx in (_OBJ_INVOICE, _OBJ_WAREHOUSE, _OBJ_COUPON, _OBJ_TRUCK,
                                   _OBJ_COST, _ACT_CHANGE, _ACT_ESTIMATE)):
        return False
    return True


def _bare_product_noun(t: str) -> Optional[str]:
    # PHASE 6 POST-DEPLOY (defect class A) — same structural clause
    # separation the opener extractor uses, so a slot answer that also
    # carries a question ("เป็นรองเท้า ราคาเท่าไหร่") yields the product
    # alone. Shared function, not a second copy of the rule.
    s, _q = split_question_clause((t or "").strip())
    s = s.strip()
    for _ in range(3):
        s = _BARE_PRODUCT_STRIP_RE.sub("", s).strip()
    s = re.sub(r"\s+", "", s)
    # "ๆ" is the Thai repetition mark, never part of a product's name
    # ("ของเล่นๆ" is still the product "ของเล่น").
    s = s.rstrip("ๆ").strip()
    if s and _BARE_PRODUCT_PLACEHOLDER_RE.match(s):
        return None
    if s and _DEMONSTRATIVE_REF_RE.match(s):
        return None
    # PHASE 6 POST-DEPLOY (defect class A) — a remnant that is NOTHING but
    # opener filler ("อยาก", "ต้องการ", "จะเอา") is not a product either.
    # This reuses the opener extractor's OWN filler vocabulary rather than
    # duplicating it here, so the two extractors cannot disagree about
    # what counts as filler; it is the same "only when the whole remnant
    # is filler" rule the placeholder check above applies.
    if s and _opener_filler_only(s):
        return None
    return s if 2 <= len(s) <= 30 and _THAI_CHAR_RE.search(s) else None


def _opener_filler_only(s: str) -> bool:
    try:
        from services.playground_orchestrator import _FIX23_STRIP_RE
    except Exception:  # pragma: no cover - import guard
        return False
    return not _FIX23_STRIP_RE.sub("", s).strip()


# OWNER-REAL-LINE-FIX-04 — a bare product name supplied in reply to the
# assistant's "which product?" question is a PRODUCT-SLOT response. It
# belongs to whichever journey asked for it: the default owner is the
# IMPORT_INTEREST / service-discovery journey (acknowledge + keep going).
# PRODUCT_POLICY only takes the turn when the customer is actually in a
# restriction / eligibility context, or the named product matches a
# trusted high-risk category — so the trusted policy (not this layer)
# still returns the prohibited-vs-allowed verdict.
#
# _HIGH_RISK_PRODUCT_RE is NOT a new prohibited-goods list: it mirrors
# the category vocabulary the trusted policy layer already carries
# (services/playground_orchestrator.py::_PROHIBITED_CATEGORY_WORDS) plus
# the direct product nouns that ARE one of those categories, so a
# customer who volunteers "เป็นน้ำยา" / "มีแบตเตอรี่" still reaches the
# PRODUCT_POLICY path.
_HIGH_RISK_PRODUCT_RE = re.compile(
    r"แบตเตอรี่|แบตเตอร์รี่|แบตเตอรรี่|ถ่านไฟฉาย|พาวเวอร์แบง|power\s*bank|"
    r"น้ำหอม|น้ำยา|ของเหลว|สเปรย์|สเปรผ์|aerosol|"
    r"สารเคมี|เคมีภัณฑ์|วัตถุไวไฟ|วัตถุอันตราย|ไวไฟ|แอลกอฮอล์|น้ำมันเชื้อเพลิง|"
    r"บุหรี่ไฟฟ้า|บุหรี่|พอตไฟฟ้า|"
    r"อาหารสด|ของกิน|เครื่องดื่ม|เวชภัณฑ์|อาหารเสริม|"
    r"สิ่งมีชีวิต|สัตว์เป็น|เมล็ดพันธุ์", re.IGNORECASE)

# an explicit restriction / eligibility question ("… นำเข้าได้ไหม",
# "… ห้ามไหม", "สินค้าต้องห้าม …") — used to detect that the CURRENT turn
# or a recent user turn put the exchange in a product-policy context.
_RESTRICTION_Q_RE = re.compile(
    r"(?:นำเข้า|ส่ง|ขนส่ง|เอาเข้า|ฝากส่ง|ฝากนำเข้า)\S{0,6}(?:ได้|ไหว)?\S{0,3}(?:ไหม|มั้ย|มัย|หรือเปล่า|รึเปล่า|หรือไม่)"
    r"|ห้าม\S{0,8}(?:ไหม|มั้ย|หรือเปล่า|รึเปล่า)|(?:ของ|สินค้า)?ต้องห้าม|ผิดกฎหมาย"
    r"|สินค้าที่ห้าม|ของที่ห้าม|นำเข้าไม่ได้|ส่งไม่ได้", re.IGNORECASE)


def _restriction_context_in_history(history: Optional[List[Dict]]) -> bool:
    """True when a recent USER turn framed the exchange as a
    restriction / prohibited-goods question, so a following bare product
    name is a policy check rather than an import-journey slot."""
    for turn in reversed(list(history or [])[-6:]):
        if turn.get("role") != "user":
            continue
        if _RESTRICTION_Q_RE.search(turn.get("content") or ""):
            return True
    return False


# assistant turns that mark the exchange as an import / proxy-buy
# discovery journey — the FIX-01 import_interest_reply, the frame
# acknowledgement, and the service-discovery reply. Their presence (or a
# recent user import-interest turn) is what lets a bare product-slot
# reply continue as IMPORT_INTEREST instead of a product-policy check.
_ASSIST_IMPORT_DISCOVERY_RE = re.compile(
    r"ถ้ามีลิงก์สินค้า|มีลิงก์สินค้าที่สนใจ|บอกคร่าว\s*ๆ?\s*ได้เลยว่าอยากสั่งสินค้า"
    r"|แนะนำขั้นตอน|รับทราบค่ะ\s*\(สินค้า|สนใจนำเข้า|(?:อยาก|ต้องการ)นำเข้า"
    r"|แจ้งรายละเอียดสินค้าและปริมาณ|สินค้าและปริมาณมาได้เลย")


def _import_journey_active(history: Optional[List[Dict]]) -> bool:
    """True when the exchange is inside an import / proxy-buy discovery
    journey: a recent USER turn expressed import interest, or the
    assistant's recent turn is an import-discovery ack / prompt."""
    for turn in reversed(list(history or [])[-6:]):
        role, content = turn.get("role"), turn.get("content") or ""
        if role == "user" and _is_import_interest(content):
            return True
        if role == "assistant" and _ASSIST_IMPORT_DISCOVERY_RE.search(content):
            return True
    return False


# P2-STAB — the assistant just asked for a journey slot. A compatible
# answer on the next turn is a REQUESTED_SLOT_ANSWER (P1 precedence
# tier 3) and is canonical — the gated LLM must not reclassify it.
_ASSISTANT_ASKED_QTY_RE = re.compile(
    r"แจ้งจำนวน|จำนวน\S{0,6}(?:เท่าไหร่|กี่|โดยประมาณ|ประมาณเท่าไร)|กี่(?:ชิ้น|ตัว|คู่|อัน|กล่อง|ชุด)")
_ASSISTANT_ASKED_METHOD_RE = re.compile(
    r"ทางรถหรือทางเรือ|ทางรถหรือเรือ|ส่งทางไหน|ขนส่งทางไหน|วิธี(?:การ)?จัดส่ง")
# THAI-HUMAN-LANGUAGE — a quantity answer may open with a take/want verb
# or an approximation word ("เอา 50 ชิ้น", "สัก 3 ลัง", "ขอ 10 คู่ครับ").
_BARE_QTY_ANSWER_RE = re.compile(
    r"^\s*(?:เอา|ขอ|สัก|ราวๆ|ราว|จำนวน)?\s*(?:ประมาณ\s*)?\d{1,7}\s*"
    r"(?:" + _COUNT_UNIT_ALT + r")?\s*"
    r"(?:ค่ะ|คะ|ครับ|คับ|นะ)?\s*$", re.IGNORECASE)
# LANGGRAPH UPGRADE — this file used to define _BARE_METHOD_ANSWER_RE
# TWICE. The second definition (here) silently shadowed the first at
# import time and was the narrower of the two: it knew "ทางเรือ" but not
# "ส่งเรือ", which the shared _METHOD_WORD_ALT vocabulary has recognised
# for the opener all along. The consequence was that answering the
# assistant's own "สนใจส่งทางรถหรือทางเรือคะ" with "ส่งเรือครับ" matched
# nothing and the journey dead-ended. The single definition now lives
# next to _METHOD_WORD_ALT, built from it, so the two can never diverge.


def _last_assistant(history: Optional[List[Dict]]) -> str:
    for turn in reversed(list(history or [])[-4:]):
        if turn.get("role") == "assistant":
            return turn.get("content") or ""
        if turn.get("role") == "user":
            return ""
    return ""


def _deterministic_context_authority(t: str, history: Optional[List[Dict]],
                                     op: str) -> Optional["Interpretation"]:
    """P2-STAB CENTRAL AUTHORITY RULE (task §2/§3). A high-confidence
    deterministic CONTEXT resolution is canonical; the gated LLM is
    disambiguation evidence only and must NOT override it. Returns a
    canonical Interpretation when such a signal exists (so the caller
    skips _llm_family entirely), else None.

    Covers: an explicit journey rejection; an active-frame follow-up op;
    a compatible answer to the slot the assistant just requested
    (quantity / shipping method — the 'product' case is the FIX-04 block
    just above). Never fires on an explicit topic switch / restriction
    question — those are handled by their own deterministic families."""
    journey_open = _import_journey_active(history)
    frame = derive_active_frame(history)

    # an explicit journey REJECTION ("ไม่เอาแล้ว", "ยกเลิก") while a
    # journey is open is canonical — the LLM must not guess a family (it
    # has been seen to reclassify a bare "ไม่เอาแล้ว" as
    # PURCHASE_WITHDRAWAL). Family stays UNKNOWN; the REJECT act carries
    # the meaning and the decision engine's frame-reject path owns it.
    if _FRAME_CANCEL_RE.match(t) and (journey_open or (frame and frame.product)):
        return Interpretation(intent_family="UNKNOWN", entities={},
                              follow_up_op=op, confidence=0.6, source="deterministic",
                              conversation_act="REJECT")

    if not (frame and frame.product):
        return None
    la = _last_assistant(history)

    # explicit topic-switch / restriction / reject-of-previous-answer
    # wording is NOT a slot answer — let it route on its own signals.
    if _REJECT_ACT_RE.search(t) or _RESTRICTION_Q_RE.search(t) or _TOPIC_RE.search(t):
        return None

    # (a) an active-frame follow-up op — attribute to the running frame.
    if op in ("CORRECTION", "COMPARISON", "TOPIC_CHANGE", "CONTINUE", "SET_VALUE"):
        return Interpretation(intent_family="IMPORT_INTEREST",
                              entities={"product": frame.product},
                              follow_up_op=op, confidence=0.7, source="deterministic",
                              conversation_act=_conversation_act(t))

    # (b) a compatible answer to the slot the assistant just requested.
    if _ASSISTANT_ASKED_QTY_RE.search(la) and _BARE_QTY_ANSWER_RE.match(t):
        return Interpretation(intent_family="IMPORT_INTEREST",
                              entities={"product": frame.product},
                              follow_up_op="SET_VALUE", confidence=0.7, source="deterministic",
                              conversation_act=_conversation_act(t))
    if _ASSISTANT_ASKED_METHOD_RE.search(la) and _BARE_METHOD_ANSWER_RE.match(t):
        _m = _method_label(t)
        _e = {"product": frame.product}
        if _m:
            _e["method"] = _m
        return Interpretation(intent_family="IMPORT_INTEREST", entities=_e,
                              follow_up_op="SET_VALUE", confidence=0.7, source="deterministic",
                              conversation_act=_conversation_act(t))
    return None


def interpret(message: str, history: Optional[List[Dict]] = None,
              context: Optional[Dict] = None) -> Interpretation:
    """The ONE central semantic interpretation. Natural language ->
    normalised {intent_family, entities, is_private, follow_up_op}. A
    single gated LLM call only disambiguates novel / ambiguous phrasing
    and always degrades to the deterministic result.

    P2-STAB: the gated LLM is DISAMBIGUATION EVIDENCE. It never overrides
    a high-confidence deterministic canonical resolution — an explicit
    current intent, an explicit correction / rejection / topic switch, a
    structural URL / identifier, an active-frame correction, or a
    compatible answer to a slot the assistant just requested (see
    _deterministic_context_authority)."""
    raw = message or ""
    t = raw.strip()

    kind = _structural_kind(t)
    if kind is not None:
        ent: Dict[str, object] = {}
        if kind == "identifier":
            ent["identifier"] = t
        elif kind == "url":
            ent["url"] = t
            # CUSTOMER-LINK-1 — a bare pasted URL (the WHOLE message, no
            # surrounding text) is exactly CUS-S20's own worked example
            # ("just paste the link"). Still fully deterministic/
            # structural (no LLM). OWNER-REAL-LINE-FIX-03 — ANY bare
            # well-formed URL is a link-conversion turn; the deterministic
            # link classifier (services/link_conversion_flow.py) decides
            # the sub-state (VALID / platform home / incomplete / an
            # UNSUPPORTED domain). Routing it here as LINK_CONVERSION is
            # what lets an unsupported bare URL (e.g. a google.com link)
            # reach the natural "not supported for conversion" reply
            # instead of falling through to a RAG no-info / Human CS
            # handoff. This is a BARE-URL-only fast path — a URL embedded
            # in a sentence, or a "ขอลิงก์เว็บ Taobao" text request, never
            # matches _STRUCT_URL_RE and keeps its normal _compose path.
            req = _classify_link_request(t)
            _lc_ent = {"url": req.get("url") or t}
            if req.get("platform"):
                _lc_ent["platform"] = req["platform"]
            return Interpretation(intent_family="LINK_CONVERSION", entities=_lc_ent,
                                  follow_up_op="NONE", confidence=0.9, source="structural")
        return Interpretation(intent_family="UNKNOWN", entities=ent,
                              follow_up_op="NONE", confidence=0.0, source="structural")

    op = _followup_op(t)
    fam, conf, ent = _compose(t)
    is_priv = bool(_ROLE_SELF.search(t))

    # INVOICE-PRODUCT-REGRESSION-2 (Problem B) — a bare product NAME given
    # in reply to the assistant's own "what product?" question is a
    # PRODUCT-slot response, not an UNKNOWN standalone company question.
    # Structural: the previous assistant turn asked for the product AND
    # this turn is a short noun phrase (no product dictionary). Trusted
    # policy still decides the verdict downstream — here we only name the
    # entity.
    if fam == "UNKNOWN" and _assistant_asked_for_product(history) and _looks_like_bare_product(t):
        noun = _bare_product_noun(t)
        if noun:
            # OWNER-REAL-LINE-FIX-04 — the bare product name belongs to the
            # journey that asked for it. Inside an active IMPORT_INTEREST /
            # proxy-buy discovery journey it is a PRODUCT-SLOT response and
            # the journey keeps going (acknowledge the product, invite a
            # link / ask a useful detail) — naming a product is NOT itself
            # a prohibited-goods question. It stays PRODUCT_POLICY (the
            # INVOICE-PRODUCT-REGRESSION-2 default) whenever the exchange
            # is in a restriction/eligibility context (an explicit "can I
            # import / is it banned" now or earlier), the product matches a
            # trusted high-risk category, or there is simply no active
            # import journey — so the trusted policy still owns the verdict
            # for every case that needs it.
            _policy_ctx = (_restriction_context_in_history(history)
                           or bool(_RESTRICTION_Q_RE.search(t)))
            _hi_risk = bool(_HIGH_RISK_PRODUCT_RE.search(noun)
                            or _HIGH_RISK_PRODUCT_RE.search(t))
            if (not _policy_ctx and not _hi_risk
                    and _import_journey_active(history)):
                return Interpretation(intent_family="IMPORT_INTEREST",
                                      entities={"product": noun}, is_private=is_priv,
                                      follow_up_op="NONE", confidence=0.7,
                                      source="deterministic")
            return Interpretation(intent_family="PRODUCT_POLICY",
                                  entities={"product": noun}, is_private=is_priv,
                                  follow_up_op="SET_VALUE", confidence=0.7,
                                  source="deterministic")

    source = "deterministic"
    # P2-STAB CENTRAL AUTHORITY — before consulting the gated LLM, honour
    # a high-confidence deterministic CONTEXT resolution (active-frame
    # follow-up / requested-slot answer). The LLM is disambiguation
    # evidence; it must not override this.
    if fam in ("UNKNOWN", "GENERAL") or conf < 0.5:
        _ctx = _deterministic_context_authority(t, history, op)
        if _ctx is not None:
            return _ctx

    # SEMANTIC-FIRST-2 — the gated LLM family call also runs when the
    # deterministic tier could only reach GENERAL: "conversational but no
    # specific family" is exactly the case one semantic call is meant to
    # disambiguate (e.g. "ช่วยกะราคาส่งของกล่องนี้ให้หน่อยดิ" -> GENERAL
    # deterministically, SHIPPING_ESTIMATE once interpreted).
    if (fam in ("UNKNOWN", "GENERAL") or conf < 0.5) and _worth_llm_disambiguation(t):
        llm = _llm_family(t, history)
        if llm and llm["family"] != "UNKNOWN":
            fam = llm["family"]
            conf = max(conf, 0.6)
            source = "llm"
            if llm.get("is_private"):
                is_priv = True
            for k in ("product", "destination", "method"):
                if llm.get(k) and not ent.get(k):
                    ent[k] = llm[k]
        elif llm is None:
            source = "degraded"

    # a short in-frame follow-up refines the op and, when the family is
    # still unknown, attributes it to the running import-interest frame.
    frame = derive_active_frame(history)
    if frame and frame.product and op in ("CORRECTION", "COMPARISON", "TOPIC_CHANGE", "CONTINUE", "SET_VALUE"):
        ent.setdefault("product", frame.product)
        if fam == "UNKNOWN":
            fam, conf = "IMPORT_INTEREST", max(conf, 0.55)

    return Interpretation(intent_family=fam, entities=ent, is_private=is_priv,
                          follow_up_op=op, confidence=round(conf, 2), source=source,
                          conversation_act=_conversation_act(t))


# family -> the existing rag/query_understanding.py actionable_intent
# bucket. None => let the ordinary Business-Action / private-state
# routing own the turn (never force a RAG intent).
FAMILY_TO_ACTIONABLE_INTENT = {
    "SHIPMENT_STATUS": "tracking_status",
    "INVOICE": "invoice_policy",
    "PICKUP_LOCATION": "warehouse_location",
    "SELF_PICKUP": "self_pickup_permission",
    "COUPON_USAGE": "coupon_policy",
    "MY_COUPONS": None,
    "PRODUCT_POLICY": "prohibited_goods",
    "CHARTER_TRUCK": "service_information",
    "SHIPPING_ESTIMATE": "shipping_calculation",
    "ADDRESS_CHANGE": None,
    "LINK_CONVERSION": None,
    "PURCHASE_WITHDRAWAL": None,
    "SHIPPING_WITHDRAWAL": None,
    # answered from the approved committed policy statement by the
    # Decision Engine, never forced into a RAG intent bucket.
    "CANCELLATION_POLICY": None,
    "CANCELLATION_OPERATION": None,
    "IMPORT_INTEREST": None,
    # PHASE-6B pre-RAG families — handled by their own Decision-Engine
    # branch BEFORE RAG, so they never force a RAG actionable intent.
    "HELP_INTENT": None,
    "SERVICE_DISCOVERY": None,
    "MONEY_TRANSFER_INTEREST": None,
    "WEBSITE_LINK_REQUEST": None,
    "CONTACT_INFO": None,
    "WAREHOUSE_INBOUND_JOURNEY": None,
    "GENERAL": None,
    "UNKNOWN": None,
}


# SEMANTIC-FIRST-2.1 — the families that are ALWAYS a PUBLIC company-
# information question (policy / how-to / location / service), never an
# identity-gated ERP or customer-data lookup and never a general-
# knowledge chit-chat turn. Consumed by the Decision Engine (to keep
# identity-gated Business Actions out of the candidate set) and by the
# RAG orchestrator (to keep the turn on the company-knowledge path
# instead of the General Chat Fallback). Deliberately excludes:
# SHIPMENT_STATUS / MY_COUPONS (private-account), ADDRESS_CHANGE
# (operational workflow), SHIPPING_ESTIMATE (its own calculator flow,
# resolved earlier), IMPORT_INTEREST / GENERAL / UNKNOWN (not decisive).
PUBLIC_INFO_FAMILIES = frozenset({
    "PICKUP_LOCATION", "SELF_PICKUP", "COUPON_USAGE",
    "PRODUCT_POLICY", "CHARTER_TRUCK", "INVOICE",
    # PHASE-6B — a public company-contact / website request and the
    # "supplier ships to your warehouse" journey are PUBLIC: they must
    # never be offered an identity-gated ERP / customer-data action
    # ("ขออีเมล และเว็บไซต์" must not ask for a CustCode).
    "CONTACT_INFO", "WEBSITE_LINK_REQUEST", "WAREHOUSE_INBOUND_JOURNEY",
    # PHASE 6 POST-DEPLOY — "can a bill be cancelled?" is a POLICY
    # question: public, and never an identity-gated lookup. The
    # OPERATION sibling is deliberately NOT public.
    "CANCELLATION_POLICY",
})
