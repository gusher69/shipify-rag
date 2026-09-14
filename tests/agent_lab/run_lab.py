# -*- coding: utf-8 -*-
"""LangGraph generalization lab + current-engine comparison.

    python -m tests.agent_lab.run_lab

Two tiers, for an honest reason:

  UNDERSTANDING tier (every corpus turn). The graph runs with execution
  STUBBED — the same reuse path shadow mode uses — so what is measured is
  understanding, memory, planning and authority, not retrieval latency.
  No paid API call, no ERP call.

  SAFETY tier (a bounded subset). The same turns run through the REAL
  Decision Engine so the four never-zero requirements are measured
  against actual customer-facing text, and the graph's decision is
  compared with the current engine's turn by turn.

Writes reports/agent_generalization_lab.{json,md} and
reports/agent_vs_current_engine.md. Never mutates anything.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from collections import Counter
from typing import Any, Dict, List

import tests  # pins the offline tier

from tests.agent_lab import corpus
from services.agent.runner import run_agent, compare
from services.agent.adapters import existing_engine as adapter

CTX = {"channel": "line", "tenant_id": "default", "sample_source": "TEST",
       "customer_context": {}, "developer_mode": True}
# a neutral stub so the UNDERSTANDING tier never executes a tool.
_STUB = {"reply": {"text": "[STUB]"}, "routing": {"type": "GENERAL"},
         "developer": {"selection_source": "lab_stub"}}

_ASK_ACTION_OF_SLOT = {"product": "ASK_PRODUCT", "quantity": "ASK_QUANTITY",
                       "shipping_method": "ASK_SHIPPING_METHOD"}


def _decide(text, history=None, *, stub=True):
    ctx = dict(CTX)
    if stub:
        ctx["_precomputed_engine_result"] = dict(_STUB)
    return run_agent(text, history=history or [], context=ctx)


def _slot(d, name):
    v = (d.known_slots or {}).get(name)
    if isinstance(v, dict):
        return v.get("value"), v.get("unit")
    return v, None


def run_understanding_tier(m: Dict[str, Any], misses: List[Dict]) -> None:
    # ── paraphrase families ──
    for fid, expect, forms in corpus.PARAPHRASE_FAMILIES:
        seen = []
        for text in forms:
            m["turns"] += 1
            d = _decide(text)
            seen.append((text, d))
            for key, want in expect.items():
                got = getattr(d, key, None)
                if got != want:
                    m["intent_consistency_fail"] += 1
                    misses.append({"kind": "paraphrase", "family": fid, "text": text,
                                   "field": key, "got": got, "want": want})
        # every member must land on the SAME canonical understanding.
        canon = {(d.planned_action, d.selected_tool, d.auth_required) for _t, d in seen}
        if len(canon) > 1:
            m["family_divergence"] += 1
            misses.append({"kind": "family_divergence", "family": fid,
                           "variants": sorted(str(c) for c in canon)})

    # ── entity / multi-intent matrix ──
    for case in corpus.entity_cases():
        m["turns"] += 1
        d = _decide(case["text"])
        p, _ = _slot(d, "product")
        q, u = _slot(d, "quantity")
        meth, _ = _slot(d, "shipping_method")
        if p == case["product"]:
            m["product_ok"] += 1
        else:
            misses.append({"kind": "product", "text": case["text"], "got": p,
                           "want": case["product"]})
        if q == case["quantity"]:
            m["quantity_ok"] += 1
        else:
            misses.append({"kind": "quantity", "text": case["text"], "got": q,
                           "want": case["quantity"]})
        if u == case["unit"]:
            m["unit_ok"] += 1
        else:
            misses.append({"kind": "unit", "text": case["text"], "got": u,
                           "want": case["unit"]})
        if case["method"] is None or meth == case["method"]:
            m["method_ok"] += 1
        else:
            misses.append({"kind": "method", "text": case["text"], "got": meth,
                           "want": case["method"]})
        # the question clause may never be welded onto the product
        if p and any(frag in p for frag in ("ราคา", "เท่าไหร่", "ยังไง", "กี่วัน", "ได้ไหม")):
            m["product_clause_contamination"] += 1
            misses.append({"kind": "contamination", "text": case["text"], "got": p})
        m["entity_cases"] += 1

    # ── multi-turn journeys ──
    for journey in corpus.journeys():
        history: List[Dict[str, str]] = []
        for step in journey["steps"]:
            m["turns"] += 1
            d = _decide(step["say"], history)
            for slot, want in (step.get("keeps") or {}).items():
                got, _unit = _slot(d, slot)
                if got == want:
                    m["continuity_ok"] += 1
                else:
                    m["continuity_fail"] += 1
                    misses.append({"kind": "continuity", "journey": journey["id"],
                                   "say": step["say"], "slot": slot,
                                   "got": got, "want": want})
            for slot in (step.get("never_asks") or []):
                if d.planned_action == _ASK_ACTION_OF_SLOT.get(slot):
                    m["known_slot_reask"] += 1
                    misses.append({"kind": "known_slot_reask", "journey": journey["id"],
                                   "say": step["say"], "slot": slot})
            if step.get("expect_intent") and d.primary_intent != step["expect_intent"]:
                m["topic_switch_fail"] += 1
                misses.append({"kind": "topic_switch", "journey": journey["id"],
                               "say": step["say"], "got": d.primary_intent,
                               "want": step["expect_intent"]})
            if step.get("not_actions") and d.planned_action in step["not_actions"]:
                m["stale_journey_takeover"] += 1
                misses.append({"kind": "stale_takeover", "journey": journey["id"],
                               "say": step["say"], "action": d.planned_action})
            history = history + [{"role": "user", "content": step["say"]},
                                 {"role": "assistant", "content": d.final_response
                                  or "[STUB]"}]


def run_safety_tier(m: Dict[str, Any], misses: List[Dict],
                    comparison: Counter, wrongs: List[Dict]) -> None:
    from tests.test_business_action_registry import reset_real_registry
    reset_real_registry()
    adapter.reset_engine_for_tests()
    for case in corpus.SAFETY_CASES:
        text = case["text"]
        m["turns"] += 1
        m["safety_turns"] += 1
        # the CURRENT engine answers first, exactly as production does…
        engine_result = adapter.execute(text, [], dict(CTX))
        # …and the graph runs beside it, reusing that execution.
        d = run_agent(text, history=[],
                      context={**CTX, "_precomputed_engine_result": engine_result})
        reply = d.final_response or ""

        if case.get("public") and adapter.IDENTITY_ASK_RE.search(reply):
            m["auth_violation"] += 1
            misses.append({"kind": "auth_violation", "text": text, "reply": reply[:120]})
        if case.get("private") and not d.auth_required:
            m["private_missed"] += 1
            misses.append({"kind": "private_missed", "text": text})
        erp = bool((d.as_dict().get("tool_class") == "PRIVATE_READ")
                   and (engine_result.get("developer") or {}).get("erp_http_status"))
        if not erp:
            leak = [x for x in adapter.PRIVATE_LEAK_RE.findall(reply) if x not in text]
            if leak:
                m["private_leak"] += 1
                misses.append({"kind": "private_leak", "text": text, "leak": leak[:3]})
            if adapter.FALSE_COMPLETION_RE.search(reply):
                m["false_completion"] += 1
                misses.append({"kind": "false_completion", "text": text,
                               "reply": reply[:120]})
        for flag in d.errors:
            head = flag.split(":")[0]
            if head in ("AUTH_VIOLATION", "PRIVATE_LEAK", "FALSE_ACTION_COMPLETION",
                        "KNOWN_SLOT_RE_ASK"):
                m["safety_gate_flags"] += 1

        verdict = compare(engine_result, d)
        comparison[verdict.get("class") or "NONE"] += 1
        if verdict.get("class") == "LANGGRAPH_WRONG":
            wrongs.append({"text": text, **verdict})


def main() -> int:
    started = time.time()
    m: Counter = Counter()
    misses: List[Dict] = []
    comparison: Counter = Counter()
    wrongs: List[Dict] = []

    run_understanding_tier(m, misses)
    run_safety_tier(m, misses, comparison, wrongs)

    entity_n = m["entity_cases"] or 1
    continuity_total = m["continuity_ok"] + m["continuity_fail"]
    summary = {
        "total_turns": m["turns"],
        "understanding_turns": m["turns"] - m["safety_turns"],
        "safety_turns": m["safety_turns"],
        "elapsed_s": round(time.time() - started, 1),
        "accuracy": {
            "product": round(100.0 * m["product_ok"] / entity_n, 2),
            "quantity": round(100.0 * m["quantity_ok"] / entity_n, 2),
            "unit": round(100.0 * m["unit_ok"] / entity_n, 2),
            "method": round(100.0 * m["method_ok"] / entity_n, 2),
            "context_continuity": round(100.0 * m["continuity_ok"] / (continuity_total or 1), 2),
        },
        "hard_requirements": {
            "auth_violation": m["auth_violation"],
            "private_leak": m["private_leak"],
            "false_action_completion": m["false_completion"],
            "known_slot_reask": m["known_slot_reask"],
            "stale_journey_takeover": m["stale_journey_takeover"],
            "product_clause_contamination": m["product_clause_contamination"],
            "family_divergence": m["family_divergence"],
            "intent_consistency_fail": m["intent_consistency_fail"],
            "topic_switch_fail": m["topic_switch_fail"],
            "private_missed": m["private_missed"],
        },
        "comparison": dict(comparison),
        "langgraph_wrong": wrongs,
        "misses": misses[:60],
        "miss_kinds": dict(Counter(x["kind"] for x in misses)),
    }
    summary["status"] = "PASS" if all(v == 0 for v in
                                      summary["hard_requirements"].values()) else "FAIL"

    pathlib.Path("reports").mkdir(exist_ok=True)
    with open("reports/agent_generalization_lab.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ("misses", "langgraph_wrong")},
                     ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
