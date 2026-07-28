"""Evidence classification and citation-source selection — a layer that
runs AFTER retrieval/hybrid-reranking (rag/hybrid_scoring.py), never
before it and never changing chunk order, vector scores, or which chunks
survive into the final top_k. It answers a DIFFERENT question than hybrid
scoring: not "is this chunk relevant enough to retrieve/prompt with" but
"does this specific chunk directly support the exact attribute the
question asked about, or is it just related background about the same
document/entity."

Motivation: a question like "What is Shipify's mission?" can legitimately
retrieve the company's generic profile chunk too (same entity, decent
vector score, included in the prompt as useful background) — but that
chunk should never be cited as the SOURCE of the mission claim, and must
not be classified as "direct evidence" just because it mentions the same
company. This module is entity/topic-agnostic: it never hardcodes
"mission", "Shipify", or any other business-specific term — it works by
comparing the question's own content words against each chunk's section
heading, generically.
"""
from collections import Counter
from typing import Dict, List, Optional

import re

from rag.hybrid_scoring import tokenize, _thai_substring_hit

_HAS_THAI_RE = re.compile(r"[฀-๿]")

DIRECT_EVIDENCE = "DIRECT_EVIDENCE"
PARTIAL_EVIDENCE = "PARTIAL_EVIDENCE"
RELATED_CONTEXT = "RELATED_CONTEXT"
IRRELEVANT = "IRRELEVANT"

# Hybrid-scoring tiers (rag/hybrid_scoring.py's `classification` field,
# a DIFFERENT concept from this module's `evidence_classification`) that
# indicate the chunk had genuine retrieval-time support — used as a
# floor so a chunk with real lexical/structured evidence never gets
# downgraded to IRRELEVANT just because its heading text happens not to
# lexically overlap with the question (e.g. a synonym, or the chunk IS
# the whole answer with no separate heading).
_HYBRID_SUPPORTED_TIERS = {"structured_deterministic", "direct_evidence", "supporting_evidence"}


def _core_tokens(text: str, exclude: Optional[set] = None) -> set:
    toks = set(tokenize(text or ""))
    if exclude:
        toks -= exclude
    return toks


def _section_tokens(chunk: Dict) -> set:
    section = chunk.get("section_title") or ""
    heading_path = chunk.get("heading_path") or []
    heading_leaf = heading_path[-1] if heading_path else section
    return _core_tokens(section) | _core_tokens(heading_leaf)


def _overlap(q_tokens: set, target_tokens: set) -> set:
    """Which of `q_tokens` are actually present in `target_tokens` —
    exact match, OR (only for a genuinely CROSS-SCRIPT pair — one side
    Thai script, the other not) substring containment via rag/
    hybrid_scoring.py's own _thai_substring_hit. Citation Attribution Fix
    (Grounding Failure Audit): an English question tokenizes to Latin
    words ("shipping", "cost") that can never exact-match a Thai-only
    chunk's own tokens — plain set intersection silently starved every
    cross-language chunk down to RELATED_CONTEXT/IRRELEVANT, so it was
    never eligible for citation even when it was the genuine, correctly-
    used source.

    Deliberately NOT applied when both sides are plain ASCII/Latin: an
    English q_token like "shipify" would otherwise substring-match an
    unrelated content token like "shipify-example.com" (e.g. an email
    domain) purely because the entity name appears in it, over-crediting
    a chunk that never actually answers the question's attribute — exact
    match already handles same-script overlap correctly, so the looser
    substring check is scoped ONLY to the cross-script case it exists
    to fix. This affects ONLY citation/evidence classification —
    retrieval, ranking, chunk selection, and the prompt are untouched."""
    if not q_tokens or not target_tokens:
        return set()
    target_list = list(target_tokens)
    hits = set()
    for qt in q_tokens:
        if qt in target_tokens:
            hits.add(qt)
            continue
        qt_thai = bool(_HAS_THAI_RE.search(qt))
        cross_script_candidates = [ht for ht in target_list if bool(_HAS_THAI_RE.search(ht)) != qt_thai]
        if cross_script_candidates and _thai_substring_hit(qt, cross_script_candidates):
            hits.add(qt)
    return hits


def _common_tokens_across(chunks: List[Dict]) -> set:
    """Tokens that appear in most candidates' own section headings in
    THIS retrieval pool — almost always the shared entity/company name or
    a generic descriptor ("Company", "Profile"), never a hardcoded list.
    A token that shows up in the majority of headings can't be what
    distinguishes one section from another, so it's excluded from the
    attribute-overlap check below. Needs at least 2 chunks with a
    non-empty heading to mean anything; otherwise nothing is excluded."""
    counts = Counter()
    n = 0
    for c in chunks:
        toks = _section_tokens(c)
        if toks:
            n += 1
            counts.update(toks)
    if n <= 1:
        return set()
    threshold = max(2, (n + 1) // 2)
    return {t for t, cnt in counts.items() if cnt >= threshold}


def classify_evidence(question: str, chunks: List[Dict], query_variants: Optional[List[str]] = None) -> List[Dict]:
    """Sets chunk["evidence_classification"] on every chunk IN PLACE and
    returns the same list (order/contents otherwise untouched — this
    NEVER reorders, drops, or adds chunks; it only annotates them).

    `query_variants` (query expansion's Thai-English/synonym variants),
    if given, is unioned with the original question's own tokens — this
    is the same root-cause fix applied to rag/hybrid_scoring.py: a query
    using a transliteration ("มิชชั่น") sharing no literal tokens with an
    English heading ("Our Mission") only reaches DIRECT_EVIDENCE via its
    EXPANDED variant ("mission"), never the raw original question alone.
    """
    if not chunks:
        return chunks

    question_tokens = set(_core_tokens(question))
    for variant in (query_variants or []):
        question_tokens |= set(_core_tokens(variant))
    common_toks = _common_tokens_across(chunks)  # shared entity/descriptor words, excluded below
    q_attr = question_tokens - common_toks

    for c in chunks:
        if c.get("is_calculated") or c.get("is_structured"):
            # A deterministic calculation/structured lookup IS the direct
            # answer by construction — never "related context."
            c["evidence_classification"] = DIRECT_EVIDENCE
            continue

        section_toks = _section_tokens(c) - common_toks
        heading_overlap = _overlap(q_attr, section_toks)

        hybrid_tier = c.get("classification")

        if q_attr and heading_overlap:
            # At least one of the question's real attribute words (after
            # removing whatever's common/generic across this pool's
            # headings — usually the entity name) appears in THIS
            # chunk's own section heading — a strong, generic signal that
            # this section is specifically about that attribute, not just
            # background on the same document.
            c["evidence_classification"] = DIRECT_EVIDENCE
            continue

        content_toks = _core_tokens(c.get("text") or "") - common_toks
        content_overlap = _overlap(q_attr, content_toks)

        if q_attr and content_overlap:
            # The attribute word shows up somewhere in the chunk's body
            # text but not in its own heading — plausible but weaker
            # support than an exact section match.
            c["evidence_classification"] = PARTIAL_EVIDENCE
        elif hybrid_tier in _HYBRID_SUPPORTED_TIERS or hybrid_tier == "weak_semantic":
            # No attribute-level overlap at all, but retrieval already
            # found real lexical/structured/semantic evidence for this
            # chunk — treat it as background/related rather than
            # discarding it outright (it may still be useful prompt
            # context, e.g. the same entity's general profile).
            c["evidence_classification"] = RELATED_CONTEXT
        else:
            c["evidence_classification"] = IRRELEVANT

    return chunks


def select_citation_sources(question: str, answer_text: str, chunks: List[Dict]) -> List[Dict]:
    """Citations/Sources must be a SUBSET of the chunks actually sent to
    the LLM — never all of them by default. Only DIRECT_EVIDENCE chunks
    qualify; if none exist, PARTIAL_EVIDENCE chunks are used instead so
    an answer with only partial support still cites something. Multiple
    chunks are kept whenever multiple are genuinely DIRECT_EVIDENCE (a
    multi-attribute question answered from several sections) — this
    never artificially collapses to a single source.

    `question`/`answer_text` are accepted for interface symmetry and
    possible future refinement, but are NOT used for raw lexical
    token-overlap filtering here: an earlier version tried exactly that
    (checking whether the generated answer text shares words with each
    chunk) and it was discarded during development — this system
    routinely generates a Thai answer from English-language source
    chunks, where a literal word-overlap check between answer and chunk
    text is almost always near-zero regardless of whether the chunk is
    the genuine source, incorrectly dropping valid citations.
    classify_evidence()'s heading/attribute-overlap logic already runs on
    the QUESTION (not the generated answer) against each chunk's own
    heading, which stays meaningful regardless of what language the
    final answer ends up in, so it remains the source of truth here.

    Citation Attribution Fix (Grounding Failure Audit): a cross-script
    question/chunk pair (e.g. an English question against a Thai-only
    knowledge base) can have ZERO literal token overlap in either
    direction even when hybrid retrieval genuinely found and used the
    right chunk — classify_evidence() then has no choice but
    RELATED_CONTEXT for every candidate, and an answer citing NOTHING is
    worse than citing the single chunk retrieval itself scored highest.
    This is a citation-selection fallback only: it never changes which
    chunks were retrieved, their order, their hybrid scores, or the
    prompt — it only decides what gets labeled a "citation" when the
    attribute-overlap check above found nothing to work with.
    """
    if not chunks:
        return []
    direct = [c for c in chunks if c.get("evidence_classification") == DIRECT_EVIDENCE]
    if direct:
        return direct
    partial = [c for c in chunks if c.get("evidence_classification") == PARTIAL_EVIDENCE]
    if partial:
        return partial
    if not answer_text:
        return []
    related = [c for c in chunks if c.get("evidence_classification") == RELATED_CONTEXT]
    if not related:
        return []
    best = max(related, key=lambda c: c.get("hybrid_score") if c.get("hybrid_score") is not None else (c.get("score") or 0.0))
    return [best]
