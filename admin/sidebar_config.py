"""Config-driven sidebar navigation for the admin app.

This is the ONLY place the sidebar's structure is defined. base.html renders
it with a generic loop (item / group) — adding, moving, or renaming a menu
item never requires touching the template, only SIDEBAR_CONFIG below.

Each leaf item carries permission/roles/feature_flag/visible fields even
though there is no auth/RBAC system yet — is_item_visible() is the single
gate a future RBAC layer plugs into without touching every call site.

Items that don't have a real page yet (no auth required to reach this
conclusion — just: the route doesn't exist in admin/routes.py) are marked
comingSoon and rendered exactly like the existing "System (Phase 2)"
disabled-link pattern already used elsewhere in base.html (href="#",
dimmed, non-clickable, "Soon" badge) — no new visual pattern invented.
"""
from typing import Dict, List, Optional

from services.developer_mode import (
    get_feature_state, build_developer_menu_items_by_group, FEATURE_STATE_COMING_SOON,
)

# ── Icons ────────────────────────────────────────────────────
# Raw <svg> inner markup, 16x16 viewBox, stroke="currentColor" — matches the
# existing nav-link icon style exactly (see base.html's pre-refactor icons,
# which are reused verbatim below for every item that already existed).
ICONS: Dict[str, str] = {
    "home": (
        '<path d="M2 7.5L8 2.5l6 5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M3.5 6.5V13a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1V6.5" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>'
        '<path d="M6.5 14V10h3v4" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>'
    ),
    "book": (
        '<path d="M8 3.3C7 2.5 5.3 2.2 3 2.2v9.6c2.3 0 4 .3 5 1.1" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M8 3.3c1-.8 2.7-1.1 5-1.1v9.6c-2.3 0-4 .3-5 1.1" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>'
    ),
    # Existing "Knowledge Base" icon — unchanged.
    "database": (
        '<rect x="2" y="1.5" width="9" height="12" rx="1.5" stroke="currentColor" stroke-width="1.3"/>'
        '<path d="M5 5h5M5 7.5h5M5 10h3" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>'
        '<path d="M11 1.5v3h3" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M11 1.5l3 3" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>'
    ),
    # Existing "File Library" icon — unchanged.
    "folder": (
        '<rect x="2" y="1.5" width="12" height="13" rx="1.5" stroke="currentColor" stroke-width="1.3"/>'
        '<path d="M5 5h6M5 8h6M5 11h4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>'
    ),
    # Existing "Sync Activity" icon — unchanged.
    "refresh": (
        '<circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.3"/>'
        '<path d="M8 5v3.5l2 1.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>'
    ),
    "sparkles": (
        '<path d="M8 2l1 3 3 1-3 1-1 3-1-3-3-1 3-1 1-3z" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/>'
        '<path d="M13 10.5l.5 1.5 1.5.5-1.5.5-.5 1.5-.5-1.5-1.5-.5 1.5-.5.5-1.5z" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/>'
    ),
    # Existing "AI Playground" icon — unchanged.
    "search": (
        '<circle cx="7" cy="7" r="4.5" stroke="currentColor" stroke-width="1.3"/>'
        '<path d="M10.5 10.5L13.5 13.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>'
        '<path d="M5.5 7h3M7 5.5v3" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>'
    ),
    "message-square": (
        '<path d="M2 3.5a1.5 1.5 0 0 1 1.5-1.5h9A1.5 1.5 0 0 1 14 3.5v6a1.5 1.5 0 0 1-1.5 1.5H6l-3 3v-3H3.5A1.5 1.5 0 0 1 2 9.5v-6z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>'
    ),
    "edit": (
        '<path d="M9.5 3.5l3 3-7 7H2.5v-3l7-7z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>'
        '<path d="M8.5 4.5l3 3" stroke="currentColor" stroke-width="1.1"/>'
    ),
    "shield": (
        '<path d="M8 1.7l5 1.8v4.2c0 3.2-2.1 5.6-5 6.6-2.9-1-5-3.4-5-6.6V3.5l5-1.8z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>'
        '<path d="M5.8 8l1.6 1.6 2.8-3" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>'
    ),
    "wrench": (
        '<path d="M10.4 2.6a3 3 0 0 0-3.9 3.6L2 10.7l1.3 1.3 4.5-4.5a3 3 0 0 0 3.6-3.9l-1.8 1.8-1.4-.4-.4-1.4 1.8-1.8z" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/>'
    ),
    "table": (
        '<rect x="1.8" y="2.5" width="12.4" height="11" rx="1.3" stroke="currentColor" stroke-width="1.2"/>'
        '<path d="M1.8 6.3h12.4M6.3 2.5v11M10.5 2.5v11" stroke="currentColor" stroke-width="1" />'
    ),
    # AI Production Validation Center menu item.
    "check-circle": (
        '<circle cx="8" cy="8" r="6.3" stroke="currentColor" stroke-width="1.3"/>'
        '<path d="M5.3 8.2l1.8 1.8 3.6-3.8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>'
    ),
    "paperclip": (
        '<path d="M11.5 4.5L6 10a2 2 0 1 0 2.8 2.8l5-5a3.5 3.5 0 1 0-5-5l-5 5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>'
    ),
    "hard-drive": (
        '<rect x="1.8" y="6" width="12.4" height="8" rx="1.3" stroke="currentColor" stroke-width="1.2"/>'
        '<path d="M1.8 10h12.4" stroke="currentColor" stroke-width="1.2"/>'
        '<circle cx="4.2" cy="12" r=".6" fill="currentColor"/>'
        '<path d="M3.5 6L6 2.5h4L12.5 6" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/>'
    ),
    # Existing "Settings" icon — unchanged.
    "cog": (
        '<circle cx="8" cy="8" r="2.2" stroke="currentColor" stroke-width="1.3"/>'
        '<path d="M8 1.5v1.2M8 13.3v1.2M1.5 8h1.2M13.3 8h1.2M3.4 3.4l.85.85M11.75 11.75l.85.85M3.4 12.6l.85-.85M11.75 4.25l.85-.85" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>'
    ),
    # Moved verbatim from base.html's old hardcoded "System (Phase 2)"
    # block (User Profiles / LINE Analytics / ERP Sync icons) — same SVG,
    # now data-driven via services/developer_mode.py instead.
    "user": (
        '<circle cx="8" cy="5.5" r="2.5" stroke="currentColor" stroke-width="1.3"/>'
        '<path d="M3 13c0-2.21 2.239-4 5-4s5 1.79 5 4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>'
        '<circle cx="12.5" cy="4.5" r="1.8" stroke="currentColor" stroke-width="1.1"/>'
        '<path d="M14.5 11.5c0-1.38-1.12-2.5-2.5-2.5" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>'
    ),
    "bar-chart": (
        '<rect x="1.5" y="9" width="3" height="5.5" rx="1" stroke="currentColor" stroke-width="1.3"/>'
        '<rect x="6.5" y="5.5" width="3" height="9" rx="1" stroke="currentColor" stroke-width="1.3"/>'
        '<rect x="11.5" y="2" width="3" height="12.5" rx="1" stroke="currentColor" stroke-width="1.3"/>'
    ),
    "sync-arrows": (
        '<path d="M13.5 8A5.5 5.5 0 1 1 8 2.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>'
        '<path d="M8 2.5V1M8 2.5L10 4.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M13.5 8H15M13.5 8L11.5 6" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>'
    ),
    # "Integrations" group / ERP Integration menu item (Admin Navigation
    # Refactor) — a plug icon reads clearly as "connect to an external
    # system", matching the group's purpose (ERP/API integrations).
    "plug": (
        '<path d="M6 2v4M10 2v4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>'
        '<path d="M4.5 6h7v2.5a3.5 3.5 0 0 1-3.5 3.5v0a3.5 3.5 0 0 1-3.5-3.5V6z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>'
        '<path d="M8 12v2.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>'
    ),
    # Credential Store menu item — a key reads clearly as "secret/access".
    "key": (
        '<circle cx="5.2" cy="10.8" r="2.7" stroke="currentColor" stroke-width="1.3"/>'
        '<path d="M7.1 8.9L13 3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>'
        '<path d="M10.5 5.5l1.6 1.6M12.3 3.7l1.6 1.6" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>'
    ),
}


def _leaf(key: str, title: str, href: str, icon: str, *,
          comingSoon: bool = False, permission: Optional[str] = None,
          roles: Optional[List[str]] = None, feature_flag: Optional[str] = None,
          visible: bool = True, feature_key: Optional[str] = None) -> dict:
    return {
        "type": "item", "key": key, "title": title, "href": href, "icon": icon,
        "comingSoon": comingSoon,
        # RBAC scaffolding — unused until an auth/roles system exists.
        "permission": permission, "roles": roles, "feature_flag": feature_flag,
        "visible": visible,
        # Developer Mode (services/developer_mode.py) — when set, this
        # item's visibility/comingSoon state is computed from the
        # DeveloperFeatureManager instead of the static `visible` flag
        # above, so there's exactly one place (developer_mode.py) that
        # decides Category A/B visibility, never scattered per-template.
        "feature_key": feature_key,
    }


def _group(key: str, title: str, description: str, icon: str, items: List[dict], *,
           visible: bool = True) -> dict:
    return {"type": "group", "key": key, "title": title, "description": description,
            "icon": icon, "children": items, "visible": visible}


def _dynamic_slot(key: str) -> dict:
    """Marks a position in SIDEBAR_CONFIG where get_sidebar() must inject a
    group built from services/developer_mode.py::build_developer_menu_items_by_group()
    (Admin Navigation Refactor) — lets SIDEBAR_CONFIG stay the single
    source of truth for ORDERING (Dashboard, Knowledge, Integrations, AI,
    Developer Tools, Settings) while the dynamic groups' CONTENTS still
    come entirely from developer_mode.py, never a second hardcoded list."""
    return {"type": "dynamic_group_slot", "key": key}


# Metadata for each dynamic group slot — content (which items appear) is
# decided by developer_mode.py; this only supplies the group's own
# title/description/icon, since those aren't per-feature data.
_DYNAMIC_GROUP_META: Dict[str, dict] = {
    "integrations": {
        "title": "Integrations",
        "description": "Connect, configure, and test ERP or external business APIs.",
        "icon": "plug",
    },
    "developer_tools": {
        "title": "Developer Tools",
        "description": "Advanced developer, testing, validation, and integration tools.",
        "icon": "wrench",
    },
}


# ── The single source of truth ──────────────────────────────
# "comingSoon" items point at real, existing pages once those pages exist —
# today they render disabled, same as the pre-existing "System (Phase 2)"
# section in base.html, because building them is explicitly out of scope
# for this refactor ("Only implement the menus listed above" /
# "Do NOT create placeholder menus that are not currently part of the
# project" — these ARE part of the requested structure, but have no route
# yet, so they're shown-but-disabled rather than silently omitted or
# faked onto an unrelated page).
#
# Ordering below (Admin Navigation Refactor, 2026-07-22) is the exact
# required order: Dashboard, Knowledge, Integrations, AI, Developer
# Tools, Settings. "Integrations" and "Developer Tools" are
# _dynamic_slot() markers, not static groups — their contents are built
# from services/developer_mode.py::build_developer_menu_items_by_group()
# at render time (see get_sidebar()), so a feature's group membership is
# still decided in exactly one place.
SIDEBAR_CONFIG: List[dict] = [
    _leaf("dashboard", "Dashboard", "/admin/dashboard", "home"),
    # P3.1 / P3.2 — read-only LINE user profile + conversation-history viewer.
    _leaf("line-users", "LINE User Profile", "/admin/line-users", "user"),

    _group("knowledge", "Knowledge", "Manage all knowledge sources and synchronization.", "book", [
        _leaf("documents", "Knowledge Base", "/admin/documents", "database"),
        _leaf("file-library", "File Library", "/admin/file-library", "folder"),
        # Hidden 2026-08-13 per request — the route itself
        # (/admin/knowledge-collections) stays fully functional; this
        # only removes it from the nav (same convention as Sync Activity
        # below).
        _leaf("knowledge-collections", "Knowledge Collections", "/admin/knowledge-collections", "folder", visible=False),
        # Hidden for the customer UAT deployment (2026-08-10) — internal
        # dev/sync-history noise, not something the customer should see.
        # The route itself (/admin/sync-activity) stays fully functional;
        # this only removes it from the nav (CLAUDE.md's existing
        # "Admin sidebar navigation visibility" convention).
        _leaf("sync-activity", "Sync Activity", "/admin/sync-activity", "refresh", visible=False),
    ]),

    _dynamic_slot("integrations"),

    _group("ai", "AI", "Configure, test, and debug AI behaviour.", "sparkles", [
        # Most prominent item during the RAG demo phase — the whole
        # point of this phase is Upload -> Sync -> open THIS -> ask
        # questions -> inspect answer/sources/citations/confidence.
        # Listed first in the group.
        _leaf("preview", "AI Playground", "/admin/preview", "search"),
        _leaf("prompt-studio", "Prompt Studio", "/admin/ai/prompts", "edit"),
        _leaf("ai-policies", "AI Policies", "/admin/ai/policies", "shield"),
        _leaf("conversations", "Conversation History", "/admin/conversations", "table"),
        _leaf("golden", "Golden Test Runs", "/admin/golden", "check-circle"),
    ]),

    _dynamic_slot("developer_tools"),

    _leaf("settings", "Settings", "/admin/settings", "cog"),
]


def is_item_visible(item: dict, user: Optional[dict] = None) -> bool:
    """The single gate every future RBAC/feature-flag check plugs into.
    An item with a `feature_key` defers ENTIRELY to
    services/developer_mode.py::get_feature_state() (Developer Mode
    Category A/B) — HIDDEN excludes it, COMING_SOON/VISIBLE both keep it
    (get_sidebar() below sets the resulting `comingSoon` flag). An item
    with no `feature_key` falls back to the pre-existing static
    `visible` flag, unchanged."""
    if item.get("feature_key"):
        return get_feature_state(item["feature_key"]) != "HIDDEN"
    if not item.get("visible", True):
        return False
    # Future: check item["permission"] / item["roles"] against `user`
    # here. Left as a no-op until an auth/roles system exists.
    return True


def _build_dynamic_group(slot_key: str) -> Optional[dict]:
    """Builds the "Integrations" or "Developer Tools" group from
    services/developer_mode.py::build_developer_menu_items_by_group() —
    the single call site for both groups (Admin Navigation Refactor), so
    a feature's group membership is decided only in developer_mode.py's
    `navigation_group` field, never duplicated here. Items already come
    pre-filtered to exclude HIDDEN features; returns None (never an
    empty-children group) when nothing in the group is currently
    visible/coming-soon."""
    items = build_developer_menu_items_by_group(slot_key)
    if not items:
        return None
    meta = _DYNAMIC_GROUP_META[slot_key]
    children = []
    for i in items:
        children.append({
            "type": "item", "key": i["feature_key"], "title": i["title"],
            "href": i["route"] if i["route"] else "#",
            "icon": i["icon"], "icon_svg": ICONS.get(i["icon"], ""),
            "comingSoon": i["comingSoon"], "visible": True, "feature_key": i["feature_key"],
        })
    return {
        "type": "group", "key": slot_key.replace("_", "-"), "title": meta["title"],
        "description": meta["description"], "icon": meta["icon"],
        "icon_svg": ICONS.get(meta["icon"], ""), "children": children,
    }


def get_sidebar() -> List[dict]:
    """Return SIDEBAR_CONFIG with icon_svg baked in and filtered by
    is_item_visible, with the "Integrations" and "Developer Tools" groups
    built in-place at their _dynamic_slot() markers from
    services/developer_mode.py — this keeps the exact required ordering
    (Dashboard, Knowledge, Integrations, AI, Developer Tools, Settings)
    instead of appending dynamic groups after the whole static list.
    Deliberately does NOT take `active` — most existing page templates
    set `active` via a top-level `{% set active = "..." %}` in the CHILD
    template (a Jinja pattern visible to base.html at render time but
    never reaching the Python context dict), so active/expanded matching
    is done in Jinja (base.html), not here. This function only owns
    structure + visibility, which are the same on every page.
    """
    result = []
    for entry in SIDEBAR_CONFIG:
        if entry["type"] == "dynamic_group_slot":
            group = _build_dynamic_group(entry["key"])
            if group:
                result.append(group)
            continue
        if not is_item_visible(entry):
            continue
        if entry["type"] == "item":
            node = dict(entry)
            if node.get("feature_key"):
                node["comingSoon"] = get_feature_state(node["feature_key"]) == FEATURE_STATE_COMING_SOON
            node["icon_svg"] = ICONS.get(entry["icon"], "")
            result.append(node)
        else:  # group
            child_nodes = []
            for child in entry["children"]:
                if not is_item_visible(child):
                    continue
                c = dict(child)
                if c.get("feature_key"):
                    c["comingSoon"] = get_feature_state(c["feature_key"]) == FEATURE_STATE_COMING_SOON
                c["icon_svg"] = ICONS.get(child["icon"], "")
                child_nodes.append(c)
            if not child_nodes:
                # A group whose every child is hidden (by feature flag or
                # otherwise) must never render as an empty menu group —
                # drop it entirely rather than leaving a dangling header/
                # divider with nothing underneath.
                continue
            node = dict(entry)
            node["icon_svg"] = ICONS.get(entry["icon"], "")
            node["children"] = child_nodes
            result.append(node)

    return result
