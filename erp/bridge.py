import pymysql
from typing import Dict, Optional
from config import DATABASE_URL


def get_connection():
    """เชื่อมต่อ MySQL ERP (read-only)"""
    # TODO: ใส่ credentials จริงจาก Mod ใน Phase 2
    return pymysql.connect(
        host=   "ERP_HOST",
        user=   "ERP_USER",
        password="ERP_PASSWORD",
        database="ERP_DATABASE",
        charset="utf8mb4",
        connect_timeout=5,
        read_timeout=5,
    )


def get_stock(sku: str) -> Dict:
    """ดึงสต็อกสินค้าตาม SKU"""
    try:
        conn = get_connection()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # TODO: ปรับ query ให้ตรงกับ schema ERP จริง
            cursor.execute(
                "SELECT sku, name, stock_qty, price FROM products WHERE sku = %s LIMIT 1",
                (sku,)
            )
            result = cursor.fetchone()
        conn.close()
        return result or {"error": "ไม่พบสินค้า"}
    except Exception as e:
        return {"error": f"ดึงสต็อกไม่ได้: {str(e)}"}


def get_order(order_id: str) -> Dict:
    """ดึงสถานะออเดอร์"""
    try:
        conn = get_connection()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # TODO: ปรับ query ให้ตรงกับ schema ERP จริง
            cursor.execute(
                "SELECT order_id, status, tracking_no, updated_at FROM orders WHERE order_id = %s LIMIT 1",
                (order_id,)
            )
            result = cursor.fetchone()
        conn.close()
        return result or {"error": "ไม่พบออเดอร์"}
    except Exception as e:
        return {"error": f"ดึงออเดอร์ไม่ได้: {str(e)}"}


if __name__ == "__main__":
    print("ERP Bridge — รอ credentials จาก Mod ใน Phase 2")
