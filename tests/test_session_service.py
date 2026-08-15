"""Tests for the AI Session Management system (services/session_service.py)."""
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock
from dataclasses import dataclass, field
from typing import List, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.session_service as ss


class FakeQuery:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._filters = []
        self._pending_update = None
        self._limit = None
        self._order = None
        self._single = False

    def select(self, *a, **k): return self
    def is_(self, col, val):
        self._filters.append(lambda r: (r.get(col) is None) == (val == "null"))
        return self
    def eq(self, col, val):
        self._filters.append(lambda r: r.get(col) == val)
        return self
    def in_(self, col, values):
        self._filters.append(lambda r: r.get(col) in values)
        return self
    def ilike(self, col, pattern):
        needle = pattern.strip("%").lower()
        self._filters.append(lambda r: needle in (r.get(col) or "").lower())
        return self
    def order(self, col, desc=False):
        self._order = (col, desc)
        return self
    def limit(self, n):
        self._limit = n
        return self
    def single(self):
        self._single = True
        return self

    def insert(self, rows):
        rows = rows if isinstance(rows, list) else [rows]
        inserted = []
        for row in rows:
            row = dict(row)
            row.setdefault("id", str(uuid.uuid4()))
            self.store.setdefault(self.name, []).append(row)
            inserted.append(row)
        self._result = inserted
        return self

    def upsert(self, payload, on_conflict=None):
        rows = payload if isinstance(payload, list) else [payload]
        key = on_conflict
        out = []
        for row in rows:
            row = dict(row)
            existing = None
            if key:
                existing = next((r for r in self.store.get(self.name, []) if r.get(key) == row.get(key)), None)
            if existing:
                existing.update(row)
                out.append(existing)
            else:
                row.setdefault("id", str(uuid.uuid4()))
                self.store.setdefault(self.name, []).append(row)
                out.append(row)
        self._result = out
        return self

    def update(self, fields):
        self._pending_update = fields
        return self

    def delete(self):
        self._pending_update = "__DELETE__"
        return self

    def execute(self):
        if hasattr(self, "_result"):
            return MagicMock(data=self._result)
        data = self.store.get(self.name, [])
        for f in self._filters:
            data = [r for r in data if f(r)]
        if self._pending_update == "__DELETE__":
            remaining = [r for r in self.store.get(self.name, []) if r not in data]
            self.store[self.name] = remaining
        elif self._pending_update:
            for r in data:
                r.update(self._pending_update)
        if self._order:
            col, desc = self._order
            data = sorted(data, key=lambda r: r.get(col), reverse=desc)
        if self._limit:
            data = data[:self._limit]
        if self._single:
            if len(data) != 1:
                raise Exception(f"single() expected exactly one row, got {len(data)}")
            return MagicMock(data=data[0])
        return MagicMock(data=data)


class FakeSb:
    def __init__(self):
        self.store = {}
    def table(self, name):
        return FakeQuery(self.store, name)


@dataclass
class FakeVerdict:
    name: str
    status: str


@dataclass
class FakePolicy:
    escalate: bool = False
    active_count: int = 0
    verdicts: List[FakeVerdict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass
class FakeTemplate:
    id: str = "line_oa_default"
    name: str = "LINE OA Default"
    version: str = "1"
    system_prompt: str = "system"


@dataclass
class FakePrompt:
    template: FakeTemplate = field(default_factory=FakeTemplate)
    final_prompt_text: str = "[SYSTEM]...[USER]..."


@dataclass
class FakeResult:
    answer: str = "Test answer"
    chunks: List[Dict] = field(default_factory=lambda: [{"source": "a.pdf", "file_name": "a.pdf", "score": 0.9}])
    context: str = "context"
    prompt: FakePrompt = field(default_factory=FakePrompt)
    policy: FakePolicy = field(default_factory=FakePolicy)
    stages: List = field(default_factory=list)
    model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    temperature: float = 0.3
    input_tokens: int = 100
    output_tokens: int = 50
    latency_ms: float = 250.0
    estimated_cost_usd: float = 0.001
    confidence: float = 0.9
    confidence_label: str = "High"
    services_used: List[Dict] = field(default_factory=list)


class TestSessionCRUD(unittest.TestCase):
    def setUp(self):
        self.fake_sb = FakeSb()
        patcher = patch("services.session_service._get_sb", return_value=self.fake_sb)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.svc = ss.SessionService()

    def test_create_session_returns_row_with_id(self):
        session = self.svc.create_session(name="My Test")
        self.assertIsNotNone(session)
        self.assertEqual(session["name"], "My Test")
        self.assertIn("id", session)

    def test_get_session_returns_none_for_missing(self):
        self.assertIsNone(self.svc.get_session("nonexistent"))

    def test_rename_session(self):
        session = self.svc.create_session(name="Old Name")
        ok = self.svc.rename_session(session["id"], "New Name")
        self.assertTrue(ok)
        renamed = self.svc.get_session(session["id"])
        self.assertEqual(renamed["name"], "New Name")

    def test_delete_session_soft_deletes(self):
        session = self.svc.create_session()
        self.svc.delete_session(session["id"])
        self.assertIsNone(self.svc.get_session(session["id"]))

    def test_list_sessions_excludes_deleted(self):
        s1 = self.svc.create_session(name="Keep me")
        s2 = self.svc.create_session(name="Delete me")
        self.svc.delete_session(s2["id"])
        sessions = self.svc.list_sessions()
        ids = [s["id"] for s in sessions]
        self.assertIn(s1["id"], ids)
        self.assertNotIn(s2["id"], ids)


class TestGetRecentHistory(unittest.TestCase):
    """Customer Intelligence V1 (2026-08-15), Phase 4 — Context Continuity.
    get_recent_history() must return a bounded, oldest-first window scoped
    to ONE conversation only."""

    def setUp(self):
        self.fake_sb = FakeSb()
        patcher = patch("services.session_service._get_sb", return_value=self.fake_sb)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.svc = ss.SessionService()

    def _seed_turns(self, session_id, turns):
        rows = []
        for i, (role, content) in enumerate(turns):
            rows.append({"session_id": session_id, "turn_index": i, "role": role, "content": content})
        self.fake_sb.store.setdefault("ai_session_messages", []).extend(rows)

    def test_returns_oldest_first_within_bound(self):
        self._seed_turns("conv-1", [
            ("user", "turn1 user"), ("assistant", "turn1 assistant"),
            ("user", "turn2 user"), ("assistant", "turn2 assistant"),
            ("user", "turn3 user"), ("assistant", "turn3 assistant"),
        ])
        history = self.svc.get_recent_history("conv-1", max_turns=2)
        self.assertEqual([h["content"] for h in history],
                          ["turn2 user", "turn2 assistant", "turn3 user", "turn3 assistant"])
        self.assertEqual([h["role"] for h in history], ["user", "assistant", "user", "assistant"])

    def test_scoped_to_single_conversation_only(self):
        self._seed_turns("conv-A", [("user", "A says SP1014"), ("assistant", "ok A")])
        self._seed_turns("conv-B", [("user", "B says FT1004"), ("assistant", "ok B")])
        history_a = self.svc.get_recent_history("conv-A")
        contents = [h["content"] for h in history_a]
        self.assertIn("A says SP1014", contents)
        self.assertNotIn("B says FT1004", contents)
        self.assertNotIn("ok B", contents)

    def test_no_messages_returns_empty_list(self):
        self.assertEqual(self.svc.get_recent_history("conv-empty"), [])

    def test_query_failure_returns_empty_list_not_a_crash(self):
        broken_sb = MagicMock()
        broken_sb.table.side_effect = Exception("db down")
        with patch("services.session_service._get_sb", return_value=broken_sb):
            self.assertEqual(self.svc.get_recent_history("conv-1"), [])


class TestRecordTurn(unittest.TestCase):
    def setUp(self):
        self.fake_sb = FakeSb()
        patcher = patch("services.session_service._get_sb", return_value=self.fake_sb)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.svc = ss.SessionService()

    def test_record_turn_creates_session_when_none_given(self):
        result = FakeResult()
        session = self.svc.record_turn(None, "Where is the warehouse?", result)
        self.assertIsNotNone(session)
        self.assertEqual(session["message_count"], 2)
        self.assertEqual(len(session["messages"]), 2)
        self.assertEqual(session["messages"][0]["role"], "user")
        self.assertEqual(session["messages"][1]["role"], "assistant")

    def test_record_turn_persists_trace_with_chunks_and_prompt(self):
        result = FakeResult()
        session = self.svc.record_turn(None, "question", result)
        assistant_msg = session["messages"][1]
        self.assertIsNotNone(assistant_msg["trace"])
        self.assertEqual(assistant_msg["trace"]["chunks"][0]["source"], "a.pdf")
        self.assertEqual(assistant_msg["trace"]["prompt"]["template_id"], "line_oa_default")

    def test_record_turn_appends_to_existing_session(self):
        result = FakeResult()
        session = self.svc.record_turn(None, "first question", result)
        session_id = session["id"]
        session2 = self.svc.record_turn(session_id, "second question", result)
        self.assertEqual(session2["message_count"], 4)
        self.assertEqual(len(session2["messages"]), 4)

    def test_record_turn_updates_totals(self):
        result = FakeResult(input_tokens=100, output_tokens=50, estimated_cost_usd=0.002)
        session = self.svc.record_turn(None, "q", result)
        self.assertEqual(session["total_input_tokens"], 100)
        self.assertEqual(session["total_output_tokens"], 50)
        self.assertAlmostEqual(session["total_cost_usd"], 0.002)

    def test_record_turn_never_raises_on_persistence_failure(self):
        with patch("services.session_service._get_sb", side_effect=Exception("db down")):
            result = self.svc.record_turn(None, "q", FakeResult())
        self.assertIsNone(result)


class TestDuplicateAndClear(unittest.TestCase):
    def setUp(self):
        self.fake_sb = FakeSb()
        patcher = patch("services.session_service._get_sb", return_value=self.fake_sb)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.svc = ss.SessionService()

    def test_duplicate_session_copies_messages(self):
        original = self.svc.record_turn(None, "q1", FakeResult())
        dup = self.svc.duplicate_session(original["id"])
        self.assertIsNotNone(dup)
        self.assertNotEqual(dup["id"], original["id"])
        self.assertEqual(len(dup["messages"]), len(original["messages"]))
        self.assertIn("(copy)", dup["name"])

    def test_clear_conversation_removes_messages_keeps_session(self):
        session = self.svc.record_turn(None, "q1", FakeResult())
        self.svc.clear_conversation(session["id"])
        cleared = self.svc.get_session(session["id"])
        self.assertIsNotNone(cleared)
        self.assertEqual(cleared["messages"], [])
        self.assertEqual(cleared["message_count"], 0)


class TestCompareSessions(unittest.TestCase):
    def setUp(self):
        self.fake_sb = FakeSb()
        patcher = patch("services.session_service._get_sb", return_value=self.fake_sb)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.svc = ss.SessionService()

    def test_compare_returns_diff_for_both_sessions(self):
        a = self.svc.record_turn(None, "question A", FakeResult(answer="answer A"))
        b = self.svc.record_turn(None, "question B", FakeResult(answer="answer B"))
        diff = self.svc.compare_sessions(a["id"], b["id"])
        self.assertIsNotNone(diff)
        self.assertEqual(diff["answer_diff"]["a"], "answer A")
        self.assertEqual(diff["answer_diff"]["b"], "answer B")

    def test_compare_returns_none_for_missing_session(self):
        a = self.svc.create_session()
        self.assertIsNone(self.svc.compare_sessions(a["id"], "missing"))


class TestExport(unittest.TestCase):
    def setUp(self):
        self.fake_sb = FakeSb()
        patcher = patch("services.session_service._get_sb", return_value=self.fake_sb)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.svc = ss.SessionService()

    def test_export_markdown_includes_question_and_answer(self):
        session = self.svc.record_turn(None, "Where is the warehouse?", FakeResult(answer="Guangzhou"))
        md = self.svc.export_markdown(session["id"])
        self.assertIn("Where is the warehouse?", md)
        self.assertIn("Guangzhou", md)

    def test_export_json_returns_full_session(self):
        session = self.svc.record_turn(None, "q", FakeResult())
        data = self.svc.export_json(session["id"])
        self.assertEqual(data["id"], session["id"])
        self.assertEqual(len(data["messages"]), 2)

    def test_export_all_json_bundles_every_session_with_full_messages(self):
        s1 = self.svc.record_turn(None, "q1", FakeResult(answer="a1"))
        s2 = self.svc.record_turn(None, "q2", FakeResult(answer="a2"))
        data = self.svc.export_all_json()
        self.assertEqual(data["session_count"], 2)
        ids = {s["id"] for s in data["sessions"]}
        self.assertEqual(ids, {s1["id"], s2["id"]})
        for s in data["sessions"]:
            self.assertEqual(len(s["messages"]), 2)

    def test_export_all_json_respects_channel_filter(self):
        self.svc.record_turn(None, "line question", FakeResult(answer="a"))
        playground_session = self.svc.get_or_create_active_conversation("playground:X", channel="playground")
        self.svc.record_conversation_turn(playground_session["id"], "pg question", {"reply": {"text": "pg answer"}})
        data = self.svc.export_all_json(channel="playground")
        self.assertEqual(data["session_count"], 1)
        self.assertEqual(data["sessions"][0]["id"], playground_session["id"])


if __name__ == "__main__":
    unittest.main()
