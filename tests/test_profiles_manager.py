"""Tests for profiles/manager.py's Customer Intelligence V1 (2026-08-15)
additions: identifier memory (cust_code/last_order_code/last_shipment_code/
last_tracking) and primary_intent persisted from update_profile_from_turn(),
per Phase 7's rules (never erase a known identifier on a vague later
message, never invent one that was never in the message)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from profiles.manager import update_profile_from_turn


class _FakeSingleResult:
    def __init__(self, data):
        self.data = data


class _FakeUpdateQuery:
    def __init__(self, table):
        self._table = table
        self._payload = None

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def execute(self):
        self._table.profile.update(self._payload)
        return _FakeSingleResult([dict(self._table.profile)])


class _FakeSelectQuery:
    def __init__(self, table):
        self._table = table

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        return self

    def single(self):
        self._single = True
        return self

    def execute(self):
        if not self._table.profile:
            raise Exception("no row")
        return _FakeSingleResult(dict(self._table.profile))


class _FakeProfileTable:
    def __init__(self, initial):
        self.profile = dict(initial)


class _FakeSupabase:
    def __init__(self, initial_profile):
        self._table = _FakeProfileTable(initial_profile)

    def table(self, _name):
        return _RouterQuery(self._table)


class _RouterQuery:
    """profiles.manager calls .select(...) or .update(...) as the FIRST
    chained call -- route to the right fake based on which one is used."""
    def __init__(self, table):
        self._table = table

    def select(self, *a, **k):
        return _FakeSelectQuery(self._table).select(*a, **k)

    def update(self, payload):
        return _FakeUpdateQuery(self._table).update(payload)


def _decide_result(**overrides):
    base = {"reply": {"text": "ok ค่ะ"}, "error": None, "alert": None}
    base.update(overrides)
    return base


class TestIdentifierPersistence(unittest.TestCase):
    def _run(self, profile, collected_parameters, actionable_intent=None):
        fake_sb = _FakeSupabase(profile)
        with patch("profiles.manager.supabase", fake_sb):
            conversation_fields = {
                "collected_parameters": collected_parameters,
                "actionable_intent": actionable_intent, "broad_intent": None,
                "routing_type": "API", "erp_request": {}, "escalated": False,
            }
            update_profile_from_turn("U1", decide_result=_decide_result(),
                                      conversation_fields=conversation_fields, is_new_conversation=True)
            return fake_sb._table.profile

    def test_f_cust_code_persisted_when_present(self):
        updated = self._run({"line_user_id": "U1"}, {"CustCode": "SP1014"})
        self.assertEqual(updated["cust_code"], "SP1014")

    def test_order_shipment_tracking_codes_persisted(self):
        updated = self._run({"line_user_id": "U1"}, {
            "OrderCode": "POS100820260809001", "ShipmentCode": "FT318220260726001", "Tracking": "testlineOnNut007",
        })
        self.assertEqual(updated["last_order_code"], "POS100820260809001")
        self.assertEqual(updated["last_shipment_code"], "FT318220260726001")
        self.assertEqual(updated["last_tracking"], "testlineOnNut007")

    def test_i_no_identifier_in_message_never_invents_one(self):
        updated = self._run({"line_user_id": "U1"}, {})
        self.assertNotIn("cust_code", updated)
        self.assertNotIn("last_order_code", updated)

    def test_vague_later_message_never_erases_known_cust_code(self):
        # Turn 1 already stored cust_code=SP1014 on the profile row.
        existing_profile = {"line_user_id": "U1", "cust_code": "SP1014"}
        updated = self._run(existing_profile, {})  # Turn 2: no CustCode in this message
        self.assertEqual(updated["cust_code"], "SP1014")

    def test_primary_intent_updates_to_latest(self):
        updated = self._run({"line_user_id": "U1"}, {}, actionable_intent="track_shipment")
        self.assertEqual(updated["primary_intent"], "track_shipment")

    def test_unknown_intent_never_overwrites(self):
        updated = self._run({"line_user_id": "U1"}, {}, actionable_intent="unknown")
        self.assertNotIn("primary_intent", updated)

    def test_j_no_secret_code_ever_persisted(self):
        # collected_parameters can only ever contain customer_message-
        # sourced values (services/decision_engine.py never puts a
        # credential_store parameter like SecretCode in there) -- this
        # asserts the profile-write path doesn't introduce a new field
        # for it even if one somehow appeared.
        updated = self._run({"line_user_id": "U1"}, {"CustCode": "SP1014", "SecretCode": "should-never-appear"})
        self.assertNotIn("secret_code", updated)
        self.assertNotIn("SecretCode", str(updated.get("cust_code", "")))


if __name__ == "__main__":
    unittest.main()
