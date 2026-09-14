# -*- coding: utf-8 -*-
"""LangGraph conversation agent for the Shipify AI platform.

WHAT THIS IS
    A bounded LangGraph that ORCHESTRATES the platform's already-validated
    primitives — the one central semantic interpreter, the canonical
    ConversationResolution, the Decision Engine and its tools, the RAG
    pipeline, the deterministic private/public authority — into one
    explicit, traceable pipeline:

        UNDERSTAND -> REMEMBER -> PLAN -> CHOOSE TOOL -> EXECUTE SAFELY
        -> VALIDATE -> ANSWER -> PERSIST

WHAT THIS IS NOT
    A second decision engine. No business rule, routing table, policy
    verdict, auth check or Thai phrase pattern is re-implemented here.
    Every node delegates to the existing module that already owns that
    decision (see services/agent/adapters/existing_engine.py, which is
    the ONLY place this package is allowed to reach into the rest of the
    system). The goal is FEWER competing authorities, not one more.

HOW IT BECOMES AUTHORITATIVE
    Never implicitly. `config.LANGGRAPH_MODE` has four explicit values —
    off / shadow / owner_test / production — and defaults to `shadow`,
    in which the current engine answers every customer and the graph only
    produces a comparable decision for telemetry.
"""
from services.agent.state import AgentState, AgentDecision, new_state  # noqa: F401
