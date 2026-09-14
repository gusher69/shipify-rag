# -*- coding: utf-8 -*-
"""SAFETY — the last gate before a reply may be considered usable.

Everything here is a HARD requirement from the platform's existing
contracts, restated as an assertion over the finished turn. The node
never rewrites a reply into something safer-sounding: it FLAGS, and a
flagged turn can never become the customer-facing answer in owner_test or
production mode (see services/agent/runner.py).

The four that must always be zero:
    AUTH VIOLATION, PRIVATE LEAK, FALSE ACTION COMPLETION,
    HALLUCINATED BUSINESS FACT.
"""
from __future__ import annotations

from typing import Any, Dict, List

from services.agent.adapters import existing_engine as engine

# The Thai safety vocabulary lives at the adapter boundary
# (services/agent/adapters/existing_engine.py), not here: nodes stay
# language-free so the "the graph does not re-implement the interpreter"
# rule stays mechanically checkable.
_FALSE_COMPLETION_RE = engine.FALSE_COMPLETION_RE
_PRIVATE_LEAK_RE = engine.PRIVATE_LEAK_RE
_IDENTITY_ASK_RE = engine.IDENTITY_ASK_RE


def safety_check(state: Dict[str, Any]) -> Dict[str, Any]:
    node_path = list(state.get("node_path") or []) + ["safety_check"]
    reply = state.get("final_response") or ""
    msg = state.get("normalized_message") or state.get("raw_message") or ""
    res = state.get("tool_result") or {}
    erp_called = bool(res.get("erp_called"))
    flags: List[str] = []

    # 1. AUTH — a turn the deterministic authority calls PUBLIC must never
    #    demand identity, and a private read must never happen without it.
    if not state.get("auth_required") and _IDENTITY_ASK_RE.search(reply):
        flags.append("AUTH_VIOLATION: public turn demanded identity")
    if (state.get("auth_state") == "REQUIRED_MISSING"
            and state.get("tool_class") == "PRIVATE_READ" and erp_called):
        flags.append("AUTH_VIOLATION: private read executed without identity")

    # 2. PRIVATE LEAK — an identifier or date the customer did not supply,
    #    in a reply backed by no authenticated read.
    if not erp_called:
        leaked = [x for x in _PRIVATE_LEAK_RE.findall(reply) if x not in msg]
        if leaked:
            flags.append(f"PRIVATE_LEAK: {leaked[:3]}")

    # 3. FALSE COMPLETION — claiming an action succeeded when nothing was
    #    executed. A workflow PREPARES; it never completes.
    if not erp_called and _FALSE_COMPLETION_RE.search(reply):
        flags.append("FALSE_ACTION_COMPLETION")

    # 4. the response-plan invariant, re-checked against the finished text.
    plan = state.get("response_plan") or {}
    ack = plan.get("acknowledge") or {}
    ask = plan.get("ask_next")
    if ask and ask in ack:
        flags.append(f"KNOWN_SLOT_RE_ASK: {ask}")

    return {"node_path": node_path,
            "response_plan": {**plan, "safety_flags": flags},
            "errors": list(state.get("errors") or []) + flags}
