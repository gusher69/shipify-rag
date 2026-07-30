"""Developer Mode — centralized feature-visibility manager for the
Admin Platform (DeveloperFeatureManager).

Single source of truth for: whether Developer Mode is active, whether a
given feature is a completed developer-only page or an unfinished
placeholder, and what STATE that implies for both the sidebar (Category
A/B distinction) and server-side route guards. Nothing about AI
behavior, the Decision Engine, LINE OA, or ERP connectors is touched
here — this is UI/access-control only.

Persistence reuses the EXISTING Settings page mechanism
(admin/routes.py's _read_env()/_write_env() against the project's .env
file) — DEVELOPER_MODE is just one more env-backed setting, the same
pattern already used for ADMIN_USERNAME/OPENAI_API_KEY/etc. No new
persistence layer, no localStorage.

Precedence rule (documented, never silently reordered):
  1. An explicit environment hard-disable (e.g. SHOW_BUSINESS_ACTION_CENTER
     already set to "false") always wins — even with Developer Mode ON,
     that specific feature stays hidden. Preserves the three pre-existing
     env flags exactly as before; nothing about them changes.
  2. Developer Mode OFF hides every developer-only feature outright.
  3. Developer Mode ON + implementation_status == "complete" -> VISIBLE
     (Category A).
  4. Developer Mode ON + implementation_status == "coming_soon" ->
     COMING_SOON — shown, but never clickable, never a real route
     (Category B).
A feature that isn't developer_only at all (every normal admin menu —
Category C) is always VISIBLE and is never touched by this module.
"""
import os
from typing import Dict, List, Optional

FEATURE_STATE_VISIBLE = "VISIBLE"
FEATURE_STATE_HIDDEN = "HIDDEN"
FEATURE_STATE_COMING_SOON = "COMING_SOON"
FEATURE_STATE_DISABLED = "DISABLED"

# ── Feature Definitions (Sidebar Refactor) — the single source of truth.
# Category A (implementation_status="complete") were previously gated by
# their own one-off SHOW_* env flag directly in admin/sidebar_config.py;
# that flag is preserved here as `env_override` so existing deployments
# that already set it keep working identically. Category B
# (implementation_status="coming_soon") were previously a mix of an
# always-hidden sidebar_config.py "tools" group and three items hardcoded
# directly in admin/templates/base.html's "System (Phase 2)" block —
# both are now generated from this one dict instead.
#
# `navigation_group` (Admin Navigation Refactor, 2026-07-22) — which
# sidebar group a feature belongs to (`"integrations"` or
# `"developer_tools"`); admin/sidebar_config.py groups
# build_developer_menu_items()'s output by this field instead of a
# second, separately-maintained grouping list.
#
# `business_action_center`'s underlying page/route/registry/executor/
# provider architecture is UNCHANGED — only its Admin-facing title moved
# from "Business Action Center" to "ERP Integration" and its sidebar
# placement moved from the AI group to a new top-level "Integrations"
# group. "Business Action" remains the internal architecture name
# throughout the Python codebase (classes, tables, routes); "ERP
# Integration" is the user-facing label only.
#
# `erp_sync` (previously a Coming Soon placeholder here) has been
# REMOVED per the Admin Navigation Refactor — audited and confirmed to
# be a placeholder-only feature key (route=None, no backend, no DB
# table, referenced nowhere outside this dict/sidebar rendering/its own
# tests) that duplicated business_action_center's real, working ERP
# integration functionality. See the refactor's final report for the
# audit trail.
FEATURE_DEFINITIONS: Dict[str, Dict] = {
    "rag_benchmark": {
        "title": "AI Evaluation", "route": "/admin/ai/benchmark", "icon": "table",
        "developer_only": True, "implementation_status": "complete",
        "env_override": "SHOW_AI_EVALUATION", "navigation_group": "developer_tools",
    },
    "ai_validation": {
        "title": "Production Validation", "route": "/admin/ai/validation", "icon": "check-circle",
        "developer_only": True, "implementation_status": "complete",
        "env_override": "SHOW_PRODUCTION_VALIDATION", "navigation_group": "developer_tools",
    },
    # Always-on (2026-07-21) — the user asked for ERP Integration to
    # behave like Knowledge Base: a normal Admin feature, never gated
    # behind Developer Mode. `developer_only=False` means
    # get_feature_state() returns VISIBLE unconditionally regardless of
    # the Developer Mode toggle; the SHOW_* env override is kept so it
    # can still be hard-disabled if ever needed, same mechanism as
    # before.
    "business_action_center": {
        "title": "ERP Integration", "route": "/admin/ai/business-actions", "icon": "plug",
        "developer_only": False, "implementation_status": "complete",
        "env_override": "SHOW_BUSINESS_ACTION_CENTER", "navigation_group": "integrations",
    },
    # ERP/AI Product Architecture Separation (this sprint) — ERP testing
    # (conversation-driven verification that an imported ERP API works)
    # is its own page/responsibility, distinct from ERP Integration
    # (configuration) and from AI Playground (pure AI/RAG testing). Same
    # always-on visibility as business_action_center — never gated
    # behind Developer Mode, since ERP testing is a normal, everyday
    # Admin task, not an advanced/unfinished tool.
    "erp_conversation_tester": {
        "title": "ERP Conversation Tester", "route": "/admin/erp/conversation-tester", "icon": "message-square",
        "developer_only": False, "implementation_status": "complete",
        "env_override": "SHOW_BUSINESS_ACTION_CENTER", "navigation_group": "integrations",
    },
    "excel_engine": {
        "title": "Excel Engine", "route": None, "icon": "table",
        "developer_only": True, "implementation_status": "coming_soon", "env_override": None,
        "navigation_group": "developer_tools",
    },
    "attachment_manager": {
        "title": "Attachment Manager", "route": None, "icon": "paperclip",
        "developer_only": True, "implementation_status": "coming_soon", "env_override": None,
        "navigation_group": "developer_tools",
    },
    "storage": {
        "title": "Storage", "route": None, "icon": "hard-drive",
        "developer_only": True, "implementation_status": "coming_soon", "env_override": None,
        "navigation_group": "developer_tools",
    },
    "user_profiles": {
        "title": "User Profiles", "route": None, "icon": "user",
        "developer_only": True, "implementation_status": "coming_soon", "env_override": None,
        "navigation_group": "developer_tools",
    },
    "line_analytics": {
        "title": "LINE Analytics", "route": None, "icon": "bar-chart",
        "developer_only": True, "implementation_status": "coming_soon", "env_override": None,
        "navigation_group": "developer_tools",
    },
}


def _env_hard_disabled(env_override: Optional[str]) -> bool:
    """Precedence 1 — only an EXPLICIT "false" counts as a hard disable;
    an unset var (the vast majority of deployments) never disables
    anything, matching the pre-existing SHOW_* flags' own default-true
    behavior."""
    if not env_override:
        return False
    return os.getenv(env_override, "true").strip().lower() == "false"


def is_developer_mode_enabled() -> bool:
    """Defaults OFF. Reads the same env-var-backed setting the Settings
    page persists via admin/routes.py's _write_env()."""
    return os.getenv("DEVELOPER_MODE", "false").strip().lower() == "true"


def get_feature_state(feature_key: str, *, developer_mode: Optional[bool] = None) -> str:
    """The ONE function every visibility decision (sidebar rendering AND
    server-side route guards) goes through — never duplicate this logic
    inline in a template or a route handler.

    `developer_mode`, if given, avoids re-reading the env var once per
    feature when checking many features in a row (see
    build_developer_menu_items())."""
    feature = FEATURE_DEFINITIONS.get(feature_key)
    if not feature:
        raise ValueError(f"Unknown developer feature: {feature_key!r}")
    if _env_hard_disabled(feature.get("env_override")):
        return FEATURE_STATE_HIDDEN
    if not feature.get("developer_only"):
        return FEATURE_STATE_VISIBLE
    dev_mode = is_developer_mode_enabled() if developer_mode is None else developer_mode
    if not dev_mode:
        return FEATURE_STATE_HIDDEN
    if feature.get("implementation_status") == "complete":
        return FEATURE_STATE_VISIBLE
    return FEATURE_STATE_COMING_SOON


def is_feature_route_accessible(feature_key: str) -> bool:
    """Server-side route guard helper (Access Control requirement) —
    VISIBLE is the only state that permits opening a Category A route
    directly; HIDDEN/COMING_SOON must both deny access, never rely on
    the sidebar link simply being absent."""
    return get_feature_state(feature_key) == FEATURE_STATE_VISIBLE


def build_developer_menu_items() -> List[Dict]:
    """Returns sidebar-ready dicts for every developer-only feature that
    isn't HIDDEN — used by admin/sidebar_config.py to build the dynamic
    "Developer Tools" group (Category B) without a second, separately-
    maintained list anywhere in a template."""
    dev_mode = is_developer_mode_enabled()
    items = []
    for key, feature in FEATURE_DEFINITIONS.items():
        state = get_feature_state(key, developer_mode=dev_mode)
        if state == FEATURE_STATE_HIDDEN:
            continue
        items.append({
            "feature_key": key, "title": feature["title"], "route": feature["route"],
            "icon": feature["icon"], "state": state,
            "comingSoon": state == FEATURE_STATE_COMING_SOON,
            "implementation_status": feature["implementation_status"],
            "navigation_group": feature.get("navigation_group"),
        })
    return items


def build_developer_menu_items_by_group(group: str) -> List[Dict]:
    """Convenience filter — admin/sidebar_config.py's single call site for
    building both the "Integrations" and "Developer Tools" groups,
    instead of filtering build_developer_menu_items() inline in two
    places."""
    return [i for i in build_developer_menu_items() if i["navigation_group"] == group]
