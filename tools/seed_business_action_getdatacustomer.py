"""Registers the "GetDataCustomer — Customer Data Lookup" Business
Action via the existing Registry Service (services/
business_action_registry.py) — never raw SQL, never a hardcoded
secret value. The real SecretCode value lives ONLY in the
FASTTRADE_AI_CHAT_SECRET_CODE environment variable (outside source
control); this script stores just that ENV VAR NAME as a secret_ref,
never the value itself.

Usage:
    python -m tools.seed_business_action_getdatacustomer
"""
from admin.routes import get_sb
from services.business_action_registry import get_registry

ACTION_KEY = "customer_data_lookup"
SECRET_REF = "FASTTRADE_AI_CHAT_SECRET_CODE"  # env var NAME only — see .env.example


def main():
    reg = get_registry(get_sb())
    existing = reg.get_by_key(ACTION_KEY)
    if existing:
        action_id = existing["id"]
        print(f"Reusing existing action {ACTION_KEY!r} ({action_id})")
    else:
        action = reg.create({
            "action_key": ACTION_KEY,
            "name": "GetDataCustomer",
            "display_name": "ค้นหาข้อมูลลูกค้า",
            "description": "ค้นหาข้อมูลลูกค้าจากรหัสลูกค้า อีเมล ชื่อ หรือเบอร์โทร และคืนข้อมูลลูกค้า Wallet และคูปอง",
            "action_type": "API",
            "category": "customer",
            "priority": 10,  # same convention as other real customer-facing lookups in this platform
            "enabled": True,
            "ai_description": (
                "ใช้ Action นี้เมื่อลูกค้าหรือเจ้าหน้าที่ต้องการค้นหาข้อมูลเฉพาะของลูกค้า เช่น ข้อมูลสมาชิก "
                "โปรไฟล์ลูกค้า Wallet ยอดเงินในกระเป๋า หรือคูปอง โดยต้องมีข้อมูลสำหรับค้นหาอย่างน้อยหนึ่งรายการ "
                "ได้แก่ รหัสลูกค้า อีเมล ชื่อ หรือเบอร์โทร"
            ),
            "search_keywords": ["ลูกค้า", "สมาชิก", "customer", "member", "wallet", "coupon", "คูปอง",
                                 "ข้อมูลลูกค้า", "โปรไฟล์ลูกค้า", "รหัสลูกค้า"],
        }, created_by="seed_script")
        action_id = action["id"]
        print(f"Created action {ACTION_KEY!r} ({action_id})")

    reg.replace_examples(action_id, [
        {"example_text": q} for q in (
            "เช็คข้อมูลลูกค้าให้หน่อย", "ขอดูข้อมูลสมาชิก", "ลูกค้ารหัส C00001 คือใคร",
            "เช็ค Wallet ของลูกค้ารหัส C00001", "ลูกค้าคนนี้มีคูปองอะไรบ้าง", "ค้นหาลูกค้าจากเบอร์โทร",
            "ค้นหาลูกค้าจากอีเมล", "เช็คข้อมูลคุณสมชาย", "ดูยอด Wallet ของสมาชิก", "ขอข้อมูลโปรไฟล์ลูกค้า",
        )
    ])

    reg.replace_parameters(action_id, [
        {"name": "SecretCode", "display_name": "Secret Code", "required": True,
         "input_source": "secret_configuration", "secret_ref": SECRET_REF, "send_as": "form",
         "visible_to_customer": False, "visible_in_developer_mode": False, "loggable": False,
         "description": "ค่า Secret สำหรับยืนยันตัวตนกับ FastTrade API — ไม่เก็บค่าจริงในฐานข้อมูล อ่านจาก environment variable เท่านั้น"},
        {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": False,
         "input_source": "customer_message", "send_as": "form", "example_value": "C00001",
         "validation_type": "non_empty"},
        {"name": "CustEmail", "display_name": "อีเมลลูกค้า", "required": False,
         "input_source": "customer_message", "send_as": "form", "example_value": "customer@example.com",
         "validation_type": "email"},
        {"name": "CustName", "display_name": "ชื่อ-นามสกุลลูกค้า", "required": False,
         "input_source": "customer_message", "send_as": "form", "example_value": "สมชาย ใจดี",
         "validation_type": "non_empty"},
        {"name": "CustPhone", "display_name": "เบอร์โทรลูกค้า", "required": False,
         "input_source": "customer_message", "send_as": "form", "example_value": "0812345678",
         "validation_type": "phone_number"},
    ])

    reg.set_parameter_groups(action_id, [
        {"name": "customer_search_identifier", "rule": "AT_LEAST_ONE",
         "members": ["CustCode", "CustEmail", "CustName", "CustPhone"]},
    ])

    reg.upsert_execution(action_id, {
        "execution_target": "FastTrade AI Chat API: GetDataCustomer",
        "base_url": "https://fasttrade.in.th",
        "endpoint_path": "/web-service/ai-chat/GetDataCustomer",
        "http_method": "POST",
        "content_type": "application/x-www-form-urlencoded",
        "headers": {"Accept": "application/json"},
        "auth_type": "none",  # auth happens via the SecretCode form parameter, not an HTTP auth header
        "timeout_seconds": 10,
    })

    reg.replace_tags(action_id, ["customer", "erp", "fasttrade"])

    print(f"customer_data_lookup ready. Resolved URL: https://fasttrade.in.th/web-service/ai-chat/GetDataCustomer")
    print(f"SecretCode is read from environment variable: {SECRET_REF} (not stored in the database).")


if __name__ == "__main__":
    main()
