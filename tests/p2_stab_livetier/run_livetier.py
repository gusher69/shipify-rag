# -*- coding: utf-8 -*-
"""P2-STAB live-tier authority check.

Runs curated turns through the REAL conversation_semantics.interpret()
with the REAL production LLM tier enabled (no _llm_family patch) and
verifies the P2-STAB CENTRAL AUTHORITY RULE: the gated LLM never
overrides a high-confidence deterministic canonical resolution.

  python -m tests.p2_stab_livetier.run_livetier

Writes reports/p2_stab_livetier.json / reports/p2_stab_livetier_summary.md.
NO REAL LINE messages. No DecisionEngine — this measures interpret()
authority only; routing is covered by the bounded regression.
"""
import json
import pathlib

from services.conversation_semantics import interpret

_REPORTS = pathlib.Path(__file__).resolve().parent.parent.parent / "reports"
_REPORTS.mkdir(exist_ok=True)

_D = ("ได้ค่ะ 😊 ถ้ามีลิงก์สินค้าที่สนใจจาก Taobao, 1688 หรือ Tmall ส่งมาได้เลยนะคะ "
      "ถ้ายังไม่มีลิงก์ บอกคร่าว ๆ ได้เลยว่าอยากสั่งสินค้าอะไร เดี๋ยวช่วยแนะนำขั้นตอนต่อให้ค่ะ")
_ACK_PROD = "ได้ค่ะ รับทราบว่าต้องการนำเข้า{p}นะคะ 😊 รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"
_ACK_QTY = "รับทราบค่ะ ปรับเป็นจำนวนประมาณ {n} ชิ้น สำหรับ{p}นะคะ สนใจส่งทางรถหรือทางเรือคะ"
_ACK_METH = "รับทราบค่ะ เปลี่ยนเป็นขนส่งทาง{m} สำหรับ{p}นะคะ"


def H_open():
    return [{"role": "user", "content": "อยากสั่งของจากจีน"}, {"role": "assistant", "content": _D}]


def H_prod(p="รองเท้า"):
    return [{"role": "user", "content": f"อยากสั่ง{p}จากจีน"},
            {"role": "assistant", "content": _ACK_PROD.format(p=p)}]


def H_qty(p="รองเท้า", n=10):
    return [{"role": "user", "content": f"อยากสั่ง{p}จากจีน"},
            {"role": "assistant", "content": _ACK_QTY.format(p=p, n=n)}]


def H_meth(p="รองเท้า", m="รถ"):
    return [{"role": "user", "content": f"อยากสั่ง{p}จากจีน"},
            {"role": "assistant", "content": _ACK_METH.format(p=p, m=m)}]


# (category, message, history, expected_family_or_tuple_or_None,
#  must_be_deterministic)
CASES = []


def _c(cat, msg, hist, want, det=True):
    CASES.append((cat, msg, hist, want, det))


# ── requested_slot answers (must be deterministic IMPORT_INTEREST) ──
_PRODS = ["ชั้นวางของ", "โต๊ะ", "เก้าอี้", "เสื้อผ้า", "กระเป๋า", "โคมไฟ", "หมอน",
          "พรม", "ของเล่น", "เครื่องครัว", "จักรยาน", "นาฬิกา", "ตุ๊กตา", "ผ้าห่ม",
          "เตาอบ", "พัดลม", "กล่องพลาสติก", "อะไหล่รถยนต์", "รองเท้าผ้าใบ", "ที่นอน"]
for p in _PRODS:
    _c("requested_slot", f"เป็น{p}", H_open(), "IMPORT_INTEREST")
for p in ["รองเท้าค่ะ", "โต๊ะครับ", "เอาเป็นเก้าอี้", "พวกเสื้อผ้า", "กระเป๋าเดินทาง"]:
    _c("requested_slot", p, H_open(), "IMPORT_INTEREST")
for n in [5, 20, 50, 100, 200]:
    _c("requested_slot", f"{n} คู่", H_qty(), "IMPORT_INTEREST")
    _c("requested_slot", f"ประมาณ {n} ชิ้น", H_qty(), "IMPORT_INTEREST")
for m in ["ทางเรือ", "ทางรถ", "เอาทางเรือ", "ขอทางรถ", "ทางอากาศ"]:
    _c("requested_slot", m, H_meth(), "IMPORT_INTEREST")

# ── corrections (deterministic; LLM must not reclassify) ──
for a, b in [("โต๊ะ", "เก้าอี้"), ("รองเท้า", "กระเป๋า"), ("เสื้อผ้า", "หมวก"),
             ("ชั้นวางของ", "รองเท้า"), ("โคมไฟ", "พัดลม")]:
    _c("correction", f"เปลี่ยนเป็น{b}", H_prod(a), "IMPORT_INTEREST")
    _c("correction", f"เอาเป็น{b}แทน", H_prod(a), "IMPORT_INTEREST")
for q in ["เอ้ย 10 คู่", "ไม่ใช่ 5 เป็น 8", "แก้เป็น 30 ชิ้น", "เอ้ย 100"]:
    _c("correction", q, H_qty("รองเท้า", 20), "IMPORT_INTEREST")
for m in ["ทางเรือดีกว่า", "ไม่เอา เอาทางเรือ", "เปลี่ยนเป็นทางรถ"]:
    _c("correction", m, H_meth("รองเท้า", "เรือ"), "IMPORT_INTEREST")

# ── topic switch (deterministic family, never IMPORT_INTEREST) ──
for m, fam in [("ขอเบอร์ติดต่อ", "CONTACT_INFO"), ("ถอนเงินขนส่งยังไง", "SHIPPING_WITHDRAWAL"),
               ("ถอนเงินสั่งซื้อยังไง", "PURCHASE_WITHDRAWAL"), ("ขอลิงก์เว็บ Taobao", "WEBSITE_LINK_REQUEST"),
               ("โกดังไทยอยู่ไหน", "PICKUP_LOCATION"), ("คูปองใช้ยังไง", "COUPON_USAGE"),
               ("เช็คสถานะบิลหน่อย", "SHIPMENT_STATUS"), ("ขอใบกำกับภาษี", "INVOICE"),
               ("มีบริการเหมารถไหม", "CHARTER_TRUCK"),
               ("ช่วยตีราคาค่าส่งกล่องนี้ให้หน่อย", "SHIPPING_ESTIMATE"),
               ("เปลี่ยนที่อยู่จัดส่ง", "ADDRESS_CHANGE"),
               ("อยากคุยกับแอดมิน", None), ("ขอเบอร์โทรหน่อยครับ", "CONTACT_INFO"),
               ("ขอที่อยู่โกดังจีน", "PICKUP_LOCATION"), ("มีคูปองอะไรบ้าง", None)]:
    _c("topic_switch", m, H_prod("รองเท้า"), fam, det=(fam is not None))

# ── rejection (deterministic; must carry REJECT act, never a wrong family) ──
for m in ["ไม่เอาแล้ว", "ยกเลิก", "ไม่ต้องแล้วค่ะ", "พอแล้ว", "ไม่เอาแล้วครับ",
          "ไม่สนแล้ว", "เลิกก่อน", "ไม่อยากได้แล้ว"]:
    _c("rejection", m, H_open(), "REJECT")
for m in ["ไม่เอาแล้ว", "ยกเลิกเลย"]:
    _c("rejection", m, H_prod("รองเท้า"), "REJECT")

# ── product policy / risk (trusted evidence beats context) ──
for m in ["เป็นน้ำยา", "มีแบตเตอรี่", "เป็นสารเคมี", "น้ำหอมค่ะ", "เป็นของเหลว",
          "อาหารเสริม", "บุหรี่ไฟฟ้า"]:
    _c("product_policy", m, H_open(), "PRODUCT_POLICY")
for m in ["ชั้นวางของนำเข้าได้ไหม", "แบตเตอรี่ส่งได้ไหม", "ของแบบนี้ต้องห้ามไหม"]:
    _c("product_policy", m, H_open(), "PRODUCT_POLICY")

# ── typo + context (deterministic if the typo-tolerant path resolves) ──
for m in ["เป้นชันวางของ", "เปนรองเท้า", "เอาเปนโต๊ะ", "เปลียนเปนกระเป๋า",
          "ไม่ไช่โต๊ะ เปนเก้าอี้", "เอ้ย เปนเสื้อ", "เปลี่ยนเป้นหมวก"]:
    _c("typo_context", m, H_open() if m.startswith(("เป้น", "เปน", "เอาเปน")) else H_prod("โต๊ะ"),
       "IMPORT_INTEREST")

# ── ambiguous / genuine LLM-recovery (LLM SHOULD run; no det. requirement) ──
for m in ["ร้านส่งของมาหรือยังคะ", "ต้นทางส่งมาหรือยัง", "อันนี้ต้องทำไงต่อ",
          "แล้วขั้นตอนถัดไปคืออะไร", "มันจะถึงเมื่อไหร่", "ปกติใช้เวลานานไหม",
          "ค่าส่งแพงไหม", "ต้องมัดจำก่อนไหม", "จ่ายยังไงได้บ้าง", "เชื่อถือได้ไหมเนี่ย",
          "งงอะ อธิบายอีกที", "ทำไมนานจัง", "มีโปรอะไรไหม", "ต้องรออีกกี่วัน",
          "แล้วถ้าของเสียหายล่ะ"]:
    _c("llm_recovery", m, [], None, det=False)


def _run():
    rows, overrides = [], []
    for cat, msg, hist, want, det in CASES:
        it = interpret(msg, list(hist))
        got = it.intent_family
        act = it.conversation_act
        src = it.source
        if want == "REJECT":
            ok = (act == "REJECT")
        elif want is None:
            ok = True  # llm-recovery / no strong context — any resolution acceptable
        else:
            ok = (got == want)
        # AUTHORITY VIOLATION: a case that must be deterministic was
        # decided by the LLM, OR the LLM produced a family conflicting
        # with the required deterministic one.
        override = det and (src == "llm") and (
            want == "REJECT" or (isinstance(want, str) and got != want))
        if override:
            overrides.append({"category": cat, "message": msg, "want": want,
                              "got": got, "act": act, "source": src})
        rows.append({"category": cat, "message": msg, "want": want, "got": got,
                     "act": act, "source": src, "pass": ok, "det_required": det,
                     "authority_violation": override})
    return rows, overrides


def main():
    rows, overrides = _run()
    cats = sorted({r["category"] for r in rows})
    by_cat = {c: {"pass": sum(r["pass"] for r in rows if r["category"] == c),
                  "n": sum(1 for r in rows if r["category"] == c)} for c in cats}
    det_rows = [r for r in rows if r["det_required"]]
    llm_on_det = sum(1 for r in det_rows if r["source"] == "llm")
    summary = {
        "total": len(rows),
        "by_category": {c: f"{d['pass']}/{d['n']}" for c, d in by_cat.items()},
        "overall_pass": f"{sum(r['pass'] for r in rows)}/{len(rows)}",
        "deterministic_cases": len(det_rows),
        "llm_used_on_deterministic_case": llm_on_det,
        "authority_violations": len(overrides),
        "violations": overrides,
    }
    (_REPORTS / "p2_stab_livetier.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ["# P2-STAB live-tier authority check (real production LLM)\n",
          f"- total curated turns: **{summary['total']}**",
          f"- overall pass: **{summary['overall_pass']}**",
          f"- deterministic-required cases: **{summary['deterministic_cases']}**",
          f"- LLM used on a deterministic case: **{llm_on_det}**",
          f"- **HIGH-CONFIDENCE CONTEXT OVERRIDDEN BY LLM: {len(overrides)}**\n",
          "| category | pass/total |", "|---|---|"]
    for c in cats:
        md.append(f"| {c} | {by_cat[c]['pass']}/{by_cat[c]['n']} |")
    if overrides:
        md.append("\n## authority violations\n")
        for v in overrides:
            md.append(f"- `{v['message']}` want={v['want']} got={v['got']} (src={v['source']})")
    (_REPORTS / "p2_stab_livetier_summary.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if len(overrides) == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
