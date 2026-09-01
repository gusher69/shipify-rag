"""Deterministic source-of-truth conflict guard (P1.2B).

No LLM, no embeddings, no DB. Given several already-eligible trusted
evidence texts (active knowledge_chunks and/or the answers of
equivalently-matched FAQ rows), extract a SMALL set of high-risk
STRUCTURED facts — transport rate, transport duration, an entity's phone
— into normalized (key, value) pairs, and report any key that carries two
or more INCOMPATIBLE values across sources.

Scope is deliberately narrow: only scalar / range facts whose key can be
pinned precisely (transport mode + unit + segment, or entity + optional
sublocation). This is a safety net, not a general contradiction detector
— non-scalar policy-text contradictions ("คูปองคืนได้" vs "คูปองคืนไม่ได้")
are an explicit, documented out-of-scope limitation for this phase.

Normalization is used ONLY for comparison ("091-5050-775" == "0915050775",
"6,900" == "6900", "7-10" == "7–10"). The ORIGINAL trusted strings are
preserved for the answer and for the developer trace — existing
scalar-fidelity behaviour is untouched.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

# ── vocabulary (reused shape from rag/query_resolution.py &
#    services/answer_planner.py — never a second transport/warehouse list
#    of a different meaning) ─────────────────────────────────────────────
_MODE_RE = re.compile(r"ทางรถ|ทางบก|ทางเรือ")
_MODE_CANON = {"ทางรถ": "รถ", "ทางบก": "รถ", "ทางเรือ": "เรือ"}

_RATE_RE = re.compile(
    r"(\d[\d,]*)\s*บาท\s*/?\s*(กิโลกรัม|กก\.?|kg|CBM|คิว|ลบ\.?ม\.?)", re.IGNORECASE)
_UNIT_CANON = {
    "กิโลกรัม": "kg", "กก": "kg", "กก.": "kg", "kg": "kg",
    "cbm": "cbm", "คิว": "cbm", "ลบ.ม.": "cbm", "ลบ.ม": "cbm", "ลบม": "cbm",
}

_DUR_RE = re.compile(r"(\d+)\s*[–\-~]\s*(\d+)\s*วัน")
# A duration fact is only emitted when it can be pinned to a transport
# mode OR to one of these explicit segment markers — a bare "3–7 วัน"
# (e.g. a cancellation window) is never compared.
_CHINA_DOMESTIC_RE = re.compile(
    r"ร้านจีน|โรงงาน|ผู้ขาย|ซัพพลายเออร์|ซัพพลายเออร|ภายในจีน|ในจีนถึง|ถึงโกดังจีน|เข้าโกดังจีน|มาโกดังจีน")
_IMPORT_LEG_RE = re.compile(r"ถึงไทย|มาไทย|เข้าไทย|เข้าประเทศไทย|โกดังจีน.{0,6}(ไทย|ถึงไทย)|นำเข้า")

_SUBLOC_RE = re.compile(r"อ่อนนุช|นนทบุรี|บางใหญ่|บางม่วง|ลาดกระบัง|รังสิต|ลำลูกกา")
_COMPANY_RE = re.compile(r"shipify|ชิปปิฟาย|ชิปปิ้ง|fasttrade|ฟาสต์เทรด|ฟาสเทรด", re.IGNORECASE)
_COMPANY_CANON = [
    (re.compile(r"fasttrade|ฟาสต์เทรด|ฟาสเทรด", re.IGNORECASE), "fasttrade"),
    (re.compile(r"shipify|ชิปปิฟาย|ชิปปิ้ง", re.IGNORECASE), "shipify"),
]
# Thai phone-shaped run: 9–10 digits, optional - / spaces grouping.
_PHONE_RE = re.compile(r"0\d[\d\s\-]{7,12}\d")


# ── normalization (comparison only) ──────────────────────────────────
def normalize_number(s: str) -> str:
    return re.sub(r"[,\s]", "", s or "")


def normalize_phone(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def normalize_range(a: str, b: str) -> str:
    return f"{int(a)}-{int(b)}"


# ── data model ──────────────────────────────────────────────────────
@dataclass
class FactConflict:
    key: str
    normalized_values: List[str]
    original_values: List[str]
    sources: List[str]


@dataclass
class ConflictResult:
    conflicts: List[FactConflict] = field(default_factory=list)
    all_facts: List[Dict] = field(default_factory=list)  # developer trace

    def has_conflict(self) -> bool:
        return bool(self.conflicts)

    def keys(self) -> set:
        return {c.key for c in self.conflicts}

    def as_trace(self) -> List[Dict]:
        return [
            {"key": c.key, "normalized_values": c.normalized_values,
             "original_values": c.original_values, "sources": c.sources}
            for c in self.conflicts
        ]


# ── extraction ──────────────────────────────────────────────────────
def _mode_spans(text: str) -> List[Tuple[Optional[str], str]]:
    """Partition `text` so each transport-mode word owns the text from its
    own position up to the next mode word; text before the first mode
    word is owned by None."""
    marks = [(m.start(), _MODE_CANON[m.group(0)]) for m in _MODE_RE.finditer(text)]
    if not marks:
        return [(None, text)]
    spans: List[Tuple[Optional[str], str]] = []
    if marks[0][0] > 0:
        spans.append((None, text[: marks[0][0]]))
    for i, (pos, mode) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        spans.append((mode, text[pos:end]))
    return spans


def _duration_segment(span: str, mode: Optional[str]) -> Optional[str]:
    if _CHINA_DOMESTIC_RE.search(span) and "ไทย" not in span:
        return "china_domestic"
    if mode or _IMPORT_LEG_RE.search(span):
        return "import"
    return None  # unpinned -> not comparable this phase


# Local clause/segment breaks — a marker on the far side of one of these
# from the phone is not "the entity of THAT phone".
_CLAUSE_SEP_RE = re.compile(r"[/|,;\n\r•]|และ|หรือ")
# Costs
_FOLLOWING_BIAS = 25      # a marker AFTER the number is weaker than one before it
_CLAUSE_BREAK_PENALTY = 200
_MAX_ATTRIBUTION_COST = 200  # at/above this the marker is a different clause -> no attribution


def _phone_entity(text: str, start: int, end: int) -> Optional[str]:
    """Attribute one phone span to the entity marker most SPECIFICALLY
    associated with THAT number: the closest sublocation/company marker,
    preferring one that immediately precedes the number and is in the same
    local clause. When the nearest marker sits across a clause break
    ("/", ",", "และ", newline, ...), the number is left unattributed
    rather than forced onto an entity — that would be exactly what turns
    a combined "Shipify 02-… / Fasttrade 02-…" row into a false conflict."""
    markers: List = []  # (pos_start, pos_end, label)
    for m in _SUBLOC_RE.finditer(text):
        markers.append((m.start(), m.end(), m.group(0)))
    for rx, name in _COMPANY_CANON:
        for m in rx.finditer(text):
            markers.append((m.start(), m.end(), name))
    if not markers:
        return None

    best_label: Optional[str] = None
    best_cost: Optional[int] = None
    for ms, me, label in markers:
        if me <= start:                    # marker precedes the number
            gap = text[me:start]
            cost = (start - me)
        elif ms >= end:                    # marker follows the number
            gap = text[end:ms]
            cost = (ms - end) + _FOLLOWING_BIAS
        else:
            gap, cost = "", 0
        if _CLAUSE_SEP_RE.search(gap):
            cost += _CLAUSE_BREAK_PENALTY
        if best_cost is None or cost < best_cost:
            best_cost, best_label = cost, label

    if best_cost is None or best_cost >= _MAX_ATTRIBUTION_COST:
        return None                        # ambiguous -> never force an entity
    return best_label


def extract_facts(text: str, source_id: str) -> List[Dict]:
    text = text or ""
    facts: List[Dict] = []
    for mode, span in _mode_spans(text):
        for m in _RATE_RE.finditer(span):
            unit = _UNIT_CANON.get(m.group(2).lower().replace(" ", ""))
            if not unit or mode is None:  # a rate with no mode is ambiguous — skip
                continue
            facts.append({
                "key": f"rate|{mode}|{unit}",
                "normalized": normalize_number(m.group(1)),
                "original": m.group(0).strip(),
                "source": source_id,
            })
        for m in _DUR_RE.finditer(span):
            seg = _duration_segment(span, mode)
            if seg is None:
                continue
            facts.append({
                "key": f"duration|{mode or '_'}|{seg}",
                "normalized": normalize_range(m.group(1), m.group(2)),
                "original": m.group(0).strip(),
                "source": source_id,
            })
    for m in _PHONE_RE.finditer(text):
        entity = _phone_entity(text, m.start(), m.end())
        if not entity:  # no entity label -> ambiguous, never compared
            continue
        facts.append({
            "key": f"phone|{entity}",
            "normalized": normalize_phone(m.group(0)),
            "original": m.group(0).strip(),
            "source": source_id,
        })
    return facts


def detect_conflicts(evidence: Iterable[Tuple[str, str]]) -> ConflictResult:
    """`evidence`: iterable of (source_id, text)."""
    all_facts: List[Dict] = []
    for source_id, text in evidence:
        all_facts.extend(extract_facts(text, str(source_id)))

    by_key: Dict[str, List[Dict]] = {}
    for f in all_facts:
        by_key.setdefault(f["key"], []).append(f)

    conflicts: List[FactConflict] = []
    for key, facts in by_key.items():
        norms = list(dict.fromkeys(f["normalized"] for f in facts))
        if len(norms) >= 2:
            conflicts.append(FactConflict(
                key=key,
                normalized_values=norms,
                original_values=list(dict.fromkeys(f["original"] for f in facts)),
                sources=list(dict.fromkeys(f["source"] for f in facts)),
            ))
    return ConflictResult(conflicts=conflicts, all_facts=all_facts)


def detect_conflicts_in_chunks(chunks: Optional[List[Dict]]) -> ConflictResult:
    """Runs the detector over the FINAL evidence set. Each chunk's own
    text is one source; a FAQ-exact chunk that carried several
    equivalently-eligible alternative answers (rag/faq_matcher.py ->
    rag/searcher.py: `faq_conflict_texts`) contributes each of those as an
    extra source, so PATH 2 (a duplicate FAQ row with a conflicting
    answer, resolved by similarity order before it ever reaches normal
    context) is covered by the SAME detector."""
    evidence: List[Tuple[str, str]] = []
    for i, c in enumerate(chunks or []):
        sid = c.get("chunk_id") or c.get("citation") or f"chunk{i}"
        evidence.append((str(sid), c.get("text") or ""))
        for j, alt in enumerate(c.get("faq_conflict_texts") or []):
            evidence.append((f"{sid}#faq-alt{j}", alt or ""))
    return detect_conflicts(evidence)


# ── mapping conflicts -> requested components (P1.2A) ────────────────
_KEY_LABEL = {
    "rate|รถ|kg": "ค่าขนส่งทางรถ (บาท/กก.)",
    "rate|รถ|cbm": "ค่าขนส่งทางรถ (บาท/CBM)",
    "rate|เรือ|kg": "ค่าขนส่งทางเรือ (บาท/กก.)",
    "rate|เรือ|cbm": "ค่าขนส่งทางเรือ (บาท/CBM)",
    "duration|รถ|import": "ระยะเวลาขนส่งทางรถ",
    "duration|เรือ|import": "ระยะเวลาขนส่งทางเรือ",
    "duration|_|import": "ระยะเวลาการนำเข้า",
    "duration|_|china_domestic": "ระยะเวลาจากร้านจีนถึงโกดังจีน",
}


def _key_to_label(key: str) -> str:
    if key in _KEY_LABEL:
        return _KEY_LABEL[key]
    if key.startswith("phone|"):
        return f"เบอร์ติดต่อ ({key.split('|', 1)[1]})"
    return key


def components_with_conflict(request_components: Optional[List],
                              result: ConflictResult,
                              request_spec=None) -> List[str]:
    """Returns the customer-meaningful labels that must be marked
    conflicting. For a P1.2A multi-component turn these are a SUBSET of
    the request's own component labels ("รถ / rate"); for a single-
    component turn a label is synthesized straight from the conflicting
    fact key so the guard still applies."""
    if not result.has_conflict():
        return []
    ck = result.keys()
    labels: List[str] = []
    for entry in (request_components or []):
        label = entry[0] if isinstance(entry, (list, tuple)) else entry
        parts = [p.strip() for p in str(label).split("/")]
        if len(parts) != 2:
            continue
        mode, facet = parts
        if facet == "rate" and any(k.startswith(f"rate|{mode}|") for k in ck):
            labels.append(label)
        elif facet == "duration" and any(k.startswith(f"duration|{mode}|") for k in ck):
            labels.append(label)
    if labels:
        return labels
    # No P1.2A component matched (single-component turn, or a phone
    # conflict) — surface the conflict directly from its key.
    return [_key_to_label(k) for k in sorted(ck)]
