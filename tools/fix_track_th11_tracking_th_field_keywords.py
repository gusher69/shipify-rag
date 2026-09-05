"""CUSTOMER-TRACK-TH-1.1 — adds the plain-Thai transliteration keywords
to `searchdatashipment`'s TrackingTH response_mapping row.

Without this, a genuine immediate follow-up phrased in plain Thai
("แทรคไทยมีหรือยังคะ" — "has the Thai tracking arrived yet") could not
be matched by `services.decision_engine._resolve_conversation_reference`'s
own field-name-match condition (a) — its field_metadata.keywords only
listed the English-loanword forms ("tracking ไทย", "tracking thailand"),
never the plain-Thai transliteration ("แทรคไทย"/"แทร็กไทย") the customer
actually used. `_resolve_conversation_reference` already reuses this
EXACT config-driven vocabulary (Requested Field Filtering's own
field_metadata.keywords) as evidence of "still talking about the same
record" — this is a pure data fix, no code change.

Idempotent — re-running simply re-applies the same target state.

Usage:
    python -m tools.fix_track_th11_tracking_th_field_keywords
"""
from admin.routes import get_sb
from services.business_action_registry import get_registry

ACTION_KEY = "searchdatashipment"


def main():
    reg = get_registry(get_sb())
    action = reg.get_by_key(ACTION_KEY)
    if not action:
        raise SystemExit(f"Action {ACTION_KEY!r} not found — nothing to fix.")
    action_id = action["id"]
    rows = reg.get_response_mapping(action_id)
    changed = False
    for row in rows:
        if row["json_path"].endswith("TrackingTH"):
            kw = set((row.get("field_metadata") or {}).get("keywords") or [])
            before = set(kw)
            kw |= {"แทรคไทย", "แทร็กไทย", "แทรกไทย", "ติดตามไทย"}
            if kw != before:
                row.setdefault("field_metadata", {})["keywords"] = sorted(kw)
                changed = True
    if not changed:
        print(f"{ACTION_KEY!r} TrackingTH keywords already up to date.")
        return
    reg.replace_response_mapping(action_id, [
        {"json_path": r["json_path"], "mapped_label": r["mapped_label"],
         "sort_order": r.get("sort_order", i), "field_metadata": r.get("field_metadata") or {}}
        for i, r in enumerate(rows)
    ])
    print(f"Fixed {ACTION_KEY!r}: TrackingTH keywords now include the plain-Thai transliteration forms.")


if __name__ == "__main__":
    main()
