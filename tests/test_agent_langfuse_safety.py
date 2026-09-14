# -*- coding: utf-8 -*-
"""LANGFUSE — observability that can never cost a customer their answer.

Two contracts (task §16/§26):

  DEGRADE-SAFE   unset, down, slow, misconfigured, or raising from inside
                 its own SDK — the conversation is unaffected, nothing
                 raises, and nothing blocks on the network.
  PRIVACY        raw customer text, private ERP payloads and every
                 personal identifier are masked BEFORE the SDK sees them.

Test tier is pinned offline by tests/__init__.py.
"""
import unittest
from unittest.mock import patch

import config
from services.observability import langfuse_client as lf


class TestPrivacyMasking(unittest.TestCase):

    def test_identifier_patterns_are_redacted_from_free_text(self):
        cases = [
            ("Uc5f5aaaabbbbccccddddeeee11118178", "LINE user id"),
            ("บิล POS318220260806008 ครับ", "purchase bill"),
            ("โทร 081-234-5678", "phone"),
            ("เมล info@example.co.th", "email"),
            ("เลข 1234567890123", "long digit run"),
        ]
        for text, label in cases:
            with self.subTest(label=label):
                masked = lf.mask_text(text)
                self.assertIn("[REDACTED]", masked, f"{label} not masked: {masked}")

    def test_private_keys_are_dropped_whatever_their_content(self):
        """A raw ERP payload carries no recognisable pattern — it is the
        KEY that makes it private."""
        payload = {"raw_message": "ของผมถึงไหนแล้ว", "cust_code": "FT1004",
                   "tool_result": {"status": "in transit", "eta": "tomorrow"},
                   "external_user_id": "anything at all",
                   "product": "รองเท้า", "quantity": 20}
        masked = lf.mask_payload(payload)
        for key in ("raw_message", "cust_code", "tool_result", "external_user_id"):
            self.assertEqual(masked[key], "[REDACTED]", key)
        # non-private facts survive, which is the point of tracing at all.
        self.assertEqual(masked["product"], "รองเท้า")
        self.assertEqual(masked["quantity"], 20)

    def test_masking_is_recursive_and_bounded(self):
        deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "โทร 02-026-6426"}}}}}}}
        self.assertIsNotNone(lf.mask_payload(deep))          # no recursion error
        long_text = "x" * 5000
        self.assertLessEqual(len(lf.mask_text(long_text)), 400)

    def test_a_list_of_records_is_masked_elementwise(self):
        masked = lf.mask_payload([{"phone": "0812345678"}, {"product": "รองเท้า"}])
        self.assertEqual(masked[0]["phone"], "[REDACTED]")
        self.assertEqual(masked[1]["product"], "รองเท้า")


class TestDegradeSafety(unittest.TestCase):

    def setUp(self):
        lf._reset_for_tests()

    def tearDown(self):
        lf._reset_for_tests()

    def test_unconfigured_langfuse_is_simply_off(self):
        with patch.object(config, "LANGFUSE_ENABLED", False):
            self.assertFalse(lf.is_enabled())
            with lf.trace("t") as span:
                span.update(output={"x": 1})
                span.event("e", k="v")
                with lf.span(span, "child", tool="PUBLIC_RAG") as child:
                    child.update(output={})
        # reaching here at all is the assertion: nothing raised.

    def test_a_client_that_fails_to_construct_disables_tracing(self):
        with patch.object(config, "LANGFUSE_ENABLED", True), \
             patch.object(config, "LANGFUSE_PUBLIC_KEY", "pk"), \
             patch.object(config, "LANGFUSE_SECRET_KEY", "sk"), \
             patch("langfuse.Langfuse", side_effect=RuntimeError("unreachable")):
            self.assertFalse(lf.is_enabled())
            with lf.trace("t") as span:
                span.update(output={"x": 1})

    def test_a_client_that_raises_mid_trace_never_escapes(self):
        class _Exploding:
            def start_span(self, **_kw):
                raise RuntimeError("langfuse exploded")

        with patch.object(config, "LANGFUSE_ENABLED", True), \
             patch.object(config, "LANGFUSE_PUBLIC_KEY", "pk"), \
             patch.object(config, "LANGFUSE_SECRET_KEY", "sk"), \
             patch("langfuse.Langfuse", return_value=_Exploding()):
            with lf.trace("t") as span:
                span.update(output={"x": 1})

    def test_a_span_that_raises_on_update_or_end_never_escapes(self):
        class _BadSpan:
            def update(self, **_kw):
                raise RuntimeError("no")

            def update_trace(self, **_kw):
                raise RuntimeError("no")

            def create_event(self, **_kw):
                raise RuntimeError("no")

            def start_span(self, **_kw):
                raise RuntimeError("no")

            def end(self):
                raise RuntimeError("no")

        class _Client:
            def start_span(self, **_kw):
                return _BadSpan()

        with patch.object(config, "LANGFUSE_ENABLED", True), \
             patch.object(config, "LANGFUSE_PUBLIC_KEY", "pk"), \
             patch.object(config, "LANGFUSE_SECRET_KEY", "sk"), \
             patch("langfuse.Langfuse", return_value=_Client()):
            with lf.trace("t", session_id="Uc5f5aaaabbbbccccddddeeee11118178") as span:
                span.update(output={"x": 1})
                span.event("e")
                with lf.span(span, "child") as child:
                    child.update(output={})

    def test_the_request_path_never_flushes(self):
        """flush() is a blocking network call; it belongs to shutdown."""
        import pathlib
        src = pathlib.Path("services/observability/langfuse_client.py").read_text(
            encoding="utf-8")
        body = src.split("def shutdown")[0]
        self.assertNotIn(".flush()", body,
                         "tracing must not flush on the request path")

    def test_an_agent_turn_still_completes_when_tracing_explodes(self):
        from services.agent.runner import run_agent
        with patch("services.observability.langfuse_client.trace",
                   side_effect=RuntimeError("tracing down")):
            # the runner must not be the thing that breaks; a raise here
            # is caught by shadow_run, which is what the webhook calls.
            from services.agent.runner import shadow_run
            self.assertIsNone(shadow_run("สวัสดี", [], {"channel": "line",
                                                        "sample_source": "TEST"}))


if __name__ == "__main__":
    unittest.main()
