# -*- coding: utf-8 -*-
"""Thai human-language lab — typo robustness, paraphrase consistency,
identifier protection, unknown-product preservation and multi-turn
context under noise, all measured on the LangGraph path.

    python -m tests.language_lab.run_lab            # full lab -> reports/
    python -m tests.language_lab.run_lab --quick    # journeys subset

Two execution tiers, for an honest reason:

  SINGLE-TURN tier (typo / paraphrase / identifier / unknown-product).
  The graph runs with execution STUBBED — the same reuse path shadow
  mode uses — so what is measured is normalisation + understanding +
  planning + authority. No paid API call, no ERP call.

  JOURNEY tier (>= 100 multi-turn conversations). The graph runs
  UNSTUBBED over the real Decision Engine with the REAL reply text
  carried between turns, because on this platform the assistant's own
  wording IS the conversation state. Test tier pinned offline by
  tests/__init__.py: retrieval degrades to the engine's documented
  no-information replies, which is exactly what the safety metrics need.

Writes reports/thai_language_intelligence.{json,md}. Never mutates
anything.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

import tests  # noqa: F401  pins the offline tier

from tests.language_lab import corpus as C
from services.agent.runner import run_agent
from services.agent.adapters import existing_engine as adapter
from services.language import normalize
from services.conversation_semantics import (
    _ASSISTANT_ASKED_PRODUCT_RE, _ASSISTANT_ASKED_QTY_RE, _ASSISTANT_ASKED_METHOD_RE,
)

CTX = {"channel": "line", "tenant_id": "default", "sample_source": "TEST",
       "customer_context": {}, "developer_mode": True}
_STUB = {"reply": {"text": "[STUB]"}, "routing": {"type": "GENERAL"},
         "developer": {"selection_source": "lab_stub"}}
_ASK_ACTIONS = {"product": "ASK_PRODUCT", "quantity": "ASK_QUANTITY",
                "shipping_method": "ASK_SHIPPING_METHOD"}
_ASK_RES = {"product": _ASSISTANT_ASKED_PRODUCT_RE, "quantity": _ASSISTANT_ASKED_QTY_RE,
            "shipping_method": _ASSISTANT_ASKED_METHOD_RE}
# numbers a reply may legitimately contain without a grounded read: the
# committed public contact block.
_CONTACT_NUMBERS = {"02-026-6426", "080-289-3956", "026-6426", "02", "026", "6426", "080", "289", "3956"}


def _no_backoff() -> None:
    """The offline tier answers every OpenAI call with 401; the embedding
    client still sleeps 1+2+4+8s between its retries. That wait is
    network etiquette, not behaviour — skip it in the lab."""
    try:
        import time as _time
        import services.embedding_service as _es

        class _NoSleepTime:               # only THAT module's sleep, not the process's
            sleep = staticmethod(lambda _s: None)

            def __getattr__(self, name):
                return getattr(_time, name)

        if not isinstance(_es.time, _NoSleepTime):
            _es.time = _NoSleepTime()
    except Exception:
        pass


def _decide(text: str, history=None, *, stub: bool = True):
    ctx = dict(CTX)
    if stub:
        ctx["_precomputed_engine_result"] = dict(_STUB)
    return run_agent(text, history=history or [], context=ctx)


def _slot(d, name) -> Tuple[Any, Any]:
    v = (d.known_slots or {}).get(name)
    if isinstance(v, dict):
        return v.get("value"), v.get("unit")
    return v, None


def _entity(d, name) -> Any:
    v = (d.entities or {}).get(name)
    return v.get("value") if isinstance(v, dict) else v


# ── single-turn evaluation against a Truth ───────────────────────────
def check_truth(d, truth: C.Truth) -> List[str]:
    fails: List[str] = []
    fam_ok = (d.primary_intent in truth.family) if isinstance(truth.family, set) \
        else d.primary_intent == truth.family
    if not fam_ok:
        fails.append(f"family {d.primary_intent} != {truth.family}")
    if truth.check_product:
        got = _slot(d, "product")[0]
        if truth.product == "*":
            if not got:
                fails.append("product missing")
        elif truth.product is None:
            if got:
                fails.append(f"unexpected product {got!r}")
        elif got != truth.product:
            fails.append(f"product {got!r} != {truth.product!r}")
    if truth.check_quantity:
        q, u = _slot(d, "quantity")
        if truth.quantity is None:
            if q:
                fails.append(f"unexpected quantity {q}")
        else:
            if q != truth.quantity:
                fails.append(f"quantity {q} != {truth.quantity}")
            if truth.unit and u != truth.unit:
                fails.append(f"unit {u!r} != {truth.unit!r}")
    if truth.check_method:
        m = _slot(d, "shipping_method")[0]
        if truth.method is None:
            if m:
                fails.append(f"unexpected method {m}")
        elif m != truth.method:
            fails.append(f"method {m!r} != {truth.method!r}")
    if truth.auth is not None and bool(d.auth_required) != truth.auth:
        fails.append(f"auth_required {d.auth_required} != {truth.auth}")
    return fails


def _history(key: Optional[str]) -> List[Dict[str, str]]:
    return list(C.HISTORIES.get(key or "", []))


def evaluate_base() -> Dict[str, Any]:
    rows = []
    for text, truth in C.BASE:
        d = _decide(text, _history(truth.history))
        rows.append({"text": text, "fails": check_truth(d, truth)})
    return {"total": len(rows), "passed": sum(1 for r in rows if not r["fails"]),
            "failures": [r for r in rows if r["fails"]]}


def evaluate_typos(cases: Optional[List[C.TypoCase]] = None) -> Dict[str, Any]:
    cases = cases if cases is not None else C.typo_cases()
    rows, by_op = [], defaultdict(lambda: [0, 0])
    for c in cases:
        d = _decide(c.noisy, _history(c.history))
        fails = check_truth(d, c.truth)
        by_op[c.op][1] += 1
        if not fails:
            by_op[c.op][0] += 1
        rows.append({"noisy": c.noisy, "clean": c.clean, "op": c.op,
                     "normalized": d.normalized_message, "intent": d.primary_intent,
                     "fails": fails})
    passed = sum(1 for r in rows if not r["fails"])
    return {"total": len(rows), "passed": passed,
            "recovery_pct": round(100.0 * passed / max(1, len(rows)), 2),
            "by_operator": {k: {"passed": v[0], "total": v[1]} for k, v in sorted(by_op.items())},
            "failures": [r for r in rows if r["fails"]]}


def evaluate_paraphrases() -> Dict[str, Any]:
    rows, groups = [], {}
    for g in C.PARAPHRASE_GROUPS:
        gp = 0
        for text in g.variants:
            d = _decide(text, _history(g.truth.history))
            fails = check_truth(d, g.truth)
            gp += 0 if fails else 1
            rows.append({"group": g.name, "text": text, "intent": d.primary_intent, "fails": fails})
        groups[g.name] = {"passed": gp, "total": len(g.variants)}
    passed = sum(1 for r in rows if not r["fails"])
    return {"total": len(rows), "groups": len(C.PARAPHRASE_GROUPS), "passed": passed,
            "consistency_pct": round(100.0 * passed / max(1, len(rows)), 2),
            "by_group": groups, "failures": [r for r in rows if r["fails"]]}


def evaluate_identifiers() -> Dict[str, Any]:
    rows = []
    for text, ident in C.IDENTIFIER_CASES:
        n = normalize(text)
        d = _decide(text)
        ok_norm = ident in n.normalized_text
        ok_graph = ident in (d.normalized_message or "")
        rows.append({"text": text, "identifier": ident, "normalized": n.normalized_text,
                     "mutated": not (ok_norm and ok_graph)})
    return {"total": len(rows), "mutations": sum(1 for r in rows if r["mutated"]),
            "failures": [r for r in rows if r["mutated"]]}


def evaluate_unknown_products() -> Dict[str, Any]:
    rows = []
    for text, noun in C.UNKNOWN_PRODUCT_CASES:
        n = normalize(text)
        hist = _history("ASK_PRODUCT") if not re.search(r"ได้ไหม|ได้หรอ|ได้มั้ย", text) else []
        d = _decide(text, hist)
        got = _slot(d, "product")[0]
        rows.append({"text": text, "noun": noun, "normalized": n.normalized_text,
                     "resolved_product": got,
                     "preserved": noun in n.normalized_text and got == noun})
    return {"total": len(rows), "preserved": sum(1 for r in rows if r["preserved"]),
            "failures": [r for r in rows if not r["preserved"]]}


def evaluate_over_correction() -> Dict[str, Any]:
    """A real phrase must reach the interpreter unchanged — through the
    graph, so semantic recovery is included, not just the normaliser."""
    rows = []
    for text in C.REAL_PHRASE_CASES:
        d = _decide(text)
        norm = re.sub(r"\s+", " ", d.normalized_message or "").strip()
        rows.append({"text": text, "normalized": norm, "changed": norm != re.sub(r"\s+", " ", text).strip()})
    return {"total": len(rows), "over_corrections": sum(1 for r in rows if r["changed"]),
            "failures": [r for r in rows if r["changed"]]}


# ── journeys (unstubbed) ─────────────────────────────────────────────
def _numbers(s: str) -> set:
    return set(re.findall(r"\d[\d,.-]*\d|\d", s or ""))


def evaluate_journeys(js: Optional[List[C.Journey]] = None) -> Dict[str, Any]:
    _no_backoff()
    js = js if js is not None else C.journeys()
    counters = Counter()
    turn_rows: List[Dict[str, Any]] = []
    journeys_ok = 0
    for j in js:
        history: List[Dict[str, str]] = []
        seen_numbers: set = set()
        j_ok = True
        for turn in j.turns:
            seen_numbers |= _numbers(turn.text)
            d = _decide(turn.text, history, stub=False)
            reply = d.final_response or ""
            known = {k: v for k, v in (d.known_slots or {}).items() if v}
            fails: List[str] = []
            exp = turn.expect
            if "quantity" in exp:
                q, u = _slot(d, "quantity")
                if (q, u) != tuple(exp["quantity"]):
                    fails.append(f"quantity {(q, u)} != {exp['quantity']}")
            if "product" in exp:
                got = _slot(d, "product")[0]
                if got != exp["product"]:
                    fails.append(f"product {got!r} != {exp['product']!r}")
            if "method" in exp and _slot(d, "shipping_method")[0] != exp["method"]:
                fails.append(f"method {_slot(d, 'shipping_method')[0]!r} != {exp['method']!r}")
            if "entity_product" in exp and _entity(d, "product") != exp["entity_product"]:
                fails.append(f"entity product {_entity(d, 'product')!r} != {exp['entity_product']!r}")
            if "intent" in exp and d.primary_intent != exp["intent"]:
                fails.append(f"intent {d.primary_intent} != {exp['intent']}")
                if d.planned_action in _ASK_ACTIONS.values():
                    counters["stale_context_takeover"] += 1
            if "action" in exp and d.planned_action != exp["action"]:
                fails.append(f"action {d.planned_action} != {exp['action']}")
            if "auth" in exp and bool(d.auth_required) != exp["auth"]:
                fails.append(f"auth {d.auth_required} != {exp['auth']}")
                counters["auth_violation"] += 1
            for na in exp.get("not_action", []):
                if d.planned_action == na:
                    fails.append(f"re-asked via {na}")
            # KNOWN SLOT RE-ASK — the plan and the actual reply text
            for slot, action in _ASK_ACTIONS.items():
                if known.get(slot) and d.planned_action == action:
                    counters["known_slot_reask"] += 1
                    fails.append(f"KNOWN_SLOT_RE_ASK plan {slot}")
                if known.get(slot) and slot != "shipping_method" and _ASK_RES[slot].search(reply):
                    counters["known_slot_reask"] += 1
                    fails.append(f"KNOWN_SLOT_RE_ASK reply {slot}")
            # safety — the graph's own gate plus the adapter vocabulary
            for e in d.errors or []:
                head = e.split(":")[0]
                if head == "AUTH_VIOLATION":
                    counters["auth_violation"] += 1
                elif head == "PRIVATE_LEAK":
                    counters["private_leak"] += 1
                elif head == "FALSE_ACTION_COMPLETION":
                    counters["false_completion"] += 1
                if head in ("AUTH_VIOLATION", "PRIVATE_LEAK", "FALSE_ACTION_COMPLETION", "KNOWN_SLOT_RE_ASK"):
                    fails.append(e)
            if not d.auth_required and adapter.IDENTITY_ASK_RE.search(reply):
                counters["auth_violation"] += 1
                fails.append("public turn asked identity")
            if adapter.FALSE_COMPLETION_RE.search(reply):
                counters["false_completion"] += 1
                fails.append("false completion wording")
            # HALLUCINATED BUSINESS FACT — a number the customer never
            # said and the committed contact block does not contain
            hall = {n for n in _numbers(reply) if n not in seen_numbers and n not in _CONTACT_NUMBERS
                    and not any(n in c for c in _CONTACT_NUMBERS)}
            if hall and d.routing_type not in ("RAG", "BOTH", "HYBRID"):
                counters["hallucinated_fact"] += 1
                fails.append(f"ungrounded numbers {sorted(hall)[:3]}")
            if fails:
                j_ok = False
            turn_rows.append({"journey": j.name, "text": turn.text, "noisy": turn.noisy,
                              "normalized": d.normalized_message, "intent": d.primary_intent,
                              "action": d.planned_action, "route": d.routing_type,
                              "known": {k: _slot(d, k) for k in known}, "fails": fails,
                              "reply": reply[:120]})
            history = history + [{"role": "user", "content": turn.text},
                                 {"role": "assistant", "content": reply}]
        journeys_ok += 1 if j_ok else 0
    total_turns = len(turn_rows)
    ok_turns = sum(1 for r in turn_rows if not r["fails"])
    return {"journeys": len(js), "journeys_passed": journeys_ok, "turns": total_turns,
            "turns_passed": ok_turns,
            "continuity_pct": round(100.0 * ok_turns / max(1, total_turns), 2),
            "noisy_turns": sum(1 for r in turn_rows if r["noisy"]),
            "known_slot_reask": counters["known_slot_reask"],
            "stale_context_takeover": counters["stale_context_takeover"],
            "auth_violation": counters["auth_violation"],
            "private_leak": counters["private_leak"],
            "false_completion": counters["false_completion"],
            "hallucinated_fact": counters["hallucinated_fact"],
            "failures": [r for r in turn_rows if r["fails"]]}


# ── report ───────────────────────────────────────────────────────────
def main(argv: List[str]) -> int:
    quick = "--quick" in argv
    started = time.time()
    _no_backoff()
    import pythainlp, rapidfuzz
    report: Dict[str, Any] = {
        "pythainlp_version": pythainlp.__version__,
        "rapidfuzz_version": rapidfuzz.__version__,
        "base": evaluate_base(),
        "typo": evaluate_typos(),
        "paraphrase": evaluate_paraphrases(),
        "identifiers": evaluate_identifiers(),
        "unknown_products": evaluate_unknown_products(),
        "over_correction": evaluate_over_correction(),
    }
    js = C.journeys()
    report["journeys"] = evaluate_journeys(js[:20] if quick else js)
    report["elapsed_s"] = round(time.time() - started, 1)

    out = pathlib.Path("reports")
    out.mkdir(exist_ok=True)
    (out / "thai_language_intelligence.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    t, p, j = report["typo"], report["paraphrase"], report["journeys"]
    md = [
        "# Thai human-language intelligence lab", "",
        f"PyThaiNLP {report['pythainlp_version']} · RapidFuzz {report['rapidfuzz_version']} · "
        f"offline tier · {report['elapsed_s']}s", "",
        "| Metric | Value | Gate |", "|---|---|---|",
        f"| Base ground truth | {report['base']['passed']}/{report['base']['total']} | 100% |",
        f"| Typo cases | {t['total']} | >= 300 |",
        f"| Typo semantic recovery | {t['recovery_pct']}% ({t['passed']}/{t['total']}) | >= 95% |",
        f"| Paraphrase cases | {p['total']} in {p['groups']} groups | >= 300 |",
        f"| Paraphrase intent consistency | {p['consistency_pct']}% ({p['passed']}/{p['total']}) | >= 98% |",
        f"| Identifier mutation | {report['identifiers']['mutations']} / {report['identifiers']['total']} | 0 |",
        f"| Unknown product preservation | {report['unknown_products']['preserved']}/{report['unknown_products']['total']} | 100% |",
        f"| Over-correction (real phrases changed) | {report['over_correction']['over_corrections']} / {report['over_correction']['total']} | 0 |",
        f"| Journeys | {j['journeys']} ({j['turns']} turns, {j['noisy_turns']} noisy) | >= 100 |",
        f"| Context continuity | {j['continuity_pct']}% turns, {j['journeys_passed']}/{j['journeys']} journeys | — |",
        f"| Known slot re-ask | {j['known_slot_reask']} | 0 |",
        f"| Stale context takeover | {j['stale_context_takeover']} | 0 |",
        f"| Auth violation | {j['auth_violation']} | 0 |",
        f"| Private leak | {j['private_leak']} | 0 |",
        f"| False completion | {j['false_completion']} | 0 |",
        f"| Hallucinated business fact | {j['hallucinated_fact']} | 0 |",
        "", "## Typo recovery by operator", "", "| operator | passed | total |", "|---|---|---|",
    ]
    for k, v in t["by_operator"].items():
        md.append(f"| {k} | {v['passed']} | {v['total']} |")
    md += ["", "## Failures", ""]
    for name in ("base", "typo", "paraphrase", "identifiers", "unknown_products", "over_correction", "journeys"):
        fl = report[name]["failures"]
        md.append(f"### {name}: {len(fl)}")
        for r in fl[:60]:
            label = r.get('text') if 'journey' in r or 'text' in r else r.get('noisy')
            md.append(f"- `{r.get('journey', '')} {label or r.get('noisy')}` -> {r.get('fails') or r}")
        md.append("")
    (out / "thai_language_intelligence.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md[:24]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
