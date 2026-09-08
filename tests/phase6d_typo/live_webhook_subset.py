# -*- coding: utf-8 -*-
"""PHASE-6D — webhook-level typo acceptance with the REAL LLM + RAG.

A small representative subset of typo inputs driven through
`WebhookConversation(mode="live")` — real LINE-style input -> webhook
pre-decide -> DecisionEngine -> real Thai normalization -> semantic ->
pre-RAG flows -> real LLM + real OpenAI embeddings + real RAG retrieval
-> reply. ERP stays mocked (safe READ). No real LINE messages.

Run:  python -m tests.phase6d_typo.live_webhook_subset
Needs a real OPENAI_API_KEY in the environment.
"""
from __future__ import annotations

import json
import os
import sys

from tests.phase6c_lab.harness import WebhookConversation
from tests.phase6c_lab.validator import _PRIVATE_LEAK_RE, _FAKE_SUCCESS_RE
from tests.phase6d_typo.typo_gen import variants

_LONG_HISTORY = [
    {"role": "user", "content": "สวัสดีครับ"},
    {"role": "assistant", "content": "สวัสดีค่ะ ยินดีให้บริการค่ะ"},
] + sum(([
    {"role": "user", "content": f"อยากสอบถามเรื่องนำเข้าสินค้าล็อตที่ {i}"},
    {"role": "assistant", "content": "รับทราบค่ะ มีอะไรให้ช่วยเพิ่มเติมไหมคะ"},
] for i in range(1, 16)), [])  # ~32 turns

# (label, clean sentence, ideal family set, handoff_status, history)
_BASE = [
    ("calc", "ช่วยคำนวณค่าส่งกล่องนี้หน่อย", {"SHIPPING_ESTIMATE"}, "NONE", []),
    ("calc-payload", "กล่อง 40x30x20 หนัก 3 กิโล ทางเรือ", {"SHIPPING_ESTIMATE"}, "NOTIFIED", _LONG_HISTORY),
    ("cost-disc", "ค่าส่งแพงไหมถ้าสั่งชั้นวางของ", {"SHIPPING_ESTIMATE"}, "NONE", []),
    ("withdrawal", "ถอนเงินขนส่งยังไงคะ", {"SHIPPING_WITHDRAWAL", "GENERAL", "UNKNOWN"}, "NONE", []),
    ("contact", "ขอเบอร์ติดต่อหน่อยค่ะ", {"CONTACT_INFO"}, "NONE", []),
    ("link", "ช่วยแปลงลิงก์ให้หน่อย", {"LINK_CONVERSION"}, "NONE", []),
    ("website", "ขอลิงก์เว็บ Taobao กับ 1688", {"WEBSITE_LINK_REQUEST", "LINK_CONVERSION"}, "NONE", []),
    ("import", "อยากนำเข้าเครื่องจักรจากจีน", {"IMPORT_INTEREST", "SERVICE_DISCOVERY"}, "NONE", []),
    ("discovery", "ที่นี่มีบริการอะไรบ้าง", {"SERVICE_DISCOVERY"}, "NONE", []),
    ("invoice", "ขอใบกำกับภาษีได้ไหมคะ", {"INVOICE"}, "NONE", []),
    ("general", "ของแตกง่ายควรแพ็กยังไงดี", {"PRODUCT_POLICY", "GENERAL", "UNKNOWN"}, "NONE", []),
]


def main() -> int:
    if "sk-invalid" in (os.environ.get("OPENAI_API_KEY") or "sk-invalid"):
        print("SKIP: needs a real OPENAI_API_KEY")
        return 0
    from tests.test_business_action_registry import reset_real_registry
    reset_real_registry()
    from services.decision_engine import DecisionEngine
    engine = DecisionEngine()

    rows = []
    ok = fails = 0
    for label, clean, ideal, hs, hist in _BASE:
        vs = [("clean", clean)] + variants(clean, seed=4242, n=3)
        for tname, msg in vs:
            conv = WebhookConversation(engine=engine, mode="live", handoff_status=hs)
            if hist:
                conv.seed_history(list(hist))
            r = conv.send(msg)
            dev = r.get("developer") or {}
            tn = dev.get("typo_normalized") or {}
            reply = r.get("reply") or ""
            fam = r.get("intent_family")
            leak = bool(_PRIVATE_LEAK_RE.search(reply))
            fake = bool(_FAKE_SUCCESS_RE.search(reply)) and r.get("routing") not in ("API", "WEBHOOK")
            passed = (fam in ideal or (tname != "clean" and fam == _BASE_CLEAN_FAM.get(label))) and not leak and not fake
            # structured tokens frozen?
            from services.thai_text_normalizer import _all_protected_spans
            lost = [msg[s:e] for s, e in _all_protected_spans(msg)
                    if msg[s:e] and msg[s:e] not in tn.get("to", msg)]
            if lost:
                passed = False
            rows.append({
                "case": f"{label}:{tname}", "raw": msg, "normalized": tn.get("to", msg),
                "handoff": hs, "history_len": len(hist), "family": fam,
                "routing": r.get("routing"), "structured_lost": lost,
                "leak": leak, "fake_success": fake, "reply": reply[:200],
                "status": "PASS" if passed else "FAIL",
            })
            ok += passed
            fails += (not passed)
    summary = {"total": len(rows), "pass": ok, "fail": fails,
               "structured_value_corruption": sum(1 for r in rows if r["structured_lost"]),
               "private_data_leak": sum(1 for r in rows if r["leak"]),
               "fake_action_success": sum(1 for r in rows if r["fake_success"])}
    print(json.dumps({"summary": summary,
                      "failures": [r for r in rows if r["status"] != "PASS"]},
                     ensure_ascii=False, indent=2))
    return 0 if fails == 0 else 1


_BASE_CLEAN_FAM = {}


if __name__ == "__main__":
    # prime clean families
    from services.conversation_semantics import interpret
    for label, clean, *_ in _BASE:
        try:
            _BASE_CLEAN_FAM[label] = interpret(clean, []).intent_family
        except Exception:
            _BASE_CLEAN_FAM[label] = None
    sys.exit(main())
