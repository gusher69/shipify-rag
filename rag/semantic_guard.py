"""Semantic Invariant Guard (P0, 2026-07-21) — the generic, reusable
safety layer between the user's ORIGINAL wording and every text
transformation the pipeline applies to it (spell correction, canonical
rewrite, and any future rewrite step). Prevents a transformation from
silently introducing a new entity, removing one, changing an intent,
flipping polarity, or corrupting a Thai polite particle into part of a
business word.

This is deliberately NOT a list of hardcoded fixes for one query
("โกดังหน่อย" -> "โกดังจีน่อย" is only ONE symptom of the general problem)
— it works by taking two ENTITY SNAPSHOTS of arbitrary text (reusing
rag/query_resolution.py's existing topic/transport/location/attribute
extractor, never a second copy) and diffing them. Any topic, any tenant,
any FAQ domain gets the same protection for free.

Core principle: the ORIGINAL user message is the semantic source of
truth. A correction/rewrite is only ACCEPTED when its entity snapshot is
identical to the original's, OR the only differences are entities that
legitimately came from the immediately previous USER turn (never from an
assistant answer, an expansion variant, or the rewrite itself).
"""
import difflib
import re
from typing import Dict, List, Optional, Set

from rag.query_resolution import extract_entities
from rag.spell_correction import _protected_spans

# Thai polite particles / conversational suffixes — protected from being
# partially consumed by a correction (Part 4). Sorted longest-first so a
# longer particle ("ให้หน่อย") is checked before a shorter one it contains
# ("หน่อย") would otherwise also (correctly) flag.
_POLITE_PARTICLES = [
    "ให้หน่อย", "ขอหน่อย", "หน่อย", "ทีค่ะ", "ทีครับ", "ที", "นะคะ", "นะครับ", "นะ",
    "ค่ะ", "ครับ", "จ้า", "ได้ไหม", "รึเปล่า", "หรือยัง", "อะ", "อ่ะ",
]
_POLITE_PARTICLES_SORTED = sorted(set(_POLITE_PARTICLES), key=len, reverse=True)

# A coarse, generic negation/polarity marker set — comparing the COUNT of
# hits before/after is a conservative proxy for "polarity flipped"
# (ไม่ถึง -> ถึงแล้ว, ไม่รับ -> รับ) without needing full clause parsing.
_NEGATION_RE = re.compile(r"ไม่|ห้าม|งด|ปฏิเสธ|ยกเลิก")


def _extract_identifiers(text: str) -> Set[str]:
    """Every protected-entity substring (URL/email/phone/tracking-code/
    number-or-price/known abbreviation) — reuses rag/spell_correction.py's
    own protected-span regexes directly, never a second, separately-
    maintained pattern list."""
    text = text or ""
    return {text[s:e] for s, e in _protected_spans(text)}


def entity_snapshot(text: str) -> Dict:
    """A single, reusable snapshot of everything a semantic-diff needs —
    topic/transport/location/attribute (rag/query_resolution.py's own
    extractor, generic across every domain this codebase already
    supports), plus identifiers and a negation count. Works for ANY
    topic/tenant because it's driven by the SAME synonym-group/pattern
    machinery every other module in this pipeline already uses — no
    per-topic special-casing lives here."""
    text = text or ""
    ents = extract_entities(text)
    return {
        "topic": ents.get("topic"),
        "transport": ents.get("transport"),
        "location": ents.get("location"),
        "attribute": ents.get("attribute"),
        "identifiers": _extract_identifiers(text),
        "negation_count": len(_NEGATION_RE.findall(text)),
    }


# Slots whose VALUE is a consequential customer-facing FACT (which
# country's warehouse, which transport mode) — introducing one from
# "unspecified" is exactly the original bug (โกดังหน่อย -> โกดังจีน่อย
# fabricates a country the customer never named) and must always be
# rejected, the same as changing an already-stated one to a different
# value. `topic`/`attribute` are broader SUBJECT classification (which
# general FAQ domain, what kind of question) — revealing what the
# question already structurally was via a same-word spelling fix (e.g.
# "แผนี่" -> "แผนที่") is safe and must not be rejected; only an actual
# CHANGE to a different topic/attribute (not merely filling in a blank)
# is unsafe.
_FACT_VALUE_SLOTS = {"location", "transport"}
_SUBJECT_CLASS_SLOTS = {"topic", "attribute"}


def diff_entities(before: Dict, after: Dict) -> List[str]:
    """Returns a list of violation strings — empty when the two snapshots
    agree on everything meaningful. Each entry is machine-parseable
    ("introduced_<slot>:<value>" / "removed_<slot>:<value>" /
    "changed_<slot>:<old>->new") so callers (e.g. the Entity Introduction
    Policy below) can reason about individual violations, not just a
    yes/no verdict."""
    violations: List[str] = []
    for key in _FACT_VALUE_SLOTS | _SUBJECT_CLASS_SLOTS:
        b, a = before.get(key), after.get(key)
        if b == a:
            continue
        if b is None and a is not None:
            if key in _FACT_VALUE_SLOTS:
                violations.append(f"introduced_{key}:{a}")
            # else: a subject-class slot filled in from a same-word
            # spelling fix (revealing existing structure) — safe, no
            # violation recorded.
        elif b is not None and a is None:
            violations.append(f"removed_{key}:{b}")
        else:
            violations.append(f"changed_{key}:{b}->{a}")

    removed_ids = before["identifiers"] - after["identifiers"]
    added_ids = after["identifiers"] - before["identifiers"]
    if removed_ids:
        violations.append(f"removed_identifier:{sorted(removed_ids)}")
    if added_ids:
        violations.append(f"introduced_identifier:{sorted(added_ids)}")

    if before["negation_count"] != after["negation_count"]:
        violations.append(f"negation_changed:{before['negation_count']}->{after['negation_count']}")

    return violations


# Below this character-level similarity ratio (difflib), a transformation
# is a full paraphrase/template rewrite (e.g. Canonical Query Rewrite
# composing "ขอที่อยู่และแผนที่โกดังไทย" from scratch) — Part 7 explicitly
# ALLOWS such a rewrite to drop polite particles entirely (a clean, full
# removal is fine; only a PARTIAL, glued-in-place consumption is unsafe).
# At/above this ratio, the transformation is a local, in-place edit
# (spell correction) where the original string is still mostly intact,
# so a particle silently disappearing is much more likely to mean it was
# partially eaten by a substitution rather than deliberately dropped.
_LOCAL_EDIT_SIMILARITY_THRESHOLD = 0.5


def polite_particle_violation(original: str, candidate: str) -> Optional[str]:
    """A business term must never be formed by consuming part of a
    polite particle (Part 4) — if `original` contains a recognized
    particle as a substring and `candidate` no longer contains that exact
    substring, AND the transformation is a local, in-place edit (not a
    full paraphrase/template rewrite — see the threshold above), the
    particle was likely partially eaten by a correction rather than
    cleanly removed. This is checked independently of the entity diff
    above because the exact bug that motivated this task ("โกดังหน่อย" ->
    "โกดังจีน่อย") introduces a location entity AND corrupts "หน่อย" in
    the same edit — either signal alone is sufficient to reject."""
    if difflib.SequenceMatcher(None, original, candidate).ratio() < _LOCAL_EDIT_SIMILARITY_THRESHOLD:
        return None  # a full paraphrase/rewrite — dropping a particle entirely is allowed (Part 7)
    for particle in _POLITE_PARTICLES_SORTED:
        if particle in original and particle not in candidate:
            return f"polite_particle_corrupted:{particle}"
    return None


def _matches_carried(violation: str, carried: Dict) -> bool:
    """Entity Introduction Policy (Part 2.B) — an "introduced_<slot>"
    violation is not actually a violation if that exact value was
    legitimately carried from the immediately previous USER turn (never
    from an assistant answer, an expansion variant, or the transform
    itself)."""
    if not violation.startswith("introduced_"):
        return False
    try:
        key, value = violation.split(":", 1)
    except ValueError:
        return False
    key = key[len("introduced_"):]
    return carried.get(key) == value


def validate_transformation(original: str, candidate: str, *,
                             carried_entities: Optional[Dict] = None,
                             trust_identifier_introduction: bool = False) -> Dict:
    """The core Semantic Invariant Guard (Part 1) — call this after ANY
    transformation of user text (spell correction, canonical rewrite) and
    before letting the transformed text influence retrieval, answer
    planning, or attachment selection.

    `carried_entities`, if given, are entities already legitimately
    resolved from the previous USER turn (rag/query_resolution.py) — an
    entity introduced by the transform is allowed ONLY if it exactly
    matches one of these (Part 2.B); it is NEVER allowed if it would only
    be justified by the previous ASSISTANT answer or by an expansion
    variant, since neither is ever passed in here.

    `trust_identifier_introduction`, if True, skips the introduced-
    identifier check specifically — for a transformation produced
    ENTIRELY by an admin-curated, deterministic 1:1 registry mapping
    (e.g. rag/spell_correction.py's `direct_corrections`, "CBเอ็ม"->"CBM"),
    the Entity Introduction Policy's exemption D ("a deterministic
    registry mapping explicitly defines it without ambiguity") applies —
    this is never set for a fuzzy/guessed correction.

    Returns {"accepted": bool, "violations": [...], "reason": str}."""
    original = original or ""
    candidate = candidate or ""
    if candidate.strip() == original.strip():
        return {"accepted": True, "violations": [], "reason": "unchanged"}

    before = entity_snapshot(original)
    after = entity_snapshot(candidate)
    violations = diff_entities(before, after)

    if trust_identifier_introduction:
        violations = [v for v in violations if not v.startswith("introduced_identifier:")]

    particle_violation = polite_particle_violation(original, candidate)
    if particle_violation:
        violations.append(particle_violation)

    if carried_entities:
        violations = [v for v in violations if not _matches_carried(v, carried_entities)]

    accepted = not violations
    reason = "no semantic drift detected" if accepted else "; ".join(violations)
    return {"accepted": accepted, "violations": violations, "reason": reason}
