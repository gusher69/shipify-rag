# -*- coding: utf-8 -*-
"""P1 shadow-mode divergence: for a curated turn set, run the REAL
DecisionEngine.decide() and compare its existing routing decision with
the canonical ConversationResolution logged on the same turn.

Writes reports/p1_resolution_divergences.json. Classifies each turn:
  MATCH | EXPECTED_IMPROVEMENT | OLD_BEHAVIOR_CORRECT | RESOLVER_WRONG | AMBIGUOUS
"""
import json
import os
import pathlib
from unittest.mock import patch, MagicMock

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-p1shadow")

from tests.test_business_action_registry import reset_real_registry
reset_real_registry()
from services.decision_engine import DecisionEngine

_REPORTS = pathlib.Path(__file__).resolve().parent.parent.parent / "reports"
_REPORTS.mkdir(exist_ok=True)

_D = ("ได้ค่ะ 😊 ถ้ามีลิงก์สินค้าที่สนใจจาก Taobao, 1688 หรือ Tmall ส่งมาได้เลยนะคะ "
      "ถ้ายังไม่มีลิงก์ บอกคร่าว ๆ ได้เลยว่าอยากสั่งสินค้าอะไร เดี๋ยวช่วยแนะนำขั้นตอนต่อให้ค่ะ")


def _seed(p, **kw):
    M = {"road": "ทางรถ", "sea": "ทางเรือ", "air": "ทางอากาศ"}
    ack = f"ได้ค่ะ รับทราบว่าต้องการนำเข้า{p}"
    if kw.get("qty"):
        ack += f" จำนวนประมาณ {kw['qty']} ชิ้น"
    if kw.get("method"):
        ack += f" ขนส่ง{M[kw['method']]}"
    ack += "นะคะ 😊 รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"
    return [{"role": "user", "content": "อยากสั่งของจากจีน"},
            {"role": "assistant", "content": _D},
            {"role": "user", "content": f"เป็น{p}"},
            {"role": "assistant", "content": ack}]


# (label, message, history, existing_route_expectation, resolver_field_expectation)
TURNS = [
    ("fix01_broad", "อยากสั่งของจากจีน", [], "phase6b_service_intent", {"conversation_act": "NEW_INTENT", "primary_intent": "IMPORT_INTEREST"}),
    ("fix04_slot", "เป็นชั้นวางของ", [{"role": "user", "content": "อยากสั่งของจากจีน"}, {"role": "assistant", "content": _D}], "phase6b_service_intent", {"primary_intent": "IMPORT_INTEREST"}),
    ("fix05_corr", "เปลี่ยนเป้นรองเท้าได้ไหม", _seed("ชั้นวางของ"), "frame_correction_fix05", {"conversation_act": "CORRECTION"}),
    ("fix05_reject", "ไม่เอาแล้ว", _seed("รองเท้า"), "frame_reject_fix05", {"conversation_act": "REJECTION"}),
    ("fix05_ambig", "เปลี่ยนได้ไหม", _seed("โต๊ะ"), "frame_correction_ambiguous_fix05", {"conversation_act": "QUESTION"}),
    ("fix06_stale", "อยากสั่งของจากจีน", _seed("เก้าอี้") * 4, "phase6b_service_intent", {"precedence_winner": "NEW_INTENT"}),
    ("topic_contact", "ขอเบอร์ติดต่อ", _seed("โต๊ะ"), "phase6b_service_intent", {"conversation_act": "TOPIC_SWITCH", "topic_switch": "CONTACT_INFO"}),
    ("private_status", "ของผมถึงไหนแล้ว", [], "private_state_inquiry", {"primary_intent": "SHIPMENT_STATUS", "grounding_requirement": "PRIVATE_ERP"}),
    ("policy_q", "ชั้นวางของนำเข้าได้ไหม", [], None, {"primary_intent": "PRODUCT_POLICY"}),
    ("method_corr", "ทางเรือดีกว่า", _seed("รองเท้า", method="road"), "frame_correction_fix05", {"conversation_act": "CORRECTION"}),
    ("link_conv", "https://detail.1688.com/offer/652702302959.html", [], None, {"execution_hint": "link_conversion"}),
    ("calc", "ค่านำเข้าเท่าไหร่คะ สินค้า1ชิ้น น้ำหนัก 2กิโล ขนาด 54*12*43", [], "shipping_estimate_flow", {}),
    ("greet", "สวัสดีค่ะ", [], None, {"conversation_act": "GENERAL_CHAT"}),
]


def _run():
    eng = DecisionEngine()
    rows = []
    for label, msg, hist, exp_route, exp_res in TURNS:
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value={"answer": "[RAG]", "confidence": 0.4, "citations": [], "sources": []}), \
             patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}}, text="{}")):
            r = eng.decide(msg, history=list(hist), context={
                "channel": "line", "tenant_id": "default", "external_user_id": "U_shadow",
                "developer_mode": True, "customer_context": {}})
        d = r.get("developer") or {}
        res = d.get("conversation_resolution") or {}
        existing = {
            "selection_source": d.get("selection_source"),
            "routing": (r.get("routing") or {}).get("type"),
            "semantic_family": (d.get("semantic_interpretation") or {}).get("intent_family"),
        }
        # compare
        route_ok = (exp_route is None) or (existing["selection_source"] == exp_route)
        res_ok = all(str(res.get(k)) == str(v) for k, v in exp_res.items())
        if route_ok and res_ok:
            verdict = "MATCH"
        elif res_ok and not route_ok:
            verdict = "EXPECTED_IMPROVEMENT" if label in ("method_corr",) else "AMBIGUOUS"
        elif route_ok and not res_ok:
            verdict = "RESOLVER_WRONG"
        else:
            verdict = "AMBIGUOUS"
        rows.append({"label": label, "message": msg, "verdict": verdict,
                     "existing": existing, "resolution": res,
                     "expected_route": exp_route, "expected_resolution": exp_res})
    return rows


def main():
    rows = _run()
    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    out = {"total": len(rows), "counts": counts, "rows": rows}
    (_REPORTS / "p1_resolution_divergences.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(counts, ensure_ascii=False))
    for r in rows:
        if r["verdict"] != "MATCH":
            print(" ", r["label"], r["verdict"], "| existing:", r["existing"]["selection_source"],
                  "| res:", {k: r["resolution"].get(k) for k in ("conversation_act", "primary_intent", "precedence_winner")})


if __name__ == "__main__":
    main()
