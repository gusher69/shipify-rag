# -*- coding: utf-8 -*-
"""PHASE-6C — semantic answer validator.

Judge a conversation by CONSTRAINTS, never by exact wording. Given a
case's `expect` block and the observed turn results from the harness,
return one verdict:

    PASS
    FAIL_ROUTING        wrong intent family / wrong data source
    FAIL_CONTEXT        follow-up / correction / topic-switch not honoured
    FAIL_GROUNDING      a required trusted fact is missing / the honest
                        "no confirmed info" was owed but not given
    FAIL_HALLUCINATION  a prohibited fact appeared (fabricated price /
                        ETA / status / policy / fake action success)
    FAIL_NEXT_ACTION    a needed next question was not asked (or a slot
                        already supplied was re-asked)
    FAIL_STATE          stale identifier reuse / stale pending flow won /
                        completed workflow still controlling the turn
    FAIL_AUTH           private data surfaced without authorization, or a
                        public turn was pushed into an identity gate
    EXPECTED_LIMITATION behaviour that can only be proven on real LINE /
                        needs an external API not available here
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

_SERVICE_SRCS = {"phase6b_service_intent", "phase6b_help_affirmation", "phase6b_reject_reevaluate"}
_PRIVATE_LEAK_RE = re.compile(
    r"เลขบัตรประชาชน|\b\d{13}\b|รหัสผ่าน|secretcode|secret_code|api[_ ]?key|"
    r"CustPhone.*0\d{8}|ยอดคงเหลือ\s*[\d,]+\s*บาท")
_CUSTCODE_ASK_RE = re.compile(r"กรุณาแจ้งรหัสลูกค้า|แจ้งรหัสลูกค้าค่ะ|ยืนยันตัวตน|เบอร์โทรที่ผูก")
_FAKE_SUCCESS_RE = re.compile(r"ดำเนินการเรียบร้อยแล้ว|ทำรายการสำเร็จ|เสร็จเรียบร้อยแล้ว(?!นะคะ\s*$)")
_NOINFO_RE = re.compile(r"ยังไม่มีข้อมูล|ไม่มีข้อมูลยืนยัน|ยังไม่พบคำตอบ|ไม่พบข้อมูล")
_NEXT_Q_RE = re.compile(
    r"ไหมคะ|ไหมครับ|หรือคะ|หรือครับ|แจ้ง[^\n]{0,32}(ด้วย|มา)|รบกวน[^\n]{0,24}(แจ้ง|ขอ)|\?|"
    r"เรื่องไหน(คะ|ครับ)?|อะไร(คะ|ครับ)?|ไหน[^\n]{0,14}(คะ|ครับ)|ทางรถหรือทางเรือ|"
    r"สนใจ[^\n]{0,20}(คะ|ครับ|ไหม)|อยาก[^\n]{0,16}(ไหน|อะไร)")


def observed_source(turn: Dict) -> str:
    src = turn.get("selection_source") or ""
    routing = turn.get("routing")
    if src == "shipping_estimate_flow":
        return "CALCULATOR"
    if routing == "HUMAN_HANDOFF":
        return "HUMAN_HANDOFF"
    if turn.get("general_chat_used"):
        return "GENERAL_ASSISTANCE"
    if src in _SERVICE_SRCS:
        return "PRE_RAG_SERVICE"
    if routing in ("API", "WEBHOOK"):
        return "ERP"
    if routing == "RAG":
        return "RAG"
    if routing == "WORKFLOW":
        return "WORKFLOW"
    if routing == "GENERAL":
        # a GENERAL route can be RAG general-chat, a pre-RAG service reply,
        # or a bespoke flow (estimate). Estimate + service already caught
        # above; treat the rest as RAG/general.
        return "RAG_OR_GENERAL"
    return routing or "UNKNOWN"


_SOURCE_EQUIV = {
    "RAG": {"RAG", "RAG_OR_GENERAL"},
    "GENERAL_ASSISTANCE": {"GENERAL_ASSISTANCE", "RAG_OR_GENERAL", "PRE_RAG_SERVICE"},
    "PRE_RAG_SERVICE": {"PRE_RAG_SERVICE", "RAG_OR_GENERAL", "GENERAL_ASSISTANCE"},
    "CALCULATOR": {"CALCULATOR"},
    "ERP": {"ERP"},
    "WORKFLOW": {"WORKFLOW", "PRE_RAG_SERVICE"},
    "HUMAN_HANDOFF": {"HUMAN_HANDOFF"},
}


def _fam_ok(expected, actual) -> bool:
    if expected is None:
        return True
    exp = expected if isinstance(expected, (list, tuple, set)) else [expected]
    return actual in exp


def validate(case: Dict, turns: List[Dict]) -> Dict:
    """turns = list of harness .send() outputs, in order. The LAST turn
    is the one the `expect` block is written against; earlier turns are
    used for state / contradiction checks."""
    exp = case.get("expect") or {}
    if not turns:
        return {"status": "FAIL_STATE", "reason": "no turns produced"}
    last = turns[-1]
    reply = last.get("reply") or ""

    if exp.get("expected_limitation"):
        return {"status": "EXPECTED_LIMITATION", "reason": exp.get("expected_limitation")}

    # ── AUTH ────────────────────────────────────────────────────────
    if exp.get("no_private_leak", True) and _PRIVATE_LEAK_RE.search(reply):
        return {"status": "FAIL_AUTH", "reason": f"private-looking data in reply: {reply[:80]!r}"}
    if exp.get("public_no_identity_gate") and _CUSTCODE_ASK_RE.search(reply):
        return {"status": "FAIL_AUTH", "reason": "public turn pushed into an identity gate"}
    if exp.get("expect_identity_gate"):
        gated = bool(_CUSTCODE_ASK_RE.search(reply)) or last.get("routing") in ("API", "WEBHOOK", "WORKFLOW")
        if not gated:
            return {"status": "FAIL_AUTH", "reason": "private request did NOT reach an auth/identity gate"}

    # ── HALLUCINATION ──────────────────────────────────────────────
    for pf in exp.get("prohibited_facts", []):
        if pf and pf in reply:
            return {"status": "FAIL_HALLUCINATION", "reason": f"prohibited fact present: {pf!r}"}
    if exp.get("no_fake_action_success", True) and _FAKE_SUCCESS_RE.search(reply) \
            and last.get("routing") not in ("API", "WEBHOOK"):
        return {"status": "FAIL_HALLUCINATION", "reason": "claims action success with no execution"}

    # ── ROUTING (intent family + source) ───────────────────────────
    want_src = exp.get("source")
    got_src = observed_source(last) if want_src else None
    # When a DEDICATED deterministic flow (calculator) claimed the turn
    # via its OWN slot extractor and that is exactly what was expected,
    # the turn's `intent_family` label is not the routing decision — a
    # bare correction fragment ("ขอแก้เป็น 4 กิโล") can be mislabelled by
    # the isolated interpreter while the estimate flow still consumes it
    # correctly. Only skip the family assertion in that specific case;
    # never skip it when a private/ERP family leaked in.
    # SHIPMENT_STATUS / INVOICE / MY_COUPONS surfacing here would mean ERP
    # data leaked into a calculator turn — never skip the check for those.
    # ADDRESS_CHANGE is not ERP-backed (no record surfaced) so a spurious
    # label from a bare "ขอแก้เป็น N กิโล" fragment is tolerated.
    _priv_fams = {"SHIPMENT_STATUS", "INVOICE", "MY_COUPONS"}
    _flow_owned_ok = (want_src == "CALCULATOR" and got_src == "CALCULATOR"
                      and last.get("intent_family") not in _priv_fams
                      and last.get("selection_source") == "shipping_estimate_flow")
    if not _flow_owned_ok and not _fam_ok(exp.get("intent_family"), last.get("intent_family")):
        return {"status": "FAIL_ROUTING",
                "reason": f"intent_family {last.get('intent_family')!r} != expected {exp.get('intent_family')!r}"}
    if want_src:
        if got_src not in _SOURCE_EQUIV.get(want_src, {want_src}):
            return {"status": "FAIL_ROUTING",
                    "reason": f"source {got_src!r} (src={last.get('selection_source')!r} "
                              f"routing={last.get('routing')!r}) != expected {want_src!r}"}

    # ── CONTEXT / conversation act ────────────────────────────────
    if exp.get("conversation_act") and last.get("conversation_act") != exp["conversation_act"]:
        return {"status": "FAIL_CONTEXT",
                "reason": f"conversation_act {last.get('conversation_act')!r} != {exp['conversation_act']!r}"}
    if exp.get("must_not_repeat_prior_reply") and len(turns) >= 2:
        prev_asst = turns[-2].get("reply") or ""
        if prev_asst and _norm(prev_asst) == _norm(reply):
            return {"status": "FAIL_CONTEXT", "reason": "resent the identical previous reply"}

    # ── STATE ─────────────────────────────────────────────────────
    if exp.get("no_stale_flow", True) and last.get("selection_source") == "conversation_continuation" \
            and exp.get("source") not in ("WORKFLOW", "ERP") \
            and not exp.get("expect_identity_gate") \
            and not _CUSTCODE_ASK_RE.search(reply):
        return {"status": "FAIL_STATE", "reason": "a stale pending flow captured this turn"}
    for bad_id in exp.get("stale_identifiers_must_not_appear", []):
        cp = last.get("collected_parameters") or {}
        if bad_id in cp.values():
            return {"status": "FAIL_STATE", "reason": f"stale identifier {bad_id!r} reused"}

    # ── GROUNDING ────────────────────────────────────────────────
    for rf in exp.get("required_facts", []):
        if rf and rf not in reply:
            return {"status": "FAIL_GROUNDING", "reason": f"required fact missing: {rf!r}"}
    if exp.get("require_honest_no_info") and not _NOINFO_RE.search(reply):
        return {"status": "FAIL_GROUNDING", "reason": "owed an honest 'no confirmed info' answer, did not give one"}
    if exp.get("forbid_no_info") and _NOINFO_RE.search(reply):
        return {"status": "FAIL_GROUNDING", "reason": "dead-ended on 'no info' when the journey should continue"}

    # ── NEXT ACTION ─────────────────────────────────────────────
    if exp.get("require_next_question") and not _NEXT_Q_RE.search(reply):
        return {"status": "FAIL_NEXT_ACTION", "reason": "a follow-up question was required but not asked"}
    for slot_val in exp.get("supplied_slots_must_not_be_reasked", []):
        # a crude re-ask check: the reply asks for something the customer
        # already gave in an earlier turn
        if slot_val and slot_val.lower() in reply.lower() and _NEXT_Q_RE.search(reply):
            return {"status": "FAIL_NEXT_ACTION", "reason": f"re-asked an already-supplied slot ({slot_val!r})"}

    return {"status": "PASS", "reason": ""}


_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip()).rstrip(" ค่ะครับคะนะ")
