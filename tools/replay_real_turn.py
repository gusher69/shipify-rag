#!/usr/bin/env python3
"""Developer-only replay harness — reproduce a REAL LINE routing failure
locally from a sanitized fixture of the state the Decision Engine saw,
BEFORE patching code.

    python tools/replay_real_turn.py tests/fixtures/real_line/<case>.json

It does NOT reimplement any routing logic: it seeds a BusinessActionRegistry
from the fixture's `actions`, builds the exact `context` dict the LINE
webhook passes to `DecisionEngine.decide()`, patches out the real ERP HTTP
call and the real RAG pipeline with deterministic fakes, runs the REAL
`decide()`, and prints its developer trace + a PASS/FAIL against the
fixture's `expected` block.

Never sends a real HTTP request; never touches the DB, LINE, or an LLM.
No production runtime file is imported for anything other than being
called as-is.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Reuse the existing in-memory registry fake + RAG-result fake the unit
# tests already use — no second implementation.
from tests.test_business_action_registry import _FakeSupabase                 # noqa: E402
from tests.test_decision_engine import _fake_playground_result               # noqa: E402
from services.business_action_registry import BusinessActionRegistry         # noqa: E402
from services.decision_engine import DecisionEngine                          # noqa: E402
import services.decision_engine as _de                                      # noqa: E402

# Fixture keys whose VALUE must never be stored / printed in the clear.
_SECRET_KEY_HINTS = ("secret", "token", "password", "passwd", "apikey",
                     "api_key", "access_token", "channel_secret", "authorization")
_MASK = "[MASKED]"


def _mask_secrets(obj):
    """Recursively replace any value under a secret-looking key with
    _MASK. Applied on load so a carelessly captured fixture still cannot
    leak a credential through this tool."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(h in str(k).lower() for h in _SECRET_KEY_HINTS) and isinstance(v, str):
                out[k] = _MASK
            else:
                out[k] = _mask_secrets(v)
        return out
    if isinstance(obj, list):
        return [_mask_secrets(x) for x in obj]
    return obj


def _seed_registry(actions: list) -> BusinessActionRegistry:
    reg = BusinessActionRegistry(_FakeSupabase())
    for a in actions or []:
        row = reg.create({
            "action_key": a["action_key"], "name": a["action_key"],
            "display_name": a.get("display_name") or a["action_key"],
            "action_type": a.get("action_type", "API"),
            "category": a.get("category"), "ai_description": a.get("ai_description", ""),
            "search_keywords": a.get("keywords") or [],
            "enabled": a.get("enabled", True), "priority": a.get("priority", 0),
        })
        if a.get("parameters"):
            reg.replace_parameters(row["id"], a["parameters"])
        if a.get("response_mapping"):
            reg.replace_response_mapping(row["id"], a["response_mapping"])
        if a.get("endpoint"):
            reg.upsert_execution(row["id"], {"endpoint": a["endpoint"],
                                             "http_method": a.get("http_method", "GET")})
    return reg


def _fmt(v):
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)


def replay(fixture: dict, *, verbose: bool = True) -> bool:
    def _p(*a):
        if not verbose:
            return
        try:
            print(*a)
        except UnicodeEncodeError:                       # non-utf-8 stdout (e.g. unittest on Windows)
            print(*(str(x).encode("ascii", "replace").decode() for x in a))

    fx = _mask_secrets(fixture)
    message = fx["message"]
    history = fx.get("history") or []
    reg = _seed_registry(fx.get("actions") or [])
    engine = DecisionEngine(reg._sb)
    engine.registry = reg

    context = {
        "channel": fx.get("channel", "line"),
        "developer_mode": True,
        "customer_context": dict(fx.get("customer_context") or {}),
        "tenant_id": fx.get("tenant_id", "default"),
        "external_user_id": fx.get("external_user_id", "U_replay"),
    }
    if fx.get("verified_cust_code"):
        context["customer_context"]["cust_code"] = fx["verified_cust_code"]
    if fx.get("pending_action_id_key"):
        # fixture names the action_key; resolve to the seeded id
        try:
            context["pending_action_id"] = reg.get_by_key(fx["pending_action_id_key"])["id"]
        except Exception:
            pass

    erp_mock_response = fx.get("erp_mock_response") or {"data": {}}
    rag_answer = fx.get("rag_mock_answer", "RAG-STUB")

    # Capture the two internal collection stages without touching prod code.
    stages = {}
    _orig_replay = _de._replay_business_action_collection
    _orig_mem = _de._apply_identifier_memory

    def _replay_spy(*a, **k):
        out = _orig_replay(*a, **k)
        stages["after_replay"] = dict(out)
        return out

    def _mem_spy(action, collected, cc, **k):
        stages.setdefault("before", dict(collected))
        out = _orig_mem(action, collected, cc, **k)
        stages["after_memory"] = dict(out)
        return out

    with patch("services.action_executor.requests.request",
               return_value=MagicMock(status_code=200, json=lambda: erp_mock_response)) as erp_http, \
         patch("services.playground_orchestrator.run_playground_turn",
               return_value=_fake_playground_result(answer=rag_answer, confidence=0.9)), \
         patch.object(_de, "_replay_business_action_collection", _replay_spy), \
         patch.object(_de, "_apply_identifier_memory", _mem_spy):
        result = engine.decide(message, history=history, context=context)

    dev = result.get("developer") or {}
    ics = dev.get("information_collection_status") or {}
    exec_res = dev.get("execution_result") or {}
    routing = (result.get("routing") or {}).get("type")
    reply = (result.get("reply") or {}).get("text") or ""
    erp_called = bool(erp_http.called)
    collected_final = ics.get("collected_parameters") or {}

    exp = fx.get("expected") or {}
    checks = []
    if "selected_action" in exp:
        checks.append(("selected action", ics.get("selected_business_action") or dev.get("selected_business_action"),
                       exp["selected_action"]))
    if "routing_type" in exp:
        checks.append(("routing type", routing, exp["routing_type"]))
    if "erp_called" in exp:
        checks.append(("erp called", erp_called, bool(exp["erp_called"])))
    if "shipment_code_present" in exp:
        checks.append(("ShipmentCode present", "ShipmentCode" in collected_final, bool(exp["shipment_code_present"])))
    if "is_complete" in exp:
        checks.append(("is_complete", bool(ics.get("is_complete")), bool(exp["is_complete"])))
    reply_ok = all(s in reply for s in exp.get("reply_contains", []))
    if exp.get("reply_contains"):
        checks.append(("reply contains " + repr(exp["reply_contains"]), reply_ok, True))

    passed = all(actual == want for _, actual, want in checks)

    _p("CASE:              ", fx.get("case_id", "?"))
    _p("CAPTURED_AT:        ", fx.get("captured_at", "?"))
    _p("MESSAGE:           ", message)
    _p()
    _p("SELECTED ACTION:   ", ics.get("selected_business_action") or dev.get("selected_business_action"))
    _p("SELECTION SOURCE:  ", dev.get("selection_source"))
    _p("ROUTING TYPE:      ", routing)
    _p("TURN INTENT:       ", dev.get("turn_intent"), "| coerced:", dev.get("turn_intent_coerced"))
    _p()
    _p("PENDING ACTION:    ", context.get("pending_action_id") or None)
    _p("LAST BUSINESS ACT: ", (fx.get("customer_context") or {}).get("last_business_action"))
    _p()
    _p("COLLECTED BEFORE:       ", _fmt(stages.get("before", {})))
    _p("COLLECTED AFTER REPLAY: ", _fmt(stages.get("after_replay", {})))
    _p("COLLECTED AFTER MEMORY: ", _fmt(stages.get("after_memory", collected_final)))
    _p("MISSING PARAMETERS:     ", _fmt(ics.get("missing_parameters") or []))
    _p()
    _p("ERP CALLED:        ", "YES" if erp_called else "NO")
    _p("ERP ACTION:        ", exec_res.get("selected_business_action") or dev.get("selected_business_action")
          if erp_called else "-")
    _p()
    _p("REPLY:             ", reply.replace("\n", " / "))
    _p()
    for name, actual, want in checks:
        flag = "ok  " if actual == want else "FAIL"
        _p(f"  [{flag}] {name}: actual={_fmt(actual)} expected={_fmt(want)}")
    _p()
    _p("EXPECTED:          ", _fmt(exp))
    _p("ACTUAL:            ", _fmt({"selected_action": ics.get("selected_business_action"),
                                       "routing_type": routing, "erp_called": erp_called,
                                       "shipment_code_present": "ShipmentCode" in collected_final,
                                       "is_complete": bool(ics.get("is_complete"))}))
    _p("RESULT:            ", "PASS" if passed else "FAIL")
    return passed


def main(argv):
    if len(argv) != 2:
        print("usage: python tools/replay_real_turn.py <fixture.json>")
        return 2
    fixture = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    return 0 if replay(fixture) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
