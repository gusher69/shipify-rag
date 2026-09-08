# -*- coding: utf-8 -*-
"""PHASE-6E — response-style lab runner.

Drives every style case through the production-equivalent path (the
PHASE-6C `WebhookConversation` harness) and scores the final reply with
`evaluator.evaluate`. `--live` uses the real LLM + RAG for generated
prose (the meaningful mode for style); the default deterministic mode
still scores every fixed-string reply and is the CI-gate mode.

Writes reports/phase6e_style_report.json / phase6e_style_failures.json /
phase6e_style_summary.md.
"""
from __future__ import annotations
import argparse
import json
import os
import time
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

from tests.phase6c_lab.harness import WebhookConversation
from tests.phase6e_style.corpus import CASES
from tests.phase6e_style.evaluator import evaluate


def _mk_engine():
    from tests.test_business_action_registry import reset_real_registry
    reset_real_registry()
    from services.decision_engine import DecisionEngine
    return DecisionEngine()


def _run_case(engine, case, mode):
    conv = WebhookConversation(engine=engine, mode=mode)
    turns = case["turns"]
    seed = [t for t in turns if isinstance(t, dict)]
    msgs = [t for t in turns if isinstance(t, str)]
    if seed:
        conv.seed_history(seed)
    prior_assistant = seed[-1]["content"] if seed and seed[-1].get("role") == "assistant" else ""
    last = {}
    for m in msgs:
        last = conv.send(m)
    reply = last.get("reply") or ""
    routing = last.get("routing")
    ev = evaluate(reply, case, routing=routing, prior_assistant=prior_assistant)
    return {
        "case_id": case["id"], "family": case["family"],
        "turns": [t if isinstance(t, str) else f"[{t['role']}] {t['content']}" for t in turns],
        "reply": reply[:500],
        "routing": routing, "selection_source": last.get("selection_source"),
        "intent_family": last.get("intent_family"),
        "status": ev["status"], "axes": ev["axes"], "notes": ev["notes"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--out-dir", default=str(_ROOT / "reports"))
    args = ap.parse_args()
    os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-phase6e")
    mode = "live" if args.live else "deterministic"

    engine = _mk_engine()
    t0 = time.time()
    rows = []
    for c in CASES:
        try:
            rows.append(_run_case(engine, c, mode))
        except Exception as e:
            rows.append({"case_id": c["id"], "family": c["family"], "turns": c["turns"],
                         "reply": "", "status": "ERROR", "notes": [repr(e)], "axes": {}})
    elapsed = round(time.time() - t0, 1)

    counts = Counter(r["status"] for r in rows)
    axis_tally = Counter()
    axis_total = Counter()
    for r in rows:
        for k, v in (r.get("axes") or {}).items():
            axis_total[k] += 1
            if v:
                axis_tally[k] += 1

    n = len(rows)
    npass = counts.get("PASS", 0)
    hard = counts.get("FAIL_HARD", 0)
    internal_leak = sum(1 for r in rows if r.get("axes", {}).get("NO_INTERNAL_LANGUAGE") is False)
    fake_handoff = sum(1 for r in rows if "claims a staff follow-up" in " ".join(r.get("notes", [])))
    repeated_slot = sum(1 for r in rows if any("re-asks supplied slot" in x for x in r.get("notes", [])))
    priv_leak = sum(1 for r in rows if r.get("axes", {}).get("NO_PRIVATE_LEAK") is False)

    summary = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": mode, "elapsed_seconds": elapsed,
        "STYLE_CASES_total": n,
        "STYLE_CASES_pass": npass,
        "STYLE_CASES_fail": n - npass,
        "PASS_RATE": round(npass / max(n, 1), 4),
        "FAIL_HARD": hard,
        "FAIL_SOFT": counts.get("FAIL_SOFT", 0),
        "ERROR": counts.get("ERROR", 0),
        "INTERNAL_TERMINOLOGY_LEAK": internal_leak,
        "FAKE_ACTION_OR_HANDOFF": fake_handoff,
        "REPEATED_KNOWN_SLOT": repeated_slot,
        "PRIVATE_DATA_LEAK": priv_leak,
        "axis_pass_rates": {k: f"{axis_tally[k]}/{axis_total[k]}" for k in sorted(axis_total)},
    }

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "phase6e_style_report.json").write_text(
        json.dumps({"summary": summary, "cases": rows}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    fails = [r for r in rows if r["status"] not in ("PASS",)]
    (out / "phase6e_style_failures.json").write_text(
        json.dumps({"count": len(fails), "cases": fails}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    md = ["# PHASE-6E — Response Style Lab", "",
          f"- generated: {summary['generated']}  (mode={mode}, {elapsed}s)", "",
          f"- STYLE CASES: {n}  ·  PASS: {npass}  ·  FAIL: {n - npass}  ·  rate: {summary['PASS_RATE']*100:.1f}%",
          "",
          "## Hard acceptance counters", "",
          f"- INTERNAL TERMINOLOGY LEAK: {internal_leak}",
          f"- FAKE ACTION / HANDOFF: {fake_handoff}",
          f"- REPEATED KNOWN SLOT: {repeated_slot}",
          f"- PRIVATE DATA LEAK: {priv_leak}", "",
          "## Axis pass rates", ""]
    for k, v in summary["axis_pass_rates"].items():
        md.append(f"- {k}: {v}")
    md += ["", "## Non-passing cases", ""]
    for r in fails:
        md.append(f"- `{r['case_id']}` [{r['family']}] {r['status']} — {'; '.join(r.get('notes', []))}")
        md.append(f"  reply: {r['reply'][:160]!r}")
    (out / "phase6e_style_summary.md").write_text("\n".join(md), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    main()
