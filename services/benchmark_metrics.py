"""Pure, deterministic RAG benchmark metrics — no DB, no LLM, no I/O.
Kept separate from services/benchmark_service.py (which handles
persistence/orchestration) so every metric here is trivially unit
testable in isolation.
"""
import re
from typing import Dict, List, Optional


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def recall_at_k(retrieved_files: List[str], expected_file: Optional[str], k: int) -> bool:
    if not expected_file:
        return True  # nothing expected -> vacuously satisfied
    target = _norm(expected_file)
    return any(_norm(f) == target for f in retrieved_files[:k])


def reciprocal_rank(retrieved_files: List[str], expected_file: Optional[str]) -> float:
    if not expected_file:
        return 1.0
    target = _norm(expected_file)
    for i, f in enumerate(retrieved_files):
        if _norm(f) == target:
            return 1.0 / (i + 1)
    return 0.0


def precision_at_k(retrieved_files: List[str], expected_file: Optional[str], k: int) -> float:
    """Single-relevant-document precision@k: 1/k if the expected file is
    anywhere in the top-k, else 0 — standard for a benchmark where each
    case has exactly one expected source document."""
    if not expected_file:
        return 1.0
    return (1.0 / k) if recall_at_k(retrieved_files, expected_file, k) else 0.0


def expected_section_hit(retrieved_sections: List[Optional[str]], expected_section: Optional[str], k: int) -> bool:
    if not expected_section:
        return True
    target = _norm(expected_section)
    return any(_norm(s) == target for s in retrieved_sections[:k])


def prohibited_file_hit(retrieved_files: List[str], prohibited_files: List[str], k: int) -> List[str]:
    """Returns which prohibited files (if any) leaked into the top-k."""
    prohibited_norm = {_norm(f) for f in (prohibited_files or [])}
    if not prohibited_norm:
        return []
    return [f for f in retrieved_files[:k] if _norm(f) in prohibited_norm]


def _contains(text: str, phrase: str) -> bool:
    return _norm(phrase) in _norm(text)


def must_include_check(answer: str, must_include: List[str]) -> Dict:
    missing = [p for p in (must_include or []) if not _contains(answer, p)]
    return {"pass": not missing, "missing": missing}


def must_not_include_check(answer: str, must_not_include: List[str]) -> Dict:
    violations = [p for p in (must_not_include or []) if _contains(answer, p)]
    return {"pass": not violations, "violations": violations}


_THAI_RE = re.compile(r"[฀-๿]")


def detect_answer_language(text: str) -> str:
    if not text:
        return "English"
    thai_chars = len(_THAI_RE.findall(text))
    letters = len(re.findall(r"[^\W\d_]", text, re.UNICODE))
    if letters == 0:
        return "English"
    ratio = thai_chars / letters
    if ratio > 0.6:
        return "Thai"
    if ratio > 0.05:
        return "Mixed Thai-English"
    return "English"


def language_correct(answer: str, expected_language: Optional[str]) -> bool:
    if not expected_language:
        return True
    detected = detect_answer_language(answer)
    if expected_language == "Mixed Thai-English":
        return detected in ("Mixed Thai-English", "Thai")  # a mixed-expected case answered fully in Thai still passes
    return detected == expected_language


def citation_correct(cited_files: List[str], expected_file: Optional[str]) -> bool:
    if not expected_file:
        return True
    target = _norm(expected_file)
    return any(_norm(f) == target for f in cited_files)


def answerability_correct(actual: Optional[str], expected: Optional[str]) -> bool:
    if not expected:
        return True
    return _norm(actual) == _norm(expected)


def p95(latencies: List[float]) -> float:
    if not latencies:
        return 0.0
    s = sorted(latencies)
    idx = min(len(s) - 1, int(round(0.95 * (len(s) - 1))))
    return s[idx]


import unicodedata

# ── Phase 2: Query Understanding metrics ────────────────────────────
# Deterministic comparisons ONLY — no embedding similarity, no LLM.
# Every function here follows the same {"expected":..., "actual":...,
# "pass": bool} shape so the Failure Inspector (Part 7) never needs a
# metric-specific renderer.

_THAI_DIGIT_MAP = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def normalize_text(s: Optional[str]) -> str:
    """Thai spacing/punctuation/case/whitespace/digit normalization
    shared by every Phase 2 exact/token-set comparison — deliberately
    NOT embedding similarity (Part 5 rule: fuzzy matching must never
    hide a factually wrong answer)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_THAI_DIGIT_MAP)
    s = s.lower()
    s = re.sub(r"[^\w\s฀-๿]", "", s)  # strip punctuation, keep Thai script + word chars
    s = re.sub(r"\s+", " ", s).strip()
    return s


def field_match(expected: Optional[str], actual: Optional[str]) -> Dict:
    """Generic normalized-exact-match metric row — used for topic/
    subtopic/intent/canonical_query/resolved_query/transition. A None/
    empty `expected` is vacuously satisfied (nothing was asserted for
    this case), matching every other metric's "no expectation -> pass"
    convention in this module."""
    if not expected:
        return {"expected": expected, "actual": actual, "pass": True}
    return {"expected": expected, "actual": actual, "pass": normalize_text(expected) == normalize_text(actual)}


def entity_accuracy(expected: Optional[Dict], actual: Optional[Dict]) -> Dict:
    """Exact match (every expected slot equals its actual value) AND a
    partial-match fraction (slots that matched / slots expected) — a
    case can specify only the slots it cares about; slots the case
    doesn't mention are never compared."""
    expected = expected or {}
    actual = actual or {}
    if not expected:
        return {"expected": {}, "actual": actual, "exact_match": True, "partial_match": 1.0, "mismatched_keys": []}
    mismatched = [k for k, v in expected.items() if normalize_text(str(v)) != normalize_text(str(actual.get(k)))]
    partial = 1.0 - (len(mismatched) / len(expected))
    return {"expected": expected, "actual": actual, "exact_match": not mismatched,
            "partial_match": round(partial, 4), "mismatched_keys": mismatched}


def excluded_entity_accuracy(expected: Optional[Dict], actual: Optional[Dict]) -> Dict:
    """Same shape as entity_accuracy but for excluded_entities
    ({"location": [...], "transport": [...]}) — list-valued slots
    compared as sets so ordering never causes a false mismatch."""
    expected = expected or {}
    actual = actual or {}
    if not any(expected.values()):
        return {"expected": expected, "actual": actual, "exact_match": True, "mismatched_keys": []}
    mismatched = []
    for k, v in expected.items():
        exp_set = {normalize_text(x) for x in (v or [])}
        act_set = {normalize_text(x) for x in (actual.get(k) or [])}
        if exp_set != act_set:
            mismatched.append(k)
    return {"expected": expected, "actual": actual, "exact_match": not mismatched, "mismatched_keys": mismatched}


def conversation_state_accuracy(expected: Optional[Dict], actual: Optional[Dict]) -> Dict:
    """Per-field comparison over whichever conversation_state keys the
    case actually specifies expectations for (topic/subtopic/intent/
    transition/location/transport/... ) — never penalizes a field the
    case left unspecified."""
    expected = expected or {}
    actual = actual or {}
    if not expected:
        return {"expected": {}, "actual": actual, "exact_match": True, "mismatched_keys": []}
    mismatched = [k for k, v in expected.items() if normalize_text(str(v)) != normalize_text(str(actual.get(k)))]
    return {"expected": expected, "actual": actual, "exact_match": not mismatched, "mismatched_keys": mismatched}


def query_understanding_score(qu_metrics: Dict) -> float:
    """Fraction of the individual QU field checks that passed — used
    for the aggregate 'Query Understanding Accuracy' overview card
    (Part 5), never as a substitute for showing each field independently."""
    checks = []
    for key in ("topic", "subtopic", "intent", "canonical_query", "resolved_query", "transition"):
        row = qu_metrics.get(key)
        if row is not None:
            checks.append(bool(row.get("pass")))
    for key in ("entities", "excluded_entities", "conversation_state"):
        row = qu_metrics.get(key)
        if row is not None:
            checks.append(bool(row.get("exact_match")))
    return round(sum(checks) / len(checks), 4) if checks else 1.0


# ── Phase 2: Critical Fact evaluation ───────────────────────────────
# Structured price/fee/percent/date/phone/... facts — Part 3's answer
# to "text similarity is insufficient": a wrong number must never be
# hidden by an otherwise-fluent, superficially-similar answer.

_UNIT_ALIASES = {
    "thb": {"thb", "บาท", "฿", "baht"},
    "percent": {"%", "percent", "ร้อยละ", "เปอร์เซ็นต์"},
    "days": {"day", "days", "วัน"},
}
_UNIT_CANONICAL = {alias: canon for canon, aliases in _UNIT_ALIASES.items() for alias in aliases}


def _canonical_unit(unit: Optional[str]) -> str:
    u = normalize_text(unit)
    return _UNIT_CANONICAL.get(u, u)


def _extract_numbers_near_unit(text: str, unit: Optional[str]) -> List[float]:
    """Every number in `text` that appears within a short window of a
    mention of `unit` (or any of its aliases, or anywhere in the text
    if no unit is given) — a dependency-free stand-in for full NLU
    number-unit association."""
    norm = normalize_text(text)
    aliases = _UNIT_ALIASES.get(_canonical_unit(unit), {normalize_text(unit)}) if unit else None
    numbers = []
    for m in re.finditer(r"\d+(?:\.\d+)?", norm):
        if not aliases:
            numbers.append(float(m.group(0)))
            continue
        window = norm[max(0, m.start() - 15): m.end() + 15]
        if any(a in window for a in aliases):
            numbers.append(float(m.group(0)))
    return numbers


def evaluate_critical_fact(fact: Dict, answer_text: str) -> Dict:
    """One fact ({"key","value","unit","required"}) against the answer
    text. Status is one of: correct, wrong_value, wrong_unit,
    missing, conflicting (multiple candidate numbers found near the
    unit, none matching, i.e. the answer states a DIFFERENT number for
    the same unit rather than simply omitting it)."""
    key, expected_value, unit = fact.get("key"), fact.get("value"), fact.get("unit")
    required = fact.get("required", True)
    candidates = _extract_numbers_near_unit(answer_text or "", unit)

    try:
        expected_num = float(expected_value)
    except (TypeError, ValueError):
        expected_num = None

    if not candidates:
        status = "missing" if required else "not_applicable"
    elif expected_num is not None and any(abs(c - expected_num) < 1e-6 for c in candidates):
        status = "correct"
    elif expected_num is not None:
        status = "conflicting" if len(candidates) > 1 else "wrong_value"
    else:
        status = "missing"

    return {"key": key, "expected_value": expected_value, "unit": unit,
            "found_candidates": candidates, "status": status,
            "pass": status in ("correct", "not_applicable")}


def evaluate_critical_facts(facts: List[Dict], answer_text: str) -> Dict:
    """All facts for one case — see evaluate_critical_fact for the
    per-fact shape. `pass` is True only when every REQUIRED fact
    passes (an optional fact's absence never fails the case)."""
    facts = facts or []
    results = [evaluate_critical_fact(f, answer_text) for f in facts]
    required_results = [r for r, f in zip(results, facts) if f.get("required", True)]
    return {"facts": results, "pass": all(r["pass"] for r in required_results) if required_results else True,
            "correct_count": sum(1 for r in results if r["status"] == "correct"),
            "total_count": len(results)}


# ── Phase 2: Grounding evaluation ────────────────────────────────────
# Citation PRESENCE is not enough (Part 4) — this measures whether a
# cited source was actually retrieved, whether prohibited sources leak
# in, and whether critical facts have textual support in the retrieved/
# cited evidence. Deliberately deterministic substring-based "support"
# (no LLM claim extraction) — a conservative approximation, documented
# as a known limitation, not a hallucination-detector replacement.

GROUNDING_STATUSES = ("supported", "partially_supported", "unsupported", "contradicted", "not_applicable")

# Correct-abstention phrases — a case whose expected_answerability is
# "no_information" and whose answer matches one of these has correctly
# declined to invent an answer. Grounding Failure Audit (Production
# Validation) found evaluate_grounding() was marking every such answer
# "unsupported" purely because it has no citation, which is a metric
# false negative — a correct abstention has NOTHING to cite by
# definition, so absence of a citation must not read as ungrounded.
_ABSTENTION_PHRASES = (
    "ไม่มีข้อมูล", "ขออภัย", "ทีมงานจะติดต่อกลับ", "ไม่มีรายละเอียด",
    "no information", "not available", "i'm sorry", "i am sorry", "no data",
)


def _is_correct_abstention(answer_text: Optional[str]) -> bool:
    if not answer_text:
        return False
    norm = normalize_text(answer_text)
    return any(normalize_text(p) in norm for p in _ABSTENTION_PHRASES)


def evaluate_grounding(*, answer_text: Optional[str], citations: List[Dict], retrieved_chunks: List[Dict],
                        prohibited_files: Optional[List[str]] = None, critical_facts: Optional[List[Dict]] = None,
                        expected_answerability: Optional[str] = None) -> Dict:
    """Returns the full grounding payload for one case:
        {
          "citation_exists": bool, "citation_sources_retrieved": bool,
          "prohibited_citation": Optional[str],
          "critical_fact_support": [{"key":..., "supported": bool}, ...],
          "unsupported_critical_facts": [key, ...],
          "status": one of GROUNDING_STATUSES,
        }

    `expected_answerability`, if given, enables the correct-abstention
    branch: when the case expects "no_information" and the answer text
    is itself a correct abstention (see _is_correct_abstention), status
    is "supported" — declining to invent an answer for an unanswerable
    question IS the grounded behavior, never "unsupported" merely
    because there was nothing to cite.
    """
    if not answer_text:
        return {"citation_exists": False, "citation_sources_retrieved": True, "prohibited_citation": None,
                "critical_fact_support": [], "unsupported_critical_facts": [], "status": "not_applicable"}

    if expected_answerability == "no_information" and _is_correct_abstention(answer_text):
        return {"citation_exists": False, "citation_sources_retrieved": True, "prohibited_citation": None,
                "critical_fact_support": [], "unsupported_critical_facts": [], "status": "supported"}

    citations = citations or []
    retrieved_files = {_norm(c.get("file_name")) for c in (retrieved_chunks or [])}
    cited_files = {_norm(c.get("file_name")) for c in citations}
    prohibited_norm = {_norm(f) for f in (prohibited_files or [])}

    citation_exists = bool(citations)
    citation_sources_retrieved = cited_files.issubset(retrieved_files) if citations else True
    prohibited_hit = next((f for f in cited_files if f in prohibited_norm), None)

    evidence_text = normalize_text(" ".join((c.get("text") or "") for c in (retrieved_chunks or [])))
    fact_support = []
    for fact in (critical_facts or []):
        candidates = _extract_numbers_near_unit(evidence_text, fact.get("unit"))
        try:
            expected_num = float(fact.get("value"))
        except (TypeError, ValueError):
            expected_num = None
        supported = expected_num is not None and any(abs(c - expected_num) < 1e-6 for c in candidates)
        fact_support.append({"key": fact.get("key"), "supported": supported})

    unsupported = [f["key"] for f in fact_support if not f["supported"]]

    if prohibited_hit:
        status = "contradicted"
    elif not citation_exists:
        status = "unsupported"
    elif not citation_sources_retrieved:
        status = "unsupported"
    elif fact_support and unsupported:
        status = "unsupported" if len(unsupported) == len(fact_support) else "partially_supported"
    else:
        status = "supported"

    return {"citation_exists": citation_exists, "citation_sources_retrieved": citation_sources_retrieved,
            "prohibited_citation": prohibited_hit, "critical_fact_support": fact_support,
            "unsupported_critical_facts": unsupported, "status": status}


# ── Phase 2: Conversation metrics (aggregated across scenario turns) ──

def conversation_scenario_metrics(turn_results: List[Dict]) -> Dict:
    """Aggregates per-turn pass/fail + specific transition/entity/
    exclusion checks into the scenario-level metrics from Part 2.
    `turn_results` is the list of per-turn result dicts this module's
    other functions already produced (each with "turn_pass": bool,
    "transition_check": {...}, "entity_check": {...}, etc.)."""
    total = len(turn_results)
    if not total:
        return {"turn_pass_rate": 1.0, "scenario_pass": True, "topic_continuity": 1.0,
                "topic_switch_accuracy": 1.0, "entity_carry_accuracy": 1.0,
                "entity_replacement_accuracy": 1.0, "excluded_entity_accuracy": 1.0,
                "state_retention_accuracy": 1.0, "total_turns": 0, "passed_turns": 0}

    passed_turns = sum(1 for t in turn_results if t.get("turn_pass"))
    continuity_checks = [t for t in turn_results if t.get("expected_transition") == "same_topic"]
    switch_checks = [t for t in turn_results if t.get("expected_transition") == "switch_topic"]
    entity_carry_checks = [t for t in turn_results if t.get("expects_entity_carry")]
    entity_replace_checks = [t for t in turn_results if t.get("expects_entity_replacement")]
    excluded_checks = [t for t in turn_results if t.get("excluded_entity_check") is not None]
    state_checks = [t for t in turn_results if t.get("conversation_state_check") is not None]

    def _rate(checks, key):
        if not checks:
            return 1.0
        return round(sum(1 for c in checks if c.get(key)) / len(checks), 4)

    return {
        "turn_pass_rate": round(passed_turns / total, 4),
        "scenario_pass": passed_turns == total,
        "topic_continuity": _rate(continuity_checks, "topic_ok"),
        "topic_switch_accuracy": _rate(switch_checks, "topic_ok"),
        "entity_carry_accuracy": _rate(entity_carry_checks, "entity_ok"),
        "entity_replacement_accuracy": _rate(entity_replace_checks, "entity_ok"),
        "excluded_entity_accuracy": round(
            sum(1 for c in excluded_checks if c["excluded_entity_check"].get("exact_match")) / len(excluded_checks), 4
        ) if excluded_checks else 1.0,
        "state_retention_accuracy": round(
            sum(1 for c in state_checks if c["conversation_state_check"].get("exact_match")) / len(state_checks), 4
        ) if state_checks else 1.0,
        "total_turns": total, "passed_turns": passed_turns,
    }


FAILURE_TYPES = (
    "retrieval_miss", "ranking_failure", "wrong_evidence", "unsupported_claim",
    "wrong_citation", "language_failure", "partial_answer", "system_error",
    # Phase 2 additions — Query Understanding Only / Conversation
    # Scenario / critical-fact / grounding failures, never overlapping
    # with the retrieval/answer failure types above (those modes never
    # reach retrieval or the LLM at all).
    "query_understanding_mismatch", "conversation_state_mismatch",
    "critical_fact_mismatch", "grounding_failure",
)


def classify_query_understanding_failure(qu_metrics: Dict) -> Optional[str]:
    """Deterministic root-cause label for a Query Understanding Only
    case — first-matching rule wins, ordered from most to least
    upstream (a wrong topic makes everything downstream suspect, so it
    is reported over a merely-wrong canonical query)."""
    for key in ("topic", "subtopic", "intent"):
        row = qu_metrics.get(key)
        if row is not None and not row.get("pass"):
            return "query_understanding_mismatch"
    for key in ("entities", "excluded_entities", "conversation_state"):
        row = qu_metrics.get(key)
        if row is not None and not row.get("exact_match"):
            return "conversation_state_mismatch" if key == "conversation_state" else "query_understanding_mismatch"
    for key in ("canonical_query", "resolved_query", "transition"):
        row = qu_metrics.get(key)
        if row is not None and not row.get("pass"):
            return "query_understanding_mismatch"
    return None


def classify_failure(*, retrieval_r1: bool, retrieval_r5: bool, must_include_ok: bool,
                      must_not_include_ok: bool, citation_ok: bool, language_ok: bool,
                      answerability_ok: bool, system_error: bool) -> Optional[str]:
    """First-matching deterministic rule wins — ordered from most to
    least severe so a case with multiple problems is labeled by its root
    cause, not its most superficial symptom."""
    if system_error:
        return "system_error"
    if not retrieval_r5:
        return "retrieval_miss"
    if not retrieval_r1:
        return "ranking_failure"
    if not must_not_include_ok:
        return "unsupported_claim"
    if not citation_ok:
        return "wrong_citation"
    if not must_include_ok:
        return "wrong_evidence"
    if not language_ok:
        return "language_failure"
    if not answerability_ok:
        return "partial_answer"
    return None
