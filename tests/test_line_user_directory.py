"""P3.1 — Admin User Profile Viewer (read-only) backend tests.

Covers services/line_user_directory.py: the user_profiles ↔
customer_channel_bindings join, verified/unverified mapping, search by
name and by verified CustCode, the status filter, the fixed query shape
(no per-row binding query), and that a legacy user_profiles.cust_code is
never surfaced as a verified identity.
"""
import unittest

from tests.test_business_action_registry import _FakeSupabase
from services.line_user_directory import list_line_users, get_line_user_detail


def _seed(sb):
    sb.store["user_profiles"] = [
        {"line_user_id": "U1111aaaa2222bbbb3333", "display_name": "สมชาย ใจดี",
         "first_seen": "2026-08-01T00:00:00Z", "updated_at": "2026-09-01T10:00:00Z",
         "message_count": 12, "conversation_count": 3, "created_at": "2026-08-01T00:00:00Z",
         # legacy customer-typed convenience cache — must NEVER show as verified
         "cust_code": "TYPED9999"},
        {"line_user_id": "U4444cccc5555dddd6666", "display_name": "Anna Wong",
         "first_seen": "2026-08-10T00:00:00Z", "updated_at": "2026-09-02T09:00:00Z",
         "message_count": 4, "conversation_count": 1, "created_at": "2026-08-10T00:00:00Z"},
        {"line_user_id": "U7777eeee8888ffff9999", "display_name": "ร้านค้า B",
         "first_seen": "2026-07-01T00:00:00Z", "updated_at": "2026-08-15T09:00:00Z",
         "message_count": 40, "conversation_count": 9, "created_at": "2026-07-01T00:00:00Z"},
    ]
    sb.store["customer_channel_bindings"] = [
        {"external_user_id": "U1111aaaa2222bbbb3333", "cust_code": "FT5001", "status": "verified",
         "channel": "line", "verification_method": "staff_assisted",
         "verified_at": "2026-08-20T00:00:00Z", "updated_at": "2026-08-20T00:00:00Z",
         "created_at": "2026-08-20T00:00:00Z"},
        {"external_user_id": "U7777eeee8888ffff9999", "cust_code": "FT5002", "status": "revoked",
         "channel": "line", "verified_at": "2026-07-05T00:00:00Z", "updated_at": "2026-07-30T00:00:00Z",
         "created_at": "2026-07-05T00:00:00Z"},
    ]
    return sb


class ListMappingAndSummary(unittest.TestCase):
    def setUp(self):
        self.sb = _seed(_FakeSupabase())

    def test_verified_user_shows_bound_custcode_and_label(self):
        u = next(x for x in list_line_users(sb=self.sb)["users"]
                 if x["line_user_id"] == "U1111aaaa2222bbbb3333")
        self.assertTrue(u["verified"])
        self.assertEqual(u["cust_code"], "FT5001")
        self.assertEqual(u["status_label"], "ยืนยันแล้ว")
        self.assertEqual(u["line_user_id_masked"], "U1111...3333")

    def test_unverified_user_has_no_custcode(self):
        u = next(x for x in list_line_users(sb=self.sb)["users"]
                 if x["line_user_id"] == "U4444cccc5555dddd6666")
        self.assertFalse(u["verified"])
        self.assertIsNone(u["cust_code"])
        self.assertEqual(u["status_label"], "ยังไม่ยืนยัน")

    def test_revoked_binding_is_not_verified(self):
        u = next(x for x in list_line_users(sb=self.sb)["users"]
                 if x["line_user_id"] == "U7777eeee8888ffff9999")
        self.assertFalse(u["verified"])
        self.assertIsNone(u["cust_code"])

    def test_legacy_typed_custcode_never_surfaces(self):
        rows = list_line_users(sb=self.sb)["users"]
        self.assertNotIn("TYPED9999", [r["cust_code"] for r in rows])

    def test_summary_counts(self):
        s = list_line_users(sb=self.sb)["summary"]
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["verified"], 1)
        self.assertEqual(s["unverified"], 2)

    def test_sorted_most_recent_first(self):
        rows = list_line_users(sb=self.sb)["users"]
        self.assertEqual(rows[0]["line_user_id"], "U4444cccc5555dddd6666")  # updated 09-02


class SearchAndFilter(unittest.TestCase):
    def setUp(self):
        self.sb = _seed(_FakeSupabase())

    def test_search_by_display_name(self):
        rows = list_line_users(search="anna", sb=self.sb)["users"]
        self.assertEqual([r["display_name"] for r in rows], ["Anna Wong"])

    def test_search_by_thai_name(self):
        rows = list_line_users(search="สมชาย", sb=self.sb)["users"]
        self.assertEqual([r["line_user_id"] for r in rows], ["U1111aaaa2222bbbb3333"])

    def test_search_by_verified_custcode(self):
        rows = list_line_users(search="FT5001", sb=self.sb)["users"]
        self.assertEqual([r["line_user_id"] for r in rows], ["U1111aaaa2222bbbb3333"])

    def test_search_by_revoked_custcode_returns_nothing(self):
        # FT5002's binding is revoked -> not a verified cust_code -> not searchable
        self.assertEqual(list_line_users(search="FT5002", sb=self.sb)["users"], [])

    def test_status_filter_verified(self):
        rows = list_line_users(status="verified", sb=self.sb)["users"]
        self.assertEqual([r["line_user_id"] for r in rows], ["U1111aaaa2222bbbb3333"])

    def test_status_filter_unverified(self):
        ids = {r["line_user_id"] for r in list_line_users(status="unverified", sb=self.sb)["users"]}
        self.assertEqual(ids, {"U4444cccc5555dddd6666", "U7777eeee8888ffff9999"})

    def test_no_result_is_empty_list(self):
        self.assertEqual(list_line_users(search="zzz-nobody", sb=self.sb)["users"], [])


class QueryShapeNoNPlusOne(unittest.TestCase):
    def test_list_is_two_reads_regardless_of_row_count(self):
        sb = _seed(_FakeSupabase())
        seen = []
        real_table = sb.table
        sb.table = lambda name: (seen.append(name), real_table(name))[1]
        list_line_users(sb=sb)
        self.assertEqual(seen.count("customer_channel_bindings"), 1)
        self.assertEqual(seen.count("user_profiles"), 1)
        self.assertEqual(len(seen), 2)  # never one binding query per row


class Detail(unittest.TestCase):
    def setUp(self):
        self.sb = _seed(_FakeSupabase())

    def test_verified_detail(self):
        d = get_line_user_detail("U1111aaaa2222bbbb3333", sb=self.sb)
        self.assertEqual(d["profile"]["display_name"], "สมชาย ใจดี")
        self.assertEqual(d["profile"]["message_count"], 12)
        self.assertTrue(d["binding"]["verified"])
        self.assertEqual(d["binding"]["cust_code"], "FT5001")
        self.assertEqual(d["binding"]["verification_method"], "staff_assisted")

    def test_unverified_detail_no_custcode(self):
        d = get_line_user_detail("U4444cccc5555dddd6666", sb=self.sb)
        self.assertFalse(d["binding"]["verified"])
        self.assertIsNone(d["binding"]["cust_code"])
        self.assertEqual(d["binding"]["status_label"], "ยังไม่ยืนยัน")

    def test_revoked_detail_not_verified(self):
        d = get_line_user_detail("U7777eeee8888ffff9999", sb=self.sb)
        self.assertFalse(d["binding"]["verified"])

    def test_unknown_user_returns_none(self):
        self.assertIsNone(get_line_user_detail("Unope", sb=self.sb))


class RoutesAreReadOnlyAndAuthGuarded(unittest.TestCase):
    """Acceptance G/H — the viewer is admin-only and offers no write op."""

    def _line_user_routes(self):
        from admin.routes import app
        return [r for r in app.routes
                if getattr(r, "path", "").startswith("/admin/line-users")
                or getattr(r, "path", "").startswith("/admin/api/line-users")]

    def test_only_get_methods_registered(self):
        routes = self._line_user_routes()
        self.assertEqual(len(routes), 3)
        for r in routes:
            self.assertEqual(set(r.methods) - {"HEAD", "OPTIONS"}, {"GET"},
                             msg=f"{r.path} exposes a non-GET method")

    def test_unauthenticated_request_is_redirected_to_login(self):
        from starlette.testclient import TestClient
        from admin.routes import app
        c = TestClient(app)
        for path in ("/admin/line-users", "/admin/api/line-users",
                     "/admin/api/line-users/Uwhoever"):
            resp = c.get(path, follow_redirects=False)
            self.assertIn(resp.status_code, (302, 307), msg=path)
            self.assertIn("/admin/login", resp.headers.get("location", ""), msg=path)


if __name__ == "__main__":
    unittest.main()
