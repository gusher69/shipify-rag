"""Shared test-only Admin login helper.

Security hardening (2026-08-19) — every test that needed an authenticated
admin session previously logged in with the REAL production password
hardcoded as a literal string, committed in plaintext across 7 test files.
That literal was confirmed (via hash comparison, never printed) to match
the actual live ADMIN_PASSWORD on the customer's production server — a
genuine, persistent credential exposure via git history, independent of
this fix.

This helper never touches or needs to know the real credential. It
monkeypatches admin.routes.ADMIN_USERNAME/ADMIN_PASSWORD to a fixed,
obviously-fake test-only value for the exact duration of the login POST,
then logs in against that patched pair. admin/routes.py's own auth()
never re-checks username/password on later requests — it only verifies
the signed session_token cookie set at login — so the patch does not need
to (and does not) stay active for the rest of the test.
"""
from unittest.mock import patch

TEST_ADMIN_USERNAME = "test-admin"
TEST_ADMIN_PASSWORD = "test-only-not-a-real-credential"  # noqa: S105 (not a real secret)


def login_as_test_admin(client):
    """POSTs /admin/login using fake, test-only credentials — never the
    real ones. `client` is a starlette.testclient.TestClient (or
    compatible); the resulting session cookie is stored on it as usual."""
    import admin.routes as routes_module
    with patch.object(routes_module, "ADMIN_USERNAME", TEST_ADMIN_USERNAME), \
         patch.object(routes_module, "ADMIN_PASSWORD", TEST_ADMIN_PASSWORD):
        return client.post("/admin/login", data={
            "username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD,
        })
