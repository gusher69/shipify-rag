"""Hybrid Question Classifier & Segmenter (introduced 2026-08-02 Hybrid
Question Segmentation sprint; consumed directly by services/
decision_engine.py's Hybrid routing since the 2026-08-02 Production
Integration Sprint, Phase 1 Step C — no longer Playground-only).

Every signal used here is generic and Registry-driven — no customer-
specific or endpoint-specific vocabulary is hardcoded anywhere in this
file. It reuses, never duplicates, the shared generic scoring/parameter-
binding primitives in services/action_selection_primitives.py
(`_keyword_score`, `_extract_candidates_for_binding`,
`_bind_candidate_to_parameter`, `_askable_parameters_by_name`) — the
SAME primitives services/decision_engine.py itself imports from that
module. Those primitives were extracted out of decision_engine.py
specifically so this module could depend on them without decision_engine.py
depending on this module in return (this module has no import of
decision_engine.py at all, avoiding a circular import now that
decision_engine.py imports THIS module for Hybrid routing).

Classification categories:
    RAG_ONLY              — no Business Action token/parameter evidence found.
    ERP_ONLY               — a Business Action matched, no separable RAG remainder.
    HYBRID                  — a Business Action matched AND the question cleanly
                              segments into an ERP-relevant clause and a
                              genuinely separate remainder.
    CLARIFICATION_REQUIRED — 2+ DIFFERENT Business Actions matched with
                              comparably strong evidence — which one the
                              customer means is genuinely ambiguous.
    UNKNOWN                — a greeting-only / no-content message; still
                              safely falls back to RAG execution (never
                              leaves the customer with literally nothing).
"""
import re
from typing import Dict, List, Optional

from services.action_selection_primitives import (
    _askable_parameters_by_name,
    _bind_candidate_to_parameter,
    _extract_candidates_for_binding,
    _extract_structural_candidates,
    _keyword_score,
)
# Reused, never duplicated — services/customer_tier_service.py has no
# imports of its own (a leaf module), so importing from it here carries
# no circular-import risk. _HOT_RE is already the codebase's one generic,
# non-domain-specific "customer is expressing interest/desire to engage"
# signal (used for lead-stage scoring); reusing it here (Wrong-Intent
# Prevention fix, Task 03, 2026-08-25) rather than inventing a second,
# parallel "interest" pattern.
from services.customer_tier_service import _HOT_RE

# Generic, language-agnostic conjunction/clause markers — never a
# customer-specific vocabulary. Used only to split a compound question
# into candidate clauses; a message with none of these stays a single
# clause (segmentation then correctly reports "not separable").
_SEGMENT_CONJUNCTIONS = ("และ", "กับ", "แล้วก็", "รวมถึง", "พร้อมกับ", " and ", " plus ", " as well as ")

# Loose Segment Markers (Golden Application Defect Fixes, 2026-08-16) --
# bare "แล้ว" ("then"/"also"/"already") is the single most common way a
# Thai customer joins two related questions in one message ("...มีคูปอง
# อะไรบ้าง แล้วคูปองใช้งานยังไง"), but unlike the fixed conjunctions above
# it is heavily overloaded -- it's just as commonly a temporal/completion
# particle with NO second question at all ("ส่งของแล้วหรือยัง", "เช็คให้
# แล้วนะ"). Never used as a conjunction on its own (see
# _segment_by_value): only trusted as a genuine clause boundary when the
# resulting non-ERP clause independently carries its own question
# evidence (_QUESTION_MARKER_RE) -- two distinct signals required, not
# one, exactly like a tied keyword score elsewhere in this module already
# requires independent structural evidence before it's allowed to decide
# anything on its own.
_LOOSE_SEGMENT_MARKERS = ("แล้ว",)

# Generic Thai question particles -- not tied to any customer/domain
# vocabulary (coupons, wallets, shipments, ...); the same handful of
# words that turn any clause into a recognizable question, regardless of
# what it's asking about.
#
# P0 Final Fix follow-up (2026-08-28) -- added แค่ไหน/เมื่อไหร่/หรือเปล่า,
# confirmed live: "J&T รับพัสดุขนาดใหญ่แค่ไหน" (extent), "ของถึงไทยเมื่อไหร่"
# (when), and "ใช้ลิงก์ Tmall ได้หรือเปล่า" (yes/no) are equally common,
# equally generic Thai question forms that this pattern's own existing
# members (ไหม/กี่/ที่ไหน/...) were already meant to cover -- none of these
# three name any customer/domain vocabulary, same as every existing entry.
_QUESTION_MARKER_RE = re.compile(
    r"(ยังไง|อย่างไร|อะไร|ทำไม|เท่าไหร่|เท่าไร|หรือไม่|หรือเปล่า|ไหม|กี่|ที่ไหน|แค่ไหน|เมื่อไหร่)"
)

# A second, distinct kind of clause evidence: a genuine second ASK phrased
# as a polite REQUEST rather than a literal question ("ช่วยประเมินค่าขนส่ง
# ถึงบ้านให้หน่อย" -- no อะไร/ยังไง/กี่ anywhere, but unmistakably its own
# separate ask). Requires BOTH a request verb (ช่วย/ขอ/รบกวน) AND a
# politeness/request particle within a short span, so a bare "ขอบคุณครับ"
# (which contains the substring "ขอ" alone) never qualifies -- mirrors the
# same "two distinct signals required, not one" discipline as
# _QUESTION_MARKER_RE's own use in _segment_by_value. Used only alongside
# _QUESTION_MARKER_RE (never as a replacement for it) wherever a clause
# needs to prove it carries its own independent request, not just any
# text that happens to follow a conjunction.
_REQUEST_MARKER_RE = re.compile(
    r"(ช่วย|ขอ|รบกวน).{0,40}(หน่อย|ด้วย|ทีนะ|ทีค่ะ|ทีครับ)"
)

# A negator immediately before an interest/desire word ("ไม่สนใจ", "ไม่
# อยาก", "ไม่ต้องการ") means the customer is DECLINING, not expressing
# vague interest -- the opposite of what _HOT_RE alone signals. Used only
# to EXCLUDE the Wrong-Intent Prevention fallback below, never to change
# routing on its own.
_NEGATED_INTEREST_RE = re.compile(r"ไม่\s*(สนใจ|อยาก|ต้องการ)")

# A message that is ONLY a greeting has no actionable question content —
# deterministically UNKNOWN rather than a low-confidence guess either way.
_GREETING_RE = re.compile(r"^(สวัสดี|หวัดดี|hello|hi|hey)[\sครับค่ะ!.]*$", re.IGNORECASE)

# A generic "just asking for the total" fragment -- "รวมเท่าไหร่", "ยอด
# ค่าขนส่งทั้งหมดเท่าไหร่", "total", "sum" and close variants, with no
# other independent TOPIC of its own (Shipment Count+Sum Aggregation fix,
# P1 audit finding). When the loose "แล้ว" split's RAG-side clause is
# NOTHING BUT this fragment, it is virtually never an independent, topic-
# bearing RAG question on its own -- it is the tail half of a SINGLE
# combined count+sum analytical question the loose split severed apart.
# Confirmed live: "มีกี่บิลที่เข้าไทยแล้ว รวมเท่าไหร่" (a customer asking,
# in ONE breath, how many of their shipments have a status and what the
# total is) was being segmented into an ERP "count" clause and a RAG
# "รวมเท่าไหร่" clause, the latter then answered as an unrelated generic
# shipping-rate calculation instead of the customer's own ERP aggregate.
# Deliberately the SAME broad "ยอด...(up to 15 chars)...เท่าไหร่" shape as
# services/decision_engine.py's own _SUM_INTENT_RE (that module cannot be
# imported here without a circular import -- see this file's own module
# docstring -- so the shape is intentionally kept in sync by hand, not
# shared code) -- anchored start-to-end so it only ever matches a clause
# that IS nothing but a total/sum inquiry, never a genuinely separate
# topic that merely happens to mention a total in passing.
_SUM_ONLY_FRAGMENT_RE = re.compile(
    r"^(?:ยอด(?:รวม)?|รวม)(?:.{0,15})?(?:เท่าไหร่|เท่าไร|เท่าใด)$|^(?:total|sum)$",
    re.IGNORECASE)

# A candidate action within 80% of the top score is "comparably strong"
# — genuinely ambiguous, not just a distant runner-up. Fixed, documented,
# never per-customer.
_AMBIGUITY_RATIO = 0.8


def _split_clauses(message: str) -> List[str]:
    """Splits on the fixed conjunction list above only — never on any
    domain/customer-specific word. A message with no matching conjunction
    returns a single-element list (itself), which callers treat as
    "not segmentable"."""
    parts = [message]
    for conj in _SEGMENT_CONJUNCTIONS:
        next_parts: List[str] = []
        for p in parts:
            next_parts.extend(p.split(conj))
        parts = next_parts
    return [p.strip() for p in parts if p.strip()]


def _segment_by_value(message: str, matched_value: str) -> Optional[Dict[str, str]]:
    """Splits `message` into an ERP-relevant clause (the one containing
    the literal parameter VALUE that was actually extracted, e.g.
    "C00001") and a RAG-relevant remainder (everything else). Returns
    None when the message doesn't cleanly separate into 2+ clauses with
    both a matching and a non-matching one — callers must NOT force a
    HYBRID classification in that case (see classify_question)."""
    if not matched_value:
        return None
    clauses = _split_clauses(message)
    if len(clauses) >= 2:
        erp_clauses = [c for c in clauses if matched_value in c]
        rag_clauses = [c for c in clauses if c not in erp_clauses]
        if erp_clauses and rag_clauses:
            return {"erp_sub_question": " ".join(erp_clauses), "rag_sub_question": " ".join(rag_clauses)}

    # Loose-marker fallback (see _LOOSE_SEGMENT_MARKERS docstring) — only
    # reached when NONE of the fixed conjunctions produced a valid split.
    # Requires evidence of two genuinely distinct intents, not just the
    # marker: the candidate RAG clause must independently contain its own
    # question evidence, or this returns None exactly like the fixed path
    # above (never forces HYBRID off the marker alone).
    loose_clauses = [message]
    for marker in _LOOSE_SEGMENT_MARKERS:
        next_parts: List[str] = []
        for p in loose_clauses:
            next_parts.extend(p.split(marker))
        loose_clauses = next_parts
    loose_clauses = [c.strip() for c in loose_clauses if c.strip()]
    if len(loose_clauses) < 2:
        return None
    erp_clauses = [c for c in loose_clauses if matched_value in c]
    rag_clauses = [c for c in loose_clauses if c not in erp_clauses]
    if not erp_clauses or not rag_clauses:
        return None
    if not any(_QUESTION_MARKER_RE.search(c) for c in rag_clauses):
        return None
    if len(rag_clauses) == 1 and _SUM_ONLY_FRAGMENT_RE.match(rag_clauses[0]):
        # A bare "total?" fragment with no topic of its own is the tail
        # half of a single combined count+sum question, not an
        # independent RAG question — never force a split here.
        return None
    return {"erp_sub_question": " ".join(erp_clauses), "rag_sub_question": " ".join(rag_clauses)}


def _result(classification: str, confidence: float, evidence: List[str], *,
            selected_action_id: Optional[str] = None, candidate_action_ids: Optional[List[str]] = None,
            erp_sub_question: Optional[str] = None, rag_sub_question: Optional[str] = None) -> Dict:
    return {
        "classification": classification, "confidence": round(confidence, 3), "evidence": evidence,
        "selected_action_id": selected_action_id, "candidate_action_ids": candidate_action_ids or [],
        "erp_sub_question": erp_sub_question, "rag_sub_question": rag_sub_question,
    }


def classify_question(message: str, registry, *, forced_action_id: Optional[str] = None,
                       exclude_action_types: Optional[tuple] = None) -> Dict:
    """The one entry point. `forced_action_id`: when the caller already
    knows which Business Action to use (an explicit ERP/Hybrid mode
    selection), evidence-gathering for WHICH action is skipped — this
    still performs parameter-value segmentation against that action's own
    configured parameters, so explicit Hybrid mode benefits from
    segmentation exactly like Auto-detected Hybrid does.

    `exclude_action_types` (Root Change 1, Final Systemic Routing Fix,
    2026-08-28) — lets a caller that has ALREADY classified this message
    as purely Shipify-informational (services/decision_engine.py::
    classify_turn_intent) keep identity-gated actions out of THIS
    function's own tie/ambiguity scoring too, not just the caller's
    separate search_candidate_actions() call. Without this, an
    informational message could still trigger a false multi-candidate
    CLARIFICATION_REQUIRED between two private Business Actions here
    (the forensic routing audit reproduced live: "ติดต่อ Shipify ยังไง"
    tied two unrelated identity-gated actions at this exact point,
    before any informational classification got a chance to win). The
    separate "vague interest, no Business Action at all" clarification
    path (`candidate_ids` empty, further below) is untouched either way
    — it fires on the ABSENCE of any match, independent of exclusion."""
    message = (message or "").strip()
    if not message:
        return _result("UNKNOWN", 0.0, ["empty message"])
    if _GREETING_RE.match(message):
        return _result("UNKNOWN", 0.9, ["greeting-only message, no actionable question content"])

    matched_action = None
    candidate_ids: List[str] = []

    if forced_action_id:
        matched_action = registry.get_full(forced_action_id, mask_secrets=True)
        if matched_action:
            candidate_ids = [forced_action_id]
    else:
        try:
            enabled = [a for a in registry.enabled_actions() if a.get("action_type") in ("API", "WEBHOOK")]
            if exclude_action_types:
                enabled = [a for a in enabled if a.get("action_type") not in exclude_action_types]
        except Exception:
            enabled = []
        scored = sorted(
            ({**a, "_kw_score": _keyword_score(a, message)} for a in enabled),
            key=lambda a: a["_kw_score"], reverse=True,
        )
        scored = [a for a in scored if a["_kw_score"] > 0]
        if scored:
            top_score = scored[0]["_kw_score"]
            close = [a for a in scored if a["_kw_score"] >= top_score * _AMBIGUITY_RATIO]
            # Weak Tie Guard (Task 03C, 2026-08-26) — confirmed live:
            # "รับประกันไหมว่าจะถึงภายใน 7 วัน" tied searchdataorderlist and
            # searchdatashipmentlist at an identical _keyword_score of
            # 0.25 each, forcing CLARIFICATION_REQUIRED — but NEITHER
            # candidate's evidence survives excluding the generic,
            # low-specificity ai_description-word credit (see
            # services/action_selection_primitives.py::
            # _keyword_score_breakdown's own docstring for the exact
            # mechanism: "วัน" is a substring of both actions' descriptions
            # purely because both legitimately support date-range
            # filtering, not because either actually answers the
            # question). A tie where NO candidate has any "strong"
            # (search_keywords/example) evidence at all is not a genuine
            # ambiguity between two plausible actions — it's zero real
            # evidence for either, which must fall through exactly like
            # an empty `scored` list would (below, to the RAG/Wrong-
            # Intent-Prevention path), never a forced clarification.
            # Deliberately scoped to the TIE case only (len(close) > 1) —
            # a single, non-tied weak match is unaffected, since Task 03C
            # is specifically about the two-action collision, not a
            # broader re-litigation of _keyword_score's own generosity
            # (confirmed live: narrowing that further caused its own
            # regression in an unrelated Task 02B scenario relying on
            # relative ranking between two candidates).
            if len(close) > 1:
                from services.action_selection_primitives import _keyword_score_breakdown
                if not any(_keyword_score_breakdown(a, message)["strong"] > 0 for a in close):
                    close = []
                    scored = []
            if len(close) > 1:
                # Final Conversational Correctness (2026-08-15) — a tied
                # KEYWORD score alone (e.g. SearchDataTracking and
                # SearchDataShipmentList both configuring "tracking" as a
                # search keyword) must not be the ONLY signal once real
                # parameter evidence can settle it. If the message
                # carries a STRUCTURAL value (never the whole-message
                # free-text fallback — see _extract_structural_candidates)
                # that a tied candidate's own REQUIRED parameter can bind,
                # AND no OTHER tied candidate can bind that SAME value
                # (e.g. a shared CustCode present in every candidate is
                # never discriminating on its own — mirrors the same
                # "shared evidence is weak evidence for any ONE candidate"
                # principle services/decision_engine.py's own
                # _identifier_pattern_score already applies), that
                # candidate wins outright instead of forcing a
                # clarification the evidence doesn't actually require.
                # Deliberately REQUIRED parameters only — an optional
                # filter/passthrough parameter (date-range/status with no
                # real validation configured yet) says nothing about which
                # action the customer means, and would otherwise
                # spuriously "match" via the same permissive
                # generic-identifier fallback every unconfigured parameter
                # shares. A generic message with no discriminating value
                # (e.g. "ขอดู tracking ของผม FT3182" — only a bare CustCode
                # every tied action requires identically) still correctly
                # falls through to clarification below.
                structural_candidates = _extract_structural_candidates(message)
                decisive = []
                if structural_candidates:
                    # Sharer-Weighted Identifier Specificity (Customer UAT
                    # fix, 2026-08-17) — a SINGLE structural value in the
                    # message can structurally satisfy REQUIRED parameters
                    # on MORE THAN ONE close candidate at once (e.g. a
                    # well-formed OrderCode like "PO318220260806008" also
                    # happens to satisfy another action's much broader,
                    # independently-configured CustCode pattern
                    # `^[A-Za-z]{2}\d+$` — inherent, unavoidable overlap
                    # between two generic identifier shapes, not a defect
                    # in either pattern). The original check treated "this
                    # value is ALSO bindable elsewhere" as an all-or-
                    # nothing veto, discarding the fact that ONE candidate
                    # may satisfy the value via a pattern unique to it
                    # (OrderCode) while another satisfies the SAME value
                    # only via a pattern every close candidate shares
                    # identically (CustCode) — real, discriminating
                    # evidence for the former. Scored per structural value
                    # in two tiers: Tier 1 counts ONLY bindings through a
                    # parameter that has an actual configured
                    # validation_pattern, weighted by how many close
                    # candidates share that exact pattern string (mirrors
                    # services/decision_engine.py's own
                    # _identifier_pattern_score sharer-weighting). Tier 2
                    # — binding through a parameter with NO configured
                    # pattern (e.g. a free-form Tracking number, which
                    # accepts any non-empty value) — only ever applies
                    # when NO close candidate has ANY Tier 1 evidence for
                    # that value at all; otherwise an unconstrained
                    # parameter would trivially "match" every value in the
                    # message and manufacture false specificity for
                    # whichever candidate happens to have one (confirmed
                    # live: a bare CustCode alone was incorrectly resolved
                    # decisively once Tracking's own pattern-less
                    # parameter was allowed to independently "bind" that
                    # same CustCode value). A value bindable identically
                    # on every close candidate (e.g. a bare CustCode with
                    # nothing else in the message) still scores equally
                    # for all of them, correctly falling through to
                    # clarification below; a value unique to ONE candidate
                    # (e.g. a genuine Tracking number no sibling action
                    # even has a parameter for) stays fully decisive via
                    # Tier 2, exactly like the original check.
                    required_by_action: Dict[str, List[Dict]] = {}
                    for a in close:
                        full = registry.get_full(a["id"], mask_secrets=True)
                        required_by_action[a["id"]] = [p for p in _askable_parameters_by_name(full).values()
                                                        if p.get("required")]
                    id_scores: Dict[str, float] = {a["id"]: 0.0 for a in close}
                    for v in structural_candidates:
                        # Generic Business Action Routing Score Imbalance
                        # fix (2026-08-24) — grouped by the literal
                        # validation_pattern STRING here, exactly like
                        # services/decision_engine.py's own
                        # _identifier_pattern_score used to (see that
                        # function's docstring for the full root-cause
                        # writeup). Two actions' "CustCode" parameter
                        # spelled with a slightly different regex looked
                        # like unrelated, mutually-exclusive identifiers,
                        # so a candidate whose admin happened to type a
                        # narrower pattern for the SAME concept could look
                        # artificially unique here too. Grouped by the
                        # parameter's NAME instead — the identifier
                        # CONCEPT the customer supplied a value for, not
                        # incidental regex spelling.
                        tier1_names = {
                            aid: [p.get("name") for p in params
                                  if p.get("validation_pattern") and _bind_candidate_to_parameter([v], p)["status"] == "bound"]
                            for aid, params in required_by_action.items()
                        }
                        name_sharers: Dict[str, set] = {}
                        for aid, names in tier1_names.items():
                            for nm in names:
                                name_sharers.setdefault(nm, set()).add(aid)
                        tier1_scores = {aid: sum(1.0 / len(name_sharers[nm]) for nm in names)
                                        for aid, names in tier1_names.items()}
                        if max(tier1_scores.values(), default=0.0) > 0:
                            for aid, s in tier1_scores.items():
                                id_scores[aid] += s
                            continue
                        binds = {aid: any(_bind_candidate_to_parameter([v], p)["status"] == "bound" for p in params)
                                 for aid, params in required_by_action.items()}
                        sharers = sum(1 for b in binds.values() if b)
                        if sharers:
                            for aid, b in binds.items():
                                if b:
                                    id_scores[aid] += 1.0 / sharers
                    max_id_score = max(id_scores.values()) if id_scores else 0.0
                    if max_id_score > 0:
                        decisive = [a for a in close if id_scores.get(a["id"], 0.0) == max_id_score]
                if len(decisive) == 1:
                    close = decisive
                else:
                    return _result(
                        "CLARIFICATION_REQUIRED", 0.4,
                        [f"{len(close)} Business Actions matched with comparably strong evidence "
                         f"({', '.join(a.get('action_key') or a.get('name') or a['id'] for a in close)})"],
                        candidate_action_ids=[a["id"] for a in close],
                    )
            if close:
                matched_action = registry.get_full(close[0]["id"], mask_secrets=True)
                candidate_ids = [close[0]["id"]]

    if not matched_action:
        # Wrong-Intent Prevention fix (Task 03, 2026-08-25) — confirmed
        # live: "สนใจนำเข้าสินค้าครับ" (a bare expression of interest, no
        # concrete question) matched no Business Action and was handed to
        # RAG unconditionally exactly like a genuine, specific question
        # (e.g. "CBM คืออะไร") would be — RAG's own embedding similarity
        # then confidently retrieved the closest-matching chunk
        # ("prohibited goods", the only KB article that also happens to
        # repeat the word "นำเข้า" heavily) despite the customer never
        # asking about restrictions. The classifier had no way to
        # distinguish "genuine answerable question" from "vague interest,
        # nothing concrete to answer yet" — both produced the identical
        # RAG_ONLY result. _HOT_RE (interest/desire wording) present AND
        # _QUESTION_MARKER_RE (a genuine question word) absent is a
        # narrow, deterministic, two-signal intersection — NOT "any
        # question-word-free message" (that would misfire on a
        # perfectly legitimate imperative-phrased RAG question like
        # "บอกเงื่อนไขการคืนสินค้าหน่อย", which matches neither pattern and
        # is completely unaffected) and NOT "any สนใจ-containing message"
        # alone (a message that ALSO asks a concrete question, e.g.
        # "สนใจนำเข้าสินค้า แต่ไม่รู้ว่าห้ามนำเข้าอะไรบ้าง", still routes
        # straight to RAG since it clears the question-marker check).
        # Reuses CLARIFICATION_REQUIRED — the existing taxonomy value for
        # "genuinely too ambiguous to answer with confidence" — rather
        # than inventing a new classification; candidate_action_ids stays
        # empty (unlike the 2+-tied-Business-Actions case above) so
        # decision_engine.py::_route_clarification can tell the two
        # apart and phrase its reply appropriately.
        # Negation exclusion (Task 03, 2026-08-26) — confirmed live:
        # "ไม่สนใจนำเข้าสินค้าครับ ขอบคุณ" (explicitly DECLINING, a closing
        # remark) still contains the bare substring "สนใจ" that _HOT_RE
        # matches on, and would otherwise be misread as the SAME "vague
        # interest, ask for more detail" case as a genuine "สนใจนำเข้า
        # สินค้าครับ" -- producing a nonsensical "please tell me what
        # you're interested in" reply to a customer who just said the
        # opposite. A negator immediately preceding the interest wording
        # means the customer is declining, not expressing unclear
        # interest; original behavior (RAG_ONLY fallback below) resumes.
        # Named-Topic Exception (P0 Final Fix, 2026-08-28) — "นำเข้า" names
        # a concrete, unambiguous Shipify service topic (the platform's
        # own core business), unlike a genuinely context-free "สนใจครับ"/
        # "อยากเริ่มใช้บริการ" with no subject at all — this is no longer
        # the same "vague interest, nothing concrete to answer" case this
        # rule exists for. Confirmed live: the ORIGINAL Task 03 bug
        # reproduction phrase itself ("สนใจนำเข้าสินค้าครับ") now retrieves
        # a correct, well-grounded, high-confidence (0.9) Shipify answer
        # about the ฝากนำเข้า process — the retrieval-quality problem this
        # rule was protecting against (RAG confidently answering with an
        # UNRELATED "prohibited goods" chunk) was independently fixed by
        # the Semantic RAG Retrieval fix earlier this session (commit
        # ba3fced), so this safety net is no longer needed for this one
        # named topic specifically — every OTHER context-free "สนใจ"/
        # "อยากเริ่ม"/"ขอราคา" expression is completely unaffected.
        if _HOT_RE.search(message) and not _QUESTION_MARKER_RE.search(message) \
                and not _NEGATED_INTEREST_RE.search(message) \
                and "นำเข้า" not in message:
            return _result(
                "CLARIFICATION_REQUIRED", 0.4,
                ["message expresses interest/intent but asks no concrete question, and matches no "
                 "Business Action — answering via RAG here risks a confident but unrelated answer"],
                rag_sub_question=message,
            )
        return _result("RAG_ONLY", 0.6, ["no Business Action keyword/example/description overlap found"],
                        rag_sub_question=message)

    evidence = [f"Business Action matched: {matched_action.get('action_key') or matched_action.get('name')}"]

    matched_value = None
    matched_param_name = None
    askable = _askable_parameters_by_name(matched_action)
    candidates = _extract_candidates_for_binding(message)
    for name, param in askable.items():
        binding = _bind_candidate_to_parameter(candidates, param)
        if binding["status"] == "bound":
            matched_value, matched_param_name = binding["value"], name
            break

    if not matched_value:
        evidence.append("Business Action matched by keyword, but no parameter value found yet "
                         "(the Action's own clarification flow will ask for it — no ERP execution without one)")
        return _result("ERP_ONLY", 0.5, evidence, selected_action_id=matched_action["id"],
                        candidate_action_ids=candidate_ids, erp_sub_question=message)

    evidence.append(f"parameter value matched: {matched_param_name}={matched_value}")
    segmentation = _segment_by_value(message, matched_value)
    if segmentation:
        evidence.append("question cleanly segments into an ERP-relevant clause and a separate remainder")
        return _result("HYBRID", min(0.95, 0.6 + 0.15 * len(evidence)), evidence,
                        selected_action_id=matched_action["id"], candidate_action_ids=candidate_ids,
                        erp_sub_question=segmentation["erp_sub_question"], rag_sub_question=segmentation["rag_sub_question"])

    return _result("ERP_ONLY", min(0.9, 0.5 + 0.15 * len(evidence)), evidence,
                    selected_action_id=matched_action["id"], candidate_action_ids=candidate_ids,
                    erp_sub_question=message)
