# -*- coding: utf-8 -*-
"""CUSTOMER-RAG-2.1 — charter-truck (เหมารถ / TC19) multi-turn slot
collection.

TC19's customer-approved flow is: confirm the service (the FAQ answer,
CUSTOMER-RAG-2) -> collect 4 fields over one or more turns, remembering
what was already given -> when all 4 are present, hand to Human CS.

    slots: bill (ShipmentCode) · destination · recipient_name · recipient_phone

This is a DETERMINISTIC, history-derived collector — the same
"assistant reply IS the state" pattern services/conversation_semantics.py
(SEM-GEN-1) uses, so no new pending-state table and no LLM. It runs
BEFORE the identifier-driven Business-Action search in the Decision
Engine, so a bill/tracking id supplied here does NOT resurrect
`searchdatashipment`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from services.slot_filling_engine import _validate_generic_identifier as _valid_id

# The TC19 FAQ answer (CUSTOMER-RAG-2) and any charter missing-slot
# prompt — either as the most recent assistant turn opens the collection.
_TC19_ANSWER_RE = re.compile(r"มีบริการเหมารถให้ได้|เหมารถให้ได้นะคะ")
_CHARTER_PROMPT_RE = re.compile(r"เหมารถ.{0,40}(รบกวนแจ้ง|แจ้ง(เลขบิล|ปลายทาง|ชื่อผู้รับ|เบอร์))")
_CHARTER_DONE_RE = re.compile(r"ประสานงานเรื่องเหมารถ|รับเรื่องเหมารถ")

_FRAME_LOOKBACK = 10

# field extractors — marker-anchored first, then bare-token fallback.
_BILL_MARKER_RE = re.compile(r"(?:เลขบิล|บิลขนส่ง|เลขที่บิล|บิล|ship\w*|FT)\s*[:：]?\s*([A-Za-z]{1,3}\d{6,})", re.IGNORECASE)
_DEST_MARKER_RE = re.compile(
    r"(?:ปลายทาง|โลเคชั่น|โลเคชัน|ส่งไปที่|ส่งไป|ส่งที่|จัดส่งไปที่|จังหวัด|อำเภอ|เขต)\s*[:：]?\s*"
    r"([ก-๙A-Za-z][ก-๙A-Za-z0-9 .\-]{1,28}?)(?=\s*(?:ชื่อผู้รับ|ผู้รับ|เบอร์|โทร|บิล|เลขบิล|$|\n|,))")
_NAME_MARKER_RE = re.compile(
    r"(?:ชื่อผู้รับ|ชื่อคนรับ|ผู้รับชื่อ|คนรับชื่อ|ชื่อ)\s*[:：]?\s*"
    r"((?:คุณ|นาย|นาง|นางสาว|น\.ส\.)?\s*[ก-๙A-Za-z][ก-๙A-Za-z .\-]{0,28}?)"
    r"(?=\s*(?:เบอร์|โทร|มือถือ|ปลายทาง|บิล|เลขบิล|$|\n|,))")
_PHONE_RE = re.compile(r"(?<!\d)(0\d[\d\- ]{7,10}\d)(?!\d)")
_ORDINARY_ID_STOP = re.compile(r"^(?:เลขบิล|บิล|ปลายทาง|ผู้รับ|เบอร์|โทร|ชื่อ)$")


@dataclass
class CharterState:
    bill: Optional[str] = None
    destination: Optional[str] = None
    recipient_name: Optional[str] = None
    recipient_phone: Optional[str] = None

    def as_dict(self) -> Dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    def missing(self) -> List[str]:
        order = [("bill", self.bill), ("destination", self.destination),
                 ("recipient_name", self.recipient_name), ("recipient_phone", self.recipient_phone)]
        return [k for k, v in order if not v]

    def complete(self) -> bool:
        return not self.missing()


def _norm_phone(raw: str) -> str:
    return re.sub(r"[\s\-]", "", raw or "")


def extract_charter_fields(message: str, into: Optional[CharterState] = None) -> CharterState:
    """Merge whatever charter-truck fields the message supplies into
    `into` (current wording wins). Field order is irrelevant."""
    st = into or CharterState()
    t = message or ""

    if not st.bill:
        m = _BILL_MARKER_RE.search(t)
        if m:
            st.bill = m.group(1).upper()
        else:
            for tok in re.split(r"\s+", t):
                tok = tok.strip(" .,:")
                if tok and not tok.isdigit() and _valid_id(tok) and re.match(r"^[A-Za-z]{1,3}\d{6,}$", tok):
                    st.bill = tok.upper()
                    break

    if not st.destination:
        m = _DEST_MARKER_RE.search(t)
        if m:
            d = m.group(1).strip(" .,:")
            if d and not _ORDINARY_ID_STOP.match(d):
                st.destination = d

    if not st.recipient_name:
        m = _NAME_MARKER_RE.search(t)
        if m:
            n = re.sub(r"^(?:คุณ|นาย|นาง|นางสาว|น\.ส\.)\s*", "", m.group(1).strip(" .,:")).strip()
            if n and not _ORDINARY_ID_STOP.match(n) and not n.isdigit():
                st.recipient_name = n

    if not st.recipient_phone:
        m = _PHONE_RE.search(t)
        if m:
            p = _norm_phone(m.group(1))
            if 9 <= len(p) <= 10:
                st.recipient_phone = p

    return st


def derive_charter_state(history: Optional[List[Dict]]) -> Optional[CharterState]:
    """Reconstruct the active charter-truck collection from recent turns,
    or None when no collection is open (or it was already completed /
    handed off). Deterministic, no LLM."""
    turns = list(history or [])[-_FRAME_LOOKBACK:]
    if not turns:
        return None

    open_idx = None
    for i in range(len(turns) - 1, -1, -1):
        t = turns[i]
        c = t.get("content") or ""
        if t.get("role") != "assistant":
            continue
        if _CHARTER_DONE_RE.search(c):
            return None  # this collection already handed off
        if _TC19_ANSWER_RE.search(c) or _CHARTER_PROMPT_RE.search(c):
            open_idx = i
            break
    if open_idx is None:
        return None

    st = CharterState()
    for t in turns[open_idx + 1:]:
        if t.get("role") == "user":
            extract_charter_fields(t.get("content") or "", st)
    return st


_FIELD_TH = {
    "bill": "เลขบิลขนส่ง",
    "destination": "โลเคชั่นปลายทาง",
    "recipient_name": "ชื่อผู้รับ",
    "recipient_phone": "เบอร์โทรผู้รับ",
}


def charter_missing_prompt(state: CharterState) -> str:
    miss = state.missing()
    if not miss:
        return ""
    got = []
    if state.bill:
        got.append(f"เลขบิล {state.bill}")
    if state.destination:
        got.append(f"ปลายทาง {state.destination}")
    if state.recipient_name:
        got.append(f"ผู้รับ {state.recipient_name}")
    if state.recipient_phone:
        got.append(f"เบอร์ {state.recipient_phone}")
    ack = f"รับทราบค่ะ ({' • '.join(got)}) " if got else ""
    want = " และ ".join(_FIELD_TH[k] for k in miss)
    return f"{ack}รบกวนแจ้ง{want}เพิ่มเติมด้วยนะคะ"


def charter_handoff_summary(state: CharterState) -> str:
    return ("คำขอใช้บริการเหมารถ (ส่งต่อในไทย) — "
            f"เลขบิล: {state.bill or '-'} | ปลายทาง: {state.destination or '-'} | "
            f"ชื่อผู้รับ: {state.recipient_name or '-'} | เบอร์ผู้รับ: {state.recipient_phone or '-'}")
