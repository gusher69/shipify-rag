"""PromptStudioService — CRUD, versioning, and channel-assignment logic
for AI system prompts (migrations/017_prompt_studio.sql). Read access for
actually BUILDING a prompt lives in services/prompt_builder.py (which
this module's write paths keep in sync); this module is the admin-facing
write side: create/edit/delete/duplicate/activate/version/rollback/assign.

Every method degrades gracefully (returns None/[]/False on failure)
rather than raising, matching the convention used by
services/session_service.py.
"""
from config import SUPABASE_URL, SUPABASE_KEY
from datetime import datetime, timezone
from typing import Dict, List, Optional

CHANNELS = ["Global", "LINE OA", "Website", "Shopee", "Lazada", "TikTok Shop", "Facebook", "Instagram"]

# Phase 3.4 (2026-08-05) — Customer Tier Prompt. Fixed 4-tier set, matching
# services/customer_tier_service.py::TIERS exactly (never user-editable —
# the tier set itself is a platform constant, only which PROMPT is
# assigned to each tier is admin-editable).
TIERS = ["cold", "warm", "hot", "negative"]

_supabase = None


def _get_sb():
    from services.supabase_client import get_supabase  # shared bounded-timeout client
    return get_supabase()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PromptStudioService:
    # ── Prompt CRUD ──────────────────────────────────────────────
    def list_prompts(self, search: Optional[str] = None, channel: Optional[str] = None,
                      is_active: Optional[bool] = None, is_default: Optional[bool] = None) -> List[Dict]:
        try:
            q = _get_sb().table("ai_prompt_templates").select("*").is_("deleted_at", "null")
            if channel:
                q = q.eq("channel", channel)
            if is_active is not None:
                q = q.eq("is_active", is_active)
            if is_default is not None:
                q = q.eq("is_default", is_default)
            rows = q.order("created_at", desc=True).execute().data or []
        except Exception as e:
            print(f"[PromptStudio] list_prompts failed: {e}")
            return []
        if search:
            needle = search.lower()
            rows = [r for r in rows if needle in (r.get("name") or "").lower()
                    or needle in (r.get("description") or "").lower()
                    or needle in (r.get("system_prompt") or "").lower()
                    or needle in (r.get("tone") or "").lower()]
        return rows

    def get_prompt(self, prompt_id: str) -> Optional[Dict]:
        try:
            res = _get_sb().table("ai_prompt_templates").select("*").eq("id", prompt_id) \
                .is_("deleted_at", "null").execute()
            return (res.data or [None])[0]
        except Exception as e:
            print(f"[PromptStudio] get_prompt failed: {e}")
            return None

    def create_prompt(self, data: Dict) -> Optional[Dict]:
        row = {
            "name": data.get("name") or "New Prompt", "description": data.get("description"),
            "channel": data.get("channel"), "language": data.get("language") or "th",
            "tone": data.get("tone"), "system_prompt": data.get("system_prompt") or "",
            "response_rules": data.get("response_rules") or {}, "fallback_rules": data.get("fallback_rules") or {},
            "safety_rules": data.get("safety_rules") or {}, "is_active": bool(data.get("is_active", True)),
        }
        try:
            res = _get_sb().table("ai_prompt_templates").insert(row).execute()
            return (res.data or [None])[0]
        except Exception as e:
            print(f"[PromptStudio] create_prompt failed: {e}")
            return None

    def update_prompt(self, prompt_id: str, data: Dict) -> Optional[Dict]:
        """Option A from the spec — "Save updates current draft": edits
        the row in place, does NOT bump version or create a new row."""
        allowed = {"name", "description", "channel", "language", "tone", "system_prompt",
                   "response_rules", "fallback_rules", "safety_rules"}
        update = {k: v for k, v in data.items() if k in allowed}
        update["updated_at"] = _now_iso()
        try:
            res = _get_sb().table("ai_prompt_templates").update(update).eq("id", prompt_id).execute()
            return (res.data or [None])[0]
        except Exception as e:
            print(f"[PromptStudio] update_prompt failed: {e}")
            return None

    def delete_prompt(self, prompt_id: str, force: bool = False) -> Dict:
        """Soft delete. Refuses (unless force=True) when this would leave
        the platform with no Global Default, or when the prompt is
        currently assigned to a live channel — the two safety checks the
        spec calls out explicitly."""
        prompt = self.get_prompt(prompt_id)
        if not prompt:
            return {"ok": False, "error": "Prompt not found"}

        warnings = []
        if prompt.get("is_default"):
            others = [p for p in self.list_prompts(is_default=True) if p["id"] != prompt_id]
            if not others:
                return {"ok": False, "error": "Cannot delete the only Global Default prompt. "
                                               "Set another prompt as default first."}

        assignments = self.list_assignments()
        assigned_channels = [a["channel"] for a in assignments
                              if a.get("prompt_template_id") == prompt_id and a.get("is_active")]
        if assigned_channels and not force:
            return {"ok": False, "warning": True,
                    "error": f"This prompt is currently assigned to: {', '.join(assigned_channels)}. "
                             f"Deleting it will leave those channels without an explicit prompt "
                             f"(they will fall back to Global Default). Pass force=true to proceed anyway."}

        try:
            _get_sb().table("ai_prompt_templates").update(
                {"deleted_at": _now_iso()}
            ).eq("id", prompt_id).execute()
            return {"ok": True}
        except Exception as e:
            print(f"[PromptStudio] delete_prompt failed: {e}")
            return {"ok": False, "error": str(e)}

    def duplicate_prompt(self, prompt_id: str) -> Optional[Dict]:
        source = self.get_prompt(prompt_id)
        if not source:
            return None
        copy = {
            "name": f"{source['name']} (copy)", "description": source.get("description"),
            "channel": source.get("channel"), "language": source.get("language"),
            "tone": source.get("tone"), "system_prompt": source.get("system_prompt"),
            "response_rules": source.get("response_rules") or {}, "fallback_rules": source.get("fallback_rules") or {},
            "safety_rules": source.get("safety_rules") or {},
            # Active by default — the simplified customer-facing Prompt
            # Studio UI has no separate "Activate" action anymore, so an
            # inactive duplicate would be invisible/unusable (excluded from
            # the AI Playground's active-only dropdown) with no way to fix
            # it short of Set as Customer Default. is_default is still
            # always false for a duplicate.
            "is_active": True,
        }
        try:
            res = _get_sb().table("ai_prompt_templates").insert(copy).execute()
            return (res.data or [None])[0]
        except Exception as e:
            print(f"[PromptStudio] duplicate_prompt failed: {e}")
            return None

    def rename_prompt(self, prompt_id: str, name: str) -> Optional[Dict]:
        return self.update_prompt(prompt_id, {"name": name})

    def activate_prompt(self, prompt_id: str) -> bool:
        try:
            _get_sb().table("ai_prompt_templates").update(
                {"is_active": True, "updated_at": _now_iso()}
            ).eq("id", prompt_id).execute()
            return True
        except Exception as e:
            print(f"[PromptStudio] activate_prompt failed: {e}")
            return False

    def set_default(self, prompt_id: str) -> bool:
        """Exactly one prompt should ever be is_default=true — clear the
        previous default(s) first, then set the new one, inside the same
        call so there's never a window with zero or multiple defaults
        visible to a concurrent reader."""
        sb = _get_sb()
        try:
            sb.table("ai_prompt_templates").update({"is_default": False}).eq("is_default", True).execute()
            sb.table("ai_prompt_templates").update(
                {"is_default": True, "is_active": True, "updated_at": _now_iso()}
            ).eq("id", prompt_id).execute()
            return True
        except Exception as e:
            print(f"[PromptStudio] set_default failed: {e}")
            return False

    # ── Versioning ───────────────────────────────────────────────
    def save_as_new_version(self, prompt_id: str, data: Dict) -> Optional[Dict]:
        """Option B from the spec — "Save as New Version": the OLD row is
        left completely untouched (rollback-able); a new row is inserted
        with version+1 and parent_id pointing at the version it was
        edited from. `parent_id` always points to the ORIGINAL root of
        the lineage (not just the immediate previous version), so
        `list_versions` can find every version of a prompt with one
        query regardless of how many times it's been re-versioned."""
        source = self.get_prompt(prompt_id)
        if not source:
            return None
        root_id = source.get("parent_id") or source["id"]
        siblings = self._version_lineage(root_id)
        next_version = max((s.get("version") or 1) for s in siblings) + 1 if siblings else (source.get("version") or 1) + 1

        row = {
            "name": data.get("name", source["name"]), "description": data.get("description", source.get("description")),
            "channel": data.get("channel", source.get("channel")), "language": data.get("language", source.get("language")),
            "tone": data.get("tone", source.get("tone")),
            "system_prompt": data.get("system_prompt", source["system_prompt"]),
            "response_rules": data.get("response_rules", source.get("response_rules") or {}),
            "fallback_rules": data.get("fallback_rules", source.get("fallback_rules") or {}),
            "safety_rules": data.get("safety_rules", source.get("safety_rules") or {}),
            "is_active": True, "is_default": False,
            "version": next_version, "parent_id": root_id,
        }
        try:
            res = _get_sb().table("ai_prompt_templates").insert(row).execute()
            return (res.data or [None])[0]
        except Exception as e:
            print(f"[PromptStudio] save_as_new_version failed: {e}")
            return None

    def _version_lineage(self, root_id: str) -> List[Dict]:
        try:
            sb = _get_sb()
            root = sb.table("ai_prompt_templates").select("*").eq("id", root_id).execute().data or []
            children = sb.table("ai_prompt_templates").select("*").eq("parent_id", root_id).execute().data or []
            return root + children
        except Exception as e:
            print(f"[PromptStudio] _version_lineage failed: {e}")
            return []

    def list_versions(self, prompt_id: str) -> List[Dict]:
        prompt = self.get_prompt(prompt_id)
        if not prompt:
            return []
        root_id = prompt.get("parent_id") or prompt["id"]
        versions = self._version_lineage(root_id)
        versions.sort(key=lambda v: v.get("version") or 1)
        return versions

    def rollback(self, prompt_id: str, target_version_id: str) -> Optional[Dict]:
        """Rolling back does NOT delete newer versions (never destroys
        history) — it creates a NEW version whose content matches
        `target_version_id`, becoming the newest, active version."""
        target = self.get_prompt(target_version_id)
        if not target:
            return None
        return self.save_as_new_version(prompt_id, {
            "name": target["name"], "description": target.get("description"),
            "channel": target.get("channel"), "language": target.get("language"),
            "tone": target.get("tone"), "system_prompt": target["system_prompt"],
            "response_rules": target.get("response_rules") or {}, "fallback_rules": target.get("fallback_rules") or {},
            "safety_rules": target.get("safety_rules") or {},
        })

    # ── Channel assignment ───────────────────────────────────────
    def list_assignments(self) -> List[Dict]:
        try:
            res = _get_sb().table("ai_prompt_assignments").select(
                "*, ai_prompt_templates(id,name,version)"
            ).eq("is_active", True).execute()
            return res.data or []
        except Exception as e:
            print(f"[PromptStudio] list_assignments failed: {e}")
            return []

    def assign_channel(self, channel: str, prompt_template_id: str) -> Dict:
        """Enforces "only one active prompt assignment per channel" by
        deactivating any existing active assignment for this channel
        before inserting the new one — belt-and-suspenders alongside the
        DB's own partial unique index (uniq_active_assignment_per_channel)."""
        if channel not in CHANNELS:
            return {"ok": False, "error": f"Unknown channel '{channel}'. Must be one of: {', '.join(CHANNELS)}"}
        prompt = self.get_prompt(prompt_template_id)
        if not prompt:
            return {"ok": False, "error": "Prompt template not found"}
        sb = _get_sb()
        try:
            sb.table("ai_prompt_assignments").update(
                {"is_active": False, "updated_at": _now_iso()}
            ).eq("channel", channel).eq("is_active", True).execute()
            res = sb.table("ai_prompt_assignments").insert({
                "channel": channel, "prompt_template_id": prompt_template_id, "is_active": True,
            }).execute()
            return {"ok": True, "assignment": (res.data or [None])[0]}
        except Exception as e:
            print(f"[PromptStudio] assign_channel failed: {e}")
            return {"ok": False, "error": str(e)}

    def update_assignment(self, assignment_id: str, prompt_template_id: str) -> Dict:
        assignment = None
        try:
            res = _get_sb().table("ai_prompt_assignments").select("*").eq("id", assignment_id).execute()
            assignment = (res.data or [None])[0]
        except Exception as e:
            print(f"[PromptStudio] update_assignment lookup failed: {e}")
        if not assignment:
            return {"ok": False, "error": "Assignment not found"}
        return self.assign_channel(assignment["channel"], prompt_template_id)

    # ── Customer Tier Prompt (Phase 3.4, 2026-08-05) ────────────────
    # Mirrors the channel-assignment methods above exactly, keyed by tier
    # instead of channel — services/prompt_builder.py::get_active_prompt()
    # reads ai_prompt_tier_assignments the same way it reads
    # ai_prompt_assignments for channels, with tier taking priority.
    def list_tier_assignments(self) -> List[Dict]:
        try:
            res = _get_sb().table("ai_prompt_tier_assignments").select(
                "*, ai_prompt_templates(id,name,version)"
            ).eq("is_active", True).execute()
            return res.data or []
        except Exception as e:
            print(f"[PromptStudio] list_tier_assignments failed: {e}")
            return []

    def assign_tier(self, tier: str, prompt_template_id: str) -> Dict:
        """Enforces "only one active prompt assignment per tier", same
        belt-and-suspenders pattern as assign_channel (DB partial unique
        index uniq_active_assignment_per_tier is the real enforcement)."""
        if tier not in TIERS:
            return {"ok": False, "error": f"Unknown tier '{tier}'. Must be one of: {', '.join(TIERS)}"}
        prompt = self.get_prompt(prompt_template_id)
        if not prompt:
            return {"ok": False, "error": "Prompt template not found"}
        sb = _get_sb()
        try:
            sb.table("ai_prompt_tier_assignments").update(
                {"is_active": False, "updated_at": _now_iso()}
            ).eq("tier", tier).eq("is_active", True).execute()
            res = sb.table("ai_prompt_tier_assignments").insert({
                "tier": tier, "prompt_template_id": prompt_template_id, "is_active": True,
            }).execute()
            return {"ok": True, "assignment": (res.data or [None])[0]}
        except Exception as e:
            print(f"[PromptStudio] assign_tier failed: {e}")
            return {"ok": False, "error": str(e)}

    def unassign_tier(self, tier: str) -> Dict:
        """Removes the tier's prompt override entirely — the tier then
        falls back to channel/global resolution (see
        services/prompt_builder.py::get_active_prompt), which is a valid,
        common state (tier prompts are opt-in, unlike channel assignment)."""
        try:
            _get_sb().table("ai_prompt_tier_assignments").update(
                {"is_active": False, "updated_at": _now_iso()}
            ).eq("tier", tier).eq("is_active", True).execute()
            return {"ok": True}
        except Exception as e:
            print(f"[PromptStudio] unassign_tier failed: {e}")
            return {"ok": False, "error": str(e)}

    # ── Test ─────────────────────────────────────────────────────
    def test_prompt(self, prompt_id: str, question: str, top_k: int = 3) -> Optional[object]:
        """Runs the SAME orchestrator the AI Playground uses, forcing the
        given prompt template — this is what /admin/api/ai/prompts/test
        calls, so "test a prompt" and "test in Playground" never diverge."""
        from services.playground_orchestrator import run_playground_turn
        try:
            return run_playground_turn(question, template_id=prompt_id, top_k=top_k)
        except Exception as e:
            print(f"[PromptStudio] test_prompt failed: {e}")
            return None


_service_singleton: Optional[PromptStudioService] = None


def get_prompt_studio_service() -> PromptStudioService:
    global _service_singleton
    if _service_singleton is None:
        _service_singleton = PromptStudioService()
    return _service_singleton
