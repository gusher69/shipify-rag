"""Focused tests for the bounded-timeout Supabase client + stale-connection
recovery (services/supabase_client.py, services/business_action_registry.py).

The production incident (2026-08-31): a stale/half-open Supabase HTTP/2
keep-alive connection with no effective read timeout blocked a LINE
worker in ssl.recv() for ~200s inside
BusinessActionRegistry.get -> postgrest .execute().
"""
import sys
import threading
import time
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.supabase_client as sc
from services.business_action_registry import BusinessActionRegistry, _EmptyResult


class _Query:
    """Stand-in for a PostgREST query builder — only .execute() is used."""

    def __init__(self, behaviour):
        self._behaviour = behaviour  # callable() -> result, or raises

    def execute(self):
        return self._behaviour()


class _FakeResult:
    def __init__(self, data):
        self.data = data


class TestBoundedTimeoutConfig(unittest.TestCase):
    def test_client_timeout_is_short_and_explicit(self):
        # A dead connection must fail in seconds, not 120–200.
        self.assertLessEqual(sc.CLIENT_TIMEOUT.read, 30.0)
        self.assertLessEqual(sc.CLIENT_TIMEOUT.connect, 10.0)
        self.assertIsNotNone(sc.CLIENT_TIMEOUT.write)
        self.assertIsNotNone(sc.CLIENT_TIMEOUT.pool)

    def test_reset_clears_singleton(self):
        sc._client = object()
        sc.reset_supabase()
        self.assertIsNone(sc._client)


class _RegistryTestBase(unittest.TestCase):
    def setUp(self):
        self.reset_calls = 0
        self._orig_reset = sc.reset_supabase
        self._orig_get = sc.get_supabase

        def fake_reset():
            self.reset_calls += 1
            sc._client = None

        sc.reset_supabase = fake_reset
        sc.get_supabase = lambda: self.second_sb

    def tearDown(self):
        sc.reset_supabase = self._orig_reset
        sc.get_supabase = self._orig_get


class TestResilientRead(_RegistryTestBase):
    def test_A_normal_response_unchanged(self):
        calls = []

        class Chain:
            def table(self, *a, **k): return self
            select = eq = is_ = order = table

            def execute(self):
                calls.append(1)
                return _FakeResult([{"id": "abc", "action_key": "k"}])

        reg = BusinessActionRegistry(Chain())
        row = reg.get("abc")
        self.assertEqual(row, {"id": "abc", "action_key": "k"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.reset_calls, 0)  # no recovery on the happy path

    def test_B_read_timeout_fails_fast_and_degrades_to_empty(self):
        attempts = []

        def boom():
            attempts.append(time.monotonic())
            raise httpx.ReadTimeout("simulated stale-connection read timeout")

        bad_sb = type("SB", (), {})()
        self.second_sb = bad_sb  # retry client also dead

        reg = BusinessActionRegistry(bad_sb)
        reg._resilient_read = reg._resilient_read  # bind
        t0 = time.monotonic()
        result = reg._resilient_read(lambda sb: _Query(boom))
        elapsed = time.monotonic() - t0

        self.assertIsInstance(result, _EmptyResult)
        self.assertEqual(result.data, [])
        self.assertEqual(len(attempts), 2)          # 1 original + 1 retry, no storm
        self.assertEqual(self.reset_calls, 1)       # client was recycled once
        self.assertLess(elapsed, 2.0)               # nothing like a ~200s wait

    def test_C_stale_first_attempt_then_success(self):
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] == 1:
                raise httpx.ConnectError("stale pooled connection")
            return _FakeResult([{"id": "x"}])

        self.second_sb = object()  # fresh client used for the retry
        reg = BusinessActionRegistry(object())
        result = reg._resilient_read(lambda sb: _Query(flaky))

        self.assertEqual(result.data, [{"id": "x"}])
        self.assertEqual(state["n"], 2)
        self.assertEqual(self.reset_calls, 1)

    def test_D_both_attempts_fail_exits_quickly_no_infinite_retry(self):
        n = {"c": 0}

        def always_bad():
            n["c"] += 1
            raise httpx.TimeoutException("down")

        self.second_sb = object()
        reg = BusinessActionRegistry(object())
        t0 = time.monotonic()
        result = reg._resilient_read(lambda sb: _Query(always_bad))
        self.assertIsInstance(result, _EmptyResult)
        self.assertEqual(n["c"], 2)                       # exactly 2, never more
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_D2_get_returns_None_on_total_failure(self):
        self.second_sb = object()
        reg = BusinessActionRegistry(object())
        reg._resilient_read = lambda build: _EmptyResult()
        self.assertIsNone(reg.get("whatever"))
        self.assertEqual(reg.list(), [])

    def test_E_one_workers_timeout_does_not_block_another(self):
        """A timed-out Supabase call in one 'worker' must not create
        global application blocking — independent registries / calls
        each resolve on their own bounded path."""
        self.second_sb = object()
        done = {}

        def slow_worker():
            reg = BusinessActionRegistry(object())

            def boom():
                raise httpx.ReadTimeout("stale")

            t0 = time.monotonic()
            reg._resilient_read(lambda sb: _Query(boom))
            done["slow"] = time.monotonic() - t0

        def fast_worker():
            reg = BusinessActionRegistry(object())
            t0 = time.monotonic()
            reg._resilient_read(lambda sb: _Query(lambda: _FakeResult([{"ok": 1}])))
            done["fast"] = time.monotonic() - t0

        ts = [threading.Thread(target=slow_worker), threading.Thread(target=fast_worker)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=5)
        self.assertIn("fast", done)
        self.assertIn("slow", done)
        self.assertLess(done["fast"], 2.0)
        self.assertLess(done["slow"], 2.0)


if __name__ == "__main__":
    unittest.main()
