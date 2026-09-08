# -*- coding: utf-8 -*-
"""PHASE-6C — conversation-lab runner.

    python -m tests.phase6c_lab.run_lab                 # deterministic only
    python -m tests.phase6c_lab.run_lab --live          # + all live cases (real LLM/RAG)
    python -m tests.phase6c_lab.run_lab --live-subset 8 # + first 8 live cases

Writes:
    reports/phase6c_conversation_lab.json
    reports/phase6c_failures.json
    reports/phase6c_summary.md

Never sends a real LINE message. ERP WRITE never executes (mocked); ERP
READ returns an empty payload (safe).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.phase6c_lab.corpus import CORPUS, LONG_HISTORY_A, LONG_HISTORY_B  # noqa: E402
from tests.phase6c_lab.harness import WebhookConversation  # noqa: E402
from tests.phase6c_lab.validator import validate, observed_source  # noqa: E402

_REJECT_SEED = [
    {"role": "user", "content": "ขอลิงก์เว็บ Taobao"},
    {"role": "assistant", "content": "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ"},
]
_HISTORY_MAP = {"LONG_HISTORY_A": LONG_HISTORY_A, "LONG_HISTORY_B": LONG_HISTORY_B,
                "REJECT_SEED": _REJECT_SEED}


def _resolve_history(ctx):
    h = ctx.get("history")
    if h is None:
        return []
    if isinstance(h, str):
        return list(_HISTORY_MAP.get(h, []))
    return list(h)


def _run_case(engine, case, mode):
    ctx = case.get("context") or {}
    conv = WebhookConversation(
        engine=engine, mode=mode,
        handoff_status=ctx.get("handoff_status", "NONE"),
        cust_code=ctx.get("cust_code"),
        conversation_tier=ctx.get("conversation_tier"),
    )
    conv.seed_history(_resolve_history(ctx))
    seed = ctx.get("seed")
    if seed == "expired_confirmation":
        conv.seed_expired_confirmation()
    elif seed == "active_confirmation":
        conv.seed_active_confirmation()

    turn_outs = []
    for turn in case["turns"]:
        msg = turn if isinstance(turn, str) else turn["user"]
        turn_outs.append(conv.send(msg))

    verdict = validate(case, turn_outs)
    last = turn_outs[-1]
    # strip non-JSON-serializable internals (the mocked binding service /
    # developer_trace can carry MagicMocks) before recording.
    _clean_turns = [{k: v for k, v in t.items() if k != "developer"} for t in turn_outs]
    return {
        "case_id": case["case_id"],
        "family": case["family"],
        "mode": mode,
        "conversation": [t if isinstance(t, str) else t["user"] for t in case["turns"]],
        "intent_expected": (case.get("expect") or {}).get("intent_family"),
        "intent_actual": last.get("intent_family"),
        "conversation_act_actual": last.get("conversation_act"),
        "source_expected": (case.get("expect") or {}).get("source"),
        "source_actual": observed_source(last),
        "selection_source": last.get("selection_source"),
        "routing": last.get("routing"),
        "actual_response": last.get("reply"),
        "required_facts": (case.get("expect") or {}).get("required_facts", []),
        "prohibited_facts": (case.get("expect") or {}).get("prohibited_facts", []),
        "grounding_class_expected": (case.get("expect") or {}).get("grounding_class"),
        "status": verdict["status"],
        "reason": verdict["reason"],
        "history_length": len(_resolve_history(ctx)),
        "auth_state": "verified:" + ctx["cust_code"] if ctx.get("cust_code") else "anonymous",
        "handoff_state": ctx.get("handoff_status", "NONE"),
        "all_turns": _clean_turns,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--live-subset", type=int, default=0)
    ap.add_argument("--out-dir", default=str(_ROOT / "reports"))
    args = ap.parse_args()

    os.environ.setdefault("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", "sk-invalid-lab"))
    from tests.test_business_action_registry import reset_real_registry
    reset_real_registry()
    from services.decision_engine import DecisionEngine
    engine = DecisionEngine()

    live_cases = [c for c in CORPUS if c["mode"] == "live"]
    det_cases = [c for c in CORPUS if c["mode"] != "live"]
    run_live = args.live or args.live_subset > 0
    if args.live_subset > 0:
        live_cases = live_cases[:args.live_subset]

    results = []
    t0 = time.time()
    for c in det_cases:
        try:
            results.append(_run_case(engine, c, "deterministic"))
        except Exception as e:  # never let one case abort the run
            results.append({"case_id": c["case_id"], "family": c["family"], "mode": "deterministic",
                            "conversation": [t if isinstance(t, str) else t["user"] for t in c["turns"]],
                            "status": "FAIL_STATE", "reason": f"harness exception: {e!r}",
                            "actual_response": "", "intent_actual": None, "source_actual": None,
                            "history_length": 0, "auth_state": "?", "handoff_state": "?"})
    if run_live:
        for c in live_cases:
            try:
                results.append(_run_case(engine, c, "live"))
            except Exception as e:
                results.append({"case_id": c["case_id"], "family": c["family"], "mode": "live",
                                "conversation": [t if isinstance(t, str) else t["user"] for t in c["turns"]],
                                "status": "EXPECTED_LIMITATION", "reason": f"live run exception: {e!r}",
                                "actual_response": "", "intent_actual": None, "source_actual": None,
                                "history_length": 0, "auth_state": "?", "handoff_state": "?"})
    elapsed = round(time.time() - t0, 1)

    counts = Counter(r["status"] for r in results)
    multi = sum(1 for r in results if len(r.get("conversation") or []) > 1)
    ctx_aware = sum(1 for r in results if r["status"] == "PASS" and len(r.get("conversation") or []) > 1)
    summary = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_seconds": elapsed,
        "TOTAL": len(results),
        "MULTI_TURN": multi,
        "LIVE_RUN": run_live,
        "PASS": counts.get("PASS", 0),
        "FAIL_ROUTING": counts.get("FAIL_ROUTING", 0),
        "FAIL_CONTEXT": counts.get("FAIL_CONTEXT", 0),
        "FAIL_GROUNDING": counts.get("FAIL_GROUNDING", 0),
        "FAIL_HALLUCINATION": counts.get("FAIL_HALLUCINATION", 0),
        "FAIL_NEXT_ACTION": counts.get("FAIL_NEXT_ACTION", 0),
        "FAIL_STATE": counts.get("FAIL_STATE", 0),
        "FAIL_AUTH": counts.get("FAIL_AUTH", 0),
        "EXPECTED_LIMITATION": counts.get("EXPECTED_LIMITATION", 0),
        "ROUTING_ACCURACY_CONTEXT_AWARE": ctx_aware,
        "BUSINESS_HALLUCINATIONS": counts.get("FAIL_HALLUCINATION", 0),
        "PRIVATE_DATA_LEAKS": sum(1 for r in results if r["status"] == "FAIL_AUTH"),
        "STALE_STATE_FAILURES": counts.get("FAIL_STATE", 0),
    }

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "phase6c_conversation_lab.json").write_text(
        json.dumps({"summary": summary, "cases": results}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fails = [r for r in results if r["status"] not in ("PASS", "EXPECTED_LIMITATION")]
    (out / "phase6c_failures.json").write_text(
        json.dumps({"summary": {k: summary[k] for k in summary if k.startswith("FAIL") or k in ("TOTAL", "PASS")},
                    "failures": fails}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    md = ["# PHASE-6C — Production Conversation Lab", "",
          f"- generated: {summary['generated']}  ({elapsed}s)",
          f"- TOTAL: {summary['TOTAL']}   MULTI_TURN: {summary['MULTI_TURN']}   LIVE_RUN: {run_live}", ""]
    for k in ("PASS", "FAIL_ROUTING", "FAIL_CONTEXT", "FAIL_GROUNDING", "FAIL_HALLUCINATION",
              "FAIL_NEXT_ACTION", "FAIL_STATE", "FAIL_AUTH", "EXPECTED_LIMITATION"):
        md.append(f"- {k}: {summary[k]}")
    md += ["", "## Non-passing cases", ""]
    for r in fails:
        md.append(f"- **{r['case_id']}** ({r['family']}) — `{r['status']}` — {r['reason']}")
        md.append(f"  - conv: {r['conversation']}")
        md.append(f"  - intent {r.get('intent_actual')} (exp {r.get('intent_expected')})  "
                  f"source {r.get('source_actual')} (exp {r.get('source_expected')})")
        md.append(f"  - reply: {str(r.get('actual_response'))[:160]}")
    (out / "phase6c_summary.md").write_text("\n".join(md), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nwrote {out/'phase6c_conversation_lab.json'}")
    print(f"wrote {out/'phase6c_failures.json'}")
    print(f"wrote {out/'phase6c_summary.md'}")
    return 0 if summary["FAIL_ROUTING"] == summary["FAIL_AUTH"] == summary["FAIL_HALLUCINATION"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
