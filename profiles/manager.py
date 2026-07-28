from typing import Dict, Optional
from datetime import datetime, timezone
from supabase import create_client

from config import SUPABASE_URL, SUPABASE_KEY

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TABLE = "user_profiles"


def get_profile(line_user_id: str) -> Optional[Dict]:
    try:
        result = supabase.table(TABLE).select("*").eq("line_user_id", line_user_id).single().execute()
        return result.data
    except Exception:
        return None


def upsert_profile(line_user_id: str, data: Dict):
    try:
        row = {
            "line_user_id":  line_user_id,
            "display_name":  data.get("display_name", ""),
            "segment":       calc_segment(data.get("order_count", 0), data.get("total_spend", 0)),
            "order_count":   data.get("order_count", 0),
            "total_spend":   data.get("total_spend", 0),
            "chat_style":    data.get("chat_style", "short"),
            "last_active":   datetime.now(timezone.utc).isoformat(),
            "notes":         data.get("notes", ""),
        }
        supabase.table(TABLE).upsert(row, on_conflict="line_user_id").execute()
    except Exception as e:
        print(f"❌ upsert_profile: {e}")


def calc_segment(order_count: int, total_spend: float) -> str:
    """ปรับเกณฑ์ตามที่คลียร์กับ Mod"""
    if order_count == 0:
        return "cold"
    elif order_count <= 2 or total_spend < 5000:
        return "warm"
    else:
        return "hot"
