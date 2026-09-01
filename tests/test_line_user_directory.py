"""P3.1 — Admin User Profile Viewer (read-only) backend tests.

Covers services/line_user_directory.py: the user_profiles ↔
customer_channel_bindings join, verified/unverified mapping, search by
name and by verified CustCode, the status filter, the fixed query shape
(no per-row binding query), and that a legacy user_profiles.cust_code is
never surfaced as a verified identity.
"""
import unittest

from tests.test_business_action_registry import _FakeSupabase
from services.line_user_directory import (
    list_line_users, get_line_user_detail,
    list_user_sessions, get_user_session_messages,
)


def _seed(sb):
    # NOTE: user_profiles has `last_active` (not `updated_at`) as its
    # last-activity column — mirrors the real production schema.
    sb.store["user_profiles"] = [
        {"line_user_id": "U1111aaaa2222bbbb3333", "display_name": "สมชาย ใจดี",
         "first_seen": "2026-08-01T00:00:00Z", "last_active": "2026-09-01T10:00:00Z",
         "message_count": 12, "conversation_count": 3, "created_at": "2026-08-01T00:00:00Z",
         # legacy customer-typed convenience cache — must NEVER show as verified
         "cust_code": "TYPED9999"},
        {"line_user_id": "U4444cccc5555dddd6666", "display_name": "Anna Wong",
         "first_seen": "2026-08-10T00:00:00Z", "last_active": "2026-09-02T09:00:00Z",
         "message_count": 4, "conversation_count": 1, "created_at": "2026-08-10T00:00:00Z"},
        {"line_user_id": "U7777eeee8888ffff9999", "display_name": "ร้านค้า B",
         "first_seen": "2026-07-01T00:00:00Z", "last_active": "2026-08-15T09:00:00Z",
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


# ── P3.2 — Conversation History Viewer ────────────────────────────────

_UA = "U1111aaaa2222bbbb3333"   # user A (from _seed)
_UB = "U4444cccc5555dddd6666"   # user B


def _seed_sessions(sb):
    _seed(sb)
    sb.store["ai_sessions"] = [
        {"id": "sessA1", "name": "อยากนำเข้าสินค้าจากจีน", "channel": "line",
         "line_user_id": _UA, "message_count": 4, "deleted_at": None,
         "created_at": "2026-09-01T16:20:00Z", "updated_at": "2026-09-01T16:28:00Z",
         "last_message_at": "2026-09-01T16:28:00Z"},
        {"id": "sessA2", "name": "สอบถามค่าส่ง", "channel": "line",
         "line_user_id": _UA, "message_count": 2, "deleted_at": None,
         "created_at": "2026-09-03T09:00:00Z", "updated_at": "2026-09-03T09:05:00Z",
         "last_message_at": "2026-09-03T09:05:00Z"},
        {"id": "sessA_del", "name": "ลบแล้ว", "channel": "line",
         "line_user_id": _UA, "message_count": 2, "deleted_at": "2026-09-02T00:00:00Z",
         "created_at": "2026-09-02T09:00:00Z", "updated_at": "2026-09-02T09:05:00Z"},
        {"id": "sessA_pg", "name": "playground run", "channel": "playground",
         "line_user_id": _UA, "message_count": 6, "deleted_at": None,
         "created_at": "2026-09-04T09:00:00Z", "updated_at": "2026-09-04T09:05:00Z"},
        {"id": "sessB1", "name": "เบอร์โกดัง", "channel": "line",
         "line_user_id": _UB, "message_count": 2, "deleted_at": None,
         "created_at": "2026-09-01T10:00:00Z", "updated_at": "2026-09-01T10:02:00Z",
         "last_message_at": "2026-09-01T10:02:00Z"},
    ]
    sb.store["ai_session_messages"] = [
        {"session_id": "sessA1", "turn_index": 0, "role": "user",
         "content": "อยากนำเข้าสินค้าจากจีน", "created_at": "2026-09-01T16:20:00Z"},
        {"session_id": "sessA1", "turn_index": 1, "role": "assistant",
         "content": "ต้องการนำเข้าสินค้าประเภทไหนคะ", "created_at": "2026-09-01T16:20:05Z"},
        {"session_id": "sessA1", "turn_index": 2, "role": "user",
         "content": "น้ำหอมครับ", "created_at": "2026-09-01T16:21:00Z"},
        {"session_id": "sessA1", "turn_index": 3, "role": "assistant",
         "content": "น้ำหอมจัดเป็นของเหลว...", "created_at": "2026-09-01T16:21:05Z"},
        # an internal/system row that must never be rendered
        {"session_id": "sessA1", "turn_index": 4, "role": "system",
         "content": "INTERNAL: rag_chunk_ids=[...] prompt_template=v7", "created_at": "2026-09-01T16:21:06Z"},
        {"session_id": "sessA2", "turn_index": 0, "role": "user",
         "content": "ค่าส่งเท่าไหร่", "created_at": "2026-09-03T09:00:00Z"},
        {"session_id": "sessA2", "turn_index": 1, "role": "assistant",
         "content": "ขึ้นกับน้ำหนักค่ะ", "created_at": "2026-09-03T09:00:05Z"},
        {"session_id": "sessB1", "turn_index": 0, "role": "user",
         "content": "ขอเบอร์โกดังไทย", "created_at": "2026-09-01T10:00:00Z"},
        {"session_id": "sessB1", "turn_index": 1, "role": "assistant",
         "content": "02-123-4567 ค่ะ", "created_at": "2026-09-01T10:00:05Z"},
    ]
    # traces / events exist but must never be read by the viewer
    sb.store["ai_session_traces"] = [
        {"session_id": "sessA1", "message_id": "x", "chunks": [{"text": "SECRET CHUNK"}],
         "prompt": {"system_prompt": "you are ..."}, "policy": {}},
    ]
    return sb


class SessionList(unittest.TestCase):
    def setUp(self):
        self.sb = _seed_sessions(_FakeSupabase())

    def test_lists_only_this_users_line_sessions(self):
        rows = list_user_sessions(_UA, sb=self.sb)
        ids = [r["session_id"] for r in rows]
        self.assertIn("sessA1", ids)
        self.assertIn("sessA2", ids)
        self.assertNotIn("sessB1", ids)      # user isolation
        self.assertNotIn("sessA_del", ids)   # soft-deleted excluded
        self.assertNotIn("sessA_pg", ids)    # non-line channel excluded

    def test_most_recent_first(self):
        rows = list_user_sessions(_UA, sb=self.sb)
        self.assertEqual([r["session_id"] for r in rows], ["sessA2", "sessA1"])

    def test_row_shape_and_message_count(self):
        row = next(r for r in list_user_sessions(_UA, sb=self.sb) if r["session_id"] == "sessA1")
        self.assertEqual(row["message_count"], 4)
        self.assertEqual(row["started_at"], "2026-09-01T16:20:00Z")
        self.assertEqual(row["last_active_at"], "2026-09-01T16:28:00Z")
        self.assertEqual(row["name"], "อยากนำเข้าสินค้าจากจีน")

    def test_unknown_user_has_no_sessions(self):
        self.assertEqual(list_user_sessions("Unobody", sb=self.sb), [])


class Transcript(unittest.TestCase):
    def setUp(self):
        self.sb = _seed_sessions(_FakeSupabase())

    def test_returns_user_and_assistant_in_order(self):
        out = get_user_session_messages(_UA, "sessA1", sb=self.sb)
        self.assertEqual([(m["role"], m["content"]) for m in out["messages"]], [
            ("user", "อยากนำเข้าสินค้าจากจีน"),
            ("assistant", "ต้องการนำเข้าสินค้าประเภทไหนคะ"),
            ("user", "น้ำหอมครับ"),
            ("assistant", "น้ำหอมจัดเป็นของเหลว..."),
        ])

    def test_internal_roles_excluded(self):
        out = get_user_session_messages(_UA, "sessA1", sb=self.sb)
        self.assertTrue(all(m["role"] in ("user", "assistant") for m in out["messages"]))
        blob = repr(out)
        self.assertNotIn("INTERNAL:", blob)
        self.assertNotIn("SECRET CHUNK", blob)
        self.assertNotIn("system_prompt", blob)

    def test_message_dict_has_only_safe_keys(self):
        out = get_user_session_messages(_UA, "sessA1", sb=self.sb)
        for m in out["messages"]:
            self.assertEqual(set(m.keys()), {"role", "content", "at"})

    def test_other_sessions_messages_not_included(self):
        out = get_user_session_messages(_UA, "sessA1", sb=self.sb)
        contents = [m["content"] for m in out["messages"]]
        self.assertNotIn("ค่าส่งเท่าไหร่", contents)      # from sessA2
        self.assertNotIn("ขอเบอร์โกดังไทย", contents)     # from sessB1

    def test_user_isolation_denied(self):
        # user B asking for user A's session -> not found
        self.assertIsNone(get_user_session_messages(_UB, "sessA1", sb=self.sb))

    def test_tenant_isolation_denied(self):
        # a different tenant's customer is simply a different line_user_id;
        # a forged/guessed session_id owned by someone else returns None.
        self.assertIsNone(get_user_session_messages("Uother_tenant_user", "sessB1", sb=self.sb))

    def test_deleted_session_denied(self):
        self.assertIsNone(get_user_session_messages(_UA, "sessA_del", sb=self.sb))

    def test_non_line_session_denied(self):
        self.assertIsNone(get_user_session_messages(_UA, "sessA_pg", sb=self.sb))

    def test_unknown_session_denied(self):
        self.assertIsNone(get_user_session_messages(_UA, "nope", sb=self.sb))

    def test_session_with_no_messages_returns_empty_list(self):
        self.sb.store["ai_sessions"].append(
            {"id": "sessEmpty", "name": "ว่าง", "channel": "line", "line_user_id": _UA,
             "message_count": 0, "deleted_at": None, "created_at": "2026-09-05T00:00:00Z",
             "updated_at": "2026-09-05T00:00:00Z"})
        out = get_user_session_messages(_UA, "sessEmpty", sb=self.sb)
        self.assertEqual(out["messages"], [])
        self.assertEqual(out["session"]["session_id"], "sessEmpty")


class QueryShapeP32(unittest.TestCase):
    def _count_tables(self, fn):
        sb = _seed_sessions(_FakeSupabase())
        seen = []
        real = sb.table
        sb.table = lambda name: (seen.append(name), real(name))[1]
        fn(sb)
        return seen

    def test_session_list_is_one_read(self):
        seen = self._count_tables(lambda sb: list_user_sessions(_UA, sb=sb))
        self.assertEqual(seen, ["ai_sessions"])

    def test_transcript_is_two_reads_no_per_message_query(self):
        seen = self._count_tables(lambda sb: get_user_session_messages(_UA, "sessA1", sb=sb))
        self.assertEqual(seen, ["ai_sessions", "ai_session_messages"])
        self.assertNotIn("ai_session_traces", seen)
        self.assertNotIn("ai_session_events", seen)

    def test_denied_transcript_never_reads_messages(self):
        seen = self._count_tables(lambda sb: get_user_session_messages(_UB, "sessA1", sb=sb))
        self.assertEqual(seen, ["ai_sessions"])  # ownership check fails -> stop


class RoutesAreReadOnlyAndAuthGuarded(unittest.TestCase):
    """Acceptance I / privacy — the viewer is admin-only and offers no write op."""

    def _line_user_routes(self):
        from admin.routes import app
        return [r for r in app.routes
                if getattr(r, "path", "").startswith("/admin/line-users")
                or getattr(r, "path", "").startswith("/admin/api/line-users")]

    def test_only_get_methods_registered(self):
        routes = self._line_user_routes()
        # P3.1: page + list + detail ; P3.2: sessions + transcript
        self.assertEqual(len(routes), 5)
        for r in routes:
            self.assertEqual(set(r.methods) - {"HEAD", "OPTIONS"}, {"GET"},
                             msg=f"{r.path} exposes a non-GET method")

    def test_unauthenticated_request_is_redirected_to_login(self):
        from starlette.testclient import TestClient
        from admin.routes import app
        c = TestClient(app)
        for path in ("/admin/line-users", "/admin/api/line-users",
                     "/admin/api/line-users/Uwhoever",
                     "/admin/api/line-users/Uwhoever/sessions",
                     "/admin/api/line-users/Uwhoever/sessions/sess1"):
            resp = c.get(path, follow_redirects=False)
            self.assertIn(resp.status_code, (302, 307), msg=path)
            self.assertIn("/admin/login", resp.headers.get("location", ""), msg=path)


if __name__ == "__main__":
    unittest.main()
