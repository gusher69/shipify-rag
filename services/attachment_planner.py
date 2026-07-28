"""Attachment Planner — decides WHETHER to send an attachment, WHICH
ones, how many, and in what order relative to text — from the
attachments already resolved onto the FINAL retrieved/evidence chunks
(rag/searcher.py's `_fetch_attachments_for_chunks`, surfaced as each
chunk's `attachments` list). This module never fetches attachments
itself and never invents a URL — it only selects/orders from what's
already present on the evidence that survived retrieval, which is
already intent-consistent by construction (e.g. if Conversation
Resolver 2.0 + FAQ Exact Match already narrowed retrieval to the
payment-instruction FAQ row, that row's own attachments are already the
"payment step" ones — no separate cross-referencing needed).

Deterministic, pure Python, no LLM call.
"""
from typing import Dict, List, Optional

DEFAULT_MAX_IMAGES = 2
DEFAULT_MAX_DOCUMENTS = 1

_IMAGE_PREFIXES = ("image/",)


def _is_image(att: Dict) -> bool:
    return (att.get("mime_type") or "").startswith(_IMAGE_PREFIXES)


def _collect_candidate_attachments(chunks: List[Dict]) -> List[Dict]:
    """Flattens every chunk's `attachments`, deduplicated by URL —
    "never send duplicate URLs," and each entry is tagged with the
    source chunk's id so the planner never has to guess where an
    attachment came from."""
    seen_urls = set()
    candidates: List[Dict] = []
    for chunk in chunks:
        for att in (chunk.get("attachments") or []):
            url = att.get("public_url") or att.get("download_url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            candidates.append({
                "url": url,
                "filename": att.get("filename"),
                "type": "image" if _is_image(att) else "document",
                "source_chunk_id": chunk.get("chunk_id") or chunk.get("id"),
            })
    return candidates


def _attachment_conflicts_with_validated_entities(att: Dict, chunks: List[Dict],
                                                   validated_entities: Dict) -> bool:
    """Attachment Compatibility Validator (Part 10, P0 2026-07-21) — an
    attachment's entity set must be a subset of, or compatible with, the
    VALIDATED answer entity set (the same original-query/carried-context
    entities the Semantic Invariant Guard already trusts — never an
    entity that only came from a corrupted correction/rewrite/expansion
    variant). Reuses rag/query_resolution.py's own extractor against the
    attachment's SOURCE CHUNK text — never a second, separately-
    maintained entity scanner."""
    validated_location = validated_entities.get("location")
    if not validated_location:
        return False  # nothing validated to conflict with — never invent a rejection
    source_chunk_id = att.get("source_chunk_id")
    for chunk in chunks:
        if (chunk.get("chunk_id") or chunk.get("id")) != source_chunk_id:
            continue
        from rag.query_resolution import extract_entities
        chunk_location = extract_entities(chunk.get("text") or "").get("location")
        if chunk_location and chunk_location != validated_location:
            return True
    return False


def plan_attachments(
    actionable_intent: str,
    requested_attributes: Optional[List[str]] = None,
    answer_plan: Optional[Dict] = None,
    chunks: Optional[List[Dict]] = None,
    channel: str = "playground",
    policy_set: Optional[Dict] = None,
    validated_entities: Optional[Dict] = None,
) -> Dict:
    """Returns:
        {
          "should_send": bool,
          "selected_attachments": [{"url", "filename", "type", "purpose", "source_chunk_id"}, ...],
          "attachment_order": ["text_intro", "attachment", "text_followup"] (or [] if not sending),
          "selection_reason": str,
          "omitted_attachments": [...],
        }
    """
    chunks = chunks or []
    answer_plan = answer_plan or {}
    candidates = _collect_candidate_attachments(chunks)

    if not candidates:
        return {"should_send": False, "selected_attachments": [], "attachment_order": [],
                "selection_reason": "no attachments available on the retrieved evidence",
                "omitted_attachments": []}

    rules = (policy_set or {}).get("config", {}).get("attachment_rules", {}) if policy_set else {}
    allow_images = rules.get("send_image_if_available", True)
    allow_documents = rules.get("send_file_link_if_available", True)

    filtered = [c for c in candidates if (c["type"] == "image" and allow_images)
                or (c["type"] == "document" and allow_documents)]
    if validated_entities:
        filtered = [c for c in filtered
                    if not _attachment_conflicts_with_validated_entities(c, chunks, validated_entities)]
    omitted = [c for c in candidates if c not in filtered]

    if not filtered:
        return {"should_send": False, "selected_attachments": [], "attachment_order": [],
                "selection_reason": "AI Policies attachment rules disallow the available attachment type(s)",
                "omitted_attachments": omitted}

    # Cap: 2 images, 1 document by default.
    images = [c for c in filtered if c["type"] == "image"][:DEFAULT_MAX_IMAGES]
    documents = [c for c in filtered if c["type"] == "document"][:DEFAULT_MAX_DOCUMENTS]
    selected = images + documents
    omitted += [c for c in filtered if c not in selected]

    purpose = {
        "warehouse_map": "warehouse_map", "warehouse_location": "warehouse_map",
        "attachment_request": "requested_attachment",
        "payment_instruction": "payment_steps", "coupon_policy": "coupon_steps",
    }.get(actionable_intent, "supporting_evidence")
    for att in selected:
        att["purpose"] = purpose

    reason_map = {
        "warehouse_map": "Customer explicitly requested a map",
        "attachment_request": "Customer explicitly requested an image/file",
        "payment_instruction": "Payment steps are shown via an image",
        "coupon_policy": "Coupon usage is shown via an image",
    }
    selection_reason = reason_map.get(actionable_intent, "Attached to the selected evidence")

    shape = answer_plan.get("response_shape")
    order = (["attachment"] if shape == "attachment_only"
             else ["text_intro", "attachment", "text_followup"])

    return {
        "should_send": True,
        "selected_attachments": selected,
        "attachment_order": order,
        "selection_reason": selection_reason,
        "omitted_attachments": omitted,
    }
