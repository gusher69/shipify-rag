# -*- coding: utf-8 -*-
"""PHASE-6E — response-style evaluator (heuristic, deterministic).

Scores a customer-facing reply string against the PHASE-6E style
principles. STYLE ONLY — it never inspects or judges routing / source /
business truth (those are the delta regression's job). Returns per-axis
booleans plus an overall pass.
"""
from __future__ import annotations
import re
from typing import Dict, List

# hard: internal / system wording that must never reach a customer
_INTERNAL_RE = re.compile(
    r"SAFE_FALLBACK|KB_NOT_FOUND|\bRAG\b|\bERP\b|\bintent(?:_family)?\b|\bworkflow\b|"
    r"pending state|pending_confirmation|classifier|decision[ _]engine|business[ _]action|"
    r"vector ?db|embedding|\[RAG|ฐานความรู้|selection_source|routing_type|actionable_intent|"
    r"handoff_status|null\b|None\b|\{'|\[\{", re.IGNORECASE)

# soft internal-ish phrasing the style guidance discourages
_SOFT_INTERNAL_RE = re.compile(r"ไม่พบข้อมูลใน(?:ระบบ|ฐานข้อมูล)|ไม่พบข้อมูลที่เกี่ยวข้อง|ในฐานความรู้")

_ROBOTIC_RE = re.compile(r"กรุณาระบุ|โปรดระบุ|โปรดกรอก|กรุณากรอก|โปรดส่ง|กรุณาป้อน|ระบุข้อมูลต่อไปนี้")

_GREETING_RE = re.compile(r"^\s*(?:สวัสดี|หวัดดี|ยินดีต้อนรับ|สวัสดีค่ะ|สวัสดีครับ)")

_NOINFO_RE = re.compile(r"ยังไม่มีข้อมูล|ยืนยันไม่ได้|ยังยืนยันไม่ได้|ไม่มีข้อมูลที่ยืนยัน")

_FAKE_HANDOFF_RE = re.compile(
    r"เดี๋ยว(?:แอดมิน|เจ้าหน้าที่|ทีมงาน)(?:จะ)?(?:ตรวจสอบ|ติดต่อกลับ|ดำเนินการ)|"
    r"ประสาน(?:งาน)?(?:ให้)?(?:เจ้าหน้าที่|แอดมิน|ทีมงาน)|ทีมงานจะติดต่อกลับ|"
    r"ส่งเรื่องให้(?:เจ้าหน้าที่|แอดมิน)(?:แล้ว|ให้แล้ว)")

_NEXT_Q_RE = re.compile(
    r"\?|ไหมคะ|ไหมครับ|มั้ยคะ|ยังไงคะ|อย่างไรคะ|อะไรคะ|ไหนคะ|หรือคะ|หรือครับ|"
    r"เท่าไหร่(?:คะ|ครับ)?|กี่\S+(?:คะ|ครับ)|รบกวน(?:แจ้ง|ขอ|ส่ง)|ขอ[^\n]{0,20}(?:หน่อย|ด้วย)(?:นะ)?คะ|"
    r"ส่ง[^\n]{0,12}มาได้เลย|บอกได้เลย|แจ้งมาได้")

_OFFER_OPEN_RE = re.compile(
    r"บอกได้เลย|สอบถาม[^\n]{0,14}ได้|ส่ง[^\n]{0,12}มาได้|ยินดีช่วย|ถามได้เลย|\?|"
    r"ติดต่อ[^\n]{0,24}(?:ได้|นะคะ)|ติดตาม[^\n]{0,20}ได้|แนะนำให้[^\n]{0,20}ติดต่อ|"
    r"ทักมา[^\n]{0,14}(?:ได้|เลย)|ช่องทาง[^\n]{0,20}(?:ได้|ค่ะ)|LINE\s*:?\s*@?Shipify")

_POLITE_GATE_RE = re.compile(r"(?:รหัสลูกค้า|ยืนยันตัวตน|เลขบิล|เบอร์ที่ผูก|เลขที่บิล)")

_PRIVATE_LEAK_RE = re.compile(
    r"เลขบัตรประชาชน|\b\d{13}\b|รหัสผ่าน|api[_ ]?key|ยอดคงเหลือ\s*[\d,]+\s*บาท")

_SLOT_WORDS = {
    "weight": re.compile(r"น้ำหนัก|กี่กิโล|กี่โล"),
    "dims": re.compile(r"ขนาด|กว้าง.?ยาว.?สูง|กxยxส"),
    "method": re.compile(r"ทางรถหรือทางเรือ|ขนส่งแบบไหน|ทางไหน"),
    "brand": re.compile(r"แบรนด์\s*(?:SP|FT)|SP หรือ FT"),
}
_ASK_RE = re.compile(r"รบกวน(?:แจ้ง|ขอ)|ขอ[^\n]{0,14}(?:หน่อย|ด้วย)|ต้องการทราบ|แจ้ง[^\n]{0,10}ด้วย")


def _sentences(t: str) -> List[str]:
    return [s for s in re.split(r"[\n。]|(?<=[ค่ะครับคะนะ])\s+", t) if s.strip()]


def evaluate(reply: str, case: Dict, *, routing: str = None, prior_assistant: str = "") -> Dict:
    exp = case.get("style_expect") or {}
    r = (reply or "").strip()
    axes: Dict[str, bool] = {}
    notes: List[str] = []

    # NO_INTERNAL_LANGUAGE (hard)
    m = _INTERNAL_RE.search(r)
    axes["NO_INTERNAL_LANGUAGE"] = not m
    if m:
        notes.append(f"internal term: {m.group(0)!r}")

    # NATURALNESS — robotic wording is the multi-item "กรุณาระบุ A, B, C"
    # shape (see _ROBOTIC_RE). A short polite "กรุณาแจ้ง<one thing>ค่ะ" is
    # ordinary Thai CS and is fine.
    natural = not _ROBOTIC_RE.search(r) and not _SOFT_INTERNAL_RE.search(r) and len(r) > 0
    if re.match(r"^\s*กรุณา\S*ระบุ|^\s*โปรด", r):
        natural = False
        notes.append("robotic opening")
    axes["NATURALNESS"] = natural

    # CONCISENESS — simple turns stay short; detail allowed only if asked
    asked_detail = any(k in "".join(case["turns"][-1:] if case["turns"] else "")
                       for k in ("ละเอียด", "ทั้งหมด", "อธิบาย"))
    limit = 900 if asked_detail else 520
    axes["CONCISENESS"] = len(r) <= limit and len(_sentences(r)) <= (10 if asked_detail else 6)
    if not axes["CONCISENESS"]:
        notes.append(f"len={len(r)} sents={len(_sentences(r))}")

    # CONTEXT_CONTINUITY — no fresh greeting mid-conversation
    if exp.get("no_greeting_repeat") or prior_assistant:
        axes["CONTEXT_CONTINUITY"] = not _GREETING_RE.search(r)
        if _GREETING_RE.search(r):
            notes.append("greeting repeated mid-conversation")
    else:
        axes["CONTEXT_CONTINUITY"] = True

    # NO_REPEATED_SLOT
    ok_slot = True
    supplied = set()
    joined_hist = " ".join(t if isinstance(t, str) else t.get("content", "") for t in case["turns"])
    if re.search(r"\d+\s*(?:กิโล|กก|โล)", joined_hist):
        supplied.add("weight")
    if re.search(r"\d+\s*[x×]\s*\d+\s*[x×]\s*\d+", joined_hist):
        supplied.add("dims")
    if re.search(r"ทางรถ|ทางเรือ", joined_hist):
        supplied.add("method")
    # strip an acknowledgement parenthetical ("รับทราบค่ะ (น้ำหนัก 10 กก.) …")
    # before checking — naming a supplied slot there is confirmation, not a re-ask.
    r_wo_ack = re.sub(r"\((?:[^()]*)\)", "", r) if "รับทราบ" in r or "รับ ทราบ" in r else r
    for s in supplied:
        if _SLOT_WORDS[s].search(r_wo_ack) and _ASK_RE.search(r_wo_ack):
            ok_slot = False
            notes.append(f"re-asks supplied slot: {s}")
    axes["NO_REPEATED_SLOT"] = ok_slot

    # NEXT_ACTION_QUALITY
    if exp.get("needs_next_question"):
        axes["NEXT_ACTION_QUALITY"] = bool(_NEXT_Q_RE.search(r))
        if not axes["NEXT_ACTION_QUALITY"]:
            notes.append("no next question where one was expected")
    else:
        axes["NEXT_ACTION_QUALITY"] = True

    # TRUTH_PRESERVED (style-side only): when a no-information reply DOES
    # happen it must use natural wording (no "ไม่พบข้อมูลในระบบ/ฐานความรู้"),
    # a staff follow-up is only claimed on a real handoff, a fabricated
    # figure is never invented, and a pure how-to is not deflected.
    tp = True
    if _SOFT_INTERNAL_RE.search(r):
        tp = False
        notes.append("system-flavoured no-info wording")
    if exp.get("no_fake_handoff") and _FAKE_HANDOFF_RE.search(r) and routing != "HUMAN_HANDOFF":
        tp = False
        notes.append("claims a staff follow-up with no real handoff")
    if exp.get("honest_no_info") and re.search(r"\d+(?:\.\d+)?\s*(?:%|เปอร์เซ็นต์|บาท/(?:กิโล|CBM))", r):
        # a genuinely-unavailable fact must not be answered with an invented figure
        tp = False
        notes.append("invented a specific figure for an unavailable fact")
    if exp.get("general_knowhow") and _NOINFO_RE.search(r) and len(r) < 90:
        tp = False
        notes.append("general how-to answered with a no-info deflection")
    axes["TRUTH_PRESERVED"] = tp

    # keep_open — a KB miss must leave the customer a way forward: an
    # offer, a contact channel, or a follow-up question.
    if exp.get("keep_open"):
        axes["KEEP_OPEN"] = bool(_OFFER_OPEN_RE.search(r) or _NEXT_Q_RE.search(r))
        if not axes["KEEP_OPEN"]:
            notes.append("KB miss dead-ended without keeping the conversation open")

    # polite identity gate — a private request should ask for the ONE
    # required identifier, with a polite particle, and nothing robotic
    # ("กรุณาแจ้งรหัสลูกค้าค่ะ" qualifies; "กรุณาระบุ A, B, C" does not).
    if exp.get("expect_polite_gate"):
        gated = bool(_POLITE_GATE_RE.search(r)) or bool(_NOINFO_RE.search(r))
        polite = bool(re.search(r"ค่ะ|คะ|นะคะ|ครับ", r)) and not _ROBOTIC_RE.search(r)
        axes["POLITE_GATE"] = gated and polite
        if not axes["POLITE_GATE"]:
            notes.append("private request not gated politely")

    if exp.get("no_private_leak") and _PRIVATE_LEAK_RE.search(r):
        axes["NO_PRIVATE_LEAK"] = False
        notes.append("private-looking data in reply")
    elif exp.get("no_private_leak"):
        axes["NO_PRIVATE_LEAK"] = True

    hard_fail = (not axes["NO_INTERNAL_LANGUAGE"]
                 or not axes.get("NO_PRIVATE_LEAK", True)
                 or not axes["TRUTH_PRESERVED"])
    soft_fail = sum(1 for k, v in axes.items() if not v)
    status = "PASS" if (not hard_fail and soft_fail == 0) else (
        "FAIL_HARD" if hard_fail else "FAIL_SOFT")
    return {"status": status, "axes": axes, "notes": notes, "reply": r[:400]}
