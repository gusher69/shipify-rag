"""Registers the 5 mock-provider-backed Business Actions (Business
Action Framework + ERP Sync Preparation milestone, Phases 2/3) via the
EXISTING Registry Service (services/business_action_registry.py) — no
new framework, same pattern as
tools/seed_business_action_getdatacustomer.py.

Each action is action_type="TOOL" (services/action_executor.py's
internal, deterministic executor — no HTTP endpoint, no secret) whose
execution_target matches a key in
services/action_executor.py::TOOL_REGISTRY, which dispatches to
services/business_action_providers.py::get_provider(<category>).lookup().
Today every category resolves to its Mock provider; a future real ERP
connector (Phase 7) is registered in business_action_providers.py only —
these action rows never change.

Usage:
    python -m tools.seed_business_action_providers
"""
from admin.routes import get_sb
from services.business_action_registry import get_registry

_ACTIONS = [
    {
        "action_key": "customer_lookup",
        "name": "Customer Lookup (Mock Provider)",
        "display_name": "ค้นหาข้อมูลลูกค้า (ผู้ให้บริการทดสอบ)",
        "description": "ค้นหาข้อมูลลูกค้าพื้นฐานจากรหัสลูกค้า อีเมล ชื่อ หรือเบอร์โทร ผ่าน Mock Customer Provider",
        "category": "customer",
        "ai_description": "ใช้เมื่อต้องการทดสอบการค้นหาข้อมูลลูกค้าโดยยังไม่เชื่อมต่อ ERP จริง",
        "search_keywords": ["ลูกค้า", "customer", "mock customer"],
        "tool": "customer_lookup",
        "params": [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False,
             "input_source": "customer_message", "example_value": "C00001"},
            {"name": "CustEmail", "display_name": "อีเมลลูกค้า", "required": False,
             "input_source": "customer_message", "example_value": "customer@example.com"},
            {"name": "CustName", "display_name": "ชื่อลูกค้า", "required": False,
             "input_source": "customer_message", "example_value": "สมชาย ใจดี"},
            {"name": "CustPhone", "display_name": "เบอร์โทรลูกค้า", "required": False,
             "input_source": "customer_message", "example_value": "0812345678"},
        ],
        "groups": [{"name": "customer_search_identifier", "rule": "AT_LEAST_ONE",
                    "members": ["CustCode", "CustEmail", "CustName", "CustPhone"]}],
    },
    {
        "action_key": "order_lookup",
        "name": "Order Lookup (Mock Provider)",
        "display_name": "ค้นหาข้อมูลออเดอร์ (ผู้ให้บริการทดสอบ)",
        "description": "ค้นหาสถานะและรายละเอียดออเดอร์จากเลขออเดอร์ ผ่าน Mock Order Provider",
        "category": "order",
        "ai_description": "ใช้เมื่อต้องการทดสอบการค้นหาสถานะออเดอร์โดยยังไม่เชื่อมต่อ ERP จริง",
        "search_keywords": ["ออเดอร์", "order", "mock order"],
        "tool": "order_lookup",
        "params": [
            {"name": "OrderCode", "display_name": "เลขออเดอร์", "required": True,
             "input_source": "customer_message", "example_value": "PO202601001"},
        ],
        "groups": [],
    },
    {
        "action_key": "tracking_lookup",
        "name": "Tracking Lookup (Mock Provider)",
        "display_name": "ติดตามพัสดุ (ผู้ให้บริการทดสอบ)",
        "description": "ตรวจสอบสถานะพัสดุจากเลขพัสดุ ผ่าน Mock Tracking Provider",
        "category": "tracking",
        "ai_description": "ใช้เมื่อต้องการทดสอบการติดตามพัสดุโดยยังไม่เชื่อมต่อ ERP จริง",
        "search_keywords": ["ติดตามพัสดุ", "tracking", "เลขพัสดุ", "mock tracking"],
        "tool": "tracking_lookup",
        "params": [
            {"name": "TrackingNumber", "display_name": "เลขพัสดุ", "required": True,
             "input_source": "customer_message", "example_value": "TH1234567890"},
        ],
        "groups": [],
    },
    {
        "action_key": "finance_lookup",
        "name": "Finance Lookup (Mock Provider)",
        "display_name": "ตรวจสอบข้อมูลการเงิน/ใบกำกับภาษี (ผู้ให้บริการทดสอบ)",
        "description": "ตรวจสอบสถานะการชำระเงินและใบกำกับภาษีจากเลขใบกำกับภาษีหรือเลขออเดอร์ ผ่าน Mock Finance Provider",
        "category": "finance",
        "ai_description": "ใช้เมื่อต้องการทดสอบการตรวจสอบข้อมูลการเงิน/ใบกำกับภาษีโดยยังไม่เชื่อมต่อ ERP จริง",
        "search_keywords": ["การเงิน", "ใบกำกับภาษี", "finance", "invoice", "payment", "mock finance"],
        "tool": "finance_lookup",
        "params": [
            {"name": "InvoiceNumber", "display_name": "เลขใบกำกับภาษี", "required": False,
             "input_source": "customer_message", "example_value": "INV202601001"},
            {"name": "OrderCode", "display_name": "เลขออเดอร์", "required": False,
             "input_source": "customer_message", "example_value": "PO202601001"},
        ],
        "groups": [{"name": "finance_reference", "rule": "AT_LEAST_ONE",
                    "members": ["InvoiceNumber", "OrderCode"]}],
    },
    {
        "action_key": "product_lookup",
        "name": "Product Lookup (Mock Provider)",
        "display_name": "ค้นหาข้อมูลสินค้า (ผู้ให้บริการทดสอบ)",
        "description": "ค้นหาข้อมูลสต็อกและราคาสินค้าจากรหัสสินค้า (SKU) ผ่าน Mock Product Provider",
        "category": "product",
        "ai_description": "ใช้เมื่อต้องการทดสอบการค้นหาข้อมูลสินค้า/สต็อกโดยยังไม่เชื่อมต่อ ERP จริง",
        "search_keywords": ["สินค้า", "สต็อก", "product", "inventory", "sku", "mock product"],
        "tool": "product_lookup",
        "params": [
            {"name": "SKU", "display_name": "รหัสสินค้า", "required": True,
             "input_source": "customer_message", "example_value": "SKU-001"},
        ],
        "groups": [],
    },
]


def main():
    reg = get_registry(get_sb())
    for spec in _ACTIONS:
        existing = reg.get_by_key(spec["action_key"])
        if existing:
            action_id = existing["id"]
            print(f"Reusing existing action {spec['action_key']!r} ({action_id})")
        else:
            action = reg.create({
                "action_key": spec["action_key"],
                "name": spec["name"],
                "display_name": spec["display_name"],
                "description": spec["description"],
                "action_type": "TOOL",
                "category": spec["category"],
                "priority": 10,
                "enabled": True,
                "ai_description": spec["ai_description"],
                "search_keywords": spec["search_keywords"],
            }, created_by="seed_script")
            action_id = action["id"]
            print(f"Created action {spec['action_key']!r} ({action_id})")

        reg.replace_parameters(action_id, spec["params"])
        if spec["groups"]:
            reg.set_parameter_groups(action_id, spec["groups"])
        reg.upsert_execution(action_id, {"execution_target": spec["tool"]})
        reg.replace_tags(action_id, [spec["category"], "mock_provider", "phase_provider_framework"])
        print(f"  -> {spec['action_key']} wired to TOOL_REGISTRY['{spec['tool']}'] "
              f"(services/business_action_providers.py category={spec['category']!r})")

    print("\nAll 5 mock-provider Business Actions ready. "
          "Real ERP connectors are Phase 7 future work — see services/business_action_providers.py.")


if __name__ == "__main__":
    main()
