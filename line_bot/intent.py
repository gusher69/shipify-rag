from openai import OpenAI
from config import OPENAI_API_KEY, OPENAI_CHAT_MODEL

client = OpenAI(api_key=OPENAI_API_KEY)

INTENTS = ["สต็อก", "ออเดอร์", "นโยบาย", "ทั่วไป"]

SYSTEM_PROMPT = """คุณเป็นระบบแยกประเภทคำถามลูกค้า Shipify
แยกคำถามเป็น 1 ใน 4 ประเภท:
- สต็อก: ถามเรื่องสินค้า ราคา ความพร้อม
- ออเดอร์: ถามเรื่องสถานะ tracking การจัดส่ง
- นโยบาย: ถามเรื่องการคืน เคลม เงื่อนไข
- ทั่วไป: FAQ วิธีสั่งซื้อ ข้อมูลบริษัท อื่นๆ
ตอบแค่ 1 คำ: สต็อก / ออเดอร์ / นโยบาย / ทั่วไป"""


def classify(message: str) -> str:
    """จำแนก intent ของข้อความ"""
    try:
        response = client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": message}
            ],
            max_tokens=10,
            temperature=0,
        )
        intent = response.choices[0].message.content.strip()
        return intent if intent in INTENTS else "ทั่วไป"
    except Exception as e:
        print(f"❌ classify ล้มเหลว: {e}")
        return "ทั่วไป"


if __name__ == "__main__":
    tests = [
        "ของฉันส่งถึงไหนแล้ว",
        "มีสินค้านี้ไหม",
        "คืนสินค้าได้ไหม",
        "สั่งซื้อยังไง",
    ]
    for t in tests:
        print(f"'{t}' → {classify(t)}")
