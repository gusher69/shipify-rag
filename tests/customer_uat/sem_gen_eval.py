# -*- coding: utf-8 -*-
"""SEM-GEN-1 — conversational semantic backbone: held-out generalization.

REAL DecisionEngine + REAL DB registry + REAL RAG + REAL LLM resolver
(ERP HTTP faked). The HELD-OUT products below were NOT used while
implementing the backbone — they are the generalization proof.

    python -m tests.customer_uat.sem_gen_eval

Writes tests/customer_uat/sem_gen_eval.json + docs/customer_uat_sources/SEM_GEN_1.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from services.decision_engine import DecisionEngine  # noqa: E402

_OUT_JSON = _ROOT / "tests" / "customer_uat" / "sem_gen_eval.json"
_OUT_MD = _ROOT / "docs" / "customer_uat_sources" / "SEM_GEN_1.md"

# products NEVER referenced in services/conversation_semantics.py or the
# implementation probes — pure held-out.
HELD_OUT = ["ตุ๊กตา", "จานชาม", "ผ้าห่ม", "ปากกา", "โคมไฟ", "ตะกร้า"]
HELD_OUT_PROHIBITED = ["ไฟแช็ก", "มีดทำครัว"]      # flammable / sharp

_NOINFO = "ไม่มีข้อมูลยืนยัน"
_PROHIB = ("ต้องห้าม", "ไม่รับนำเข้า", "ไม่สามารถนำเข้า", "วัตถุอันตราย", "วัตถุไวไฟ", "ของมีคม")


def _eng():
    return DecisionEngine()


def _turn(eng, hist, msg):
    ctx = {"channel": "line", "tenant_id": "default", "external_user_id": "U_semgen_eval",
           "developer_mode": True, "customer_context": {}}
    with patch("services.action_executor.requests.request",
               return_value=MagicMock(status_code=200, json=lambda: {"data": {}})):
        r = eng.decide(msg, history=hist, context=ctx)
    dev = r.get("developer") or {}
    reply = (r.get("reply") or {}).get("text") or ""
    hist = hist + [{"role": "user", "content": msg}, {"role": "assistant", "content": reply}]
    sig = {"routing": (r.get("routing") or {}).get("type"),
           "op": dev.get("semantic_op"), "rewrite": dev.get("semantic_rewrite"),
           "handoff": (r.get("handoff_payload") or {}).get("reason"),
           "reply": reply}
    return hist, sig


def _no_handoff(sig):
    return sig["routing"] != "HUMAN_HANDOFF" and not sig["handoff"] and _NOINFO not in sig["reply"]


def main():
    eng = _eng()
    results = []

    def rec(scenario, msg, sig, ok, note=""):
        results.append({"scenario": scenario, "message": msg, "op": sig["op"],
                        "routing": sig["routing"], "verdict": "PASS" if ok else "FAIL",
                        "note": note, "reply": sig["reply"][:120]})

    # A — generic clarification, no contamination
    h, s = _turn(eng, [], "สั่งเยอะได้ไหม")
    rec("A clarification", "สั่งเยอะได้ไหม", s,
        s["routing"] == "WORKFLOW" and "แบตเตอรี่" not in s["reply"] and _no_handoff(s))

    # B/C — product continuation with HELD-OUT products (CHANGE_TARGET x3)
    h, s = _turn(eng, [], f"สนใจนำเข้า{HELD_OUT[0]}")
    rec("C open target", f"สนใจนำเข้า{HELD_OUT[0]}", s, _no_handoff(s))
    for p in HELD_OUT[1:4]:
        h, s = _turn(eng, h, f"{p}ล่ะ")
        rec("C CHANGE_TARGET", f"{p}ล่ะ", s,
            s["op"] == "CHANGE_TARGET" and _no_handoff(s)
            and not any(x in s["reply"] for x in HELD_OUT_PROHIBITED),
            "op=" + str(s["op"]))

    # D — quantity + correction
    h, s = _turn(eng, [], f"สนใจนำเข้า{HELD_OUT[4]}")
    h, s = _turn(eng, h, "200 ตัว")
    rec("D SET_QUANTITY", "200 ตัว", s, s["op"] == "SET_QUANTITY" and "200" in s["reply"] and _no_handoff(s))
    h, s = _turn(eng, h, "ไม่ใช่ 200 เอา 300")
    rec("D CORRECT_QUANTITY", "ไม่ใช่ 200 เอา 300", s,
        s["op"] == "CORRECT_QUANTITY" and "300" in s["reply"] and "200" not in s["reply"].split("300")[-1]
        and _no_handoff(s), "op=" + str(s["op"]))

    # E — change method, keep product + quantity
    h, s = _turn(eng, h, "ถ้าทางเรือล่ะ")
    kept = (HELD_OUT[4] in s["reply"]) and ("300" in s["reply"])
    rec("E CHANGE_METHOD", "ถ้าทางเรือล่ะ", s,
        s["op"] == "CHANGE_METHOD" and ("เรือ" in s["reply"]) and kept and _no_handoff(s),
        f"product/qty kept={kept}")

    # F — change topic: coupon intent wins
    h, s = _turn(eng, [], f"สนใจนำเข้า{HELD_OUT[5]}")
    h, s = _turn(eng, h, "งั้นถามเรื่องคูปองดีกว่า")
    rec("F CHANGE_TOPIC", "งั้นถามเรื่องคูปองดีกว่า", s,
        ("คูปอง" in s["reply"]) and (HELD_OUT[5] not in s["reply"]) and _no_handoff(s),
        "op=" + str(s["op"]))

    # G — prohibited target (held-out flammable): CHANGE_TARGET then policy
    h, s = _turn(eng, [], f"สนใจนำเข้า{HELD_OUT[0]}")
    h, s = _turn(eng, h, f"ถ้าเป็น{HELD_OUT_PROHIBITED[0]}ล่ะ")
    rec("G prohibited target", f"ถ้าเป็น{HELD_OUT_PROHIBITED[0]}ล่ะ", s,
        s["op"] == "CHANGE_TARGET" and any(x in s["reply"] for x in _PROHIB) and _no_handoff(s),
        "op=" + str(s["op"]))

    # H — stale battery cannot resurface
    h = []
    h, _ = _turn(eng, h, "สนใจนำเข้าแบตเตอรี่")
    h, _ = _turn(eng, h, "ของผมเข้าไทยหรือยัง")
    h, _ = _turn(eng, h, "ทางเรือกี่วัน")
    h, _ = _turn(eng, h, "ทางรถกี่วัน")
    h, s = _turn(eng, h, "สั่งเยอะได้ไหม")
    rec("H stale contamination", "สั่งเยอะได้ไหม", s,
        "แบตเตอรี่" not in s["reply"] and _no_handoff(s))

    # ── aggregate ────────────────────────────────────────────────────
    by = {}
    for r in results:
        k = r["scenario"].split(" ", 1)[0] if " " in r["scenario"] else r["scenario"]
        by.setdefault(r["scenario"], []).append(r["verdict"])
    p = sum(1 for r in results if r["verdict"] == "PASS")
    change_target = [r for r in results if r["op"] == "CHANGE_TARGET"]
    out = {
        "pass": p, "total": len(results),
        "change_target_accuracy": f"{sum(1 for r in change_target if r['verdict']=='PASS')}/{len(change_target)}",
        "false_human_handoff": sum(1 for r in results if r["routing"] == "HUMAN_HANDOFF"),
        "stale_contamination": "PASS" if all(
            r["verdict"] == "PASS" for r in results if r["scenario"].startswith("H")) else "FAIL",
        "results": results,
    }
    _OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    L = ["# SEM-GEN-1 — conversational semantic backbone (held-out generalization)", "",
         "REAL DecisionEngine + REAL DB registry + REAL RAG + REAL LLM resolver; ERP faked.",
         f"Held-out products: {', '.join(HELD_OUT + HELD_OUT_PROHIBITED)} "
         "(none referenced in the implementation).", "",
         f"**{p}/{len(results)} PASS · CHANGE_TARGET {out['change_target_accuracy']} · "
         f"false Human CS {out['false_human_handoff']} · stale-contamination {out['stale_contamination']}**", "",
         "| scenario | message | op | routing | verdict | reply |",
         "|---|---|---|---|---|---|"]
    for r in results:
        L.append(f"| {r['scenario']} | {r['message']} | {r['op']} | {r['routing']} | "
                 f"**{r['verdict']}** | {r['reply'][:80]} |")
    L += ["", "_Generated by tests/customer_uat/sem_gen_eval.py._"]
    _OUT_MD.write_text("\n".join(L), encoding="utf-8")
    print(f"wrote {_OUT_JSON}\nwrote {_OUT_MD}")
    print(json.dumps({k: v for k, v in out.items() if k != "results"}, ensure_ascii=False, indent=2))
    return out


if __name__ == "__main__":
    main()
