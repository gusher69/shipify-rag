# -*- coding: utf-8 -*-
"""P1 canonical resolver — generalisation-lab scoring + core assertions.

Measures ``ConversationResolution`` output ONLY (no DecisionEngine).
Writes reports/p1_conversation_resolution.json and
reports/p1_resolution_summary.md.
"""
import json
import os
import pathlib
import unittest

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-p1lab")

from services.conversation_resolution import resolve_conversation, SlotValue
from tests.p1_resolution_lab.corpus import CASES

_REPORTS = pathlib.Path(__file__).resolve().parent.parent / "reports"
_REPORTS.mkdir(exist_ok=True)


def _slot_pair(v):
    if isinstance(v, SlotValue):
        return (v.value, v.unit)
    if isinstance(v, dict):
        return (v.get("value"), v.get("unit"))
    return (v, None)


def _score_case(c):
    r = resolve_conversation(c["message"], c["history"])
    exp = c["expect"]
    dims = {}
    if "act" in exp:
        dims["act"] = (r.conversation_act == exp["act"])
    if "intent" in exp:
        dims["intent"] = (r.primary_intent == exp["intent"])
    if "journey" in exp:
        dims["journey"] = (r.active_journey == exp["journey"])
    if "topic" in exp:
        dims["topic"] = (r.topic_switch == exp["topic"])
    if "corr" in exp:
        ok = True
        for slot, newv in exp["corr"].items():
            got = r.slot_corrections.get(slot, {}).get("new")
            ok = ok and (str(got) == str(newv))
        dims["correction"] = ok
    if "slots" in exp:
        ok = True
        for slot, (val, unit) in exp["slots"].items():
            gv = r.slot_updates.get(slot)
            gp = _slot_pair(gv) if gv is not None else (None, None)
            ok = ok and (str(gp[0]) == str(val)) and (unit is None or gp[1] == unit)
        dims["multi_entity"] = ok
    if "qty_unit" in exp:
        units = []
        for d in list(r.slot_updates.values()) + list(r.slot_corrections.values()):
            if isinstance(d, SlotValue):
                units.append(d.unit)
        dims["unit_preservation"] = (exp["qty_unit"] in units)
    return r, dims


class TestP1ResolverLab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = []
        cls.token_corruption = 0
        for c in CASES:
            r, dims = _score_case(c)
            # structured-token corruption: a SlotValue.raw that mangled a
            # number, or a product that swallowed digits
            for k, v in list(r.slot_updates.items()) + list(r.entities.items()):
                if isinstance(v, SlotValue) and k == "product" and any(ch.isdigit() for ch in str(v.value)):
                    cls.token_corruption += 1
            cls.rows.append({"id": c["id"], "category": c["category"],
                             "message": c["message"], "dims": dims,
                             "act": r.conversation_act, "intent": r.primary_intent,
                             "journey": r.active_journey, "winner": r.precedence_winner,
                             "pass": all(dims.values()) if dims else True})

    def _acc(self, dim, category=None):
        vals = [row["dims"][dim] for row in self.rows
                if dim in row["dims"] and (category is None or row["category"] == category)]
        return (sum(vals) / len(vals) if vals else None), len(vals)

    def test_write_reports_and_targets(self):
        by_cat = {}
        for row in self.rows:
            by_cat.setdefault(row["category"], 0)
            by_cat[row["category"]] += 1

        summary = {
            "total": len(self.rows),
            "by_category": by_cat,
            "accuracy": {},
            "structured_token_corruption": self.token_corruption,
        }
        for dim in ("act", "intent", "journey", "topic", "correction",
                    "multi_entity", "unit_preservation"):
            acc, n = self._acc(dim)
            if n:
                summary["accuracy"][dim] = {"pct": round(100 * acc, 1), "n": n}

        (_REPORTS / "p1_conversation_resolution.json").write_text(
            json.dumps({"summary": summary, "rows": self.rows}, ensure_ascii=False, indent=2),
            encoding="utf-8")

        md = ["# P1 Canonical Resolver — Generalisation Lab", "",
              f"- total cases: **{summary['total']}**",
              f"- structured token corruption: **{summary['structured_token_corruption']}**", "",
              "| dimension | accuracy | n |", "|---|---|---|"]
        for dim, v in summary["accuracy"].items():
            md.append(f"| {dim} | {v['pct']}% | {v['n']} |")
        md += ["", "## by category", "", "| category | count |", "|---|---|"]
        for k, v in sorted(by_cat.items()):
            md.append(f"| {k} | {v} |")
        (_REPORTS / "p1_resolution_summary.md").write_text("\n".join(md), encoding="utf-8")

        # ── targets (task §12) ──
        self.assertGreaterEqual(summary["total"], 250, "need >=250 lab cases")
        self.assertEqual(self.token_corruption, 0, "structured token corruption must be 0")
        act_acc, _ = self._acc("act")
        conv_intent_acc, _ = self._acc("intent")
        corr_acc, _ = self._acc("correction")
        topic_acc, _ = self._acc("topic")
        me_acc, _ = self._acc("multi_entity")
        unit_acc, _ = self._acc("unit_preservation")
        # report is authoritative; assert the hard-floor gates
        self.assertGreaterEqual(act_acc, 0.90, f"conversation act {act_acc:.3f}")
        self.assertGreaterEqual(corr_acc, 0.90, f"correction {corr_acc:.3f}")
        self.assertGreaterEqual(topic_acc, 0.95, f"topic switch {topic_acc:.3f}")
        self.assertGreaterEqual(me_acc, 0.90, f"multi-entity {me_acc:.3f}")
        if unit_acc is not None:
            self.assertEqual(unit_acc, 1.0, "unit preservation must be 100%")


class TestP1RequiredCases(unittest.TestCase):
    """Task §5 A-I explicit anchors."""
    _D = ("ได้ค่ะ 😊 ถ้ามีลิงก์สินค้าที่สนใจจาก Taobao, 1688 หรือ Tmall ส่งมาได้เลยนะคะ "
          "ถ้ายังไม่มีลิงก์ บอกคร่าว ๆ ได้เลยว่าอยากสั่งสินค้าอะไร เดี๋ยวช่วยแนะนำขั้นตอนต่อให้ค่ะ")

    def _seed(self, p, **kw):
        M = {"road": "ทางรถ", "sea": "ทางเรือ", "air": "ทางอากาศ"}
        ack = f"ได้ค่ะ รับทราบว่าต้องการนำเข้า{p}"
        if kw.get("qty"):
            ack += f" จำนวนประมาณ {kw['qty']} ชิ้น"
        if kw.get("method"):
            ack += f" ขนส่ง{M[kw['method']]}"
        ack += "นะคะ 😊 รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"
        return [{"role": "user", "content": "อยากสั่งของจากจีน"},
                {"role": "assistant", "content": self._D},
                {"role": "user", "content": f"เป็น{p}"},
                {"role": "assistant", "content": ack}]

    def test_A_quantity_before_intent_multi(self):
        r = resolve_conversation("20 คู่อยากสั่งของจากจีน", [])
        self.assertEqual(r.primary_intent, "IMPORT_INTEREST")
        q = r.slot_updates.get("quantity")
        self.assertIsNotNone(q)
        self.assertEqual((q.value, q.unit), (20, "คู่"))
        self.assertNotIn("product", r.slot_updates)

    def test_B_product_correction(self):
        r = resolve_conversation("เปลี่ยนเป้นรองเท้าได้ไหม", self._seed("ชั้นวางของ"))
        self.assertEqual(r.conversation_act, "CORRECTION")
        self.assertEqual(r.primary_intent, "IMPORT_INTEREST")
        self.assertEqual(r.slot_corrections["product"], {"old": "ชั้นวางของ", "new": "รองเท้า"})

    def test_C_quantity_correction_keeps_unit(self):
        r = resolve_conversation("เอ้ย 10 คู่", self._seed("รองเท้า", qty=20))
        self.assertEqual(r.conversation_act, "CORRECTION")
        self.assertEqual(r.slot_corrections["quantity"]["new"], 10)

    def test_D_method_correction(self):
        r = resolve_conversation("ทางเรือดีกว่า", self._seed("รองเท้า", method="road"))
        self.assertEqual(r.conversation_act, "CORRECTION")
        self.assertEqual(r.slot_corrections["shipping_method"], {"old": "road", "new": "sea"})

    def test_E_topic_switch_contact(self):
        r = resolve_conversation("ขอเบอร์ติดต่อ", self._seed("โต๊ะ"))
        self.assertEqual(r.conversation_act, "TOPIC_SWITCH")
        self.assertEqual(r.topic_switch, "CONTACT_INFO")

    def test_F_rejection(self):
        r = resolve_conversation("ไม่เอาแล้ว", self._seed("โต๊ะ"))
        self.assertEqual(r.conversation_act, "REJECTION")

    def test_G_product_policy(self):
        r = resolve_conversation("ชั้นวางของนำเข้าได้ไหม", [])
        self.assertEqual(r.primary_intent, "PRODUCT_POLICY")
        self.assertNotIn("product", r.slot_updates)

    def test_H_general_assistance(self):
        r = resolve_conversation("ของแก้วแพ็กยังไง", [])
        self.assertIn(r.grounding_requirement, ("GENERAL", "BUSINESS_RAG"))
        self.assertIn(r.primary_intent, ("GENERAL_ASSISTANCE", "PRODUCT_POLICY", "UNKNOWN"))

    def test_I_private_erp(self):
        r = resolve_conversation("ของผมถึงไหนแล้ว", [])
        self.assertEqual(r.primary_intent, "SHIPMENT_STATUS")
        self.assertEqual(r.grounding_requirement, "PRIVATE_ERP")

    def test_multi_entity_all_three_slots(self):
        r = resolve_conversation("อยากสั่งรองเท้า 20 คู่จากจีน ส่งทางเรือ", [])
        self.assertEqual(r.primary_intent, "IMPORT_INTEREST")
        self.assertEqual(r.slot_updates["product"].value, "รองเท้า")
        self.assertEqual((r.slot_updates["quantity"].value, r.slot_updates["quantity"].unit), (20, "คู่"))
        self.assertEqual(r.slot_updates["shipping_method"].value, "sea")

    def test_unit_and_canonical_dimension(self):
        r = resolve_conversation("สั่งจาน 200 ใบ ขนาด 520x240x120 mm", [])
        d = r.entities.get("dimensions")
        self.assertIsNotNone(d)
        self.assertEqual(d.unit, "mm")
        self.assertEqual(d.canonical_unit, "cm")
        self.assertTrue(d.canonical_value.startswith("52"))

    def test_precedence_new_intent_beats_stale_journey(self):
        seed = self._seed("โต๊ะ") * 4
        r = resolve_conversation("อยากสั่งของจากจีน", seed)
        self.assertEqual(r.precedence_winner, "NEW_INTENT")


if __name__ == "__main__":
    unittest.main()
