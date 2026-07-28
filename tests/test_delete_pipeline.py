"""Tests for the unified file-delete pipeline (admin/routes.py):

- _verify_no_orphans() must attach the EXACT reason for any leftover row
  (the real exception from the failed step, or a specific "step reported
  success but row still active" note) — never the generic "Data remains".
- _delete_file_id() is the single shared per-file cleanup routine used by
  every delete entry point (delete_file, delete_bulk, the SSE
  delete-stream generator) — this is what makes the "N divergent delete
  implementations, each missing something" bug class (chunks-only, then
  missing knowledge_items/attachments/excel, then missing
  knowledge_graph_nodes/edges) structurally impossible going forward.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ENABLE_KNOWLEDGE_ANALYZER_LLM", "false")
os.environ.setdefault("ENABLE_KNOWLEDGE_GRAPH", "false")

import admin.routes as routes


class FakeTable:
    def __init__(self, store, name):
        self.store, self.name, self._eq, self._is = store, name, {}, {}
        self._count_mode = False
        self._pending_delete = False
        self._pending_update = None

    def select(self, *a, **k):
        self._count_mode = k.get("count") == "exact"
        return self

    def delete(self):
        self._pending_delete = True
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
        rows = self._matching_rows()
        result = MagicMock(data=rows)
        result.count = len(rows) if self._count_mode else None
        if self._pending_delete:
            remaining = [r for r in self.store.get(self.name, []) if r not in rows]
            self.store[self.name] = remaining
        elif self._pending_update:
            for row in rows:
                row.update(self._pending_update)
        return result


class FakeSb:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return FakeTable(self.store, name)


class TestVerifyNoOrphansReasons(unittest.TestCase):
    def test_clean_state_reports_no_orphans(self):
        sb = FakeSb()
        with patch.object(routes, "get_sb", return_value=sb):
            orphans = routes._verify_no_orphans("file-1")
        self.assertEqual(orphans, {})

    def test_orphan_with_no_step_info_gets_specific_fallback_reason_not_generic(self):
        sb = FakeSb()
        sb.store["knowledge_graph_nodes"] = [
            {"id": "n1", "knowledge_file_id": "file-1", "deleted_at": None},
        ]
        with patch.object(routes, "get_sb", return_value=sb):
            orphans = routes._verify_no_orphans("file-1")
        self.assertIn("knowledge_graph_nodes", orphans)
        reason = orphans["knowledge_graph_nodes"]["reason"]
        self.assertNotEqual(reason, "Data remains")
        self.assertIn("graph_nodes", reason)
        self.assertEqual(orphans["knowledge_graph_nodes"]["count"], 1)

    def test_orphan_from_a_failed_step_surfaces_the_exact_exception_message(self):
        sb = FakeSb()
        sb.store["knowledge_graph_edges"] = [
            {"id": "e1", "knowledge_file_id": "file-1", "deleted_at": None},
        ]
        steps = {"graph_edges": {"status": "error", "count": None,
                                  "reason": "connection reset by peer"}}
        with patch.object(routes, "get_sb", return_value=sb):
            orphans = routes._verify_no_orphans("file-1", steps)
        self.assertEqual(orphans["knowledge_graph_edges"]["reason"], "connection reset by peer")

    def test_soft_deleted_rows_are_not_orphans(self):
        sb = FakeSb()
        sb.store["knowledge_graph_nodes"] = [
            {"id": "n1", "knowledge_file_id": "file-1", "deleted_at": "2026-01-01T00:00:00Z"},
        ]
        with patch.object(routes, "get_sb", return_value=sb):
            orphans = routes._verify_no_orphans("file-1")
        self.assertEqual(orphans, {})

    def test_hard_deleted_chunks_counted_as_orphan_when_still_present(self):
        sb = FakeSb()
        sb.store["knowledge_chunks"] = [{"id": "c1", "file_id": "file-1"}]
        with patch.object(routes, "get_sb", return_value=sb):
            orphans = routes._verify_no_orphans("file-1")
        self.assertIn("knowledge_chunks", orphans)


class TestDeleteFileIdSharedRoutine(unittest.TestCase):
    """_delete_file_id() must be the ONE place every delete entry point
    routes through — these tests exercise it directly, independent of the
    HTTP layer, confirming it reports all required checklist steps."""

    def _run(self, sb, file_id="file-1", name="test.pdf"):
        with patch.object(routes, "get_sb", return_value=sb), \
             patch.object(routes, "delete_excel_data", return_value={"excel_rows": 0, "excel_sheets": 0, "excel_workbooks": 0}), \
             patch.object(routes, "_handle_sync_job_files_on_delete", return_value=None), \
             patch.object(routes, "soft_delete_file", return_value=None), \
             patch("services.knowledge_graph_service.get_knowledge_graph_service") as mock_kg:
            mock_kg.return_value.delete_graph_for_file.return_value = {
                "edges_deleted": 2, "nodes_deleted": 3, "nodes_decremented": 0, "error": None,
            }
            return routes._delete_file_id(file_id, name, storage_meta=None)

    def test_all_required_steps_present_and_done_on_clean_delete(self):
        sb = FakeSb()
        result = self._run(sb)
        steps = result["steps"]
        required = ["chunks", "embeddings", "knowledge_items", "attachments",
                    "graph_nodes", "graph_edges", "sync_jobs", "disk", "file_record"]
        for step_name in required:
            self.assertIn(step_name, steps, f"missing required step: {step_name}")
            self.assertEqual(steps[step_name]["status"], "done", f"step not done: {step_name}")

    def test_graph_nodes_and_edges_counts_come_from_knowledge_graph_service(self):
        sb = FakeSb()
        result = self._run(sb)
        self.assertEqual(result["steps"]["graph_nodes"]["count"], 3)
        self.assertEqual(result["steps"]["graph_edges"]["count"], 2)

    def test_graph_cleanup_error_is_reported_with_exact_reason(self):
        sb = FakeSb()
        with patch.object(routes, "get_sb", return_value=sb), \
             patch.object(routes, "delete_excel_data", return_value={"excel_rows": 0, "excel_sheets": 0, "excel_workbooks": 0}), \
             patch.object(routes, "_handle_sync_job_files_on_delete", return_value=None), \
             patch.object(routes, "soft_delete_file", return_value=None), \
             patch("services.knowledge_graph_service.get_knowledge_graph_service") as mock_kg:
            mock_kg.return_value.delete_graph_for_file.return_value = {
                "edges_deleted": 0, "nodes_deleted": 0, "nodes_decremented": 0,
                "error": "graph_nodes cleanup failed for node n1: RLS policy violation",
            }
            result = routes._delete_file_id("file-1", "test.pdf", storage_meta=None)
        self.assertEqual(result["steps"]["graph_nodes"]["status"], "error")
        self.assertIn("RLS policy violation", result["steps"]["graph_nodes"]["reason"])


if __name__ == "__main__":
    unittest.main()
