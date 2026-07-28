"""Phase 2 Part 2 of the AI Playground Intelligence Pipeline — Context
Builder. Sits between Hybrid Retrieval and the LLM:

    Candidate Top K -> remove duplicates -> merge overlapping chunks
    -> group by source document -> compress repeated information
    -> Final Context Top K -> LLM

Every step preserves citation/page_number/source metadata — a merged or
compressed chunk always keeps `merged_citations` (every citation that
contributed to it) alongside its own `citation`/`page_number`/
`file_name`, so nothing sent to the LLM is ever untraceable back to its
source. No LLM calls anywhere in this module; every step is a pure,
deterministic function over the chunk-dict shape rag/searcher.py already
produces.
"""
import re
from typing import Dict, List, Optional, Tuple

# Near-identical chunk text (regardless of source) -> treat as a
# duplicate, keep the first (already-highest-ranked) occurrence.
DEDUP_THRESHOLD = 0.92
# Meaningful overlap between ADJACENT same-document chunks -> merge into
# one. Lower than DEDUP_THRESHOLD on purpose: two adjacent page-split
# chunks legitimately share less text than two true duplicates.
MERGE_THRESHOLD = 0.35
# Lines shorter than this are never compressed away — short lines (a
# lone number, a single word) are too easy to false-positive-match
# across unrelated chunks.
COMPRESS_LINE_MIN_LEN = 8


_TRAILING_PUNCT_RE = re.compile(r"[.,!?;:'\"]+$")


def _normalize_for_compare(text: str) -> str:
    """Lowercased/whitespace-collapsed for comparison purposes ONLY —
    never mutates the actual stored chunk text. Also strips per-word
    trailing punctuation (so "day." and "day!" compare as the same
    token) since a stray punctuation difference must never be the sole
    reason two otherwise-identical chunks fail to dedup."""
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    words = [_TRAILING_PUNCT_RE.sub("", w) for w in normalized.split(" ")]
    return " ".join(w for w in words if w)


def _overlap_ratio(a: str, b: str) -> float:
    """Cheap word-overlap ratio — a dependency-free stand-in for a real
    diff, good enough to detect near-duplicate/overlapping chunk text
    without adding a new library. Symmetric on the SMALLER of the two
    word sets, so a short chunk fully contained in a longer one still
    scores as a strong overlap."""
    wa = set(_normalize_for_compare(a).split())
    wb = set(_normalize_for_compare(b).split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def _remove_duplicate_chunks(chunks: List[Dict]) -> Tuple[List[Dict], int]:
    """Chunks arrive already rank-ordered (best first) — keeps the FIRST
    occurrence of each near-identical text and folds every duplicate's
    citation into the survivor's `merged_citations` list."""
    kept: List[Dict] = []
    removed = 0
    for c in chunks:
        text = c.get("text") or ""
        survivor = None
        for k in kept:
            if _overlap_ratio(text, k.get("text") or "") >= DEDUP_THRESHOLD:
                survivor = k
                break
        if survivor is not None:
            survivor.setdefault("merged_citations", [survivor.get("citation")])
            if c.get("citation") and c["citation"] not in survivor["merged_citations"]:
                survivor["merged_citations"].append(c["citation"])
            removed += 1
        else:
            c.setdefault("merged_citations", [c.get("citation")] if c.get("citation") else [])
            kept.append(c)
    return kept, removed


def _merge_text(earlier: str, later: str) -> str:
    """Appends only the non-overlapping remainder of `later` onto
    `earlier` — finds the longest suffix of `earlier` that's a prefix of
    `later` (the actual overlap a page-splitting chunker leaves at chunk
    boundaries) and drops just that shared part before appending, so
    nothing is duplicated and nothing is lost."""
    a_words, b_words = earlier.split(), later.split()
    max_check = min(len(a_words), len(b_words), 50)
    overlap_len = 0
    for k in range(max_check, 0, -1):
        if a_words[-k:] == b_words[:k]:
            overlap_len = k
            break
    remainder = " ".join(b_words[overlap_len:])
    if not remainder:
        return earlier
    return earlier.rstrip() + "\n\n" + remainder


def _merge_two(lower_idx_chunk: Dict, higher_idx_chunk: Dict) -> Dict:
    """Merges two ADJACENT (by chunk_index) chunks from the same
    document. Text is always merged in reading order (lower index first)
    regardless of which one scored higher; the higher-scoring chunk's
    OTHER metadata (section_title, heading_path, etc.) is kept as
    primary since that's what best describes the merged content, but
    `page_number`/`citation` are kept from the lower-index (earliest)
    chunk — the merged text STARTS there, so that's the correct citation
    anchor."""
    lower_score = lower_idx_chunk.get("hybrid_score", lower_idx_chunk.get("score", 0)) or 0
    higher_score = higher_idx_chunk.get("hybrid_score", higher_idx_chunk.get("score", 0)) or 0
    primary = lower_idx_chunk if lower_score >= higher_score else higher_idx_chunk

    merged = dict(primary)
    merged["text"] = _merge_text(lower_idx_chunk.get("text") or "", higher_idx_chunk.get("text") or "")
    merged["page_number"] = lower_idx_chunk.get("page_number")
    merged["citation"] = lower_idx_chunk.get("citation")

    citations: List[str] = []
    for c in (lower_idx_chunk, higher_idx_chunk):
        for cit in (c.get("merged_citations") or ([c["citation"]] if c.get("citation") else [])):
            if cit and cit not in citations:
                citations.append(cit)
    merged["merged_citations"] = citations
    return merged


def _merge_overlapping_chunks(chunks: List[Dict]) -> Tuple[List[Dict], int]:
    """Merges adjacent (consecutive chunk_index), same-file chunks whose
    text meaningfully overlaps — common in this codebase's page-aware
    chunker, which intentionally lets text overlap at chunk boundaries
    (see ingestion/ingest.py). Chunks without a file_id/chunk_index
    (e.g. structured Excel/calculated results) are never merged, only
    passed through untouched."""
    by_file: Dict[str, List[Dict]] = {}
    no_file: List[Dict] = []
    for c in chunks:
        fid = c.get("file_id")
        chunk_idx = c.get("chunk_index")
        if fid is not None and chunk_idx is not None:
            by_file.setdefault(fid, []).append(c)
        else:
            no_file.append(c)

    result: List[Dict] = []
    merged_count = 0
    for fid, group in by_file.items():
        ordered = sorted(group, key=lambda c: c["chunk_index"])
        i = 0
        while i < len(ordered):
            current = ordered[i]
            if i + 1 < len(ordered):
                nxt = ordered[i + 1]
                adjacent = (nxt["chunk_index"] - current["chunk_index"]) <= 1
                if adjacent and _overlap_ratio(current.get("text") or "", nxt.get("text") or "") >= MERGE_THRESHOLD:
                    current = _merge_two(current, nxt)
                    merged_count += 1
                    i += 2
                    result.append(current)
                    continue
            result.append(current)
            i += 1
    result.extend(no_file)
    return result, merged_count


def _group_by_source(chunks: List[Dict]) -> Dict[str, List[Dict]]:
    groups: Dict[str, List[Dict]] = {}
    for c in chunks:
        key = c.get("file_name") or c.get("source") or "unknown"
        groups.setdefault(key, []).append(c)
    return groups


def _compress_repeated_lines(chunks: List[Dict]) -> Tuple[List[Dict], int]:
    """If the same non-trivial line of text appears in 2+ chunks (e.g.
    repeated boilerplate/footer/header text), keeps it only in the FIRST
    chunk that has it and strips it from later occurrences. Only ever
    touches a chunk's own `text` field — citation/page_number/source and
    every other field are untouched, and a chunk is never removed
    entirely just because all its lines were compressed (an
    all-boilerplate chunk is a real, if rare, edge case; better to keep
    an empty-ish chunk than silently vanish a citation)."""
    seen_lines = set()
    compressed_count = 0
    for c in chunks:
        text = c.get("text") or ""
        lines = text.split("\n")
        kept_lines = []
        for line in lines:
            key = _normalize_for_compare(line)
            if len(key) >= COMPRESS_LINE_MIN_LEN:
                if key in seen_lines:
                    compressed_count += 1
                    continue
                seen_lines.add(key)
            kept_lines.append(line)
        c["text"] = "\n".join(kept_lines)
    return chunks, compressed_count


def build_context(chunks: List[Dict], final_top_k: Optional[int] = None) -> Tuple[List[Dict], Dict]:
    """The full Context Builder pipeline. Returns (final_chunks, summary).

    `final_top_k`, if given, truncates the result to that many chunks
    AFTER all dedup/merge/compress steps (so it's the FINAL context size,
    not the candidate pool size) — None keeps everything that survives.

    `summary` is exactly what Phase 2's Explainability requirement calls
    "Context Builder Summary": counts at every stage, plus which source
    documents ended up represented in the final context — never the
    individual citations themselves (those stay on the chunks).
    """
    original_count = len(chunks)
    deduped, duplicates_removed = _remove_duplicate_chunks(chunks)
    merged, chunks_merged = _merge_overlapping_chunks(deduped)
    compressed, compressed_lines_removed = _compress_repeated_lines(merged)

    groups = _group_by_source(compressed)
    final_chunks = compressed[: final_top_k] if final_top_k else compressed

    summary = {
        "original_count": original_count,
        "after_dedup_count": len(deduped),
        "duplicates_removed": duplicates_removed,
        "after_merge_count": len(merged),
        "chunks_merged": chunks_merged,
        "compressed_lines_removed": compressed_lines_removed,
        "source_documents": sorted(groups.keys()),
        "final_count": len(final_chunks),
    }
    return final_chunks, summary
