"""PolicyStudioService — CRUD + default-resolution for AI Policy sets
(migrations/024_ai_policy_sets.sql). Mirrors services/prompt_studio_service.py's
shape (list/get/create/update/duplicate/delete/set_default) but with NO
version lineage — a policy set has no "Save as New Version"/rollback,
per spec ("Do NOT: Add policy version comparison").

Every method degrades gracefully (returns None/[]/False on failure)
rather than raising, matching the convention used elsewhere in this app.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import SUPABASE_URL, SUPABASE_KEY

_supabase = None


def _get_sb():
    from services.supabase_client import get_supabase  # shared bounded-timeout client
    return get_supabase()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Sensible defaults (Requirement: "Use sensible defaults") — used both to
# seed a brand-new policy set (any field the caller doesn't supply) and as
# the ultimate in-memory fallback if the DB/migration isn't reachable at
# all (see get_default_policy_set() below), so the rest of the app never
# has to null-check every single rule field.
DEFAULT_CONFIG: Dict = {
    "business_rules": {
        "no_guess_prices": True,
        "no_guess_delivery_status": True,
        "no_answer_unavailable_info": True,
        "require_approved_knowledge": True,
    },
    "knowledge_rules": {
        "use_rag_first": True,
        "cite_source": True,
        "say_if_no_info": True,
        "prefer_latest_document": True,
    },
    "escalation_rules": {
        "enabled": True,
        "confidence_threshold": 0.5,
        "message": "ขออภัยค่ะ ทีมงานจะติดต่อกลับเพื่อช่วยเหลือเพิ่มเติมนะคะ",
        "escalate_on_no_answer": True,
        "escalate_on_dissatisfaction": True,
    },
    "attachment_rules": {
        "send_image_if_available": True,
        "send_file_link_if_available": True,
        "skip_broken_attachments": True,
        "text_first": True,
    },
    "channel_rules": {
        "line_response_length": "standard",
        "emoji_usage": "sometimes",
        "formality": "friendly",
    },
    # Human-like Multi-Message Replies — controls services/message_segmenter.py's
    # behavior, never Prompt Studio or retrieval. _merge_config() below
    # already deep-merges DEFAULT_CONFIG generically per section, so an
    # existing policy set saved before this section existed just resolves
    # to these defaults with zero migration/extra code.
    "messaging_rules": {
        "reply_mode": "auto",       # single | multi | auto
        "max_messages": 3,          # 1-3
        "message_delay": "natural",  # none | short | natural
    },
}

_FALLBACK_POLICY_SET: Dict = {
    "id": None, "name": "Standard Policy (built-in fallback)",
    "description": "Used when no policy set exists in the database yet.",
    "is_active": True, "is_default": True, "config": DEFAULT_CONFIG,
}


def _merge_config(config: Optional[Dict]) -> Dict:
    """Deep-merges a partial config over DEFAULT_CONFIG (section by
    section) so a policy set created/edited before a new rule existed
    still resolves that rule to its sensible default, never a missing
    key/None crash downstream."""
    config = config or {}
    merged = {}
    for section, defaults in DEFAULT_CONFIG.items():
        merged[section] = {**defaults, **(config.get(section) or {})}
    return merged


class PolicyStudioService:
    def list_policy_sets(self, search: Optional[str] = None) -> List[Dict]:
        try:
            rows = _get_sb().table("ai_policy_sets").select("*").is_("deleted_at", "null") \
                .order("created_at", desc=True).execute().data or []
        except Exception as e:
            print(f"[PolicyStudio] list_policy_sets failed: {e}")
            return []
        if search:
            needle = search.lower()
            rows = [r for r in rows if needle in (r.get("name") or "").lower()
                    or needle in (r.get("description") or "").lower()]
        for r in rows:
            r["config"] = _merge_config(r.get("config"))
        return rows

    def get_policy_set(self, policy_id: str) -> Optional[Dict]:
        try:
            res = _get_sb().table("ai_policy_sets").select("*").eq("id", policy_id) \
                .is_("deleted_at", "null").execute()
            row = (res.data or [None])[0]
        except Exception as e:
            print(f"[PolicyStudio] get_policy_set failed: {e}")
            return None
        if row:
            row["config"] = _merge_config(row.get("config"))
        return row

    def create_policy_set(self, data: Dict) -> Optional[Dict]:
        row = {
            "name": data.get("name") or "New Policy",
            "description": data.get("description"),
            "is_active": bool(data.get("is_active", True)),
            "config": _merge_config(data.get("config")),
        }
        try:
            res = _get_sb().table("ai_policy_sets").insert(row).execute()
            return (res.data or [None])[0]
        except Exception as e:
            print(f"[PolicyStudio] create_policy_set failed: {e}")
            return None

    def update_policy_set(self, policy_id: str, data: Dict) -> Optional[Dict]:
        allowed = {"name", "description", "config"}
        update = {k: v for k, v in data.items() if k in allowed}
        if "config" in update:
            update["config"] = _merge_config(update["config"])
        update["updated_at"] = _now_iso()
        try:
            res = _get_sb().table("ai_policy_sets").update(update).eq("id", policy_id).execute()
            return (res.data or [None])[0]
        except Exception as e:
            print(f"[PolicyStudio] update_policy_set failed: {e}")
            return None

    def duplicate_policy_set(self, policy_id: str) -> Optional[Dict]:
        source = self.get_policy_set(policy_id)
        if not source:
            return None
        copy = {
            "name": f"{source['name']} (copy)", "description": source.get("description"),
            "config": source.get("config") or {},
            # Active by default — same reasoning as Prompt Studio's
            # duplicate_prompt(): no separate "Activate" control exists in
            # this simplified UI, so a duplicate must be usable immediately.
            "is_active": True,
        }
        try:
            res = _get_sb().table("ai_policy_sets").insert(copy).execute()
            return (res.data or [None])[0]
        except Exception as e:
            print(f"[PolicyStudio] duplicate_policy_set failed: {e}")
            return None

    def set_default(self, policy_id: str) -> bool:
        """Exactly one policy set should ever be is_default=true — clear
        any previous default first, then set the new one, so there's
        never a window with zero or multiple defaults visible to a
        concurrent reader (same pattern as PromptStudioService.set_default)."""
        sb = _get_sb()
        try:
            sb.table("ai_policy_sets").update({"is_default": False}).eq("is_default", True).execute()
            sb.table("ai_policy_sets").update(
                {"is_default": True, "is_active": True, "updated_at": _now_iso()}
            ).eq("id", policy_id).execute()
            return True
        except Exception as e:
            print(f"[PolicyStudio] set_default failed: {e}")
            return False

    def delete_policy_set(self, policy_id: str, force: bool = False) -> Dict:
        """Soft delete. Refuses (unless force=True) when this would leave
        the platform with no default policy set — same safety check as
        Prompt Studio's delete_prompt()."""
        policy = self.get_policy_set(policy_id)
        if not policy:
            return {"ok": False, "error": "Policy set not found"}

        if policy.get("is_default") and not force:
            others = [p for p in self.list_policy_sets() if p["id"] != policy_id]
            if not others:
                return {"ok": False, "error": "Cannot delete the only policy set. Create another one first."}
            return {"ok": False, "warning": True,
                    "error": "This is the current Default policy set. Deleting it will leave AI Playground "
                             "and LINE OA with no default until you set another one. Delete anyway?"}
        try:
            _get_sb().table("ai_policy_sets").update({"deleted_at": _now_iso()}).eq("id", policy_id).execute()
            return {"ok": True}
        except Exception as e:
            print(f"[PolicyStudio] delete_policy_set failed: {e}")
            return {"ok": False, "error": str(e)}


_service_singleton: Optional[PolicyStudioService] = None


def get_policy_studio_service() -> PolicyStudioService:
    global _service_singleton
    if _service_singleton is None:
        _service_singleton = PolicyStudioService()
    return _service_singleton


def get_default_policy_set() -> Dict:
    """The routing rule AI Playground and LINE OA both resolve through —
    never a hardcoded config. Falls back to the in-memory
    _FALLBACK_POLICY_SET (still DEFAULT_CONFIG's sensible values) if the
    DB/migration isn't reachable, so a misconfigured/missing policy set
    never breaks either caller."""
    try:
        res = _get_sb().table("ai_policy_sets").select("*").eq("is_default", True) \
            .is_("deleted_at", "null").limit(1).execute()
        if res.data:
            row = res.data[0]
            row["config"] = _merge_config(row.get("config"))
            return row
    except Exception as e:
        print(f"[PolicyStudio] get_default_policy_set DB query failed, using fallback: {e}")
    return dict(_FALLBACK_POLICY_SET)
