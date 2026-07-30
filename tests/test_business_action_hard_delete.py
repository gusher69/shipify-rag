"""Regression tests for the 2026-07-29 hard-delete rework
(services/business_action_registry.py::hard_delete_action() /
dependent_record_counts() / is_protected_fixture()).

Soft delete used to leave a dead row still occupying its action_key
forever at the DB's unique-constraint level (the getdatacustomer
incident) — "Delete" now means permanently removed, action_key
immediately reusable, no recycle bin. Never hits a real DB/network.
"""
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.business_action_registry import BusinessActionRegistry


class _Query:
    """Minimal fake supporting `.select(..., count="exact")` with a REAL
    integer `.count` on the result (the shared _FakeSupabase in
    tests/test_business_action_registry.py silently drops the count
    kwarg, which would make dependent_record_counts()'s `res.count or 0`
    see a truthy MagicMock instead of an integer)."""

    def __init__(self, store, name):
        self.store = store
        self.name = name
        self._filters = {}
        self._is_filters = {}
        self._op = None
        self._payload = None
        self._want_count = False

    def select(self, *_a, **kwargs):
        self._op = "select"
        self._want_count = kwargs.get("count") == "exact"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, k, v):
        self._filters[k] = v
        return self

    def is_(self, k, v):
        self._is_filters[k] = v
        return self

    def _matched(self):
        rows = self.store.setdefault(self.name, [])
        out = []
        for r in rows:
            if not all(r.get(k) == v for k, v in self._filters.items()):
                continue
            ok = True
            for k, v in self._is_filters.items():
                if v == "null" and r.get(k) is not None:
                    ok = False
            if ok:
                out.append(r)
        return out

    def execute(self):
        rows = self.store.setdefault(self.name, [])
        if self._op == "select":
            matched = self._matched()
            return MagicMock(data=matched, count=(len(matched) if self._want_count else None))
        if self._op == "insert":
            row = dict(self._payload)
            row.setdefault("id", str(uuid.uuid4()))
            rows.append(row)
            return MagicMock(data=[row])
        if self._op == "delete":
            matched = self._matched()
            for r in matched:
                rows.remove(r)
            if self.name == "business_actions":
                # Emulate the real ON DELETE CASCADE FKs (migrations
                # 026/031/033) — this fake has no FK engine of its own,
                # so it must reproduce that cascade explicitly for
                # dependent-table tests to mean anything.
                for action_id in (r.get("id") for r in matched):
                    for table in BusinessActionRegistry._DEPENDENT_TABLES:
                        dep_rows = self.store.setdefault(table, [])
                        self.store[table] = [d for d in dep_rows if d.get("action_id") != action_id]
            return MagicMock(data=matched)
        return MagicMock(data=[])


class _FakeSb:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _Query(self.store, name)

    def seed_action(self, **overrides):
        row = {"id": str(uuid.uuid4()), "action_key": "test_action", "name": "Test",
               "category": "General", "is_draft": True, "enabled": False, "deleted_at": None}
        row.update(overrides)
        self.store.setdefault("business_actions", []).append(row)
        return row

    def seed_dependent(self, table, action_id, **overrides):
        row = {"id": str(uuid.uuid4()), "action_id": action_id}
        row.update(overrides)
        self.store.setdefault(table, []).append(row)
        return row


class TestHardDeleteBasics(unittest.TestCase):
    def test_draft_action_is_physically_deleted(self):
        sb = _FakeSb()
        action = sb.seed_action(action_key="draft_one")
        reg = BusinessActionRegistry(sb)

        result = reg.hard_delete_action(action["id"])

        self.assertTrue(result["ok"])
        self.assertIsNone(reg.get(action["id"]))
        self.assertEqual(sb.store["business_actions"], [])

    def test_action_key_becomes_reusable_after_hard_delete(self):
        sb = _FakeSb()
        action = sb.seed_action(action_key="reuse_me")
        reg = BusinessActionRegistry(sb)
        reg.hard_delete_action(action["id"])

        self.assertIsNone(reg.get_by_key_including_deleted("reuse_me"))
        new_action = reg.create({"action_key": "reuse_me", "name": "Reused", "action_type": "API"})
        self.assertEqual(new_action["action_key"], "reuse_me")

    def test_recreating_same_action_key_succeeds(self):
        sb = _FakeSb()
        action = sb.seed_action(action_key="dup_key")
        reg = BusinessActionRegistry(sb)
        reg.hard_delete_action(action["id"])

        recreated = reg.create({"action_key": "dup_key", "name": "Recreated", "action_type": "API"})
        self.assertIsNotNone(recreated["id"])
        self.assertNotEqual(recreated["id"], action["id"])

    def test_related_schema_and_dependents_are_deleted(self):
        sb = _FakeSb()
        action = sb.seed_action(action_key="with_deps")
        sb.seed_dependent("integration_action_schemas", action["id"], version_number=1)
        sb.seed_dependent("business_action_parameters", action["id"], name="CustCode")
        sb.seed_dependent("erp_test_cases", action["id"])
        reg = BusinessActionRegistry(sb)

        result = reg.hard_delete_action(action["id"])

        self.assertEqual(result["removed_dependent_counts"]["integration_action_schemas"], 1)
        self.assertEqual(result["removed_dependent_counts"]["business_action_parameters"], 1)
        self.assertEqual(result["removed_dependent_counts"]["erp_test_cases"], 1)
        for table in ("integration_action_schemas", "business_action_parameters", "erp_test_cases"):
            self.assertEqual(len(sb.store.get(table, [])), 0)

    def test_no_orphan_records_remain_after_delete(self):
        sb = _FakeSb()
        action = sb.seed_action(action_key="clean")
        reg = BusinessActionRegistry(sb)
        reg.hard_delete_action(action["id"])
        for table in reg._DEPENDENT_TABLES:
            self.assertEqual(len(sb.store.get(table, [])), 0)


class TestProtectedFixtures(unittest.TestCase):
    def test_protected_fixture_cannot_be_deleted(self):
        sb = _FakeSb()
        action = sb.seed_action(action_key="fixture_order_lookup", category="Internal Test Fixtures")
        reg = BusinessActionRegistry(sb)

        with self.assertRaises(ValueError) as ctx:
            reg.hard_delete_action(action["id"])
        self.assertEqual(str(ctx.exception), "protected_fixture")
        # Never removed.
        self.assertIsNotNone(reg.get(action["id"]))

    def test_protected_fixture_can_be_force_deleted(self):
        sb = _FakeSb()
        action = sb.seed_action(action_key="fixture_x", category="Internal Test Fixtures")
        reg = BusinessActionRegistry(sb)

        result = reg.hard_delete_action(action["id"], force=True)
        self.assertTrue(result["ok"])
        self.assertIsNone(reg.get(action["id"]))

    def test_action_not_found_raises_clear_error(self):
        sb = _FakeSb()
        reg = BusinessActionRegistry(sb)
        with self.assertRaises(ValueError) as ctx:
            reg.hard_delete_action("nonexistent-id")
        self.assertEqual(str(ctx.exception), "action_not_found")


class TestOrphanDetectionNeverReportsFalseSuccess(unittest.TestCase):
    def test_orphans_remaining_after_delete_raise_instead_of_silent_success(self):
        """Simulates a cascade failure (should be structurally impossible
        with real FKs, but must never be silently reported as success if
        it ever happened) — dependent_record_counts() is monkeypatched to
        report a lingering row AFTER the main delete already ran."""
        sb = _FakeSb()
        action = sb.seed_action(action_key="orphan_case")
        reg = BusinessActionRegistry(sb)

        real_counts = reg.dependent_record_counts
        call_count = {"n": 0}

        def flaky_counts(action_id):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return real_counts(action_id)  # pre-delete count (0)
            return {"business_action_parameters": 1}  # post-delete: orphan found

        reg.dependent_record_counts = flaky_counts

        with self.assertRaises(RuntimeError) as ctx:
            reg.hard_delete_action(action["id"])
        self.assertIn("orphan_records_after_delete", str(ctx.exception))
        # The main row IS gone (Postgres cascade is what actually
        # guarantees no real orphans; this path only fires if that
        # guarantee were ever violated) — but the caller must see a loud
        # failure, never a quiet "ok": True.
        self.assertIsNone(reg.get(action["id"]))


class TestAuditLog(unittest.TestCase):
    """2026-07-29 production-readiness audit, Part 4/8 — an immutable
    business_action_audit_log row is written on delete, never contains
    secrets, and a write failure is surfaced (not silently swallowed)
    without undoing the already-completed delete."""

    def test_audit_event_written_without_secrets(self):
        sb = _FakeSb()
        action = sb.seed_action(action_key="audited_one", display_name="Audited One",
                                 is_draft=False, enabled=True)
        sb.seed_dependent("business_action_execution", action["id"],
                           auth_config={"api_key": "sk-should-never-leak"})
        reg = BusinessActionRegistry(sb)

        result = reg.hard_delete_action(action["id"], actor="tester")
        self.assertIsNone(result["audit_log_warning"])

        rows = sb.store["business_action_audit_log"]
        self.assertEqual(len(rows), 1)
        event = rows[0]
        self.assertEqual(event["event"], "business_action.deleted")
        self.assertEqual(event["actor"], "tester")
        self.assertEqual(event["action_key"], "audited_one")
        self.assertEqual(event["detail"]["display_name"], "Audited One")
        self.assertEqual(event["detail"]["previous_status"], "published")
        self.assertEqual(event["detail"]["deletion_result"], "ok")
        # No secret-shaped value anywhere in the event, ever.
        blob = str(event)
        self.assertNotIn("sk-should-never-leak", blob)

    def test_audit_write_failure_does_not_undo_delete(self):
        sb = _FakeSb()
        action = sb.seed_action(action_key="audit_fails")
        reg = BusinessActionRegistry(sb)

        orig_table = sb.table

        def flaky_table(name):
            if name == "business_action_audit_log":
                raise RuntimeError("simulated audit-log outage")
            return orig_table(name)

        sb.table = flaky_table

        result = reg.hard_delete_action(action["id"])
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["audit_log_warning"])
        self.assertIn("audit-log write failed", result["audit_log_warning"])
        # The delete itself is NOT undone just because logging failed.
        sb.table = orig_table
        self.assertIsNone(reg.get(action["id"]))


class TestRepeatedDeleteIsSafe(unittest.TestCase):
    """Part 7, scenario 1/7 — two delete requests for the same action (or
    a delete of an already-deleted one) must be idempotent/clear, never
    a false success or a crash."""

    def test_second_delete_of_same_action_raises_not_found_not_false_success(self):
        sb = _FakeSb()
        action = sb.seed_action(action_key="delete_twice")
        reg = BusinessActionRegistry(sb)

        first = reg.hard_delete_action(action["id"])
        self.assertTrue(first["ok"])

        with self.assertRaises(ValueError) as ctx:
            reg.hard_delete_action(action["id"])
        self.assertEqual(str(ctx.exception), "action_not_found")


if __name__ == "__main__":
    unittest.main()
