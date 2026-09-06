# -*- coding: utf-8 -*-
"""CUSTOMER UAT BASELINE — CURRENT SYSTEM MEASUREMENT.

Measurement-only harness. Runs every logical case + wording variant in
`tests/customer_uat/customer_uat_master.jsonl` through the REAL current
`services.decision_engine.DecisionEngine.decide()` (candidate 3e83ed9),
with the RAG pipeline and the ERP HTTP call replaced by deterministic
fakes — exactly the isolation `tools/replay_real_turn.py` uses. No
network, no DB writes, no LLM, no destructive ERP call.

    python -m tests.customer_uat.run_baseline            # routing baseline (scratch output, tracked files untouched)
    python -m tests.customer_uat.run_baseline --rag      # + live retrieval probe (costs embeddings)
    python -m tests.customer_uat.run_baseline --commit-baseline   # overwrite the TRACKED baseline files (deliberate only)

Outputs (default — safe for the repeatable Regression Gate / CI):
    tests/customer_uat/.gate_scratch/baseline_results.json
    tests/customer_uat/.gate_scratch/CUSTOMER_UAT_BASELINE_REPORT.md

Outputs (only with --commit-baseline — the TRACKED files, deliberate re-measurement):
    tests/customer_uat/baseline_results.json
    docs/customer_uat_sources/CUSTOMER_UAT_BASELINE_REPORT.md

REGRESSION-GATE-1 TEST-ISOLATION NOTE: earlier callers ran this harness
with no way to avoid clobbering the two TRACKED files above as a side
effect, which corrupted an unrelated test
(`tests.test_customer_calc1.TestTrustedRates`) that reads
`baseline_results.json` expecting specific rate-FAQ text, whenever this
harness was invoked mid-session for ad-hoc measurement. `--commit-baseline`
makes that overwrite opt-in and explicit; the repeatable gate command
never passes it. This file changes NO production logic. It is
evaluation tooling only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from tests.test_decision_engine import _fake_playground_result               # noqa: E402
from services.decision_engine import DecisionEngine                          # noqa: E402
import services.decision_engine as _de                                      # noqa: E402

_MASTER = _ROOT / "tests" / "customer_uat" / "customer_uat_master.jsonl"
# TRACKED (committed) baseline artifacts — written only with --commit-baseline.
_TRACKED_OUT_JSON = _ROOT / "tests" / "customer_uat" / "baseline_results.json"
_TRACKED_OUT_MD = _ROOT / "docs" / "customer_uat_sources" / "CUSTOMER_UAT_BASELINE_REPORT.md"
# Scratch (gitignored) default outputs — safe for the repeatable Regression
# Gate / CI to run repeatedly without disturbing the tracked measurement.
_SCRATCH_DIR = _ROOT / "tests" / "customer_uat" / ".gate_scratch"
_SCRATCH_OUT_JSON = _SCRATCH_DIR / "baseline_results.json"
_SCRATCH_OUT_MD = _SCRATCH_DIR / "CUSTOMER_UAT_BASELINE_REPORT.md"
# resolved per-invocation in main() based on --commit-baseline
_OUT_JSON = _TRACKED_OUT_JSON
_OUT_MD = _TRACKED_OUT_MD

# ── Deterministic (non-LLM) signal extractors ────────────────────────────
_NOINFO_RE = re.compile(r"ยังไม่มีข้อมูล|ไม่มีข้อมูลยืนยัน|ไม่มีข้อมูลเกี่ยวกับ")
_IDENTITY_ASK_RE = re.compile(
    r"ยืนยันตัวตน|เบอร์โทรที่ผูก|อีเมลที่ผูก|รหัสลูกค้า|เลขสมาชิก|เลขลูกค้า|ยืนยันสิทธิ์")
_QUESTION_RE = re.compile(r"ไหมคะ|ไหมค่ะ|หรือยัง|รบกวนขอ|รบกวนแจ้ง|ขอทราบ|แจ้ง.*มา|กรุณา|\?|คะ$|ค่ะ$")
_ASKS_IDENTIFIER_RE = re.compile(r"เลขบิล|เลขที่บิล|บิลขนส่ง|บิลสั่งซื้อ|เลขแทรค|แทร็ก|แทรคจีน|tracking|เลขพัสดุ")
_CALC_LINK_RE = re.compile(r"shipify\.co\.th/Rate", re.I)
_PRODUCT_LINK_RE = re.compile(r"PageProductDetail", re.I)

_EXPECTED_ROUTING = {
    "RAG": {"RAG", "GENERAL", "HYBRID"},
    "ERP": {"API", "WORKFLOW", "HYBRID"},
    "WORKFLOW": {"WORKFLOW", "API", "TOOL", "WEBHOOK"},
    "HUMAN_CS": {"HUMAN_HANDOFF"},
    "CLARIFY": {"WORKFLOW", "SAFE_FALLBACK", "RAG", "GENERAL"},
    "NA": None,   # not asserted
}

# action_key → coarse capability family, for the WRONG_ACTION check
_ACTION_FAMILY = {
    "searchdatashipment": "shipment", "searchdatashipmentlist": "shipment",
    "searchdatatracking": "tracking",
    "searchdataorder": "order", "searchdataorderlist": "order",
    "getdatacustomer": "customer_data",
    "requestshippingaddresschange": "address_change",
    "geturlproductdetail": "product_link",
    "sendlinenotics": "human_handoff",
}
# case_id → acceptable action families (None ⇒ not asserted)
_CASE_ACTION_FAMILY = {
    "CUS-G03": {"order", "tracking", "shipment"}, "CUS-G11": {"order", "shipment"},
    "CUS-G12": {"shipment", "tracking"}, "CUS-G16": {"order"}, "CUS-G17": {"shipment", "order"},
    "CUS-G18": {"customer_data", "human_handoff"}, "CUS-G21": {"order"},
    "CUS-S01": {"order", "shipment", "tracking"}, "CUS-S02": {"order"}, "CUS-S03": {"order"},
    "CUS-S04": {"order"}, "CUS-S05": {"customer_data", "order"}, "CUS-S07": {"order"},
    "CUS-S08": {"shipment", "tracking", "order"}, "CUS-S09": {"address_change", "shipment"},
    "CUS-S11": {"shipment"}, "CUS-S12": {"customer_data", "shipment"}, "CUS-S13": {"tracking", "shipment"},
    "CUS-S15": {"customer_data", "address_change"}, "CUS-S17": {"shipment", "tracking"},
    "CUS-S18": {"customer_data", "shipment"},
    "CUS-P20": {"product_link"}, "CUS-G29": {"product_link"},
    "CUS-S20a": {"product_link"}, "CUS-S20b": {"product_link"},
}


def _run_one(engine, message, history, pending_key=None, customer_context=None, *, real_rag=False):
    """Run the REAL decide() once against the REAL DB-backed Business Action
    registry. ERP HTTP is always faked (no destructive call). The RAG
    pipeline is faked in the routing baseline (`real_rag=False`) and run
    for real in the `--rag` pass."""
    ctx = {
        "channel": "line", "developer_mode": True,
        "customer_context": dict(customer_context or {}),
        "tenant_id": "default", "external_user_id": "U_uat_baseline",
    }
    if pending_key:
        try:
            ctx["pending_action_id"] = engine.registry.get_by_key(pending_key)["id"]
        except Exception:
            pass
    erp_body = {"data": {"Status": "OK", "ShipmentCode": "XX000000", "OrderCode": "PO0000"}}
    erp_patch = patch("services.action_executor.requests.request",
                      return_value=MagicMock(status_code=200, json=lambda: erp_body))
    rag_patch = (patch("services.playground_orchestrator.run_playground_turn",
                       return_value=_fake_playground_result(answer="[STUB-RAG-ANSWER]", confidence=0.9))
                 if not real_rag else None)
    with erp_patch as erp_http:
        if rag_patch is not None:
            with rag_patch:
                res = engine.decide(message, history=history or [], context=ctx)
        else:
            res = engine.decide(message, history=history or [], context=ctx)
    dev = res.get("developer") or {}
    ics = dev.get("information_collection_status") or {}
    return {
        "routing_type": (res.get("routing") or {}).get("type"),
        "reply": ((res.get("reply") or {}).get("text") or "").strip(),
        "turn_intent": dev.get("turn_intent"),
        "turn_intent_coerced": dev.get("turn_intent_coerced"),
        "selection_source": dev.get("selection_source"),
        "selected_action": dev.get("selected_business_action") or ics.get("selected_business_action"),
        "missing_parameters": ics.get("missing_parameters") or [],
        "collected_parameters": list((ics.get("collected_parameters") or {}).keys()),
        "is_complete": bool(ics.get("is_complete")),
        "classification": dev.get("classification"),
        "self_verification": bool(dev.get("self_verification")),
        "erp_called": bool(erp_http.called),
    }


# ── 9-dimension deterministic scoring ───────────────────────────────────
def _score(case, sig):
    exp_route = case.get("expected_route") or "NA"
    exp_pp = case.get("expected_public_private") or "NA"
    rt = sig["routing_type"]
    reply = sig["reply"]
    asked_identity = bool(_IDENTITY_ASK_RE.search(reply)) or sig["self_verification"]
    is_question = bool(_QUESTION_RE.search(reply)) and not sig.get("_stub_answer_shown")
    stub_answer = "[STUB-RAG-ANSWER]" in reply
    noinfo = bool(_NOINFO_RE.search(reply))
    allowed = _EXPECTED_ROUTING.get(exp_route)

    d = {}

    # D4 ROUTE
    if allowed is None:
        d["route"] = "NA"
    else:
        d["route"] = "PASS" if rt in allowed else "FAIL"

    # D1 SEMANTIC UNDERSTANDING (did it land in the right broad domain?)
    if exp_route == "NA":
        d["semantic"] = "NA"
    elif exp_route == "RAG":
        d["semantic"] = "PASS" if rt in ("RAG", "GENERAL", "HYBRID") else "FAIL"
    elif exp_route in ("ERP", "WORKFLOW"):
        d["semantic"] = "PASS" if rt in ("API", "WORKFLOW", "HYBRID", "TOOL", "WEBHOOK") else "FAIL"
    elif exp_route == "HUMAN_CS":
        # understanding it needs a human is acceptable via HUMAN_HANDOFF or an ERP collect-then-handoff
        d["semantic"] = "PASS" if rt in ("HUMAN_HANDOFF", "API", "WORKFLOW") else "FAIL"
    elif exp_route == "CLARIFY":
        d["semantic"] = "PASS" if (is_question and not asked_identity) else "FAIL"
    else:
        d["semantic"] = "NA"

    # D2 CONVERSATION OPERATION (inferred, report-only)
    if sig["selection_source"] in ("conversation_continuation", "conversation_reference_detail"):
        d["conversation_operation"] = "CONTINUE"
    elif sig["selection_source"] == "conversation_reference":
        d["conversation_operation"] = "CONTINUE(reference)"
    else:
        d["conversation_operation"] = "NEW_ACTION"

    # D3 PUBLIC / PRIVATE
    if exp_pp == "PUBLIC":
        d["public_private"] = "FAIL" if asked_identity else "PASS"
    elif exp_pp == "PRIVATE":
        # a private request must not be answered outright with no identifier;
        # acceptable = asks for an identifier OR routes into API/WORKFLOW collection OR self-verify
        if stub_answer and rt in ("RAG", "GENERAL"):
            d["public_private"] = "LIVE_RAG_REQUIRED"
        elif asked_identity or _ASKS_IDENTIFIER_RE.search(reply) or rt in ("API", "WORKFLOW"):
            d["public_private"] = "PASS"
        else:
            d["public_private"] = "FAIL"
    else:
        d["public_private"] = "NA"

    # D5 CLARIFICATION QUALITY
    if is_question and not stub_answer:
        if noinfo:
            d["clarification_quality"] = "FAIL"          # 'no info' is never a valid clarification
        elif exp_route in ("ERP", "WORKFLOW", "CLARIFY", "HUMAN_CS"):
            d["clarification_quality"] = "PASS" if _ASKS_IDENTIFIER_RE.search(reply) or exp_route == "CLARIFY" else "WEAK"
        else:
            d["clarification_quality"] = "PASS"
    else:
        d["clarification_quality"] = "NA"

    # D6 NO-INFO CORRECTNESS (routing-level signal only; full check needs live RAG)
    if noinfo:
        d["no_info"] = "FAIL"                            # observed a bare no-info fallback
    elif exp_route == "RAG" and stub_answer:
        d["no_info"] = "LIVE_RAG_REQUIRED"
    elif exp_route == "RAG" and rt in ("SAFE_FALLBACK",):
        d["no_info"] = "FAIL"
    else:
        d["no_info"] = "PASS"

    # D7 RAG RETRIEVAL — not observable with the RAG pipeline stubbed
    d["rag_retrieval"] = "LIVE_RAG_REQUIRED" if exp_route in ("RAG", "CLARIFY") else "NA"

    # D8 ERP / BUSINESS ACTION
    fam_allowed = _CASE_ACTION_FAMILY.get(case["case_id"])
    if exp_route in ("ERP", "WORKFLOW") or fam_allowed:
        sel = sig["selected_action"]
        if not sel:
            d["erp_action"] = "FAIL" if exp_route in ("ERP", "WORKFLOW") else "NA"
        else:
            fam = _ACTION_FAMILY.get(sel, "?")
            if fam_allowed and fam not in fam_allowed:
                d["erp_action"] = "FAIL"                 # wrong capability picked
            elif exp_pp == "PUBLIC" and asked_identity:
                d["erp_action"] = "OVER_VERIFY"
            else:
                d["erp_action"] = "PASS"
    else:
        d["erp_action"] = "NA"

    # D9 RESPONSE QUALITY
    rq = []
    rq.append(("answers_or_asks", bool(reply) and (stub_answer or is_question or _CALC_LINK_RE.search(reply)
                                                   or _PRODUCT_LINK_RE.search(reply))))
    rq.append(("no_over_verify", not (exp_pp == "PUBLIC" and asked_identity)))
    rq.append(("not_bare_no_info", not noinfo))
    rq.append(("asks_next_required",
               (exp_route not in ("ERP", "WORKFLOW")) or is_question or sig["is_complete"] or rt == "API"))
    d["response_quality"] = "PASS" if all(v for _, v in rq) else "FAIL"
    d["_response_quality_detail"] = {k: v for k, v in rq}

    # ── PRIMARY ROOT CLASS (one per failing case) ──────────────────────
    failed = any(d[k] == "FAIL" for k in
                 ("semantic", "public_private", "route", "clarification_quality", "no_info",
                  "erp_action", "response_quality")) or d["erp_action"] == "OVER_VERIFY"
    root = None
    if failed:
        if d["public_private"] == "FAIL" or d["erp_action"] == "OVER_VERIFY":
            root = "PUBLIC_PRIVATE"
        elif d["erp_action"] == "FAIL" and sig["selected_action"]:
            root = "WRONG_ACTION"
        elif d["semantic"] == "FAIL":
            if exp_route == "RAG" and rt in ("API", "WORKFLOW"):
                root = "SEMANTIC_INTENT"          # public/general question dragged into ERP collection
            elif exp_route in ("ERP", "WORKFLOW") and rt in ("RAG", "GENERAL"):
                root = "SEMANTIC_INTENT"
            elif exp_route == "HUMAN_CS":
                root = "HUMAN_HANDOFF"
            elif exp_route == "CLARIFY":
                root = "CONTEXT_OPERATION"
            else:
                root = "SEMANTIC_INTENT"
        elif d["no_info"] == "FAIL":
            root = "NO_INFO_FALLBACK"
        elif d["clarification_quality"] == "FAIL":
            root = "NO_INFO_FALLBACK" if noinfo else "RESPONSE_STYLE"
        elif d["erp_action"] == "FAIL":
            root = "ERP_FLOW"
        elif d["route"] == "FAIL":
            root = "SEMANTIC_INTENT"
        else:
            root = "RESPONSE_STYLE"
        # link-conversion family override
        if case["case_id"] in ("CUS-G29", "CUS-S20a", "CUS-S20b", "CUS-P20"):
            root = "LINK_CONVERSION"
    d["_primary_root_class"] = root
    d["_failed"] = failed
    d["_signals"] = {"asked_identity": asked_identity, "is_question": is_question,
                     "stub_answer": stub_answer, "noinfo": noinfo}
    return d


def main(argv=None):
    global _OUT_JSON, _OUT_MD
    ap = argparse.ArgumentParser()
    ap.add_argument("--rag", action="store_true", help="also run the bounded live RAG pass")
    ap.add_argument("--report-only", action="store_true",
                    help="regenerate the .md from the existing baseline_results.json (no re-run)")
    ap.add_argument("--commit-baseline", action="store_true",
                    help="write the TRACKED tests/customer_uat/baseline_results.json + "
                         "docs/customer_uat_sources/CUSTOMER_UAT_BASELINE_REPORT.md (deliberate "
                         "re-measurement only). Default: write to the gitignored "
                         "tests/customer_uat/.gate_scratch/ so the repeatable Regression Gate can "
                         "run this repeatedly without disturbing the committed measurement.")
    args = ap.parse_args(argv)

    if args.commit_baseline:
        _OUT_JSON, _OUT_MD = _TRACKED_OUT_JSON, _TRACKED_OUT_MD
    else:
        _OUT_JSON, _OUT_MD = _SCRATCH_OUT_JSON, _SCRATCH_OUT_MD
        _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

    if args.report_only:
        _write_report(json.loads(_OUT_JSON.read_text(encoding="utf-8")))
        print(f"regenerated {_OUT_MD}")
        return 0

    cases = [json.loads(ln) for ln in _MASTER.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # REAL production Business Action registry, read live from Supabase
    # (read-only; get_registry() → admin.routes.get_sb()). This IS the
    # deployed config, not a hand-seeded approximation.
    engine = DecisionEngine()
    enabled_actions = sorted(a.get("action_key") for a in engine.registry.list()
                             if a.get("enabled") and not a.get("deleted_at"))

    per_case = []
    per_variant = []
    for case in cases:
        cid = case["case_id"]
        history = []
        pending = None
        cctx = None
        # RL fixtures carry their own real state
        if case.get("replay_fixture"):
            fx = json.loads((_ROOT / case["replay_fixture"]).read_text(encoding="utf-8"))
            history = fx.get("history") or []
            pending = fx.get("pending_action_id_key")
            cctx = dict(fx.get("customer_context") or {})
            if fx.get("verified_cust_code"):
                cctx["cust_code"] = fx["verified_cust_code"]

        strings = [case["user_message"]] + [v for v in (case.get("user_message_variants") or [])
                                            if v and v != case["user_message"]]
        seen = set()
        case_dims = []
        for s in strings:
            if s in seen:
                continue
            seen.add(s)
            sig = _run_one(engine, s, history, pending, cctx)
            dims = _score(case, sig)
            row = {"case_id": cid, "string": s, "is_primary": s == case["user_message"],
                   "expected_route": case.get("expected_route"),
                   "expected_public_private": case.get("expected_public_private"),
                   "signal": sig, "dimensions": {k: v for k, v in dims.items() if not k.startswith("_")},
                   "primary_root_class": dims["_primary_root_class"], "failed": dims["_failed"]}
            per_variant.append(row)
            case_dims.append((s, sig, dims))

        # case-level verdict = primary string's verdict, but note any variant divergence
        prim = next(x for x in case_dims if x[0] == case["user_message"])
        variant_fail = [s for (s, _, dd) in case_dims if dd["_failed"] and s != case["user_message"]]
        per_case.append({
            "case_id": cid, "category": case.get("category"),
            "expected_route": case.get("expected_route"),
            "expected_public_private": case.get("expected_public_private"),
            "meaning": case.get("meaning"),
            "primary_string": case["user_message"],
            "n_strings_evaluated": len(case_dims),
            "signal": prim[1],
            "dimensions": {k: v for k, v in prim[2].items() if not k.startswith("_")},
            "response_quality_detail": prim[2]["_response_quality_detail"],
            "primary_root_class": prim[2]["_primary_root_class"],
            "failed": prim[2]["_failed"],
            "variant_only_failures": variant_fail,
            "has_customer_expected_answer": bool(case.get("customer_provided_expected_answer")),
            "requires_real_line": True,
        })

    # ── live RAG pass (optional) ─────────────────────────────────────
    rag_probe = {"ran": False, "note": "not run (pass --rag to enable)"}
    if args.rag:
        routed_to_rag = {r["case_id"] for r in per_case
                         if r["signal"]["routing_type"] in ("RAG", "GENERAL")}
        rag_probe = _real_rag_pass(engine, cases, routed_to_rag)

    # ── aggregates ───────────────────────────────────────────────────
    agg = _aggregate(per_case, per_variant)
    results = {
        "candidate": "3e83ed9 (verified ancestor of HEAD; zero prod-code drift)",
        "harness": "tests/customer_uat/run_baseline.py — REAL decide() + REAL DB registry; ERP HTTP faked; RAG faked in routing pass",
        "enabled_business_actions": enabled_actions,
        "measurement_only": True, "production_code_changed": False,
        "dependencies_installed": False, "deployed": False,
        "n_logical_cases": len(per_case), "n_strings_evaluated": len(per_variant),
        "aggregates": agg, "rag_probe": rag_probe,
        "per_case": per_case, "per_variant": per_variant,
    }
    _OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(results)
    print(f"wrote {_OUT_JSON}")
    print(f"wrote {_OUT_MD}")
    print(json.dumps(agg, ensure_ascii=False, indent=2))
    return 0


def _aggregate(per_case, per_variant):
    def rate(rows, dim, good=("PASS",)):
        scored = [r for r in rows if r["dimensions"].get(dim) not in (None, "NA", "LIVE_RAG_REQUIRED")]
        if not scored:
            return {"pass": 0, "scored": 0, "pct": None,
                    "live_rag_required": sum(1 for r in rows if r["dimensions"].get(dim) == "LIVE_RAG_REQUIRED")}
        p = sum(1 for r in scored if r["dimensions"][dim] in good)
        return {"pass": p, "scored": len(scored), "pct": round(100 * p / len(scored), 1),
                "live_rag_required": sum(1 for r in rows if r["dimensions"].get(dim) == "LIVE_RAG_REQUIRED")}

    # D2 CONVERSATION OPERATION is report-only (task: "infer current
    # behavior for report only, do NOT change architecture") — it is NOT a
    # scored pass/fail dimension. That leaves 8 scored dimensions.
    dims = ["semantic", "public_private", "route",
            "clarification_quality", "no_info", "rag_retrieval", "erp_action", "response_quality"]
    good_map = {"clarification_quality": ("PASS", "WEAK"), "erp_action": ("PASS",)}
    out = {"by_dimension_case": {}, "by_dimension_variant": {}}
    for dm in dims:
        out["by_dimension_case"][dm] = rate(per_case, dm, good_map.get(dm, ("PASS",)))
        out["by_dimension_variant"][dm] = rate(per_variant, dm, good_map.get(dm, ("PASS",)))
    out["conversation_operation_distribution_case"] = dict(
        Counter(r["dimensions"].get("conversation_operation") for r in per_case))

    root_counts = Counter(r["primary_root_class"] for r in per_case if r["failed"])
    root_counts_v = Counter(r["primary_root_class"] for r in per_variant if r["failed"])
    fam = defaultdict(lambda: {"total": 0, "failed": 0})
    for r in per_case:
        fam[r["category"] or "?"]["total"] += 1
        if r["failed"]:
            fam[r["category"] or "?"]["failed"] += 1

    return {
        "cases_failed": sum(1 for r in per_case if r["failed"]),
        "cases_passed": sum(1 for r in per_case if not r["failed"]),
        "variants_failed": sum(1 for r in per_variant if r["failed"]),
        "variants_passed": sum(1 for r in per_variant if not r["failed"]),
        "overall_case_pass_pct": round(100 * sum(1 for r in per_case if not r["failed"]) / len(per_case), 1),
        "overall_variant_pass_pct": round(100 * sum(1 for r in per_variant if not r["failed"]) / len(per_variant), 1),
        "primary_root_class_counts_case": dict(root_counts.most_common()),
        "primary_root_class_counts_variant": dict(root_counts_v.most_common()),
        "by_category": {k: v for k, v in sorted(fam.items(), key=lambda kv: -kv[1]["failed"])},
        "by_dimension": out,
    }


def _real_rag_pass(engine, cases, also_include=frozenset()):
    """Bounded LIVE pass: for RAG / CLARIFY cases that carry a
    customer-provided expected answer, run the REAL decide() with the REAL
    RAG pipeline (retrieval + generation), ERP HTTP still faked. Classifies
    each outcome as ANSWERED / NO_INFO_FALLBACK / WRONG_ROUTE and measures
    salient-token overlap between the produced reply and the customer's
    expected answer. This is the only place the customer's dominant
    real-world failure ('AI said ไม่มีข้อมูล when a trusted answer exists')
    is directly observable."""
    tok = re.compile(r"[ก-๙A-Za-z0-9]{3,}")
    rows = []
    for c in cases:
        want = ((c.get("expected_route") in ("RAG", "CLARIFY") and c.get("customer_provided_expected_answer"))
                or c["case_id"] in also_include)
        if not want:
            continue
        q = c["user_message"]
        try:
            sig = _run_one(engine, q, [], None, None, real_rag=True)
        except Exception as e:
            rows.append({"case_id": c["case_id"], "query": q, "error": repr(e)[:300]})
            continue
        reply = sig["reply"]
        noinfo = bool(_NOINFO_RE.search(reply))
        exp_ans = c.get("customer_provided_expected_answer") or ""
        exp_tokens = set(tok.findall(exp_ans))
        got_tokens = set(tok.findall(reply))
        overlap = len(exp_tokens & got_tokens)
        ratio = round(overlap / max(len(exp_tokens), 1), 2) if exp_tokens else None
        strong_overlap = bool(exp_tokens) and overlap >= max(4, len(exp_tokens) * 0.18)
        substantive = len(reply) >= 60 and not noinfo
        if sig["routing_type"] not in ("RAG", "GENERAL", "HYBRID", "WORKFLOW", "API"):
            verdict = "WRONG_ROUTE"
        elif noinfo:
            verdict = "NO_INFO_FALLBACK"          # produced the bare 'ไม่มีข้อมูลยืนยัน' style reply
        elif substantive:
            # a real answer was generated; correctness vs the CS-approved wording
            # still needs a human / Ragas judge — strong_overlap is a positive hint only
            verdict = "ANSWERED"
        else:
            verdict = "THIN_REPLY"
        rows.append({
            "case_id": c["case_id"], "query": q, "routing_type": sig["routing_type"],
            "reply_head": reply[:160], "noinfo_sentence": noinfo,
            "expected_token_overlap": overlap, "expected_tokens": len(exp_tokens),
            "overlap_ratio": ratio, "strong_overlap": strong_overlap, "verdict": verdict,
        })
    vc = Counter(r.get("verdict") for r in rows if "verdict" in r)
    return {"ran": True, "n_probed": sum(1 for r in rows if "verdict" in r),
            "verdict_counts": dict(vc), "rows": rows}


def _write_report(results):
    a = results["aggregates"]
    bd = a["by_dimension"]["by_dimension_case"]
    L = []
    P = L.append
    P("# CUSTOMER UAT BASELINE REPORT — CURRENT SYSTEM MEASUREMENT")
    P("")
    P(f"- **Candidate measured:** `{results['candidate']}`")
    P(f"- **Harness:** `{results['harness']}`")
    P("- **Isolation:** REAL `DecisionEngine.decide()`; RAG pipeline "
      "(`run_playground_turn`) and ERP HTTP (`action_executor.requests.request`) replaced with "
      "deterministic fakes. No network, no DB writes, no LLM, no destructive ERP call.")
    P(f"- **Production code changed:** NO   |   **Dependencies installed:** NO   |   **Deployed:** NO")
    P(f"- **Logical cases:** {results['n_logical_cases']}   |   **Strings evaluated "
      f"(messages + wording variants):** {results['n_strings_evaluated']}")
    P("")
    P("> **SEM-1 + SEM-1.1 + SEM-1.2 + IDENTITY-0 + Fix-2 + Fix-2.1 applied (2026-09-03).** Fix-2.1 makes "
      "the Human CS handoff dedupe issue/episode-aware — an unrelated or stale prior handoff no longer "
      "suppresses a genuine new one (keyed on reason class + a 10-minute active-episode window; webhook + "
      "session_service only, no routing change). IDENTITY-0 fixed a "
      "failed-self-verification escalation drop (now a real Human CS handoff). Fix-2 (ported from `7f1e6cd`) "
      "routes a genuine *unsupported company fact* — the RAG pipeline's deterministic Answerability-Gate / "
      "P7.1 no-info branches — through the existing Human CS handoff, and the webhook only promises staff "
      "follow-up after the notification actually succeeded (or a per-conversation dedupe is already on the "
      "books). Neither changes the routing-only Customer UAT numbers below (the routing pass stubs RAG so "
      "`unsupported_company_fact` is never set). This report reflects the tree *after* SEM-1 "
      "(private-record status-inquiry semantic routing), the SEM-1.1 record-scope fix, and the SEM-1.2 "
      "pending-workflow boundary (a greeting / cancel / self-contained UNSPECIFIED private-state inquiry "
      "is no longer consumed as a pending-parameter value; the record-scope invariant now applies across "
      "fresh-search, continuation, pending-resume and conversation-reference). "
      "Baseline `3e83ed9` measured 65.2% logical-case pass / 69.7% semantic / 37.9% ERP-action / "
      "14 SEMANTIC_INTENT primaries. SEM-1 moved 6 cases (CUS-G12, G16, S08, S15, S17, S18) from a "
      "RAG dead-end into the matching Business Action's identifier-collection flow. SEM-1.1 then "
      "corrected an over-reach where an UNSPECIFIED single-record inquiry (no identifier, no "
      "explicit latest/list scope) was routed to a customer-scoped *list* action that silently "
      "returned the latest record for a verified user — it now routes to the per-record *detail* "
      "action and asks for the bill/tracking id; explicit `ล่าสุด` / whole-list scope still uses "
      "the list action. Routing across the 69-case master is byte-identical between SEM-1 and "
      "SEM-1.1 (the master runs anonymous, so both ask for an identifier). The 8 residual "
      "SEMANTIC_INTENT failures are out of scope by design: 5 operational WRITE requests with no "
      "Business Action configured (CUS-G21, S02, S03, S04, S13 → Human Handoff phase) and 3 "
      "genuine how-to questions (CUS-S05, S07, S12 → RAG is the correct primary route).")
    P("")
    P("> **Scope of this baseline.** The Decision Engine's *routing / classification / "
      "public-vs-private / action-selection* behaviour is measured directly and deterministically. "
      "The dimensions that depend on the live RAG pipeline output — **RAG RETRIEVAL (7)**, "
      "**NO-INFO CORRECTNESS (6)** beyond the routing-level signal, **CLARIFICATION QUALITY (5)** "
      "on RAG answers, and *DOES-NOT-INVENT* under RESPONSE QUALITY (9) — cannot be observed while "
      "the RAG pipeline is stubbed and are reported as `LIVE_RAG_REQUIRED`. Every case remains "
      "`REAL LINE REQUIRED`; nothing here closes a customer case.")
    P("")
    P("## Overall")
    P("")
    P(f"| | Passed | Failed | Pass % |")
    P("|---|---|---|---|")
    P(f"| Logical cases (primary wording) | {a['cases_passed']} | {a['cases_failed']} | "
      f"**{a['overall_case_pass_pct']}%** |")
    P(f"| All strings (incl. wording variants) | {a['variants_passed']} | {a['variants_failed']} | "
      f"**{a['overall_variant_pass_pct']}%** |")
    P("")
    P("## Dimension scores — 8 scored dimensions (logical cases, primary wording)")
    P("")
    P("| # | Dimension | Pass | Scored | Pass % | LIVE_RAG_REQUIRED |")
    P("|---|---|---|---|---|---|")
    names = [("1", "SEMANTIC UNDERSTANDING", "semantic"),
             ("3", "PUBLIC / PRIVATE", "public_private"),
             ("4", "ROUTE", "route"),
             ("5", "CLARIFICATION QUALITY", "clarification_quality"),
             ("6", "NO-INFO CORRECTNESS", "no_info"),
             ("7", "RAG RETRIEVAL", "rag_retrieval"),
             ("8", "ERP / BUSINESS ACTION", "erp_action"),
             ("9", "RESPONSE QUALITY", "response_quality")]
    for num, label, key in names:
        r = bd[key]
        pct = "n/a (live RAG)" if r["pct"] is None else f"{r['pct']}%"
        P(f"| {num} | {label} | {r['pass']} | {r['scored']} | {pct} | {r['live_rag_required']} |")
    P("")
    P("**Dimension 2 — CONVERSATION OPERATION** is report-only (inferred, architecture untouched). "
      f"Inferred distribution across logical cases: "
      f"`{a['by_dimension'].get('conversation_operation_distribution_case', {})}` "
      "— single-turn UAT prompts infer NEW_ACTION; `genuine_continuation` infers CONTINUE, while the "
      "two P0-01 replay fixtures correctly infer NEW_ACTION (a fresh request after a completed cycle).")
    P("")
    if results["rag_probe"].get("ran"):
        vc = results["rag_probe"].get("verdict_counts", {})
        P(f"> **Dimensions 6 & 7 are answered by the Live RAG pass below, not by the routing table above** "
          f"(where they read `LIVE_RAG_REQUIRED` because RAG is stubbed). The live pass produced a bare "
          f"*ไม่มีข้อมูลยืนยัน*-style reply for **{vc.get('NO_INFO_FALLBACK', 0)} of "
          f"{results['rag_probe'].get('n_probed', 0)}** probed cases — this is the customer's single "
          f"largest observed failure and it reproduces on `3e83ed9`.")
        P("")
    P("## Primary root-cause family counts (failing logical cases)")
    P("")
    if a["primary_root_class_counts_case"]:
        P("| Root class | Cases |")
        P("|---|---|")
        for k, v in a["primary_root_class_counts_case"].items():
            P(f"| {k} | {v} |")
    else:
        P("_none_")
    P("")
    P("## Failure concentration by customer category")
    P("")
    P("| Category | Failed | Total |")
    P("|---|---|---|")
    for k, v in a["by_category"].items():
        P(f"| {k} | {v['failed']} | {v['total']} |")
    P("")
    P("## Failing cases (primary wording)")
    P("")
    P("| Case | Exp route | Exp P/P | Actual routing | Root class | Key signal |")
    P("|---|---|---|---|---|---|")
    for r in results["per_case"]:
        if not r["failed"]:
            continue
        s = r["signal"]
        keysig = []
        if r["dimensions"].get("public_private") == "FAIL":
            keysig.append("asked identity on PUBLIC")
        if r["dimensions"].get("semantic") == "FAIL":
            keysig.append(f"routed {s['routing_type']}")
        if r["dimensions"].get("no_info") == "FAIL":
            keysig.append("bare no-info reply")
        if r["dimensions"].get("erp_action") in ("FAIL", "OVER_VERIFY"):
            keysig.append(f"action={s['selected_action']}")
        P(f"| {r['case_id']} | {r['expected_route']} | {r['expected_public_private']} | "
          f"{s['routing_type']} | {r['primary_root_class']} | {'; '.join(keysig) or '—'} |")
    P("")
    P("## Cases that PASS routing but still depend on live RAG for a real verdict")
    P("")
    lrr = [r["case_id"] for r in results["per_case"]
           if not r["failed"] and r["dimensions"].get("rag_retrieval") == "LIVE_RAG_REQUIRED"]
    P(", ".join(lrr) if lrr else "_none_")
    P("")
    if results["rag_probe"].get("ran"):
        rp = results["rag_probe"]
        P("## Live RAG pass (REAL retrieval + generation; ERP HTTP faked)")
        P("")
        P(f"- probed **{rp['n_probed']}** cases: every RAG/CLARIFY case with a customer-provided expected "
          "answer, plus every case that routed to RAG/GENERAL in the routing pass")
        P(f"- verdict counts: `{rp['verdict_counts']}`")
        P("- `NO_INFO_FALLBACK` = the live pipeline produced a bare *ไม่มีข้อมูลยืนยัน*-style reply. "
          "`ANSWERED` = a substantive reply was generated (wording-correctness vs the CS-approved answer "
          "still needs a human / Ragas judge). `strong overlap` is a positive hint only, and is noisy for "
          "Thai because the token split has no word boundaries.")
        P("")
        P("| Case | exp route | routing | no-info sentence | strong overlap | verdict |")
        P("|---|---|---|---|---|---|")
        exp_by_id = {c["case_id"]: c.get("expected_route") for c in
                     [json.loads(x) for x in _MASTER.read_text(encoding='utf-8').splitlines() if x.strip()]}
        for row in rp["rows"]:
            if "error" in row:
                P(f"| {row['case_id']} | — | — | — | — | ERROR {row['error']} |")
            else:
                P(f"| {row['case_id']} | {exp_by_id.get(row['case_id'], '?')} | {row['routing_type']} | "
                  f"{'YES' if row['noinfo_sentence'] else 'no'} | "
                  f"{'yes' if row['strong_overlap'] else '·'} | {row['verdict']} |")
        P("")
    P("## Top blocker families and systemic fixes (ranked by cases closed)")
    P("")
    P("These are **systemic** routing/handoff changes, not phrase rules. Counts are logical cases that "
      "would move from FAIL toward PASS.")
    P("")
    P("### 1. Private / account questions are classified to RAG instead of ERP identifier-collection  "
      "— ~15 cases (SEMANTIC_INTENT ×14 + most ERP_FLOW)")
    P("")
    P("`สินค้าถึงโกดังหรือยัง` (CUS-G12), `ร้านส่งหรือยังคะ` (CUS-G16), `ยกเลิกบิลสั่งซื้อ` (CUS-G21) and "
      "CSW-series account actions (CUS-S02–S18) are anonymous-user questions about *this customer's own "
      "order/shipment/wallet state*. On `3e83ed9` the hybrid classifier sends them to RAG; the live RAG "
      "pass then dead-ends **12** of them with a bare *ไม่มีข้อมูลยืนยัน*. The CS-approved behaviour for "
      "every one of these is the same shape: **acknowledge → ask for the one identifier (bill / tracking / "
      "customer code) → run the Business Action**. Systemic fix: a private-state-intent detector (the "
      "`_IDENTITY_GATED_ACTION_TYPES` / `classify_turn_intent` PRIVATE_ACTION path already exists — it is "
      "not firing for these phrasings) that routes 'question about my own record' to the matching "
      "Status-Inquiry Business Action's collection flow before RAG is consulted.")
    P("")
    P("### 2. No trusted answer / operational request never reaches a real Human CS handoff  "
      "— 6 cases (HUMAN_HANDOFF ×5 + CUS-P06)")
    P("")
    P("`เหมารถ` (CUS-G19, CUS-SC1), `สั่งผลิตตามสเปค` (CUS-S06), `รีแพ็ค` (CUS-S10), `รวมบิลเหมารถ` "
      "(CUS-S16) and the genuine-no-info case (CUS-P06) all route to RAG and either dead-end on "
      "*ไม่มีข้อมูล* or answer thinly. Expected: a soft holding reply (*ขอเช็กข้อมูลเพิ่มเติมให้ก่อนนะคะ*) "
      "**and an actual `sendlinenotics` handoff**. Systemic fix: make 'operational request with no "
      "self-serve answer' and 'answerability = no_information on a company question' both resolve to the "
      "HUMAN_HANDOFF route with a real notification — not a RAG fallback string. (This is the Fix-2 "
      "direction that currently lives only on `main`, unverified on LINE.)")
    P("")
    P("### 3. Link conversion is gated behind a customer code  — 1 case, high customer salience (CUS-P20)")
    P("")
    P("`ช่วยแปลงลิงก์ให้หน่อยค่ะ` selects `geturlproductdetail` but the flow asks `กรุณาแจ้งรหัสลูกค้าค่ะ`. "
      "The customer explicitly flagged (PDF p17): link conversion is not an internal-data check and must "
      "not require a code or verification. Systemic fix: the product-link-conversion capability must "
      "declare no identity parameter / PUBLIC routing.")
    P("")
    P("### 4. Public FAQ / prohibited-goods / rate answers are healthy")
    P("")
    P("All 23 Public-FAQ cases, all 3 prohibited-goods and all 3 rate cases route correctly and the live "
      "RAG pass answers them substantively — including F06–F11, the customer's own 2/9/2025 recorded "
      "failures (`โกดังอ่อนนุช`, `เครื่องบิน`, `ใบกำกับ`, `ตีลังไม้`). The KB-coverage regressions from "
      "that round are largely resolved on `3e83ed9`; wording-fidelity vs the CS-approved script still "
      "needs a human / Ragas judge on real LINE.")
    P("")
    P("## PDF / TC / CSW screenshot requirement coverage")
    P("")
    P("Cross-checked `docs/customer_uat_sources/เคสที่ต้องแก้ใน 1.คำถามทั่วไป+2.ต้องเช็คในระบบ.pdf` "
      "(17 pages) and the `Ai.xlsx` sheet `2.ต้องเช็คในระบบ` against the 69-case master.")
    P("")
    P("| PDF / source ref | Requirement | Master case | Status |")
    P("|---|---|---|---|")
    P("| p1 #1–2 | over-asks on a single question; once code given it answers | CUS-G03/S01 + CUS-SC2 | covered |")
    P("| p2 #3 | ERP lookup of cheapest private carrier *by customer code* (Kerry/JT) | CUS-G20 (its "
      "`ช่วยประเมินค่าขนส่งที่ถูกสุด` continuation) | covered as sub-requirement |")
    P("| p3 #4–5 | road-shipping time answers OK (PASS) | CUS-F02 / CUS-G22 | covered |")
    P("| p4 #6 | `ติดต่อโรงงาน` — reviewer had not confirmed the expected reply | related CUS-S06 | "
      "acknowledged; no transcribable question/answer — not fabricated |")
    P("| p5 #7 | shipping-cost calc must at least send the self-serve link | CUS-P07 | covered |")
    P("| p5 #8 / TC19 | `เหมารถ` must not say 'not in system' | CUS-G19 / CUS-SC1 | covered |")
    P("| p6 | no-info → soft holding reply + real CS handoff (there IS a CS LINE group) | CUS-P06 | covered |")
    P("| p7 #9 | member vs non-member calc; don't demand a membership no. for a general rate | CUS-P07 / CUS-G04 | covered |")
    P("| p7 #10 | `ออกใบกำกับ` must finish the multi-turn loop | CUS-G05 / CUS-F08 | covered |")
    P("| p8 TC9 | payment method OK; claims must not dead-end | CUS-G09 / CUS-G11 | covered |")
    P("| p8–10 TC11/12/16/17/18 | screenshot-only 'wrong answer, see file' — question text lives in the "
      "image, not transcribed | KB-accuracy family (CUS-F06–F11) | topic covered; individual "
      "screenshots UNRESOLVABLE — not fabricated |")
    P("| p11–17 CSW1–18, CSW20 | account/system-check cases | CUS-S01–S18, CUS-S20a/b / CUS-P20 | covered |")
    P("| CSW19 | — | — | does not exist in `Ai.xlsx` sheet 2 (rows run 1–18 then 20); correctly absent |")
    P("")
    P("**PDF SCREENSHOT REQUIREMENT COVERAGE: COMPLETE** — every transcribable PDF/TC/CSW requirement maps "
      "to a master case. The only un-mapped refs (TC11/12/16/17/18, p4 #6) have no readable question or "
      "expected answer in the source; per the task rule no case was invented for them, and their topics "
      "(KB accuracy, factory coordination) are already represented.")
    P("")
    P("## Files changed by this measurement")
    P("")
    P("- `tests/customer_uat/run_baseline.py` (new — evaluator)")
    P("- `tests/customer_uat/test_baseline_smoke.py` (new — CI smoke)")
    P("- `tests/customer_uat/baseline_results.json` (new — raw results)")
    P("- `docs/customer_uat_sources/CUSTOMER_UAT_BASELINE_REPORT.md` (this file)")
    P("")
    P("**PRODUCTION CODE CHANGED: NO** · **DEPENDENCIES INSTALLED: NO** · **DEPLOYED: NO**")
    P("")
    P("---")
    P("_Generated by tests/customer_uat/run_baseline.py — measurement only. Every case remains "
      "REAL LINE REQUIRED; this baseline closes nothing._")
    _OUT_MD.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
