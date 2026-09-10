# -*- coding: utf-8 -*-
"""P2 structured conversation frame — SHADOW-WRITE lab + core assertions.

Threads each corpus sequence through
resolve_conversation -> conversation_frame_store.build_frame and checks
the FINAL structured frame + parity vs the legacy text-derived frame.
Writes reports/p2_frame_state.json / p2_frame_parity.json /
p2_frame_summary.md.

The gated LLM disambiguation in conversation_semantics is FORCED OFF here
(patched to None) so the lab is deterministic regardless of any real key
present in the environment — P2 logic must be sound on the deterministic
tier alone.
"""
import json
import os
import pathlib
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-p2lab")

import services.conversation_semantics as _cs
from services.conversation_resolution import resolve_conversation
from services.conversation_frame_store import build_frame, frame_parity, empty_frame
from tests.p2_frame_lab.corpus import CASES

_REPORTS = pathlib.Path(__file__).resolve().parent.parent / "reports"
_REPORTS.mkdir(exist_ok=True)


def _thread(turns):
    """Run a turn sequence; return (final_frame, [parity per turn], [resolutions])."""
    hist, frame, parities, resolutions = [], None, [], []
    for t in turns:
        res = resolve_conversation(t["u"], list(hist))
        legacy = _cs.derive_active_frame(list(hist))
        frame = build_frame(frame, res)
        parities.append(frame_parity(legacy, frame, res))
        resolutions.append(res)
        hist += [{"role": "user", "content": t["u"]},
                 {"role": "assistant", "content": t.get("a") or "..."}]
    return frame, parities, resolutions


def _sv(frame, name):
    return ((frame.get("slots") or {}).get(name) or {})


class TestP2FrameLab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._p = patch.object(_cs, "_llm_family", return_value=None)
        cls._p.start()
        cls.rows = []
        cls.parity_rows = []
        for c in CASES:
            frame, parities, _ = _thread(c["turns"])
            exp = c["expect"]
            checks = {}
            if "journey" in exp:
                checks["journey"] = (frame.get("journey") == exp["journey"])
            if "status" in exp:
                checks["status"] = (frame.get("status") == exp["status"])
            if "slots" in exp:
                ok = True
                for name, (val, unit) in exp["slots"].items():
                    s = _sv(frame, name)
                    ok = ok and (str(s.get("value")) == str(val))
                    if unit is not None:
                        ok = ok and (s.get("unit") == unit)
                checks["slots"] = ok
            if "unit" in exp:
                checks["unit"] = (_sv(frame, "quantity").get("unit") == exp["unit"][0])
            if "missing" in exp:
                filled = set((frame.get("slots") or {}).keys())
                checks["missing"] = all(m not in filled for m in exp["missing"])
            if exp.get("no_stale_takeover"):
                # a fresh opener after stale history: product must be the
                # FRESH one, never a leftover from the seeded rounds.
                checks["no_stale_takeover"] = (
                    _sv(frame, "product").get("value") == exp["slots"]["product"][0])
            if exp.get("weight_canonical_kg") is not None:
                checks["weight_canonical"] = (
                    _sv(frame, "weight").get("canonical_value") == exp["weight_canonical_kg"])
            if exp.get("dim_canonical"):
                checks["dim_canonical"] = (
                    _sv(frame, "dimensions").get("canonical_value") == exp["dim_canonical"])
            cls.rows.append({"id": c["id"], "category": c["category"],
                             "checks": checks, "pass": all(checks.values()) if checks else True,
                             "final": {"journey": frame.get("journey"), "status": frame.get("status"),
                                       "slots": {k: (v.get("value"), v.get("unit"))
                                                 for k, v in (frame.get("slots") or {}).items()}}})
            for pr in parities:
                cls.parity_rows.append({"id": c["id"], "class": pr["class"],
                                        "unit_preserved": pr["unit_preserved"]})

    @classmethod
    def tearDownClass(cls):
        cls._p.stop()

    def _rate(self, cat=None):
        rows = [r for r in self.rows if cat is None or r["category"] == cat]
        if not rows:
            return 1.0, 0
        return sum(r["pass"] for r in rows) / len(rows), len(rows)

    def test_frame_parity(self):
        cls = {}
        for pr in self.parity_rows:
            cls[pr["class"]] = cls.get(pr["class"], 0) + 1
        total = len(self.parity_rows)
        wrong = cls.get("STRUCTURED_WRONG", 0)
        agree = cls.get("MATCH", 0) + cls.get("STRUCTURED_IMPROVEMENT", 0)
        parity_pct = agree / total if total else 1.0
        unit_ok = all(p["unit_preserved"] for p in self.parity_rows)
        summary = {
            "cases": len(self.rows), "turns_scored": total,
            "parity_pct": round(parity_pct * 100, 2),
            "classes": cls,
            "structured_wrong": wrong,
            "unit_preservation_pct": 100.0 if unit_ok else round(
                100 * sum(p["unit_preserved"] for p in self.parity_rows) / total, 2),
            "by_category": {c: {"pct": round(self._rate(c)[0] * 100, 2), "n": self._rate(c)[1]}
                            for c in sorted({r["category"] for r in self.rows})},
            "overall_case_pct": round(self._rate()[0] * 100, 2),
        }
        (_REPORTS / "p2_frame_state.json").write_text(
            json.dumps({"summary": summary, "rows": self.rows}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        (_REPORTS / "p2_frame_parity.json").write_text(
            json.dumps({"summary": summary, "parity": cls}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        md = ["# P2 structured conversation frame — shadow lab\n",
              f"- cases: **{summary['cases']}**   turns scored: **{total}**",
              f"- FRAME PARITY (MATCH + STRUCTURED_IMPROVEMENT): **{summary['parity_pct']}%**",
              f"- STRUCTURED_WRONG: **{wrong}**",
              f"- UNIT PRESERVATION: **{summary['unit_preservation_pct']}%**",
              f"- overall case pass: **{summary['overall_case_pct']}%**\n",
              "| class | count |", "|---|---|"]
        for k, v in sorted(cls.items()):
            md.append(f"| {k} | {v} |")
        md.append("\n| category | pass % | n |\n|---|---|---|")
        for c, d in summary["by_category"].items():
            md.append(f"| {c} | {d['pct']} | {d['n']} |")
        (_REPORTS / "p2_frame_summary.md").write_text("\n".join(md), encoding="utf-8")

        failed = [r["id"] for r in self.rows if not r["pass"]]
        self.assertEqual(wrong, 0, f"STRUCTURED_WRONG on protected cases: "
                                   f"{[p for p in self.parity_rows if p['class']=='STRUCTURED_WRONG'][:10]}")
        self.assertGreaterEqual(parity_pct, 0.99, f"frame parity {parity_pct:.3f} < 0.99")
        self.assertTrue(unit_ok, "unit preservation < 100%")
        self.assertEqual(failed, [], f"lab case failures: {failed}")

    # ── core assertions (task §10 replays A-G) ──
    def _final(self, cid):
        c = next(x for x in CASES if x["id"] == cid)
        f, _, _ = _thread(c["turns"])
        return f

    def test_A_quantity_unit_kept_product_missing(self):
        f = self._final("A")
        self.assertEqual(_sv(f, "quantity").get("value"), 20)
        self.assertEqual(_sv(f, "quantity").get("unit"), "คู่")
        self.assertNotIn("product", f.get("slots") or {})
        self.assertEqual(f.get("status"), "ACTIVE")

    def test_B_product_added_quantity_kept(self):
        f = self._final("B")
        self.assertEqual(_sv(f, "product").get("value"), "ชั้นวางของ")
        self.assertEqual(_sv(f, "quantity").get("unit"), "คู่")

    def test_C_product_correction_keeps_quantity(self):
        f = self._final("C")
        self.assertEqual(_sv(f, "product").get("value"), "รองเท้า")
        self.assertEqual(_sv(f, "quantity").get("value"), 20)
        self.assertEqual(_sv(f, "quantity").get("unit"), "คู่")

    def test_D_quantity_correction_keeps_unit(self):
        f = self._final("D")
        self.assertEqual(_sv(f, "quantity").get("value"), 10)
        self.assertEqual(_sv(f, "quantity").get("unit"), "คู่")
        self.assertEqual(_sv(f, "product").get("value"), "รองเท้า")

    def test_E_method_correction_road_to_sea(self):
        f = self._final("E")
        self.assertEqual(_sv(f, "shipping_method").get("value"), "sea")
        self.assertEqual(_sv(f, "product").get("value"), "รองเท้า")

    def test_F_rejection_cancels(self):
        self.assertEqual(self._final("F").get("status"), "CANCELLED")

    def test_G_topic_switch_suspends_not_mutates(self):
        f = self._final("G")
        self.assertEqual(f.get("status"), "SUSPENDED")
        self.assertEqual(_sv(f, "product").get("value"), "รองเท้า")

    def test_long_history_no_stale_takeover(self):
        for cid in ("LH-empty", "LH-30turn", "LH-50turn", "LH-100turn"):
            f = self._final(cid)
            self.assertEqual(_sv(f, "product").get("value"), "รองเท้า", cid)
            self.assertEqual(_sv(f, "quantity").get("unit"), "คู่", cid)
            self.assertEqual(f.get("status"), "ACTIVE", cid)

    def test_cancel_then_new_starts_clean(self):
        f = self._final("LC-cancel-then-new")
        self.assertEqual(_sv(f, "product").get("value"), "เก้าอี้")
        self.assertEqual(f.get("status"), "ACTIVE")

    def test_non_journey_turns_create_no_frame(self):
        for cid in ("NJ00", "NJ01", "NJ02", "NJ03", "NJ04", "NJ05"):
            self.assertIsNone(self._final(cid).get("journey"), cid)

    def test_dimension_unit_canonical(self):
        from services.conversation_resolution import extract_entities
        e = extract_entities("ขนาด 520 x 220 x 110 mm")
        d = e.get("dimensions")
        self.assertIsNotNone(d)
        self.assertEqual(d.raw.replace(" ", ""), "520x220x110mm")
        self.assertEqual(d.canonical_value, "52x22x11")
        self.assertEqual(d.canonical_unit, "cm")
        w = extract_entities("หนัก 5000 กรัม").get("weight")
        self.assertEqual(w.canonical_value, 5.0)
        self.assertEqual(w.canonical_unit, "kg")
        self.assertEqual(w.unit, "กรัม")  # raw semantic unit kept


class TestP2StoreUnit(unittest.TestCase):
    def test_empty_frame_shape(self):
        f = empty_frame()
        self.assertEqual(f["version"], 1)
        self.assertIsNone(f["journey"])
        self.assertEqual(f["slots"], {})

    def test_build_frame_never_mutates_prev(self):
        prev = {"version": 1, "journey": "IMPORT_INTEREST", "status": "ACTIVE",
                "requested_slot": "quantity", "slots": {"product": {"value": "โต๊ะ"}},
                "turn_seq": 3}

        class _R:
            conversation_act = "REJECTION"
            primary_intent = "UNKNOWN"
            precedence_winner = "CORRECTION_REJECTION_TOPIC"
            slot_updates = {}
            slot_corrections = {}
            requested_slot = None
            known_slots = {}
        out = build_frame(prev, _R())
        self.assertEqual(prev["status"], "ACTIVE")       # unchanged
        self.assertEqual(out["status"], "CANCELLED")

    def test_parity_none_vs_none_is_match(self):
        self.assertEqual(frame_parity(None, empty_frame())["class"], "MATCH")


if __name__ == "__main__":
    unittest.main()
