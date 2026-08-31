"""Shared, bounded-timeout Supabase client factory + stale-connection recovery.

Historically every service/rag module ran its own bare
``create_client(SUPABASE_URL, SUPABASE_KEY)`` with no HTTP timeout, so
supabase-py's PostgREST client used its 120s default — and on a
stale/half-open long-lived HTTP/2 keep-alive connection that read blocked
for ~200s, hanging a whole LINE worker for minutes (production incident
2026-08-31; stack: ``business_action_registry.get`` -> postgrest
``.execute()`` -> httpx/httpcore http2 -> ``ssl.recv``).

This module centralises ONE client with explicit, short, production-safe
transport timeouts so a dead connection fails in seconds, plus
``reset_supabase()`` so the next call rebuilds the connection pool
instead of reusing the dead one. Nothing about queries, schema, auth, or
RLS changes — only the transport timeout / recovery path.
"""
import httpx

from config import SUPABASE_URL, SUPABASE_KEY

# Normal Supabase reads finish well under 1s; a pgvector RPC a few
# seconds. read=15 is generous headroom for a healthy request while
# capping a dead connection at 15s instead of 120–200s. connect/pool are
# short — a reachable Supabase accepts immediately.
CLIENT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)

_client = None


def _build():
    from supabase import create_client
    # supabase 2.31's create_client expects SyncClientOptions (the base
    # ClientOptions lacks .storage/.httpx_client and blows up inside
    # create_client). Only the transport timeouts are overridden here.
    from supabase.lib.client_options import SyncClientOptions
    return create_client(
        SUPABASE_URL,
        SUPABASE_KEY,
        options=SyncClientOptions(
            postgrest_client_timeout=CLIENT_TIMEOUT,
            storage_client_timeout=20,
            function_client_timeout=10,
        ),
    )


def get_supabase():
    """Process-wide singleton Supabase client with bounded HTTP timeouts.
    Every runtime caller that used to do its own ``create_client`` should
    go through this so they all share the same bounded transport."""
    global _client
    if _client is None:
        _client = _build()
    return _client


def reset_supabase():
    """Drop the cached client so the next ``get_supabase()`` rebuilds it
    (fresh connection pool). Call this after a transport/timeout error so
    a stale keep-alive connection is never reused. Best-effort close of
    the old client's underlying httpx sessions; never raises."""
    global _client
    old, _client = _client, None
    if old is None:
        return
    for attr in ("postgrest", "auth", "storage", "functions"):
        sub = getattr(old, attr, None)
        for sess_attr in ("session", "_session"):
            sess = getattr(sub, sess_attr, None)
            close = getattr(sess, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
