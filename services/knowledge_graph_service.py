"""KnowledgeGraphService — extracts entities/concepts and their
relationships from a document, as an ADDITIVE metadata layer on top of
the existing chunk+embedding RAG pipeline. Does not replace vector
search; future Graph RAG (vector + keyword + graph traversal) is meant
to read from these tables, not this module doing search itself yet (see
`get_graph_for_file`/`get_graph_for_chunk` — read-only lookups, no
traversal/ranking logic here by design, per the "future Graph RAG" scope
boundary).

Source of truth stays the original document: every edge stores
`evidence_text` quoted from the source, and extraction never runs
without it — an edge with no evidence is dropped, not saved with a blank
field (see `_validate_graph`).

Extraction is LLM-backed and best-effort: on any failure (no API key, bad
JSON, quota) it returns an empty graph rather than raising, exactly like
every other AI-enrichment feature added this session (alt-questions,
knowledge analyzer classification) — a failed graph extraction must never
block the underlying import.
"""
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

# Process-lifetime cache keyed by a hash of (filename, text) — returns the
# SAME dict (same GraphNode/GraphEdge object instances) on a repeat call
# for identical content. This is what lets Import Preview's accept/
# reject/edit step (which mutates these objects' `.accepted` field
# in-place) survive into Confirm, where ingestion/ingest.py's
# analyze_and_chunk() re-runs the whole KnowledgeAnalyzer fresh (same
# "re-extraction on confirm" pattern used for alt-questions in
# ingestion/smart_enrichment.py — same fix, same reason: never pay for or
# diverge from a second LLM call when the first one's result, possibly
# admin-edited, should be reused verbatim).
_graph_cache: Dict[str, Dict] = {}

NODE_TYPES = [
    "company", "service", "product", "feature", "process", "step", "policy",
    "requirement", "department", "platform", "location", "document",
    "topic", "faq", "system", "attachment", "other",
]

RELATION_TYPES = [
    "provides", "supports", "contains", "requires", "belongs_to",
    "handled_by", "located_at", "part_of", "next_step", "related_to",
    "answers", "references", "attached_to", "depends_on", "applies_to",
]

_DOMAIN_SUFFIX_RE = re.compile(r"\.(co\.th|com|co|org|net|io)$", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w฀-๿\s]")


@dataclass
class GraphNode:
    name: str
    node_type: str = "other"
    description: Optional[str] = None
    confidence: float = 0.5
    id: Optional[str] = None
    normalized_name: str = ""
    accepted: bool = True

    def __post_init__(self):
        if not self.normalized_name:
            self.normalized_name = normalize_node_name(self.name)
        if not self.id:
            self.id = str(uuid.uuid4())


@dataclass
class GraphEdge:
    source: str
    target: str
    relation_type: str = "related_to"
    relation_label: Optional[str] = None
    evidence_text: str = ""
    confidence: float = 0.5
    id: Optional[str] = None
    source_chunk_id: Optional[str] = None
    accepted: bool = True

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())


def normalize_node_name(name: str) -> str:
    """Fold cosmetic variants of the same entity together: case, common
    domain suffixes (Shipify / shipify.co.th / SHIPIFY -> "shipify"),
    and punctuation/whitespace differences."""
    n = (name or "").strip().lower()
    n = _DOMAIN_SUFFIX_RE.sub("", n)
    n = _PUNCT_RE.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def deduplicate_nodes(nodes: List[GraphNode]) -> List[GraphNode]:
    """Merge nodes that normalize to the same name — keeps the
    highest-confidence one, but preserves the longest description seen
    across duplicates (a later mention often has more detail than the
    first)."""
    by_norm: Dict[str, GraphNode] = {}
    for n in nodes:
        existing = by_norm.get(n.normalized_name)
        if not existing:
            by_norm[n.normalized_name] = n
            continue
        if n.confidence > existing.confidence:
            existing.confidence = n.confidence
            existing.node_type = n.node_type
        if n.description and (not existing.description or len(n.description) > len(existing.description)):
            existing.description = n.description
    return list(by_norm.values())


def _validate_graph(raw: Dict) -> Tuple[List[GraphNode], List[GraphEdge]]:
    """Strict validation per the spec: node type must be one of
    NODE_TYPES, relation_type one of RELATION_TYPES, an edge with no
    evidence_text is dropped (never saved without evidence), and an edge
    whose source/target doesn't resolve to a known node is dropped too."""
    nodes: List[GraphNode] = []
    seen_names = set()
    for n in raw.get("nodes") or []:
        name = str(n.get("name") or "").strip()
        if not name or name.lower() in seen_names:
            continue
        seen_names.add(name.lower())
        ntype = n.get("type") if n.get("type") in NODE_TYPES else "other"
        try:
            confidence = float(n.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        nodes.append(GraphNode(name=name, node_type=ntype,
                                description=n.get("description"), confidence=confidence))

    nodes = deduplicate_nodes(nodes)
    valid_names = {n.normalized_name for n in nodes}
    name_lookup = {n.normalized_name: n for n in nodes}

    edges: List[GraphEdge] = []
    for e in raw.get("edges") or []:
        source, target = str(e.get("source") or "").strip(), str(e.get("target") or "").strip()
        evidence = str(e.get("evidence_text") or "").strip()
        if not source or not target or not evidence:
            continue  # no evidence -> never saved, per spec section 6
        source_norm, target_norm = normalize_node_name(source), normalize_node_name(target)
        if source_norm not in valid_names or target_norm not in valid_names:
            continue  # dangling reference to an unextracted node -> drop
        rel_type = e.get("relation_type") if e.get("relation_type") in RELATION_TYPES else "related_to"
        try:
            confidence = float(e.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        edges.append(GraphEdge(
            source=name_lookup[source_norm].name, target=name_lookup[target_norm].name,
            relation_type=rel_type, relation_label=e.get("relation_label"),
            evidence_text=evidence, confidence=confidence,
        ))

    return nodes, edges


class KnowledgeGraphService:
    """The single entry point for graph extraction/persistence — importer
    code (ingestion/ingest.py, admin/routes.py) calls this, never
    re-implements extraction or normalization inline."""

    def extract_graph(self, text: str, filename: str, knowledge_type: Optional[str] = None,
                       max_chars: int = 6000) -> Dict:
        """Returns {"nodes": [GraphNode,...], "edges": [GraphEdge,...]}.
        Best-effort — returns empty lists on any failure, never raises."""
        if os.getenv("ENABLE_KNOWLEDGE_GRAPH", "true").strip().lower() in ("0", "false", "no"):
            return {"nodes": [], "edges": [], "error": False}
        if not text or not text.strip():
            return {"nodes": [], "edges": [], "error": False}

        cache_key = hashlib.sha256(f"{filename}::{text}".encode("utf-8", errors="ignore")).hexdigest()
        if cache_key in _graph_cache:
            return _graph_cache[cache_key]

        try:
            from services.llm_service import get_llm_service
            from config import OPENAI_CHAT_MODEL
            llm = get_llm_service()
            node_types = ", ".join(NODE_TYPES)
            rel_types = ", ".join(RELATION_TYPES)
            prompt = (
                "Extract a knowledge graph from the document below: important entities/"
                "concepts (companies, services, products, features, processes, steps, "
                "policies, requirements, departments, platforms, locations, systems, topics) "
                "and the relationships between them. Respond with ONLY a JSON object "
                "(no markdown fences, no explanation) in exactly this shape:\n"
                '{"nodes": [{"name": str, "type": one of [' + node_types + '], '
                '"description": str, "confidence": 0-1 float}], '
                '"edges": [{"source": str (must match a node name), "target": str (must match a node name), '
                '"relation_type": one of [' + rel_types + '], "relation_label": str, '
                '"evidence_text": str (a short QUOTE from the document that supports this relationship — '
                "REQUIRED, never invent one), \"confidence\": 0-1 float}]}\n\n"
                "Only extract relationships you have direct textual evidence for. Keep it to the most "
                "important 5-15 nodes and their real relationships — do not over-extract trivial mentions.\n\n"
                f"Document type: {knowledge_type or 'Unknown'}\nFilename: {filename}\n\n"
                f"Document content:\n{text[:max_chars]}"
            )
            resp = llm.generate([{"role": "user", "content": prompt}],
                                 model=OPENAI_CHAT_MODEL, temperature=0.1, max_tokens=1500)
            raw_text = re.sub(r"^```(?:json)?|```$", "", resp.text.strip(), flags=re.MULTILINE).strip()
            raw = json.loads(raw_text)
            nodes, edges = _validate_graph(raw)
            result = {"nodes": nodes, "edges": edges, "error": False}
            _graph_cache[cache_key] = result
            return result
        except Exception as exc:
            print(f"[knowledge_graph_service] extraction skipped/failed for {filename}: {exc}")
            return {"nodes": [], "edges": [], "error": True}

    def normalize_nodes(self, nodes: List[GraphNode]) -> List[GraphNode]:
        for n in nodes:
            n.normalized_name = normalize_node_name(n.name)
        return nodes

    def deduplicate_nodes(self, nodes: List[GraphNode]) -> List[GraphNode]:
        return deduplicate_nodes(nodes)

    def save_graph(self, sb, file_id: str, nodes: List[GraphNode], edges: List[GraphEdge],
                    chunk_id_by_evidence: Optional[Dict[str, str]] = None,
                    knowledge_item_id: Optional[str] = None) -> Dict:
        """Persists accepted nodes/edges for a file. `chunk_id_by_evidence`
        (optional) maps a chunk's text to its real DB chunk_id — used to
        best-effort resolve which chunk an edge's evidence_text actually
        came from (source_chunk_id), since extraction runs before
        chunking and can't know real chunk IDs yet (see
        ingestion.ingest.analyze_and_chunk's ordering).

        Only nodes/edges with accepted=True are saved — Import Preview's
        accept/reject/edit step (see admin/routes.py) flips `accepted`
        before this is called."""
        accepted_nodes = [n for n in nodes if n.accepted]
        if not accepted_nodes:
            return {"nodes_saved": 0, "edges_saved": 0}

        node_id_by_norm: Dict[str, str] = {}
        node_rows = []
        for n in accepted_nodes:
            node_id_by_norm[n.normalized_name] = n.id
            node_rows.append({
                "id": n.id, "knowledge_file_id": file_id, "knowledge_item_id": knowledge_item_id,
                "chunk_id": None, "node_type": n.node_type, "name": n.name,
                "normalized_name": n.normalized_name, "description": n.description,
                "confidence": n.confidence, "metadata": {},
                # ref_count=1: this file is the node's only current owner.
                # No cross-file node dedup/reuse exists yet — every save_graph
                # call creates fresh rows — but stamping it explicitly (not
                # relying only on the column's DB default) keeps this in sync
                # with delete_graph_for_file()'s decrement-on-delete logic
                # once/if a future feature starts sharing nodes across files
                # (incrementing ref_count on each additional reference).
                "ref_count": 1,
            })

        try:
            sb.table("knowledge_graph_nodes").insert(node_rows).execute()
        except Exception as exc:
            # migrations/023_graph_node_ref_count.sql not applied yet — the
            # ref_count column doesn't exist. Retry without it (the table's
            # own DEFAULT 1 still applies once the migration IS run; this
            # keeps graph extraction working in the meantime instead of
            # silently saving nothing).
            if "ref_count" in str(exc):
                try:
                    for row in node_rows:
                        row.pop("ref_count", None)
                    sb.table("knowledge_graph_nodes").insert(node_rows).execute()
                except Exception as exc2:
                    print(f"[knowledge_graph_service] node insert failed for file_id={file_id}: {exc2}")
                    return {"nodes_saved": 0, "edges_saved": 0}
            else:
                print(f"[knowledge_graph_service] node insert failed for file_id={file_id}: {exc}")
                return {"nodes_saved": 0, "edges_saved": 0}

        edge_rows = []
        for e in edges:
            if not e.accepted:
                continue
            src_norm, tgt_norm = normalize_node_name(e.source), normalize_node_name(e.target)
            src_id, tgt_id = node_id_by_norm.get(src_norm), node_id_by_norm.get(tgt_norm)
            if not src_id or not tgt_id:
                continue  # referenced node was rejected/not accepted -> skip this edge too
            chunk_id = None
            if chunk_id_by_evidence:
                for text, cid in chunk_id_by_evidence.items():
                    if e.evidence_text and e.evidence_text in text:
                        chunk_id = cid
                        break
            edge_rows.append({
                "id": e.id, "knowledge_file_id": file_id, "source_node_id": src_id,
                "target_node_id": tgt_id, "relation_type": e.relation_type,
                "relation_label": e.relation_label, "evidence_text": e.evidence_text,
                "source_chunk_id": chunk_id, "confidence": e.confidence, "metadata": {},
            })

        if edge_rows:
            try:
                sb.table("knowledge_graph_edges").insert(edge_rows).execute()
            except Exception as exc:
                print(f"[knowledge_graph_service] edge insert failed for file_id={file_id}: {exc}")
                return {"nodes_saved": len(node_rows), "edges_saved": 0}

        return {"nodes_saved": len(node_rows), "edges_saved": len(edge_rows)}

    def get_graph_for_file(self, sb, file_id: str) -> Dict:
        try:
            nodes = sb.table("knowledge_graph_nodes").select("*") \
                .eq("knowledge_file_id", file_id).is_("deleted_at", "null").execute().data or []
            edges = sb.table("knowledge_graph_edges").select("*") \
                .eq("knowledge_file_id", file_id).is_("deleted_at", "null").execute().data or []
            return {"nodes": nodes, "edges": edges}
        except Exception as exc:
            print(f"[knowledge_graph_service] get_graph_for_file failed: {exc}")
            return {"nodes": [], "edges": []}

    def get_graph_for_chunk(self, sb, chunk_id: str) -> Dict:
        try:
            edges = sb.table("knowledge_graph_edges").select("*") \
                .eq("source_chunk_id", chunk_id).is_("deleted_at", "null").execute().data or []
            node_ids = {e["source_node_id"] for e in edges} | {e["target_node_id"] for e in edges}
            nodes = []
            if node_ids:
                ids_str = "(" + ",".join(node_ids) + ")"
                nodes = sb.table("knowledge_graph_nodes").select("*") \
                    .filter("id", "in", ids_str).is_("deleted_at", "null").execute().data or []
            return {"nodes": nodes, "edges": edges}
        except Exception as exc:
            print(f"[knowledge_graph_service] get_graph_for_chunk failed: {exc}")
            return {"nodes": [], "edges": []}

    def delete_graph_for_file(self, sb, file_id: str) -> Dict:
        """Called from the SAME shared delete path every other child table
        (chunks, excel data, attachments) goes through — see
        admin/routes.py's _delete_file_id — so graph data can never orphan
        the way earlier bugs this session showed other child tables could.

        Edges always belong to exactly one file (schema: knowledge_file_id
        NOT NULL, never shared) — deleted outright. Nodes are reference-
        counted: this file's ownership is released by decrementing
        ref_count by 1; the node is only actually (soft-)deleted once its
        ref_count reaches 0. Today every node's ref_count starts at 1 (see
        save_graph — no cross-file node sharing exists yet), so this is
        observably identical to unconditional delete; it's what makes
        deletion correct once/if a future feature starts sharing a node
        across multiple files' extractions (ref_count > 1) instead of
        silently deleting a node another file still depends on, or leaving
        it orphaned forever with no owner left to release it.

        Returns {"edges_deleted": int, "nodes_deleted": int,
        "nodes_decremented": int, "error": Optional[str]} — `error`, when
        set, is the EXACT underlying exception message (never the generic
        "Data remains"), so admin/routes.py's delete summary can show
        precisely why graph cleanup didn't complete.
        """
        result = {"edges_deleted": 0, "nodes_deleted": 0, "nodes_decremented": 0, "error": None}
        now = _now_iso()

        try:
            edge_count_res = sb.table("knowledge_graph_edges").select("id", count="exact") \
                .eq("knowledge_file_id", file_id).is_("deleted_at", "null").execute()
            edge_count = edge_count_res.count or 0
            sb.table("knowledge_graph_edges").update({"deleted_at": now}) \
                .eq("knowledge_file_id", file_id).is_("deleted_at", "null").execute()
            result["edges_deleted"] = edge_count
        except Exception as exc:
            result["error"] = f"graph_edges cleanup failed: {exc}"
            print(f"[knowledge_graph_service] {result['error']} (file_id={file_id})")
            return result

        try:
            nodes = sb.table("knowledge_graph_nodes").select("id,ref_count") \
                .eq("knowledge_file_id", file_id).is_("deleted_at", "null").execute().data or []
        except Exception as exc:
            # ref_count column not present yet (migration 023 not applied) —
            # fall back to unconditional delete, the pre-ref-counting
            # behavior, rather than leaving nodes untouched.
            if "ref_count" in str(exc):
                try:
                    node_count_res = sb.table("knowledge_graph_nodes").select("id", count="exact") \
                        .eq("knowledge_file_id", file_id).is_("deleted_at", "null").execute()
                    node_count = node_count_res.count or 0
                    sb.table("knowledge_graph_nodes").update({"deleted_at": now}) \
                        .eq("knowledge_file_id", file_id).is_("deleted_at", "null").execute()
                    result["nodes_deleted"] = node_count
                    return result
                except Exception as exc2:
                    result["error"] = f"graph_nodes cleanup failed: {exc2}"
                    print(f"[knowledge_graph_service] {result['error']} (file_id={file_id})")
                    return result
            result["error"] = f"graph_nodes lookup failed: {exc}"
            print(f"[knowledge_graph_service] {result['error']} (file_id={file_id})")
            return result

        for n in nodes:
            new_ref_count = (n.get("ref_count") if n.get("ref_count") is not None else 1) - 1
            try:
                if new_ref_count <= 0:
                    sb.table("knowledge_graph_nodes").update(
                        {"deleted_at": now, "ref_count": 0}
                    ).eq("id", n["id"]).execute()
                    result["nodes_deleted"] += 1
                else:
                    sb.table("knowledge_graph_nodes").update(
                        {"ref_count": new_ref_count}
                    ).eq("id", n["id"]).execute()
                    result["nodes_decremented"] += 1
            except Exception as exc:
                result["error"] = f"graph_nodes cleanup failed for node {n['id']}: {exc}"
                print(f"[knowledge_graph_service] {result['error']} (file_id={file_id})")
                return result

        return result


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


_service_singleton: Optional[KnowledgeGraphService] = None


def get_knowledge_graph_service() -> KnowledgeGraphService:
    global _service_singleton
    if _service_singleton is None:
        _service_singleton = KnowledgeGraphService()
    return _service_singleton
