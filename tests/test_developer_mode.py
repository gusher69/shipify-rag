"""Regression tests for the centralized Developer Mode feature-visibility
system (Developer Mode milestone): services/developer_mode.py is the
single source of truth for which admin menus are visible/clickable, and
admin/sidebar_config.py must never duplicate that logic.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient

from tests._admin_test_auth import login_as_test_admin

from services.developer_mode import (
    is_developer_mode_enabled, get_feature_state, is_feature_route_accessible,
    build_developer_menu_items, FEATURE_STATE_VISIBLE, FEATURE_STATE_HIDDEN,
    FEATURE_STATE_COMING_SOON, FEATURE_DEFINITIONS,
)


def _no_env(**overrides):
    """A clean environment with no Developer Mode / SHOW_* vars set at
    all, plus any explicit overrides for a specific test."""
    base = {k: "" for k in ("DEVELOPER_MODE", "SHOW_AI_EVALUATION",
                             "SHOW_PRODUCTION_VALIDATION", "SHOW_BUSINESS_ACTION_CENTER")}
    # An empty string is falsy for our purposes but os.getenv treats
    # "" as present — use del via a dict comprehension of keys to drop.
    env = dict(os.environ)
    for k in base:
        env.pop(k, None)
    env.update(overrides)
    return env


class TestDeveloperModeDefaultsOff(unittest.TestCase):
    def test_defaults_to_off_when_unset(self):
        with patch.dict(os.environ, _no_env(), clear=True):
            self.assertFalse(is_developer_mode_enabled())

    def test_explicit_false_is_off(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="false"), clear=True):
            self.assertFalse(is_developer_mode_enabled())

    def test_explicit_true_is_on(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true"), clear=True):
            self.assertTrue(is_developer_mode_enabled())


class TestNormalMenusUnaffected(unittest.TestCase):
    """Category C — a feature with no developer_only flag is always
    VISIBLE regardless of Developer Mode. business_action_center
    (ERP Integration) was moved into this category on 2026-07-21 — it
    now behaves like Knowledge Base, never gated behind Developer Mode,
    while keeping its SHOW_BUSINESS_ACTION_CENTER hard-disable escape
    hatch."""

    def test_only_developer_only_features_are_governed(self):
        for key, feature in FEATURE_DEFINITIONS.items():
            if key in ("business_action_center", "erp_conversation_tester", "credential_store"):
                self.assertFalse(feature["developer_only"], msg=key)
                continue
            self.assertTrue(feature["developer_only"], msg=key)

    def test_erp_integration_always_visible_regardless_of_developer_mode(self):
        for dev_mode in ("true", "false"):
            with patch.dict(os.environ, _no_env(DEVELOPER_MODE=dev_mode), clear=True):
                self.assertEqual(get_feature_state("business_action_center"), FEATURE_STATE_VISIBLE,
                                  msg=f"dev_mode={dev_mode}")
                self.assertTrue(is_feature_route_accessible("business_action_center"), msg=f"dev_mode={dev_mode}")

    def test_erp_conversation_tester_always_visible_regardless_of_developer_mode(self):
        for dev_mode in ("true", "false"):
            with patch.dict(os.environ, _no_env(DEVELOPER_MODE=dev_mode), clear=True):
                self.assertEqual(get_feature_state("erp_conversation_tester"), FEATURE_STATE_VISIBLE,
                                  msg=f"dev_mode={dev_mode}")
                self.assertTrue(is_feature_route_accessible("erp_conversation_tester"), msg=f"dev_mode={dev_mode}")

    def test_credential_store_always_visible_regardless_of_developer_mode(self):
        for dev_mode in ("true", "false"):
            with patch.dict(os.environ, _no_env(DEVELOPER_MODE=dev_mode), clear=True):
                self.assertEqual(get_feature_state("credential_store"), FEATURE_STATE_VISIBLE,
                                  msg=f"dev_mode={dev_mode}")
                self.assertTrue(is_feature_route_accessible("credential_store"), msg=f"dev_mode={dev_mode}")


class TestCompletedDeveloperMenus(unittest.TestCase):
    """Category A — AI Evaluation / Production Validation."""

    def test_hidden_when_developer_mode_off(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="false"), clear=True):
            for key in ("rag_benchmark", "ai_validation"):
                self.assertEqual(get_feature_state(key), FEATURE_STATE_HIDDEN, msg=key)

    def test_visible_when_developer_mode_on(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true"), clear=True):
            for key in ("rag_benchmark", "ai_validation"):
                self.assertEqual(get_feature_state(key), FEATURE_STATE_VISIBLE, msg=key)

    def test_route_accessible_only_when_visible(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true"), clear=True):
            self.assertTrue(is_feature_route_accessible("rag_benchmark"))
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="false"), clear=True):
            self.assertFalse(is_feature_route_accessible("rag_benchmark"))


class TestComingSoonDeveloperMenus(unittest.TestCase):
    """Category B — Excel Engine / Attachment Manager / Storage /
    User Profiles / LINE Analytics / ERP Sync."""

    def test_appear_as_coming_soon_when_on(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true"), clear=True):
            for key in ("excel_engine", "attachment_manager", "storage",
                        "user_profiles", "line_analytics"):
                self.assertEqual(get_feature_state(key), FEATURE_STATE_COMING_SOON, msg=key)
            items = build_developer_menu_items()
            coming_soon_keys = {i["feature_key"] for i in items if i["comingSoon"]}
            self.assertEqual(coming_soon_keys, {"excel_engine", "attachment_manager", "storage",
                                                 "user_profiles", "line_analytics"})

    def test_hidden_completely_when_off(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="false"), clear=True):
            for key in ("excel_engine", "attachment_manager", "storage",
                        "user_profiles", "line_analytics"):
                self.assertEqual(get_feature_state(key), FEATURE_STATE_HIDDEN, msg=key)
            # business_action_center (ERP Integration), erp_conversation_tester
            # (ERP Conversation Tester), and credential_store (Credential
            # Store) are all Category C — always VISIBLE, the only items
            # build_developer_menu_items() still returns with Developer
            # Mode off.
            remaining_keys = {i["feature_key"] for i in build_developer_menu_items()}
            self.assertEqual(remaining_keys, {"business_action_center", "erp_conversation_tester", "credential_store"})

    def test_coming_soon_never_accessible_as_a_route(self):
        for feature in FEATURE_DEFINITIONS.values():
            if feature["implementation_status"] == "coming_soon":
                self.assertIsNone(feature["route"])

    def test_erp_sync_removed_as_placeholder_duplicate(self):
        """erp_sync was a placeholder Coming Soon entry that duplicated
        business_action_center's real ERP integration functionality —
        removed entirely per the Admin Navigation Refactor audit."""
        self.assertNotIn("erp_sync", FEATURE_DEFINITIONS)


class TestEnvironmentHardDisableOverridesDeveloperMode(unittest.TestCase):
    def test_env_false_hides_feature_even_with_developer_mode_on(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true", SHOW_BUSINESS_ACTION_CENTER="false"),
                         clear=True):
            self.assertEqual(get_feature_state("business_action_center"), FEATURE_STATE_HIDDEN)
            # The other two Category A features are unaffected by this one flag.
            self.assertEqual(get_feature_state("rag_benchmark"), FEATURE_STATE_VISIBLE)

    def test_env_unset_or_true_does_not_hide(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true"), clear=True):
            self.assertEqual(get_feature_state("business_action_center"), FEATURE_STATE_VISIBLE)
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true", SHOW_BUSINESS_ACTION_CENTER="true"),
                         clear=True):
            self.assertEqual(get_feature_state("business_action_center"), FEATURE_STATE_VISIBLE)


class TestSettingsPersistence(unittest.TestCase):
    """Reuses the EXISTING .env-file settings mechanism
    (admin/routes.py's _read_env()/_write_env()) — no new storage layer."""

    def test_write_env_persists_to_file_and_updates_live_process_env(self):
        import tempfile
        from pathlib import Path
        import admin.routes as routes_module

        with tempfile.TemporaryDirectory() as tmp:
            fake_env_path = Path(tmp) / ".env"
            fake_env_path.write_text("EXISTING_KEY=1\n", encoding="utf-8")
            original_path = routes_module.ENV_PATH
            try:
                routes_module.ENV_PATH = fake_env_path
                routes_module._write_env({"DEVELOPER_MODE": "true"})
                content = fake_env_path.read_text(encoding="utf-8")
                self.assertIn("DEVELOPER_MODE=true", content)
                self.assertIn("EXISTING_KEY=1", content)
                # Round-trips through _read_env() too.
                routes_module.ENV_PATH = fake_env_path
                env = routes_module._read_env()
                self.assertEqual(env.get("DEVELOPER_MODE"), "true")
                # And takes effect immediately in the live process env —
                # not just on next restart.
                self.assertEqual(os.environ.get("DEVELOPER_MODE"), "true")
            finally:
                routes_module.ENV_PATH = original_path
                os.environ.pop("DEVELOPER_MODE", None)

    def test_developer_section_key_is_registered(self):
        import admin.routes as routes_module
        self.assertIn("developer", routes_module.SECTION_KEYS)
        self.assertEqual(routes_module.SECTION_KEYS["developer"], ["DEVELOPER_MODE"])


class TestServerSideRouteGuard(unittest.TestCase):
    def test_require_developer_feature_redirects_when_off(self):
        import admin.routes as routes_module
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="false"), clear=True):
            result = routes_module.require_developer_feature(None, "rag_benchmark")
            self.assertIsNotNone(result)
            self.assertEqual(result.status_code, 302)
            self.assertIn("/admin/settings", result.headers["location"])

    def test_require_developer_feature_allows_when_on(self):
        import admin.routes as routes_module
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true"), clear=True):
            result = routes_module.require_developer_feature(None, "rag_benchmark")
            self.assertIsNone(result)

    def test_erp_integration_route_never_blocked_by_developer_mode(self):
        """business_action_center is Category C now (see
        TestNormalMenusUnaffected) — the route guard call in
        admin/routes.py's business_actions_page() is a no-op in normal
        operation; it only ever redirects if SHOW_BUSINESS_ACTION_CENTER
        is explicitly hard-disabled."""
        import admin.routes as routes_module
        for dev_mode in ("true", "false"):
            with patch.dict(os.environ, _no_env(DEVELOPER_MODE=dev_mode), clear=True):
                result = routes_module.require_developer_feature(None, "business_action_center")
                self.assertIsNone(result, msg=f"dev_mode={dev_mode}")


class TestSidebarIntegration(unittest.TestCase):
    def test_no_duplicate_menu_keys(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true"), clear=True):
            from admin.sidebar_config import get_sidebar
            keys = []
            for entry in get_sidebar():
                keys.append(entry["key"])
                if entry["type"] == "group":
                    keys.extend(c["key"] for c in entry["children"])
            self.assertEqual(len(keys), len(set(keys)), msg=f"duplicate keys found: {keys}")

    def test_erp_integration_present_in_integrations_group_regardless_of_developer_mode(self):
        """ERP Integration behaves like Knowledge Base — always visible,
        never gated behind Developer Mode (user request, 2026-07-21)."""
        for dev_mode in ("true", "false"):
            with patch.dict(os.environ, _no_env(DEVELOPER_MODE=dev_mode), clear=True):
                from admin.sidebar_config import get_sidebar
                integrations_group = next(e for e in get_sidebar() if e["key"] == "integrations")
                child_keys = {c["key"]: c for c in integrations_group["children"]}
                self.assertIn("business_action_center", child_keys, msg=f"dev_mode={dev_mode}")
                self.assertEqual(child_keys["business_action_center"]["title"], "ERP Integration")
                self.assertEqual(child_keys["business_action_center"]["href"], "/admin/ai/business-actions")
                self.assertFalse(child_keys["business_action_center"]["comingSoon"])

    def test_ai_evaluation_and_validation_present_in_developer_tools_when_on(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true"), clear=True):
            from admin.sidebar_config import get_sidebar
            dev_tools_group = next(e for e in get_sidebar() if e["key"] == "developer-tools")
            child_keys = {c["key"] for c in dev_tools_group["children"]}
            self.assertIn("rag_benchmark", child_keys)
            self.assertIn("ai_validation", child_keys)

    def test_category_a_items_absent_when_off(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="false"), clear=True):
            from admin.sidebar_config import get_sidebar
            keys = [e["key"] for e in get_sidebar()]
            # Integrations (ERP Integration) is Category C now — stays
            # present even with Developer Mode off.
            self.assertIn("integrations", keys)
            self.assertNotIn("developer-tools", keys)
            # Always-on Category C items in the AI group are unaffected.
            ai_group = next(e for e in get_sidebar() if e["key"] == "ai")
            child_keys = {c["key"] for c in ai_group["children"]}
            self.assertIn("preview", child_keys)
            self.assertIn("prompt-studio", child_keys)
            self.assertIn("ai-policies", child_keys)

    def test_ai_group_contains_only_normal_ai_features(self):
        for dev_mode in ("true", "false"):
            with patch.dict(os.environ, _no_env(DEVELOPER_MODE=dev_mode), clear=True):
                from admin.sidebar_config import get_sidebar
                ai_group = next(e for e in get_sidebar() if e["key"] == "ai")
                child_keys = {c["key"] for c in ai_group["children"]}
                # "conversations" (Conversation History, Phase 3.7, 2026-08-05)
                # and "golden" (Golden Test Runs, Golden Test Harness rebuild,
                # 2026-08-16) are normal, always-visible AI-group items, same
                # as the three that predate them — not developer-mode-gated.
                self.assertEqual(child_keys, {"preview", "prompt-studio", "ai-policies", "conversations", "golden"},
                                  msg=f"dev_mode={dev_mode}")

    def test_developer_tools_group_present_only_when_on(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true"), clear=True):
            from admin.sidebar_config import get_sidebar
            keys = [e["key"] for e in get_sidebar()]
            self.assertIn("developer-tools", keys)
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="false"), clear=True):
            from admin.sidebar_config import get_sidebar
            keys = [e["key"] for e in get_sidebar()]
            self.assertNotIn("developer-tools", keys)

    def test_no_business_action_center_string_in_sidebar(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true"), clear=True):
            from admin.sidebar_config import get_sidebar
            titles = []
            for e in get_sidebar():
                titles.append(e["title"])
                if e["type"] == "group":
                    titles.extend(c["title"] for c in e["children"])
            self.assertNotIn("Business Action Center", titles)
            self.assertIn("ERP Integration", titles)

    def test_no_erp_sync_coming_soon_item(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true"), clear=True):
            from admin.sidebar_config import get_sidebar
            keys = []
            for e in get_sidebar():
                keys.append(e["key"])
                if e["type"] == "group":
                    keys.extend(c["key"] for c in e["children"])
            self.assertNotIn("erp_sync", keys)
            self.assertNotIn("erp-sync", keys)

    def test_erp_related_menu_items_are_integration_and_conversation_tester(self):
        """Product Architecture Separation sprint — ERP Integration
        (configuration) and ERP Conversation Tester (testing) are two
        DELIBERATELY separate menu items under Integrations, never
        merged and never duplicated beyond these two."""
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true"), clear=True):
            from admin.sidebar_config import get_sidebar
            erp_titles = []
            for e in get_sidebar():
                if e["type"] == "group":
                    erp_titles.extend(c["title"] for c in e["children"] if "ERP" in c["title"])
            self.assertEqual(erp_titles, ["ERP Integration", "ERP Conversation Tester"])

    def test_sidebar_group_order(self):
        with patch.dict(os.environ, _no_env(DEVELOPER_MODE="true"), clear=True):
            from admin.sidebar_config import get_sidebar
            keys = [e["key"] for e in get_sidebar()]
            # P3.1 — read-only "ผู้ใช้งาน LINE" viewer sits directly under Dashboard.
            self.assertEqual(keys, ["dashboard", "line-users", "knowledge", "integrations",
                                     "ai", "developer-tools", "settings"])

    def test_normal_admin_menus_always_present_regardless_of_mode(self):
        for dev_mode in ("true", "false"):
            with patch.dict(os.environ, _no_env(DEVELOPER_MODE=dev_mode), clear=True):
                from admin.sidebar_config import get_sidebar
                keys = [e["key"] for e in get_sidebar()]
                for expected in ("dashboard", "knowledge", "ai", "settings"):
                    self.assertIn(expected, keys, msg=f"dev_mode={dev_mode}")


class TestPlaygroundDevModeChatBubbleWiring(unittest.TestCase):
    """AI Playground Chat Bubble Dev/Customer View (2026-08-17) — reuses
    THIS SAME Settings-page Developer Mode toggle (no new per-page
    switch) to decide whether the Auto-mode chat bubble shows the
    technical/dev view or the exact LINE OA customer view. Only the
    server-rendered JS global (DEV_MODE_ENABLED, admin/templates/
    preview.html) is tested here — the ternary logic that consumes it is
    covered by manual browser verification (see commit message); a full
    JS unit-test harness doesn't exist in this codebase."""

    def setUp(self):
        from admin.routes import app
        self.client = TestClient(app)
        login_as_test_admin(self.client)

    def test_dev_mode_off_by_default_renders_false(self):
        with patch.dict(os.environ, {"DEVELOPER_MODE": "false"}):
            resp = self.client.get("/admin/preview")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("var DEV_MODE_ENABLED = false;", resp.text)

    def test_dev_mode_on_renders_true(self):
        with patch.dict(os.environ, {"DEVELOPER_MODE": "true"}):
            resp = self.client.get("/admin/preview")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("var DEV_MODE_ENABLED = true;", resp.text)


if __name__ == "__main__":
    unittest.main()
