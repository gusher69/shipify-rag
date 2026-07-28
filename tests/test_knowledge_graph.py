"""Tests for services/knowledge_graph_service.py: normalization,
deduplication, strict validation (evidence required, dangling references
dropped), save_graph persistence via a fake Supabase client, and delete
cleanup — plus the knowledge_analyzer.py integration point.
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import knowledge_graph_service as kg


class TestNormalization(unittest.TestCase):
    def test_case_insensitive(self):
        self.assertEqual(kg.normalize_node_name("Shipify"), kg.normalize_node_name("SHIPIFY"))

    def test_domain_suffix_stripped(self):
        self.assertEqual(kg.normalize_node_name("Shipify"), kg.normalize_node_name("shipify.co.th"))

    def test_punctuation_and_whitespace_folded(self):
        self.assertEqual(kg.normalize_node_name("China  Warehouse"), kg.normalize_node_name("China-Warehouse!"))


class TestDeduplication(unittest.TestCase):
    def test_duplicate_names_merged_into_one(self):
        nodes = [
            kg.GraphNode(name="Shipify", node_type="company", confidence=0.7),
            kg.GraphNode(name="SHIPIFY", node_type="company", confidence=0.9, description="A logistics company"),
            kg.GraphNode(name="shipify.co.th", node_type="company", confidence=0.6),
        ]
        deduped = kg.deduplicate_nodes(nodes)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].confidence, 0.9)
        self.assertEqual(deduped[0].description, "A logistics company")

    def test_distinct_entities_not_merged(self):
        nodes = [kg.GraphNode(name="Shipify"), kg.GraphNode(name="China Warehouse")]
        self.assertEqual(len(kg.deduplicate_nodes(nodes)), 2)


class TestStrictValidation(unittest.TestCase):
    def test_edge_without_evidence_is_dropped(self):
        raw = {
            "nodes": [{"name": "Shipify", "type": "company"}, {"name": "China Warehouse", "type": "location"}],
            "edges": [{"source": "Shipify", "target": "China Warehouse", "relation_type": "has", "evidence_text": ""}],
        }
        nodes, edges = kg._validate_graph(raw)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(len(edges), 0)

    def test_edge_with_evidence_is_kept(self):
        raw = {
            "nodes": [{"name": "Shipify", "type": "company"}, {"name": "China Warehouse", "type": "location"}],
            "edges": [{"source": "Shipify", "target": "China Warehouse", "relation_type": "belongs_to",
                       "evidence_text": "Shipify has a warehouse in China."}],
        }
        nodes, edges = kg._validate_graph(raw)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].evidence_text, "Shipify has a warehouse in China.")

    def test_edge_referencing_unextracted_node_is_dropped(self):
        raw = {
            "nodes": [{"name": "Shipify", "type": "company"}],
            "edges": [{"source": "Shipify", "target": "A Node That Was Never Extracted",
                       "relation_type": "provides", "evidence_text": "some evidence"}],
        }
        nodes, edges = kg._validate_graph(raw)
        self.assertEqual(len(edges), 0)

    def test_invalid_node_type_falls_back_to_other(self):
        raw = {"nodes": [{"name": "X", "type": "not_a_real_type"}], "edges": []}
        nodes, _ = kg._validate_graph(raw)
        self.assertEqual(nodes[0].node_type, "other")

    def test_invalid_relation_type_falls_back_to_related_to(self):
        raw = {
            "nodes": [{"name": "A", "type": "other"}, {"name": "B", "type": "other"}],
            "edges": [{"source": "A", "target": "B", "relation_type": "not_a_real_relation",
                       "evidence_text": "A relates to B somehow."}],
        }
        _, edges = kg._validate_graph(raw)
        self.assertEqual(edges[0].relation_type, "related_to")

    def test_duplicate_node_names_in_raw_input_deduplicated(self):
        raw = {"nodes": [{"name": "Shipify", "type": "company"}, {"name": "shipify", "type": "company"}], "edges": []}
        nodes, _ = kg._validate_graph(raw)
        self.assertEqual(len(nodes), 1)


class FakeTable:
    def __init__(self, store, name):
        self.store, self.name, self._eq, self._is = store, name, {}, {}
        self._count_mode = False
        self._select_cols = None
    def select(self, *a, **k):
        self._select_cols = a[0] if a else None
        self._count_mode = k.get("count") == "exact"
        return self
    def insert(self, rows):
        self.store.setdefault(self.name, []).extend(rows if isinstance(rows, list) else [rows])
        return self
    def update(self, fields):
        self._pending_update = fields
        return self
    def eq(self, col, val):
        self._eq[col] = val
        return self
    def is_(self, col, val):
        self._is[col] = None if val == "null" else val
        return self
    def filter(self, col, op, val):
        return self
    def _matching_rows(self):
        data = self.store.get(self.name, [])
        data = [r for r in data if all(r.get(k) == v for k, v in self._eq.items())]
        data = [r for r in data if all(r.get(k) == v for k, v in self._is.items())]
        return data
    def execute(self):
        # All filters (eq/is_) are applied by the time execute() is called,
        # regardless of the order they were chained in — matches how a
        # real Supabase query builder only actually runs on .execute().
        rows = self._matching_rows()
        if hasattr(self, "_pending_update"):
            for row in rows:
                row.update(self._pending_update)
        result = MagicMock(data=rows)
        result.count = len(rows) if self._count_mode else None
        return result


class FakeSb:
    def __init__(self):
        self.store = {}
    def table(self, name):
        return FakeTable(self.store, name)


class TestSaveGraph(unittest.TestCase):
    def test_only_accepted_nodes_and_edges_are_saved(self):
        sb = FakeSb()
        a = kg.GraphNode(name="Shipify", node_type="company", accepted=True)
        b = kg.GraphNode(name="China Warehouse", node_type="location", accepted=False)
        edge_ab = kg.GraphEdge(source="Shipify", target="China Warehouse", evidence_text="ev", accepted=True)
        result = kg.get_knowledge_graph_service().save_graph(sb, "file-1", [a, b], [edge_ab])
        self.assertEqual(result["nodes_saved"], 1)
        # Edge references a rejected node -> must not be saved even though accepted=True on the edge itself.
        self.assertEqual(result["edges_saved"], 0)

    def test_accepted_edge_between_two_accepted_nodes_is_saved(self):
        sb = FakeSb()
        a = kg.GraphNode(name="Shipify", node_type="company")
        b = kg.GraphNode(name="Shipping Service", node_type="service")
        edge = kg.GraphEdge(source="Shipify", target="Shipping Service",
                             relation_type="provides", evidence_text="Shipify provides shipping services.")
        result = kg.get_knowledge_graph_service().save_graph(sb, "file-1", [a, b], [edge])
        self.assertEqual(result["nodes_saved"], 2)
        self.assertEqual(result["edges_saved"], 1)
        saved_edge = sb.store["knowledge_graph_edges"][0]
        self.assertEqual(saved_edge["evidence_text"], "Shipify provides shipping services.")

    def test_no_accepted_nodes_saves_nothing(self):
        sb = FakeSb()
        a = kg.GraphNode(name="X", accepted=False)
        result = kg.get_knowledge_graph_service().save_graph(sb, "file-1", [a], [])
        self.assertEqual(result, {"nodes_saved": 0, "edges_saved": 0})


class TestGraphLookupsAndDelete(unittest.TestCase):
    def test_get_graph_for_file_returns_nodes_and_edges(self):
        sb = FakeSb()
        sb.store["knowledge_graph_nodes"] = [{"id": "n1", "knowledge_file_id": "file-1", "deleted_at": None}]
        sb.store["knowledge_graph_edges"] = [{"id": "e1", "knowledge_file_id": "file-1", "deleted_at": None}]
        result = kg.get_knowledge_graph_service().get_graph_for_file(sb, "file-1")
        self.assertEqual(len(result["nodes"]), 1)
        self.assertEqual(len(result["edges"]), 1)

    def test_delete_graph_for_file_soft_deletes(self):
        sb = FakeSb()
        sb.store["knowledge_graph_nodes"] = [{"id": "n1", "knowledge_file_id": "file-1", "deleted_at": None}]
        sb.store["knowledge_graph_edges"] = [{"id": "e1", "knowledge_file_id": "file-1", "deleted_at": None}]
        kg.get_knowledge_graph_service().delete_graph_for_file(sb, "file-1")
        self.assertIsNotNone(sb.store["knowledge_graph_nodes"][0]["deleted_at"])
        self.assertIsNotNone(sb.store["knowledge_graph_edges"][0]["deleted_at"])

    def test_delete_returns_exact_counts_and_no_error(self):
        sb = FakeSb()
        sb.store["knowledge_graph_nodes"] = [
            {"id": "n1", "knowledge_file_id": "file-1", "deleted_at": None, "ref_count": 1},
            {"id": "n2", "knowledge_file_id": "file-1", "deleted_at": None, "ref_count": 1},
        ]
        sb.store["knowledge_graph_edges"] = [
            {"id": "e1", "knowledge_file_id": "file-1", "deleted_at": None},
        ]
        result = kg.get_knowledge_graph_service().delete_graph_for_file(sb, "file-1")
        self.assertEqual(result["nodes_deleted"], 2)
        self.assertEqual(result["nodes_decremented"], 0)
        self.assertEqual(result["edges_deleted"], 1)
        self.assertIsNone(result["error"])


class TestGraphNodeReferenceCounting(unittest.TestCase):
    """Requirement: 'If graph nodes are shared by multiple files in the
    future, support reference counting and only delete nodes whose
    reference count becomes zero.' Today every node's ref_count starts at
    1 (no cross-file sharing exists yet — see save_graph), so a single
    file's delete always brings it to 0 — these tests simulate the FUTURE
    shared-node case directly against the ref_count column."""

    def test_shared_node_with_ref_count_two_survives_first_release(self):
        sb = FakeSb()
        # A node "shared" by two files (ref_count=2) — deleting file-1
        # must only decrement it, never remove it, since file-2 still
        # depends on it.
        sb.store["knowledge_graph_nodes"] = [
            {"id": "shared-node", "knowledge_file_id": "file-1", "deleted_at": None, "ref_count": 2},
        ]
        sb.store["knowledge_graph_edges"] = []
        result = kg.get_knowledge_graph_service().delete_graph_for_file(sb, "file-1")
        self.assertEqual(result["nodes_deleted"], 0)
        self.assertEqual(result["nodes_decremented"], 1)
        node = sb.store["knowledge_graph_nodes"][0]
        self.assertEqual(node["ref_count"], 1)
        self.assertIsNone(node["deleted_at"])

    def test_shared_node_deleted_only_after_ref_count_reaches_zero(self):
        sb = FakeSb()
        sb.store["knowledge_graph_nodes"] = [
            {"id": "shared-node", "knowledge_file_id": "file-1", "deleted_at": None, "ref_count": 1},
        ]
        sb.store["knowledge_graph_edges"] = []
        result = kg.get_knowledge_graph_service().delete_graph_for_file(sb, "file-1")
        self.assertEqual(result["nodes_deleted"], 1)
        self.assertEqual(result["nodes_decremented"], 0)
        node = sb.store["knowledge_graph_nodes"][0]
        self.assertIsNotNone(node["deleted_at"])
        self.assertEqual(node["ref_count"], 0)

    def test_missing_ref_count_column_falls_back_to_unconditional_delete(self):
        """migrations/023_graph_node_ref_count.sql not applied yet — the
        select for ref_count raises; delete_graph_for_file must still
        clean up nodes (pre-ref-counting behavior) instead of leaving
        them untouched."""
        class NoRefCountTable(FakeTable):
            def execute(self):
                if self._select_cols and "ref_count" in self._select_cols:
                    raise Exception('column "ref_count" does not exist')
                return super().execute()

        class NoRefCountSb(FakeSb):
            def table(self, name):
                return NoRefCountTable(self.store, name)

        sb = NoRefCountSb()
        sb.store["knowledge_graph_nodes"] = [{"id": "n1", "knowledge_file_id": "file-1", "deleted_at": None}]
        sb.store["knowledge_graph_edges"] = []
        result = kg.get_knowledge_graph_service().delete_graph_for_file(sb, "file-1")
        self.assertEqual(result["nodes_deleted"], 1)
        self.assertIsNotNone(sb.store["knowledge_graph_nodes"][0]["deleted_at"])

    def test_edge_cleanup_failure_reports_exact_reason_not_generic(self):
        class FailingEdgeTable(FakeTable):
            def execute(self):
                if self.name == "knowledge_graph_edges":
                    raise Exception("connection reset by peer")
                return super().execute()

        class FailingEdgeSb(FakeSb):
            def table(self, name):
                return FailingEdgeTable(self.store, name)

        sb = FailingEdgeSb()
        sb.store["knowledge_graph_nodes"] = [{"id": "n1", "knowledge_file_id": "file-1", "deleted_at": None, "ref_count": 1}]
        sb.store["knowledge_graph_edges"] = [{"id": "e1", "knowledge_file_id": "file-1", "deleted_at": None}]
        result = kg.get_knowledge_graph_service().delete_graph_for_file(sb, "file-1")
        self.assertIsNotNone(result["error"])
        self.assertIn("connection reset by peer", result["error"])
        self.assertNotEqual(result["error"], "Data remains")


class TestExtractGraphDisabledOrEmpty(unittest.TestCase):
    def test_disabled_via_env_returns_empty(self):
        with patch.dict(os.environ, {"ENABLE_KNOWLEDGE_GRAPH": "false"}):
            result = kg.get_knowledge_graph_service().extract_graph("some text", "f.pdf")
        self.assertEqual(result, {"nodes": [], "edges": [], "error": False})

    def test_empty_text_returns_empty(self):
        with patch.dict(os.environ, {"ENABLE_KNOWLEDGE_GRAPH": "true"}):
            result = kg.get_knowledge_graph_service().extract_graph("", "f.pdf")
        self.assertEqual(result, {"nodes": [], "edges": [], "error": False})

    def test_llm_failure_degrades_to_empty_not_raise(self):
        with patch.dict(os.environ, {"ENABLE_KNOWLEDGE_GRAPH": "true"}), \
             patch("services.llm_service.get_llm_service", side_effect=Exception("no api key")):
            result = kg.get_knowledge_graph_service().extract_graph("some real text content", "f.pdf")
        self.assertEqual(result, {"nodes": [], "edges": [], "error": True})


if __name__ == "__main__":
    unittest.main()
