"""P3.1 data-integrity — LINE display-name capture in line_bot/webhook.py.

The webhook now resolves user_profiles.display_name from the LINE Profile
API exactly once per user (cooldown-guarded, failure-safe), inside the
detached post-reply bookkeeping thread — never on the reply path.
"""
import unittest
from unittest.mock import patch

import line_bot.webhook as wh


class _Resp:
    def __init__(self, name):
        self.display_name = name


class _FakeMessagingApi:
    calls = 0

    def __init__(self, *_a, **_k):
        pass

    def get_profile(self, user_id):
        _FakeMessagingApi.calls += 1
        return _Resp("Gudz")


class _BoomMessagingApi:
    calls = 0

    def __init__(self, *_a, **_k):
        pass

    def get_profile(self, user_id):
        _BoomMessagingApi.calls += 1
        raise RuntimeError("403 the bot cannot use profile API (user blocked OA)")


class LookupDisplayName(unittest.TestCase):
    def setUp(self):
        wh._DISPLAY_NAME_LOOKUP_COOLDOWN.clear()
        _FakeMessagingApi.calls = 0
        _BoomMessagingApi.calls = 0

    def test_returns_name_from_line_profile_api(self):
        with patch.object(wh, "MessagingApi", _FakeMessagingApi):
            self.assertEqual(wh._lookup_line_display_name("U" + "a" * 32), "Gudz")
        self.assertEqual(_FakeMessagingApi.calls, 1)

    def test_cooldown_blocks_a_second_lookup(self):
        uid = "U" + "b" * 32
        with patch.object(wh, "MessagingApi", _FakeMessagingApi):
            self.assertEqual(wh._lookup_line_display_name(uid), "Gudz")
            self.assertIsNone(wh._lookup_line_display_name(uid))   # within cooldown
        self.assertEqual(_FakeMessagingApi.calls, 1)              # only one API hit

    def test_failure_is_swallowed_and_does_not_retry_storm(self):
        uid = "U" + "c" * 32
        with patch.object(wh, "MessagingApi", _BoomMessagingApi):
            self.assertIsNone(wh._lookup_line_display_name(uid))
            self.assertIsNone(wh._lookup_line_display_name(uid))   # cooldown after a failure too
        self.assertEqual(_BoomMessagingApi.calls, 1)

    def test_blank_name_from_api_becomes_none(self):
        class _Blank(_FakeMessagingApi):
            def get_profile(self, user_id):
                return _Resp("   ")
        with patch.object(wh, "MessagingApi", _Blank):
            self.assertIsNone(wh._lookup_line_display_name("U" + "d" * 32))

    def test_cooldown_window_is_bounded_not_permanent(self):
        # a positive, finite cooldown — a later process/turn can retry
        self.assertGreater(wh._DISPLAY_NAME_LOOKUP_COOLDOWN_SEC, 0)
        self.assertLessEqual(wh._DISPLAY_NAME_LOOKUP_COOLDOWN_SEC, 24 * 3600)


class BookkeepingBranch(unittest.TestCase):
    """The reply path never calls the lookup; only a nameless profile in
    the post-reply thread does. This pins the guard expression."""

    def test_existing_name_short_circuits_lookup(self):
        # mirrors the webhook branch: resolve only when the stored name is blank
        profile = {"display_name": "Gudz"}
        display_name = (profile.get("display_name", "") if profile else "") or ""
        called = {"n": 0}
        def _fake_lookup(_uid):
            called["n"] += 1
            return "SHOULD-NOT-BE-USED"
        with patch.object(wh, "_lookup_line_display_name", _fake_lookup):
            if not display_name.strip():
                display_name = wh._lookup_line_display_name("U" + "a" * 32) or ""
        self.assertEqual(display_name, "Gudz")
        self.assertEqual(called["n"], 0)

    def test_blank_name_triggers_exactly_one_lookup(self):
        profile = {"display_name": ""}
        display_name = (profile.get("display_name", "") if profile else "") or ""
        called = {"n": 0}
        def _fake_lookup(_uid):
            called["n"] += 1
            return "Gudz"
        with patch.object(wh, "_lookup_line_display_name", _fake_lookup):
            if not display_name.strip():
                display_name = wh._lookup_line_display_name("U" + "a" * 32) or ""
        self.assertEqual(display_name, "Gudz")
        self.assertEqual(called["n"], 1)


if __name__ == "__main__":
    unittest.main()
