# -*- coding: utf-8 -*-
"""CUSTOMER 6-SOURCE CLOSURE — noisy-Thai robustness check over the
existing customer_uat_master.jsonl primary messages.

Does NOT re-derive requirements or expected behaviour (that master
inventory is closed). For every case's PRIMARY message, this generates
ONE typo/informal-particle noise variant (reusing the SAME noise
operators tests/language_lab/corpus.py already uses generically) and
runs BOTH the clean and noisy text through the production LangGraph
runtime, then diffs routing_type/turn_intent. A case is NOISE_STABLE
when the noisy variant reaches the same routing_type as the clean
baseline (word-for-word answer text is not compared — this is a
robustness check, not a wording check).

    python -m tests.customer_uat.noise_robustness_check

Writes tests/customer_uat/.gate_scratch/noise_robustness.json. Never
mutates the master inventory or the KB.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

import tests  # noqa: E402  pins the offline tier
import config  # noqa: E402
from tests.test_decision_engine import _fake_playground_result  # noqa: E402
from tests.language_lab.corpus import NOISE_OPERATORS  # noqa: E402
from services.agent.runner import authoritative_run  # noqa: E402

_MASTER = _ROOT / "tests" / "customer_uat" / "customer_uat_master.jsonl"
_OUT = _ROOT / "tests" / "customer_uat" / ".gate_scratch" / "noise_robustness.json"

config.LANGGRAPH_MODE = "production"


def _noisy(text: str, rnd: random.Random) -> str:
    for name, op in rnd.sample(NOISE_OPERATORS, k=len(NOISE_OPERATORS)):
        try:
            got = op(text, rnd)
        except Exception:
            continue
        if got and got != text:
            return got
    return text


def _run(text: str) -> dict:
    ctx = {"channel": "line", "developer_mode": True, "customer_context": {},
          "tenant_id": "default", "external_user_id": "U_noise_check", "sample_source": "REAL_LINE"}
    erp_body = {"data": {"Status": "OK", "ShipmentCode": "XX000000", "OrderCode": "PO0000"}}
    with patch("services.action_executor.requests.request",
              return_value=MagicMock(status_code=200, json=lambda: erp_body)):
        with patch("services.playground_orchestrator.run_playground_turn",
                  return_value=_fake_playground_result(answer="[STUB-RAG-ANSWER]", confidence=0.9)):
            out = authoritative_run(text, history=[], context=dict(ctx),
                                    engine_fallback=lambda: {"reply": {"text": ""},
                                                            "routing": {"type": "ERROR"},
                                                            "developer": {}})
    d = out.decision
    return {"routing_type": d.routing_type, "primary_intent": d.primary_intent,
           "normalized": d.normalized_message, "used": out.used}


def main() -> int:
    rnd = random.Random(11)
    cases = [json.loads(ln) for ln in _MASTER.read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows = []
    for case in cases:
        clean = case["user_message"]
        noisy = _noisy(clean, rnd)
        r_clean = _run(clean)
        r_noisy = _run(noisy) if noisy != clean else r_clean
        stable = r_noisy["routing_type"] == r_clean["routing_type"]
        rows.append({"case_id": case["case_id"], "clean": clean, "noisy": noisy,
                    "changed": noisy != clean, "clean_route": r_clean["routing_type"],
                    "noisy_route": r_noisy["routing_type"], "noisy_norm": r_noisy["normalized"],
                    "stable": stable})
    noised = [r for r in rows if r["changed"]]
    unstable = [r for r in noised if not r["stable"]]
    result = {"total_cases": len(rows), "noised": len(noised),
             "stable": len(noised) - len(unstable), "unstable": unstable}
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"noised {len(noised)}/{len(rows)} cases | stable {len(noised) - len(unstable)}/{len(noised)}")
    for r in unstable:
        print("  UNSTABLE", r["case_id"], repr(r["clean"]), "->", repr(r["noisy"]),
             r["clean_route"], "!=", r["noisy_route"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
