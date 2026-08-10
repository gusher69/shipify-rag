"""KnowledgeCollectionService — Phase 3.5 (2026-08-05, Conversation
Intelligence sprint). CRUD + file-assignment for Knowledge Collections
(migrations/035_phase3_conversation_intelligence.sql). Retrieval-time
filtering itself lives in rag/searcher.py::_resolve_allowed_file_ids —
this module is only the admin-facing write/read side, same split as
services/prompt_studio_service.py (write) vs services/prompt_builder.py
(read used by the pipeline).

Every method degrades gracefully (returns None/[]/False on failure),
matching every other *_service.py in this app.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import SUPABASE_URL, SUPABASE_KEY

_supabase = None


def _get_sb():
    global _supabase
    if _supabase is None:
        from supabase import create_client
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class KnowledgeCollectionService:
    def list_collections(self) -> List[Dict]:
        sb = _get_sb()
        try:
            collections = sb.table("knowledge_collections").select("*").is_("deleted_at", "null") \
                .order("is_default", desc=True).order("name").execute().data or []
        except Exception as e:
            print(f"[KnowledgeCollection] list_collections failed: {e}")
            return []
        try:
            counts_res = sb.table("knowledge_files").select("collection_id").is_("deleted_at", "null").execute().data or []
            counts: Dict[str, int] = {}
            for row in counts_res:
                cid = row.get("collection_id")
                if cid:
                    counts[cid] = counts.get(cid, 0) + 1
            for c in collections:
                c["file_count"] = counts.get(c["id"], 0)
        except Exception as e:
            print(f"[KnowledgeCollection] file count query failed: {e}")
        return collections

    def create_collection(self, name: str, description: Optional[str] = None) -> Optional[Dict]:
        try:
            row = {"name": name, "description": description or ""}
            res = _get_sb().table("knowledge_collections").insert(row).execute()
            return (res.data or [None])[0]
        except Exception as e:
            print(f"[KnowledgeCollection] create_collection failed: {e}")
            return None

    def update_collection(self, collection_id: str, data: Dict) -> Optional[Dict]:
        try:
            row = {k: v for k, v in data.items()
                   if k in ("name", "description", "prompt_template_id", "policy_set_id", "is_active")}
            row["updated_at"] = _now_iso()
            res = _get_sb().table("knowledge_collections").update(row).eq("id", collection_id).execute()
            return (res.data or [None])[0]
        except Exception as e:
            print(f"[KnowledgeCollection] update_collection failed: {e}")
            return None

    def delete_collection(self, collection_id: str) -> Dict:
        """Soft-deletes the collection. Refuses if it's the default
        collection (retrieval always needs one default to fall back to),
        or if files are still assigned to it (must be reassigned first —
        never silently orphans files' scoping)."""
        sb = _get_sb()
        try:
            row = sb.table("knowledge_collections").select("is_default").eq("id", collection_id).execute().data
            if row and row[0].get("is_default"):
                return {"ok": False, "error": "Cannot delete the default collection"}
            file_count = len(sb.table("knowledge_files").select("id").eq("collection_id", collection_id).execute().data or [])
            if file_count:
                return {"ok": False, "error": f"{file_count} file(s) still assigned — reassign them first"}
            sb.table("knowledge_collections").update({"deleted_at": _now_iso()}).eq("id", collection_id).execute()
            return {"ok": True}
        except Exception as e:
            print(f"[KnowledgeCollection] delete_collection failed: {e}")
            return {"ok": False, "error": str(e)}

    def set_default(self, collection_id: str) -> bool:
        sb = _get_sb()
        try:
            sb.table("knowledge_collections").update({"is_default": False}).eq("is_default", True).execute()
            sb.table("knowledge_collections").update({"is_default": True, "updated_at": _now_iso()}) \
                .eq("id", collection_id).execute()
            return True
        except Exception as e:
            print(f"[KnowledgeCollection] set_default failed: {e}")
            return False

    def assign_file(self, file_id: str, collection_id: str) -> bool:
        try:
            _get_sb().table("knowledge_files").update({"collection_id": collection_id}).eq("id", file_id).execute()
            from rag.searcher import _allowed_file_ids_cache
            _allowed_file_ids_cache.clear()  # reassignment must take effect on the very next retrieval call
            return True
        except Exception as e:
            print(f"[KnowledgeCollection] assign_file failed: {e}")
            return False

    def list_files_with_collection(self) -> List[Dict]:
        try:
            return _get_sb().table("knowledge_files").select("id,filename,collection_id").is_("deleted_at", "null") \
                .order("filename").execute().data or []
        except Exception as e:
            print(f"[KnowledgeCollection] list_files_with_collection failed: {e}")
            return []


_singleton: Optional[KnowledgeCollectionService] = None


def get_knowledge_collection_service() -> KnowledgeCollectionService:
    global _singleton
    if _singleton is None:
        _singleton = KnowledgeCollectionService()
    return _singleton
