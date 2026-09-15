# -*- coding: utf-8 -*-
"""PRODUCTION SMOKE A–J for the LangGraph runtime path (owner matrix).

    python -m tests.smoke_langgraph_production            # offline tier (local)
    SHIPIFY_LIVE_TIER=1 python -m tests.smoke_langgraph_production   # on the deployed container

Runs the owner's smoke matrix through `authoritative_run` with
LANGGRAPH_MODE=production semantics (graph primary, DecisionEngine as the
technical/safety fallback), carrying the real reply text between the
journey turns A→B→C→D→E, and PROVES the libraries are on the executed
path: LangGraph node_path, PyThaiNLP/RapidFuzz normalisation methods,
Langfuse trace hook, engine fallback callable. Sends NO LINE message.
Exit code 0 only when every check passes.
"""
from __future__ import annotations

import json
import os
import sys

try:
    import tests  # noqa: F401  (pins the offline tier unless SHIPIFY_LIVE_TIER=1)
    _live = tests.is_live_tier()
except ImportError:            # inside the production image tests/ is not shipped:
    _live = True               # the real keys are the runtime — that IS the live tier
import config
from services.agent.runner import authoritative_run, validate_decision
from services.agent.state import AgentDecision
from services.agent.adapters import existing_engine as adapter
from services.observability import langfuse_client as lf

config.LANGGRAPH_MODE = "production"
if not _live:
    # offline tier: every OpenAI call is a 401 and the embedding client still
    # sleeps 1+2+4+8 s between retries — enough to trip the 25 s authoritative
    # guard on a RAG turn. That wait is network etiquette, not behaviour.
    try:
        import time as _time
        import services.embedding_service as _es

        class _NoSleepTime:
            sleep = staticmethod(lambda _s: None)

            def __getattr__(self, name):
                return getattr(_time, name)
        _es.time = _NoSleepTime()
    except Exception:
        pass
    # the offline tier's 401 storms (LLM resolver + retrieval) cost ~30 s a
    # turn; the production guard is measured on real keys, not on these.
    config.LANGGRAPH_TIMEOUT_SECONDS = 180.0
CTX = {"channel": "line", "tenant_id": "default", "sample_source": "REAL_LINE",
       "customer_context": {}, "developer_mode": True, "external_user_id": "U_smoke_local"}


def _slot(d, name):
    v = (d.known_slots or {}).get(name)
    return (v.get("value"), v.get("unit")) if isinstance(v, dict) else (v, None)


def turn(text, history):
    out = authoritative_run(text, history=history, context=dict(CTX),
                            engine_fallback=lambda: adapter.execute(text, history, dict(CTX)))
    d = out.decision or AgentDecision()
    return out, d


def main() -> int:
    results = []
    hist = []

    def check(name, text, cond_fn, history=None, carry=False):
        nonlocal hist
        out, d = turn(text, history if history is not None else hist)
        ok, detail = cond_fn(out, d)
        results.append({"case": name, "text": text, "used": out.used, "normalized": d.normalized_message,
                        "intent": d.primary_intent, "action": d.planned_action,
                        "slots": {k: _slot(d, k) for k in ("product", "quantity", "shipping_method")},
                        "auth": d.auth_required, "ok": ok, "detail": detail,
                        "reply": (d.final_response or "")[:100]})
        if carry:
            hist = hist + [{"role": "user", "content": text},
                           {"role": "assistant", "content": d.final_response}]
        return out, d

    check("A", "20 คู่อยากสั่งของจากจีน",
          lambda o, d: (_slot(d, "quantity") == (20, "คู่") and d.planned_action == "ASK_PRODUCT"
                        and _slot(d, "product")[0] is None and o.used == "langgraph",
                        f"qty={_slot(d, 'quantity')} action={d.planned_action}"), carry=True)
    check("B", "รองเท้าครับ",
          lambda o, d: (_slot(d, "product")[0] == "รองเท้า" and _slot(d, "quantity") == (20, "คู่")
                        and d.planned_action not in ("ASK_QUANTITY", "ASK_PRODUCT"),
                        f"product={_slot(d, 'product')} qty={_slot(d, 'quantity')} action={d.planned_action}"), carry=True)
    check("C", "ส่งเรือครับ",
          lambda o, d: (_slot(d, "shipping_method")[0] == "sea" and _slot(d, "product")[0] == "รองเท้า"
                        and _slot(d, "quantity") == (20, "คู่"),
                        f"method={_slot(d, 'shipping_method')} product={_slot(d, 'product')} qty={_slot(d, 'quantity')}"), carry=True)
    check("D", "เอ้ย 10 คู่",
          lambda o, d: (_slot(d, "quantity") == (10, "คู่") and _slot(d, "product")[0] == "รองเท้า"
                        and _slot(d, "shipping_method")[0] == "sea",
                        f"qty={_slot(d, 'quantity')} product={_slot(d, 'product')} method={_slot(d, 'shipping_method')}"), carry=True)
    check("E", "กระต่ายนำเข้าได้หรอ",
          lambda o, d: (d.primary_intent == "PRODUCT_POLICY" and _slot(d, "product")[0] == "กระต่าย"
                        and d.planned_action not in ("ASK_PRODUCT", "ASK_QUANTITY", "ASK_SHIPPING_METHOD")
                        and not d.auth_required,
                        f"intent={d.primary_intent} product={_slot(d, 'product')} action={d.planned_action}"), carry=True)
    ask_hist = [{"role": "user", "content": "20 คู่อยากสั่งของจากจีน"},
                {"role": "assistant", "content": results[0]["reply"] or ""}]
    check("F", "เป้นรองเท้าคับ",
          lambda o, d: (d.normalized_message == "เป็นรองเท้าครับ" and _slot(d, "product")[0] == "รองเท้า",
                        f"norm={d.normalized_message!r} product={_slot(d, 'product')}"), history=ask_hist)
    check("G", "20คุ่อยากสั่งขงจากจีน",
          lambda o, d: (_slot(d, "quantity") == (20, "คู่") and d.primary_intent == "IMPORT_INTEREST"
                        and "rapidfuzz_vocab" in d.normalization_method,
                        f"norm={d.normalized_message!r} qty={_slot(d, 'quantity')} intent={d.primary_intent}"), history=[])
    check("H", "ส่งของครบแล้ว",
          lambda o, d: (d.normalized_message == "ส่งของครบแล้ว",
                        f"norm={d.normalized_message!r}"), history=[])
    check("I", "3 ลัง",
          lambda o, d: ("ลัง" in (d.normalized_message or "") and "ลิงก์" not in (d.normalized_message or ""),
                        f"norm={d.normalized_message!r}"), history=[])
    check("J", "ขอเช็คออเดอของผมหนอย",
          lambda o, d: (d.auth_required and d.auth_state == "REQUIRED_MISSING"
                        and d.planned_action == "ASK_IDENTIFIER"
                        and not [e for e in d.errors if e.startswith(("PRIVATE_LEAK", "AUTH"))]
                        and not adapter.PRIVATE_LEAK_RE.search(d.final_response or ""),
                        f"auth={d.auth_required}/{d.auth_state} action={d.planned_action}"), history=[])

    # ── library integration proof ──
    from services.language import thai_normalizer as tn
    o, d = turn("20คุ่อยากสั่งขงจากจีน", [])
    o2, d2 = turn("ใบกำกับภาษีี", [])
    node_path = d.node_path
    proof = {
        "langgraph_executed": "normalize_language" in node_path and "execute_tool" in node_path and o.used == "langgraph",
        "pythainlp_executed": tn.PYTHAINLP_AVAILABLE and "pythainlp_normalize" in d2.normalization_method
                              and d2.normalized_message == "ใบกำกับภาษี",
        "rapidfuzz_executed": tn.RAPIDFUZZ_AVAILABLE and "rapidfuzz_vocab" in d.normalization_method
                              and d.normalized_message == "20คู่อยากสั่งของจากจีน",
        "langfuse_hook": None,
        "engine_fallback_callable": None,
        "node_path": node_path,
    }
    # Langfuse: the trace context manager is entered on every run (no-op when unconfigured)
    entered = {"n": 0}
    orig = lf.trace

    class _Ctx:
        def __init__(self, *a, **k): entered["n"] += 1
        def __enter__(self): return lf._NoopSpan()
        def __exit__(self, *a): return False
    lf.trace = _Ctx
    try:
        turn("สวัสดีครับ", [])
    finally:
        lf.trace = orig
    proof["langfuse_hook"] = entered["n"] >= 1 and (lf.is_enabled() or not config.LANGFUSE_ENABLED)
    # engine fallback: a safety-flagged decision must hand the turn to the engine
    flagged = AgentDecision(final_response="x", errors=["AUTH_VIOLATION: test"], engine_result={"reply": {"text": "x"}})
    proof["engine_fallback_callable"] = validate_decision(flagged)[0] == "safety"

    print(json.dumps({"config": {"LANGGRAPH_MODE": config.LANGGRAPH_MODE,
                                 "LANGFUSE_ENABLED": bool(config.LANGFUSE_ENABLED),
                                 "live_tier": _live},
                      "smoke": results, "proof": proof}, ensure_ascii=False, indent=1, default=str))
    passed = sum(1 for r in results if r["ok"])
    print(f"SMOKE {passed}/{len(results)}  PROOF "
          f"{'PASS' if all(v for k, v in proof.items() if k != 'node_path') else 'FAIL'}")
    return 0 if passed == len(results) and all(v for k, v in proof.items() if k != "node_path") else 1


if __name__ == "__main__":
    sys.exit(main())
