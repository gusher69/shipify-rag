"""Focused tests for the bounded OpenAI chat client + one fresh-client
retry (services/llm_service.py).

Production incident 2026-08-31: the chat client had NO timeout (effective
600s read), so a half-open keep-alive connection to api.openai.com
stalled one LINE turn ~83s. Fix: read=20s, 1 application-level retry with
a fresh client, then fail safe.
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from openai import APITimeoutError, APIConnectionError

import services.llm_service as llm_mod
from services.llm_service import OpenAIProvider, _CHAT_TIMEOUT_KW


class _Usage:
    prompt_tokens = 3
    completion_tokens = 4


class _Msg:
    content = "ok"


class _Choice:
    message = _Msg()


class _Resp:
    choices = [_Choice()]
    usage = _Usage()

    def model_dump(self):
        return {}


def _timeout_err():
    return APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))


def _conn_err():
    return APIConnectionError(message="boom",
                              request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))


class _FakeCompletions:
    def __init__(self, behaviours):
        self._behaviours = list(behaviours)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        b = self._behaviours.pop(0) if self._behaviours else _Resp()
        if isinstance(b, Exception):
            raise b
        return b


class _FakeClient:
    def __init__(self, behaviours):
        self.chat = type("C", (), {"completions": _FakeCompletions(behaviours)})()


class TestChatClientTimeoutConfig(unittest.TestCase):
    def test_read_timeout_is_bounded_and_short(self):
        self.assertLessEqual(_CHAT_TIMEOUT_KW["read"], 20.0)
        self.assertLessEqual(_CHAT_TIMEOUT_KW["connect"], 5.0)
        self.assertIn("write", _CHAT_TIMEOUT_KW)
        self.assertIn("pool", _CHAT_TIMEOUT_KW)

    def test_real_client_build_applies_timeout_and_no_sdk_retry(self):
        c = llm_mod._build_openai_client("sk-test")
        self.assertEqual(c.timeout.read, 20.0)
        self.assertEqual(c.timeout.connect, 5.0)
        self.assertEqual(c.max_retries, 0)  # SDK retry disabled; we do exactly one app-level retry


class TestGenerateRetry(unittest.TestCase):
    def setUp(self):
        self._orig_build = llm_mod._build_openai_client
        self.built = []

    def tearDown(self):
        llm_mod._build_openai_client = self._orig_build
        llm_mod._instance = None

    def _provider_with(self, first_behaviours, retry_behaviours=None):
        seq = [_FakeClient(first_behaviours)]
        if retry_behaviours is not None:
            seq.append(_FakeClient(retry_behaviours))

        def fake_build(_key):
            self.built.append(1)
            return seq[len(self.built) - 1] if len(self.built) <= len(seq) else _FakeClient([_Resp()])

        llm_mod._build_openai_client = fake_build
        return OpenAIProvider("sk-test")

    def test_A_healthy_response_unchanged(self):
        p = self._provider_with([_Resp()])
        r = p.generate([{"role": "user", "content": "hi"}], model="gpt-4o-mini")
        self.assertEqual(r.text, "ok")
        self.assertEqual(p._client.chat.completions.calls, 1)   # no retry
        self.assertEqual(len(self.built), 1)                     # no fresh client built

    def test_B_first_transport_error_then_retry_succeeds_on_fresh_client(self):
        p = self._provider_with([_timeout_err()], retry_behaviours=[_Resp()])
        t0 = time.monotonic()
        r = p.generate([{"role": "user", "content": "hi"}], model="gpt-4o-mini")
        self.assertEqual(r.text, "ok")
        self.assertEqual(len(self.built), 2)                     # exactly one fresh client for the retry
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_C_both_attempts_fail_exits_bounded_and_resets_singleton(self):
        llm_mod._instance = object()
        p = self._provider_with([_conn_err()], retry_behaviours=[_timeout_err()])
        t0 = time.monotonic()
        with self.assertRaises((APITimeoutError, APIConnectionError)):
            p.generate([{"role": "user", "content": "hi"}], model="gpt-4o-mini")
        self.assertLess(time.monotonic() - t0, 2.0)             # no long wait, no infinite loop
        self.assertIsNone(llm_mod._instance)                     # next turn starts clean

    def test_D_max_one_retry_total_two_attempts(self):
        p = self._provider_with([_timeout_err()], retry_behaviours=[_timeout_err()])
        with self.assertRaises((APITimeoutError, APIConnectionError)):
            p.generate([{"role": "user", "content": "hi"}], model="gpt-4o-mini")
        # 1 original client + exactly 1 fresh client for the single retry = 2 total attempts, never more
        self.assertEqual(len(self.built), 2)

    def test_D2_non_transport_error_is_not_retried(self):
        p = self._provider_with([ValueError("not a transport error")])
        with self.assertRaises(ValueError):
            p.generate([{"role": "user", "content": "hi"}], model="gpt-4o-mini")
        self.assertEqual(len(self.built), 1)  # no fresh client, no retry


if __name__ == "__main__":
    unittest.main()
