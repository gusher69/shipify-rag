"""Parallel lexical retrieval path (Part 7) — finds chunks by keyword/
heading match directly against the database, independent of vector
search, so a chunk with an EXACT heading/keyword match but weak vector
similarity still enters the candidate pool (see rag/searcher.py's merge
step). Without this, a chunk that never makes the vector top-K (as
confirmed in the audit — "Our Mission" ranked #12-14 out of 20 there) can
never be rescued by hybrid reranking, because reranking only reorders
whatever vector search already returned.

Implementation note: this uses `ILIKE` against `knowledge_chunks.content`/
`section_title`/`metadata->>'heading_path'`/`source`, not Postgres full-
text search (tsvector) or trigram (pg_trgm) — both would need a new
extension/index migration. ILIKE is a correct, safe, dependency-free stand-
in for a small-to-medium knowledge base; if/when full-text search is
worth the schema investment, this module's public interface
(`lexical_search(question, limit)`) doesn't need to change for the caller.
"""
from typing import Dict, List

from rag.hybrid_scoring import tokenize


def lexical_search(query_variants: List[str], limit: int = 15) -> List[Dict]:
    """Searches knowledge_chunks for any meaningful token from ANY of the
    given query variants (see rag/query_expansion.py), matching against
    content, section_title, heading_path (jsonb), and the source filename.
    Returns chunk dicts shaped like rag/searcher.py's vector-search
    output (score=None — these are LEXICAL candidates; hybrid scoring
    computes their real ranking signal from keyword/heading overlap, not
    from a vector similarity that was never computed for them)."""
    tokens = set()
    for variant in query_variants:
        tokens.update(tokenize(variant))
    # Longer tokens are much less likely to produce noisy false-positive
    # ILIKE hits (e.g. a 2-letter token would match almost anything).
    tokens = {t for t in tokens if len(t) >= 3}
    if not tokens:
        return []

    try:
        from admin.routes import get_sb
        sb = get_sb()
    except Exception as e:
        print(f"[lexical_search] could not get Supabase client: {e}")
        return []

    seen_ids = set()
    results: List[Dict] = []
    try:
        # Latency fix (Task 05, 2026-08-26) — confirmed live: this used to
        # issue ONE SEPARATE Supabase round-trip PER token, sequentially
        # (11 tokens -> 11 network round-trips -> ~3.2s measured for a
        # single company-intent-expanded query, the single largest
        # contributor to that turn's total latency). The overall contract
        # was already "at most `limit` distinct chunks matching ANY token
        # in ANY field" (the early-return below already capped the total
        # this way regardless of how many tokens were involved) — batching
        # every token's OR-conditions into ONE combined `.or_()` string,
        # queried once, preserves that exact same contract (same matching
        # criteria, same final `limit` cap) while requiring exactly one
        # round-trip regardless of token count. The only behavior
        # difference is which specific `limit`-sized subset of matching
        # rows Postgrest returns when more than `limit` rows match overall
        # — never a correctness change, since every returned row still
        # genuinely matches the same criteria as before.
        or_conditions = ",".join(
            f"content.ilike.%{token}%,section_title.ilike.%{token}%,source.ilike.%{token}%,"
            f"metadata->>section_title.ilike.%{token}%"
            for token in tokens
        )
        res = sb.table("knowledge_chunks").select(
            "id,file_id,content,source,intent,is_active,metadata,page_number,section_title,version"
        ).eq("is_active", True).or_(or_conditions).limit(limit).execute()
        for r in (res.data or []):
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])
            meta = r.get("metadata") or {}
            results.append({
                "text": r["content"], "source": r.get("source") or meta.get("file_name", ""),
                "intent": r.get("intent", ""), "score": None,  # no vector score — lexical-only candidate
                "citation": f"Source: {meta.get('file_name') or r.get('source')}",
                "file_name": meta.get("file_name"), "file_id": r.get("file_id"),
                "chunk_id": r["id"], "page_number": meta.get("page_number") or r.get("page_number"),
                "section_title": meta.get("section_title") or r.get("section_title"),
                "chunk_index": meta.get("chunk_index"), "version": meta.get("version") or r.get("version"),
                "category": meta.get("category"), "language": meta.get("language"),
                "is_structured": False, "attachments": [],
                "knowledge_type": meta.get("knowledge_type"), "chunk_strategy": meta.get("chunk_strategy"),
                "heading_path": meta.get("heading_path"), "step_index": meta.get("step_index"),
                "suggested_questions": meta.get("suggested_questions"),
                "embedding_provider": meta.get("embedding_provider"),
                "embedding_model": meta.get("embedding_model"),
                "embedding_version": meta.get("embedding_version"),
                "embedding_dimensions": meta.get("embedding_dimensions"),
                "document_purpose": meta.get("document_purpose"),
                "content_signals": meta.get("content_signals"),
                "from_lexical_search": True,
            })
            if len(results) >= limit:
                return results
    except Exception as e:
        print(f"[lexical_search] query failed: {e}")
    return results
