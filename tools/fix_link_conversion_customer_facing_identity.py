"""CUSTOMER-LINK-1 — corrects `geturlproductdetail`'s CustCode parameter
so Link Conversion is PUBLIC (never asks the customer for a code, never
blocks an unverified/anonymous customer), without breaking the wire
contract if the real upstream API happens to accept the field.

Customer requirement (tests/customer_uat/customer_uat_master.jsonl
CUS-P20, linked CUS-G29/CUS-S20; corroborated by the PDF reviewer's own
annotation in docs/customer_uat_sources/CUSTOMER_UAT_SOURCE_MANIFEST.md:
"don't require identity for link conversion"): "Link conversion is NOT
an internal-data check — it must NOT require a customer code or
identity verification."

Root cause (both must change together — verified live against the
production registry + services/authorization_service.py before this
script was written):
  1. `CustCode.required = True` blocked the customer-facing collection
     loop (asked "กรุณาแจ้งรหัสลูกค้าค่ะ" before anything else).
  2. `CustCode.input_source = "customer_message"` ALSO made
     `authorization_service.requires_verified_identity()` treat this
     action as identity-gated regardless of `required` (it checks
     input_source, not required — see that function's own docstring,
     which already names GetUrlProductDetail explicitly) — an
     unverified/anonymous customer's request was denied outright by the
     Authorization Gate, never even reaching parameter collection.

Fix: `input_source` -> "customer_profile" (best-effort: if the current
LINE user IS a verified, bound customer — services/customer_binding_
service.py / customer_channel_bindings, never user_profiles.cust_code —
their own cust_code is supplied automatically via context["customer_
context"], see services/action_executor.py::_resolve_param_value's
customer_profile branch); `required` -> False (never blocks an
anonymous customer, and never enters the customer-facing ask-loop
either way, since `customer_profile` is not customer_message). The
parameter itself, and the real wire field name "CustCode", are
preserved unchanged — if the upstream genuinely still requires SOME
value here, that is now honestly surfaced as a real UPSTREAM_FAILURE /
CONVERSION_NOT_FOUND_OR_REJECTED result (services/link_conversion_
flow.py::classify_conversion_result) rather than silently invented.

Also gives the URL parameter (input_source="system_generated", never
customer-facing-collectable by name) a real customer-facing follow-up
question instead of its admin-internal description note, so
`_generate_parameter_question` (services/decision_engine.py) asks the
customer for the link in the customer-approved wording (CUS-P20 Case 1)
instead of echoing "ลิงก์สินค้าจาก 1688, Taobao หรือ Tmall (ดึงจาก
ข้อความอัตโนมัติ)" verbatim as a question.

Idempotent — re-running this script simply re-applies the same target
state; safe to run more than once.

Usage:
    python -m tools.fix_link_conversion_customer_facing_identity
"""
from admin.routes import get_sb
from services.business_action_registry import get_registry

ACTION_KEY = "geturlproductdetail"


def main():
    reg = get_registry(get_sb())
    action = reg.get_by_key(ACTION_KEY)
    if not action:
        raise SystemExit(f"Action {ACTION_KEY!r} not found — nothing to fix.")
    action_id = action["id"]
    params = reg.get_parameters(action_id)
    by_name = {p["name"]: p for p in params}
    if set(by_name) != {"SecretCode", "CustCode", "URL"}:
        raise SystemExit(f"Unexpected parameter set for {ACTION_KEY!r}: {sorted(by_name)} "
                          "— refusing to guess; update this script after reviewing the change.")

    secret = by_name["SecretCode"]
    custcode = dict(by_name["CustCode"])
    url = dict(by_name["URL"])

    custcode["input_source"] = "customer_profile"
    custcode["required"] = False
    # No longer a customer-typed field, but keep it developer-visible so
    # a best-effort auto-supplied value is still inspectable in dev mode.
    custcode["visible_to_customer"] = False

    url["description"] = "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ"

    ordered = [
        {"name": "SecretCode", "display_name": secret.get("display_name"),
         "required": secret.get("required", True), "input_source": secret.get("input_source"),
         "credential_ref": secret.get("credential_ref"), "send_as": secret.get("send_as", "form"),
         "visible_to_customer": secret.get("visible_to_customer", False),
         "visible_in_developer_mode": secret.get("visible_in_developer_mode", False),
         "loggable": secret.get("loggable", False), "sort_order": secret.get("sort_order", 0)},
        {"name": "CustCode", "display_name": custcode.get("display_name"),
         "required": custcode["required"], "input_source": custcode["input_source"],
         "validation_pattern": custcode.get("validation_pattern"),
         "send_as": custcode.get("send_as", "form"),
         "visible_to_customer": custcode["visible_to_customer"],
         "visible_in_developer_mode": custcode.get("visible_in_developer_mode", True),
         "loggable": custcode.get("loggable", True), "sort_order": custcode.get("sort_order", 1)},
        {"name": "URL", "display_name": url.get("display_name"),
         "required": url.get("required", True), "input_source": url.get("input_source"),
         "description": url["description"], "send_as": url.get("send_as", "form"),
         "visible_to_customer": url.get("visible_to_customer", True),
         "visible_in_developer_mode": url.get("visible_in_developer_mode", True),
         "loggable": url.get("loggable", True), "sort_order": url.get("sort_order", 2)},
    ]
    reg.replace_parameters(action_id, ordered)
    print(f"Fixed {ACTION_KEY!r} ({action_id}): "
          f"CustCode input_source=customer_profile required=False; "
          f"URL description set to the customer-facing ask.")


if __name__ == "__main__":
    main()
