"""CUSTOMER-G16-G17-SELLER-SHIPPED-ORDER-STATUS-1 — response-mapping
keyword fix for `searchdataorder`.

Two data-only problems surfaced by CUS-G16 ("ร้านส่งหรือยังคะ") and
CUS-G17 ("ติดตามสถานะ สินค้า"), both routed correctly to a customer-
scoped `searchdataorder` read but answered with only the bare order
CODE, dropping the STATUS the customer actually asked about:

  1. The `$.data.Order.Code` row carried the keyword "PO". That is a
     substring of EVERY order value ("POS100820260809001" contains
     "po"), so Requested-Field Filtering
     (services.action_selection_primitives.select_requested_mapped_fields)
     always matched the Code row on the customer's own identifier and
     narrowed the reply to "เลขที่คำสั่งซื้อ: POS... ค่ะ" — an echo of
     the number the customer just supplied, never the status. Removing
     "PO" lets a bare-identifier turn match nothing, so the filter's
     "safe by construction" default returns the FULL record (Code +
     Status + Total + Tracking + DateConfirm) for the generic composer.

  2. The `$.data.Order.Status` row's keywords were only ["สถานะ",
     "status"], so a seller-dispatch / status-tracking question that
     never says the word "สถานะ" ("ร้านส่งหรือยัง", "ติดตามสถานะสินค้า")
     did not select the Status field. Add the source wordings.

Pure data fix — no code change. Idempotent (re-running re-applies the
same target state). Mirrors tools/fix_track_th11_tracking_th_field_keywords.py.

Usage:
    python -m tools.fix_g16g17_searchdataorder_status_keywords
"""
from admin.routes import get_sb
from services.business_action_registry import get_registry

ACTION_KEY = "searchdataorder"
_STATUS_ADD = {
    "ร้านส่ง", "ร้านจัดส่ง", "ร้านค้าส่ง", "ร้านจีนส่ง",
    "ติดตามสถานะ", "สถานะสินค้า", "สถานะคำสั่งซื้อ", "ติดตามสถานะสินค้า",
}
_CODE_DROP = {"po", "PO", "Po"}


def main():
    reg = get_registry(get_sb())
    action = reg.get_by_key(ACTION_KEY)
    if not action:
        raise SystemExit(f"Action {ACTION_KEY!r} not found — nothing to fix.")
    action_id = action["id"]
    rows = reg.get_response_mapping(action_id)
    changed = False
    for row in rows:
        jp = row["json_path"]
        kw = list((row.get("field_metadata") or {}).get("keywords") or [])
        before = list(kw)
        if jp.endswith(".Status"):
            kw = sorted(set(kw) | _STATUS_ADD)
        elif jp.endswith(".Code"):
            kw = [k for k in kw if str(k).strip() not in _CODE_DROP]
        if kw != before:
            row.setdefault("field_metadata", {})["keywords"] = kw
            changed = True
            print(f"  {jp}: {before}  ->  {kw}")
    if not changed:
        print(f"{ACTION_KEY!r} response-mapping keywords already up to date.")
        return
    reg.replace_response_mapping(action_id, [
        {"json_path": r["json_path"], "mapped_label": r["mapped_label"],
         "sort_order": r.get("sort_order", i), "field_metadata": r.get("field_metadata") or {}}
        for i, r in enumerate(rows)
    ])
    print(f"Fixed {ACTION_KEY!r}: Status keywords widened; over-broad 'PO' dropped from Code.")


if __name__ == "__main__":
    main()
