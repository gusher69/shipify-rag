"""Tests for services/webhook_event_dedup_service.py (Webhook Redelivery
Dedup fix, Rapid-Message Concurrency investigation, 2026-08-25).

A tiny dedicated fake Supabase client that actually enforces the real
migration's UNIQUE (tenant_id, channel, webhook_event_id) constraint —
raising the real postgrest.exceptions.APIError with code 23505 on a
second insert of the same key, exactly like the real Postgres table
would — is the only way to prove claim_event()'s duplicate-detection
logic (as opposed to its happy path) without a real database.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from postgrest.exceptions import APIError

from services.webhook_event_dedup_service import WebhookEventDedupService, get_webhook_event_dedup_service


class _FakeInsert:
    def __init__(self, table, row):
        self._table = table
        self._row = row

    def execute(self):
        key = (self._row["tenant_id"], self._row["channel"], self._row["webhook_event_id"])
        if key in self._table._seen:
            raise APIError({"message": "duplicate key value violates unique constraint",
                             "code": "23505", "details": None, "hint": None})
        self._table._seen.add(key)
        self._table._rows.append(self._row)
        return type("Resp", (), {"data": [self._row]})()


class _FakeTable:
    def __init__(self):
        self._seen = set()
        self._rows = []

    def insert(self, row):
        return _FakeInsert(self, row)


class _FakeSupabaseWithUniqueConstraint:
    def __init__(self):
        self._tables = {}

    def table(self, name):
        return self._tables.setdefault(name, _FakeTable())


class _RaisingTable:
    """Simulates the DB being unreachable for any reason OTHER than a
    genuine duplicate."""
    def insert(self, row):
        raise ConnectionError("db unreachable")


class _RaisingSupabase:
    def table(self, name):
        return _RaisingTable()


class TestClaimEventFirstTimeSucceeds(unittest.TestCase):
    def test_first_claim_returns_true(self):
        svc = WebhookEventDedupService(_FakeSupabaseWithUniqueConstraint())
        claimed = svc.claim_event(tenant_id="default", channel="line", webhook_event_id="evt-1",
                                    conversation_key="U1")
        self.assertTrue(claimed)


class TestClaimEventDuplicateIsRejected(unittest.TestCase):
    def test_second_claim_of_same_event_returns_false(self):
        sb = _FakeSupabaseWithUniqueConstraint()
        svc = WebhookEventDedupService(sb)
        first = svc.claim_event(tenant_id="default", channel="line", webhook_event_id="evt-dup", conversation_key="U1")
        second = svc.claim_event(tenant_id="default", channel="line", webhook_event_id="evt-dup", conversation_key="U1")
        self.assertTrue(first)
        self.assertFalse(second)

    def test_different_event_id_same_user_is_independent(self):
        sb = _FakeSupabaseWithUniqueConstraint()
        svc = WebhookEventDedupService(sb)
        first = svc.claim_event(tenant_id="default", channel="line", webhook_event_id="evt-A", conversation_key="U1")
        second = svc.claim_event(tenant_id="default", channel="line", webhook_event_id="evt-B", conversation_key="U1")
        self.assertTrue(first)
        self.assertTrue(second)

    def test_same_event_id_different_channel_is_independent(self):
        """Defensive scoping — matches every other multi-tenant table in
        this codebase (tenant_id + channel), even though a real LINE
        webhookEventId (ULID) is already globally unique on its own."""
        sb = _FakeSupabaseWithUniqueConstraint()
        svc = WebhookEventDedupService(sb)
        first = svc.claim_event(tenant_id="default", channel="line", webhook_event_id="evt-1", conversation_key="U1")
        second = svc.claim_event(tenant_id="default", channel="other_channel", webhook_event_id="evt-1", conversation_key="U1")
        self.assertTrue(first)
        self.assertTrue(second)


class TestClaimEventFailsOpenOnInfraError(unittest.TestCase):
    """A dedup table being unreachable must never itself block a real
    customer message from ever being answered — matches every other
    best-effort persistence call in this codebase (session history,
    profile updates) which degrade gracefully rather than raise."""

    def test_db_unreachable_still_returns_true(self):
        svc = WebhookEventDedupService(_RaisingSupabase())
        claimed = svc.claim_event(tenant_id="default", channel="line", webhook_event_id="evt-1", conversation_key="U1")
        self.assertTrue(claimed)

    def test_missing_webhook_event_id_returns_true_without_touching_db(self):
        svc = WebhookEventDedupService(_RaisingSupabase())  # would raise if ever called
        claimed = svc.claim_event(tenant_id="default", channel="line", webhook_event_id=None, conversation_key="U1")
        self.assertTrue(claimed)


class TestFactorySingleton(unittest.TestCase):
    def test_passing_sb_returns_a_fresh_instance_not_cached(self):
        sb = _FakeSupabaseWithUniqueConstraint()
        svc1 = get_webhook_event_dedup_service(sb)
        svc2 = get_webhook_event_dedup_service(sb)
        self.assertIsNot(svc1, svc2)
        self.assertIsInstance(svc1, WebhookEventDedupService)


if __name__ == "__main__":
    unittest.main()
