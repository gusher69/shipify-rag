# -*- coding: utf-8 -*-
"""Langfuse tracing for the LangGraph agent — OBSERVABILITY ONLY.

Two properties this module guarantees, both tested
(tests/test_agent_langfuse_safety.py):

1. DEGRADE-SAFE. Every public function swallows every exception and
   returns a no-op handle. If Langfuse is unset, unreachable, slow,
   misconfigured, or raises from inside its own SDK, the customer's
   conversation is unaffected: no exception escapes, and no call blocks
   on the network (the SDK batches and flushes on its own background
   thread; we never call flush() on the request path).

2. PRIVACY-PRESERVING. Raw customer text, private ERP payloads and every
   personal identifier are masked BEFORE they are handed to the SDK, by
   `mask_payload` below — not by a server-side setting we would have to
   trust. What leaves this process is the SHAPE of a turn (which intent,
   which entities by NAME, which tool, which route, how long, whether it
   was grounded), never the customer's identity or their records.
"""
from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional

import config

# ── PII masking ──────────────────────────────────────────────────────
# Applied to every string that reaches a trace. The rules are the same
# sanitisation contract the committed reports and tests already follow:
# LINE user id, customer code, phone, email, address, tracking/order
# identifiers, names and private ERP payloads never leave the process.
_MASK = "[REDACTED]"
_PATTERNS = (
    # LINE user id (U + 32 hex) — the webhook's own verified sender id.
    re.compile(r"\bU[0-9a-f]{32}\b", re.IGNORECASE),
    # customer / bill / order / shipment codes used across this ERP.
    re.compile(r"\b(?:PO|POS|PA|PE|FT|FE|SA|SP)[A-Za-z0-9_\-]*\d[A-Za-z0-9_\-]*\b"),
    # any long digit run: tracking numbers, account numbers, ids.
    re.compile(r"\b\d{8,}\b"),
    # Thai / international phone numbers.
    re.compile(r"(?<!\d)0\d{1,2}[- ]?\d{3}[- ]?\d{3,4}(?!\d)"),
    re.compile(r"\+66[- ]?\d[- ]?\d{3}[- ]?\d{3,4}"),
    # email.
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
)
# keys whose VALUE is private by nature, whatever it looks like.
_PRIVATE_KEYS = frozenset({
    "external_user_id", "user_id", "line_user_id", "cust_code", "custcode",
    "customer_code", "phone", "custphone", "email", "address", "shippingaddress",
    "tracking", "trackingno", "tracking_no", "shipmentcode", "ordercode",
    "identifier", "identifiers", "raw_message", "normalized_message",
    "message", "reply", "final_response", "tool_result", "erp_response",
    "rag_context", "content", "name", "recipient",
})
_MAX_STR = 400


def mask_text(value: str) -> str:
    """Redact every identifier pattern from a free-text string."""
    s = value or ""
    for rx in _PATTERNS:
        s = rx.sub(_MASK, s)
    return s[:_MAX_STR]


def mask_payload(value: Any, *, _depth: int = 0) -> Any:
    """Recursively mask a trace payload.

    A key in `_PRIVATE_KEYS` is dropped to a constant regardless of its
    content — that is what keeps a raw private ERP response or the
    customer's own message out of the trace even when it contains no
    recognisable identifier pattern. Everything else is pattern-masked.
    """
    if _depth > 6:
        return _MASK
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if key.strip().lower() in _PRIVATE_KEYS:
                out[key] = _MASK if v not in (None, "", [], {}) else None
            else:
                out[key] = mask_payload(v, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [mask_payload(v, _depth=_depth + 1) for v in list(value)[:50]]
    return mask_text(str(value))


# ── the client ───────────────────────────────────────────────────────
_lock = threading.Lock()
_client = None
_init_failed = False


def _get_client():
    """Lazily build ONE client. A failure is remembered so a broken or
    unreachable Langfuse is not retried on every single turn."""
    global _client, _init_failed
    if _client is not None or _init_failed:
        return _client
    with _lock:
        if _client is not None or _init_failed:
            return _client
        if not getattr(config, "LANGFUSE_ENABLED", False):
            _init_failed = True
            return None
        try:
            from langfuse import Langfuse
            _client = Langfuse(
                public_key=config.LANGFUSE_PUBLIC_KEY,
                secret_key=config.LANGFUSE_SECRET_KEY,
                host=config.LANGFUSE_HOST,
                timeout=int(config.LANGFUSE_TIMEOUT_SECONDS),
                # never mask on the server's terms — we mask first, this
                # is only a second line of defence.
                mask=lambda data: mask_payload(data),
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            print(f"[langfuse] disabled — client init failed: {exc!r}")
            _client, _init_failed = None, True
    return _client


def is_enabled() -> bool:
    return _get_client() is not None


class _NoopSpan:
    """The handle returned when tracing is off or broken. Every method is
    a no-op, so caller code never needs an `if tracing:` branch."""

    def update(self, **_kw) -> None:
        return None

    def event(self, *_a, **_kw) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class _Span:
    def __init__(self, span):
        self._span = span

    def update(self, **kw) -> None:
        try:
            self._span.update(**{k: mask_payload(v) for k, v in kw.items()})
        except Exception:
            pass

    def event(self, name: str, **kw) -> None:
        try:
            self._span.create_event(name=str(name)[:80],
                                    metadata=mask_payload(kw) or None)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        try:
            self._span.end()
        except Exception:
            pass
        return False


@contextmanager
def trace(name: str, *, session_id: Optional[str] = None,
          metadata: Optional[Dict[str, Any]] = None,
          input_payload: Optional[Any] = None):
    """Open a root trace for one conversation turn.

    Yields a span handle that is ALWAYS safe to call, whether or not
    Langfuse is available. Never raises, never blocks on the network.
    """
    client = _get_client()
    if client is None:
        yield _NoopSpan()
        return
    span = None
    try:
        span = client.start_span(name=str(name)[:80],
                                 input=mask_payload(input_payload),
                                 metadata=mask_payload(metadata) or None)
        if session_id:
            # a session REFERENCE, never the LINE user id itself.
            try:
                span.update_trace(session_id=mask_text(str(session_id)))
            except Exception:
                pass
    except Exception as exc:
        print(f"[langfuse] trace start failed, continuing untraced: {exc!r}")
        yield _NoopSpan()
        return
    handle = _Span(span)
    try:
        yield handle
    finally:
        try:
            span.end()
        except Exception:
            pass


@contextmanager
def span(parent, name: str, **metadata):
    """A child span under `parent` (which may itself be a no-op handle)."""
    inner = getattr(parent, "_span", None)
    if inner is None:
        yield _NoopSpan()
        return
    child = None
    try:
        child = inner.start_span(name=str(name)[:80],
                                 metadata=mask_payload(metadata) or None)
    except Exception:
        yield _NoopSpan()
        return
    try:
        yield _Span(child)
    finally:
        try:
            child.end()
        except Exception:
            pass


def shutdown() -> None:
    """Flush on process exit ONLY — never on the request path."""
    client = _get_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        pass


def _reset_for_tests() -> None:
    """Drop the memoised client so a test can change configuration."""
    global _client, _init_failed
    with _lock:
        _client, _init_failed = None, False
