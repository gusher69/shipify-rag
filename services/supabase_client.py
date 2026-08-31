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

# Production evidence (2026-08-31): a fresh HTTP/1.1 request to Supabase
# from the production host is ~0.2s, but supabase-py's default PostgREST
# client uses ONE long-lived HTTP/2 connection — and when that single
# multiplexed connection goes half-open, every request on it stalls (and
# httpcore's HTTP/2 read-timeout handling for a dead stream is
# unreliable), which is what produced the ~105–200s LINE worker hangs.
# HTTP/1.1 with a short keep-alive expiry sidesteps that entirely: no
# multiplexing stall, and a connection can't sit idle long enough to be
# silently dropped by a NAT/firewall before we reuse it.
#
# Lowered 15 -> 5 (2026-08-31): real LINE turns still occasionally blocked
# ~30-70s in ssl.recv() on a pooled HTTP/1.1 connection the peer had
# half-closed. Manual customer messages are always >5s apart, so a pooled
# connection is effectively never reused across turns anyway; within a
# single turn (calls ~0.15s apart) it still stays warm and is reused, so
# there is no per-request TCP/TLS cost on the healthy path.
_KEEPALIVE_EXPIRY = 5.0

_client = None
_httpx_client = None


def _build():
    global _httpx_client
    from supabase import create_client
    # supabase 2.31's create_client expects SyncClientOptions (the base
    # ClientOptions lacks .storage/.httpx_client and blows up inside
    # create_client).
    from supabase.lib.client_options import SyncClientOptions, SyncHttpxClient
    _httpx_client = SyncHttpxClient(
        http2=False,
        timeout=CLIENT_TIMEOUT,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20,
                            keepalive_expiry=_KEEPALIVE_EXPIRY),
    )
    return create_client(
        SUPABASE_URL,
        SUPABASE_KEY,
        options=SyncClientOptions(
            postgrest_client_timeout=CLIENT_TIMEOUT,
            storage_client_timeout=20,
            function_client_timeout=10,
            httpx_client=_httpx_client,
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
    with a fresh connection pool. Call this after a transport/timeout
    error so a stale keep-alive connection is never reused. Best-effort
    close of the underlying httpx client; never raises."""
    global _client, _httpx_client
    old_hx, _httpx_client = _httpx_client, None
    _client = None
    if old_hx is not None:
        try:
            old_hx.close()
        except Exception:
            pass
