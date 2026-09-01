"""P3.1 / P3.2 — LINE User Profile viewer backend tests.

Covers services/line_user_directory.py:
  * REAL-LINE provenance filter (shape ^U[0-9a-f]{32}$ AND a channel='line'
    session/binding) — synthetic playground:* and probe ids are excluded
  * user_profiles ↔ customer_channel_bindings verified/unverified mapping
    (legacy user_profiles.cust_code never surfaced as verified)
  * search by name / by verified CustCode, status filter, fixed query shape
  * P3.2 conversation session list + transcript, session/user/tenant
    isolation, internal-message exclusion, read-only routes
"""
import unittest

from tests.test_business_action_registry import _FakeSupabase
from services.line_user_directory import (
    list_line_users, get_line_user_detail,
    list_user_sessions, get_user_session_messages,
)

# Real LINE userIds: 'U' + 32 lowercase hex.
_UA = "U" + "a" * 32          # verified binding — "สมชาย ใจดี"
_UB = "U" + "b" * 32          # no binding      — "Anna Wong"
_UC = "U" + "c" * 32          # revoked binding — "ร้านค้า B"
_UNSEEN = "U" + "d" * 32      # U-shape, profile row, but NO LINE provenance
_SYN_PG = "playground:UAT-01"                     # synthetic journey user
_SYN_PROBE = "Uprobe0000000000000000000000000A"   # has a line session, bad shape


def _seed(sb):
    # user_profiles: `last_active` (not `updated_at`) is the activity column.
    sb.store["user_profiles"] = [
        {"line_user_id": _UA, "display_name": "สมชาย ใจดี",
         "first_seen": "2026-08-01T00:00:00Z", "last_active": "2026-09-01T10:00:00Z",
         "message_count": 12, "conversation_count": 3, "created_at": "2026-08-01T00:00:00Z",
         "cust_code": "TYPED9999",   # legacy typed cache — must NEVER show as verified
         "lead_stage": "WARM", "lead_score": 55,
         "lead_reasons": ["product_identified", "rate_interest", "duration_interest"],
         "lead_stage_updated_at": "2026-09-01T10:00:00Z",
         "sentiment_status": "NEGATIVE",
         "sentiment_reasons": ["repeated_wrong_answer", "human_requested"],
         "sentiment_updated_at": "2026-09-01T18:30:00Z",
         "negative_last_detected_at": "2026-09-01T18:30:00Z",
         "negative_last_alert_at": "2026-09-01T18:30:05Z"},
        {"line_user_id": _UB, "display_name": "Anna Wong",
         "first_seen": "2026-08-10T00:00:00Z", "last_active": "2026-09-02T09:00:00Z",
         "message_count": 4, "conversation_count": 1, "created_at": "2026-08-10T00:00:00Z",
         "lead_stage": "HOT", "lead_score": 80, "sentiment_status": "NORMAL"},
        {"line_user_id": _UC, "display_name": "ร้านค้า B",
         "first_seen": "2026-07-01T00:00:00Z", "last_active": "2026-08-15T09:00:00Z",
         "message_count": 40, "conversation_count": 9, "created_at": "2026-07-01T00:00:00Z",
         "lead_stage": "COLD", "lead_score": 5, "sentiment_status": "NEGATIVE",
         "sentiment_reasons": ["complaint"]},
        # ── rows that must NOT reach the directory ──
        {"line_user_id": _UNSEEN, "display_name": "No Provenance",
         "first_seen": "2026-09-01T00:00:00Z", "last_active": "2026-09-09T00:00:00Z",
         "message_count": 0, "conversation_count": 0, "created_at": "2026-09-01T00:00:00Z"},
        {"line_user_id": _SYN_PG, "display_name": "UAT One",
         "first_seen": "2026-08-01T00:00:00Z", "last_active": "2026-09-08T00:00:00Z",
         "message_count": 20, "conversation_count": 4, "created_at": "2026-08-01T00:00:00Z"},
        {"line_user_id": _SYN_PROBE, "display_name": "",
         "first_seen": "2026-08-27T00:00:00Z", "last_active": "2026-09-07T00:00:00Z",
         "message_count": 2, "conversation_count": 1, "created_at": "2026-08-27T00:00:00Z"},
    ]
    sb.store["customer_channel_bindings"] = [
        {"external_user_id": _UA, "cust_code": "FT5001", "status": "verified",
         "channel": "line", "verification_method": "staff_assisted",
         "verified_at": "2026-08-20T00:00:00Z", "updated_at": "2026-08-20T00:00:00Z",
         "created_at": "2026-08-20T00:00:00Z"},
        {"external_user_id": _UC, "cust_code": "FT5002", "status": "revoked",
         "channel": "line", "verified_at": "2026-07-05T00:00:00Z",
         "updated_at": "2026-07-30T00:00:00Z", "created_at": "2026-07-05T00:00:00Z"},
    ]
    # Provenance + P3.2 transcript source. channel='line' == real webhook origin.
    sb.store["ai_sessions"] = [
        {"id": "sessA1", "name": "อยากนำเข้าสินค้าจากจีน", "channel": "line",
         "line_user_id": _UA, "message_count": 4, "deleted_at": None,
         "created_at": "2026-09-01T16:20:00Z", "updated_at": "2026-09-01T16:28:00Z",
         "last_message_at": "2026-09-01T16:28:00Z"},
        {"id": "sessA2", "name": "สอบถามค่าส่ง", "channel": "line",
         "line_user_id": _UA, "message_count": 2, "deleted_at": None,
         "created_at": "2026-09-03T09:00:00Z", "updated_at": "2026-09-03T09:05:00Z",
         "last_message_at": "2026-09-03T09:05:00Z"},
        {"id": "sessA_del", "name": "ลบแล้ว", "channel": "line", "line_user_id": _UA,
         "message_count": 2, "deleted_at": "2026-09-02T00:00:00Z",
         "created_at": "2026-09-02T09:00:00Z", "updated_at": "2026-09-02T09:05:00Z"},
        {"id": "sessA_pg", "name": "playground run", "channel": "playground",
         "line_user_id": _UA, "message_count": 6, "deleted_at": None,
         "created_at": "2026-09-04T09:00:00Z", "updated_at": "2026-09-04T09:05:00Z"},
        {"id": "sessB1", "name": "เบอร์โกดัง", "channel": "line", "line_user_id": _UB,
         "message_count": 2, "deleted_at": None,
         "created_at": "2026-09-01T10:00:00Z", "updated_at": "2026-09-01T10:02:00Z",
         "last_message_at": "2026-09-01T10:02:00Z"},
        {"id": "sessC1", "name": "โปรไฟล์", "channel": "line", "line_user_id": _UC,
         "message_count": 0, "deleted_at": None,
         "created_at": "2026-08-15T09:00:00Z", "updated_at": "2026-08-15T09:00:00Z"},
        # synthetic footprints — must not confer directory membership
        {"id": "sessPG", "name": "journey", "channel": "playground", "line_user_id": _SYN_PG,
         "message_count": 20, "deleted_at": None,
         "created_at": "2026-08-01T00:00:00Z", "updated_at": "2026-09-08T00:00:00Z"},
        {"id": "sessProbe", "name": "LINE: Uprobe000000", "channel": "line",
         "line_user_id": _SYN_PROBE, "message_count": 2, "deleted_at": None,
         "created_at": "2026-08-27T00:00:00Z", "updated_at": "2026-09-07T00:00:00Z"},
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
        {"session_id": "sessA1", "turn_index": 4, "role": "system",
         "content": "INTERNAL: rag_chunk_ids=[...] prompt_template=v7",
         "created_at": "2026-09-01T16:21:06Z"},
        {"session_id": "sessA2", "turn_index": 0, "role": "user",
         "content": "ค่าส่งเท่าไหร่", "created_at": "2026-09-03T09:00:00Z"},
        {"session_id": "sessA2", "turn_index": 1, "role": "assistant",
         "content": "ขึ้นกับน้ำหนักค่ะ", "created_at": "2026-09-03T09:00:05Z"},
        {"session_id": "sessB1", "turn_index": 0, "role": "user",
         "content": "ขอเบอร์โกดังไทย", "created_at": "2026-09-01T10:00:00Z"},
        {"session_id": "sessB1", "turn_index": 1, "role": "assistant",
         "content": "02-123-4567 ค่ะ", "created_at": "2026-09-01T10:00:05Z"},
        {"session_id": "sessProbe", "turn_index": 0, "role": "user",
         "content": "probe", "created_at": "2026-08-27T00:00:00Z"},
    ]
    sb.store["ai_session_traces"] = [
        {"session_id": "sessA1", "message_id": "x", "chunks": [{"text": "SECRET CHUNK"}],
         "prompt": {"system_prompt": "you are ..."}, "policy": {}},
    ]
    return sb


class ProvenanceFilter(unittest.TestCase):
    def setUp(self):
        self.sb = _seed(_FakeSupabase())

    def test_only_real_line_users_listed(self):
        ids = {u["line_user_id"] for u in list_line_users(sb=self.sb)["users"]}
        self.assertEqual(ids, {_UA, _UB, _UC})

    def test_playground_journey_user_excluded(self):
        ids = {u["line_user_id"] for u in list_line_users(sb=self.sb)["users"]}
        self.assertNotIn(_SYN_PG, ids)

    def test_probe_id_with_line_session_excluded_by_shape(self):
        ids = {u["line_user_id"] for u in list_line_users(sb=self.sb)["users"]}
        self.assertNotIn(_SYN_PROBE, ids)

    def test_ushape_without_provenance_excluded(self):
        ids = {u["line_user_id"] for u in list_line_users(sb=self.sb)["users"]}
        self.assertNotIn(_UNSEEN, ids)

    def test_summary_counts_only_real_users(self):
        s = list_line_users(sb=self.sb)["summary"]
        self.assertEqual((s["total"], s["verified"], s["unverified"]), (3, 1, 2))

    def test_detail_denied_for_synthetic(self):
        self.assertIsNone(get_line_user_detail(_SYN_PG, sb=self.sb))
        self.assertIsNone(get_line_user_detail(_SYN_PROBE, sb=self.sb))
        self.assertIsNone(get_line_user_detail(_UNSEEN, sb=self.sb))

    def test_detail_ok_for_real_user(self):
        self.assertIsNotNone(get_line_user_detail(_UA, sb=self.sb))

    def test_sessions_denied_for_probe_shape(self):
        self.assertEqual(list_user_sessions(_SYN_PROBE, sb=self.sb), [])
        self.assertIsNone(get_user_session_messages(_SYN_PROBE, "sessProbe", sb=self.sb))


class ListMappingAndSummary(unittest.TestCase):
    def setUp(self):
        self.sb = _seed(_FakeSupabase())

    def test_verified_user_shows_bound_custcode_and_label(self):
        u = next(x for x in list_line_users(sb=self.sb)["users"] if x["line_user_id"] == _UA)
        self.assertTrue(u["verified"])
        self.assertEqual(u["cust_code"], "FT5001")
        self.assertEqual(u["status_label"], "ยืนยันแล้ว")
        self.assertEqual(u["line_user_id_masked"], "Uaaaa...aaaa")

    def test_unverified_user_has_no_custcode(self):
        u = next(x for x in list_line_users(sb=self.sb)["users"] if x["line_user_id"] == _UB)
        self.assertFalse(u["verified"])
        self.assertIsNone(u["cust_code"])
        self.assertEqual(u["status_label"], "ยังไม่ยืนยัน")

    def test_revoked_binding_is_not_verified(self):
        u = next(x for x in list_line_users(sb=self.sb)["users"] if x["line_user_id"] == _UC)
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
        self.assertEqual(rows[0]["line_user_id"], _UB)  # last_active 09-02


class SearchAndFilter(unittest.TestCase):
    def setUp(self):
        self.sb = _seed(_FakeSupabase())

    def test_search_by_display_name(self):
        rows = list_line_users(search="anna", sb=self.sb)["users"]
        self.assertEqual([r["display_name"] for r in rows], ["Anna Wong"])

    def test_search_by_thai_name(self):
        rows = list_line_users(search="สมชาย", sb=self.sb)["users"]
        self.assertEqual([r["line_user_id"] for r in rows], [_UA])

    def test_search_by_verified_custcode(self):
        rows = list_line_users(search="FT5001", sb=self.sb)["users"]
        self.assertEqual([r["line_user_id"] for r in rows], [_UA])

    def test_search_by_revoked_custcode_returns_nothing(self):
        self.assertEqual(list_line_users(search="FT5002", sb=self.sb)["users"], [])

    def test_status_filter_verified(self):
        rows = list_line_users(status="verified", sb=self.sb)["users"]
        self.assertEqual([r["line_user_id"] for r in rows], [_UA])

    def test_status_filter_unverified(self):
        ids = {r["line_user_id"] for r in list_line_users(status="unverified", sb=self.sb)["users"]}
        self.assertEqual(ids, {_UB, _UC})

    def test_no_result_is_empty_list(self):
        self.assertEqual(list_line_users(search="zzz-nobody", sb=self.sb)["users"], [])


class QueryShapeNoNPlusOne(unittest.TestCase):
    def test_list_is_bounded_read_set(self):
        sb = _seed(_FakeSupabase())
        seen = []
        real_table = sb.table
        sb.table = lambda name: (seen.append(name), real_table(name))[1]
        list_line_users(sb=sb)
        # exactly: bindings once, profiles once, provenance (ai_sessions) once
        self.assertEqual(seen.count("customer_channel_bindings"), 1)
        self.assertEqual(seen.count("user_profiles"), 1)
        self.assertEqual(seen.count("ai_sessions"), 1)
        self.assertEqual(len(seen), 3)  # never one binding/session query per row


class Detail(unittest.TestCase):
    def setUp(self):
        self.sb = _seed(_FakeSupabase())

    def test_verified_detail(self):
        d = get_line_user_detail(_UA, sb=self.sb)
        self.assertEqual(d["profile"]["display_name"], "สมชาย ใจดี")
        self.assertEqual(d["profile"]["message_count"], 12)
        self.assertTrue(d["binding"]["verified"])
        self.assertEqual(d["binding"]["cust_code"], "FT5001")
        self.assertEqual(d["binding"]["verification_method"], "staff_assisted")

    def test_unverified_detail_no_custcode(self):
        d = get_line_user_detail(_UB, sb=self.sb)
        self.assertFalse(d["binding"]["verified"])
        self.assertIsNone(d["binding"]["cust_code"])
        self.assertEqual(d["binding"]["status_label"], "ยังไม่ยืนยัน")

    def test_revoked_detail_not_verified(self):
        d = get_line_user_detail(_UC, sb=self.sb)
        self.assertFalse(d["binding"]["verified"])

    def test_unknown_user_returns_none(self):
        self.assertIsNone(get_line_user_detail("U" + "e" * 32, sb=self.sb))

    # P4 — Acceptance L (admin surface)
    def test_list_row_carries_lead_stage_and_score(self):
        u = next(x for x in list_line_users(sb=self.sb)["users"] if x["line_user_id"] == _UA)
        self.assertEqual(u["lead_stage"], "WARM")
        self.assertEqual(u["lead_score"], 55)

    def test_list_row_defaults_lead_stage_when_absent(self):
        for row in self.sb.store["user_profiles"]:
            if row["line_user_id"] == _UB:
                row.pop("lead_stage", None); row.pop("lead_score", None)
        u = next(x for x in list_line_users(sb=self.sb)["users"] if x["line_user_id"] == _UB)
        self.assertEqual(u["lead_stage"], "COLD")
        self.assertEqual(u["lead_score"], 0)

    def test_detail_lead_block(self):
        lead = get_line_user_detail(_UA, sb=self.sb)["lead"]
        self.assertEqual(lead["stage"], "WARM")
        self.assertEqual(lead["score"], 55)
        self.assertEqual(lead["updated_at"], "2026-09-01T10:00:00Z")
        self.assertEqual(lead["reasons"],
                         ["ระบุสินค้าแล้ว", "ถามค่าขนส่ง / ค่าบริการ", "ถามระยะเวลาขนส่ง"])
        self.assertEqual(lead["reason_keys"],
                         ["product_identified", "rate_interest", "duration_interest"])

    # P4.1 — Acceptance L (admin surface)
    def test_list_row_carries_sentiment(self):
        u = next(x for x in list_line_users(sb=self.sb)["users"] if x["line_user_id"] == _UA)
        self.assertEqual(u["sentiment"], "NEGATIVE")
        v = next(x for x in list_line_users(sb=self.sb)["users"] if x["line_user_id"] == _UB)
        self.assertEqual(v["sentiment"], "NORMAL")

    def test_detail_sentiment_block(self):
        s = get_line_user_detail(_UA, sb=self.sb)["sentiment"]
        self.assertEqual(s["status"], "NEGATIVE")
        self.assertEqual(s["reasons"], ["แจ้งว่าระบบตอบผิดซ้ำ", "ขอคุยกับเจ้าหน้าที่"])
        self.assertEqual(s["reason_keys"], ["repeated_wrong_answer", "human_requested"])
        self.assertEqual(s["last_detected_at"], "2026-09-01T18:30:00Z")
        self.assertEqual(s["last_alert_at"], "2026-09-01T18:30:05Z")

    def test_detail_sentiment_defaults_normal(self):
        for row in self.sb.store["user_profiles"]:
            if row["line_user_id"] == _UB:
                row.pop("sentiment_status", None)
        s = get_line_user_detail(_UB, sb=self.sb)["sentiment"]
        self.assertEqual(s["status"], "NORMAL")
        self.assertEqual(s["reasons"], [])


class ListFilters(unittest.TestCase):
    """P4.1 finalize — Lead Stage + Sentiment filters (server-side, combine
    with search + account status; summary always global)."""

    def setUp(self):
        self.sb = _seed(_FakeSupabase())

    def _ids(self, **kw):
        return {u["line_user_id"] for u in list_line_users(sb=self.sb, **kw)["users"]}

    # seed: _UA WARM/NEGATIVE, _UB HOT/NORMAL, _UC COLD/NEGATIVE
    def test_lead_stage_filter(self):
        self.assertEqual(self._ids(lead_stage="WARM"), {_UA})
        self.assertEqual(self._ids(lead_stage="HOT"), {_UB})
        self.assertEqual(self._ids(lead_stage="COLD"), {_UC})

    def test_sentiment_filter(self):
        self.assertEqual(self._ids(sentiment="NORMAL"), {_UB})
        self.assertEqual(self._ids(sentiment="NEGATIVE"), {_UA, _UC})

    def test_warm_plus_negative(self):
        self.assertEqual(self._ids(lead_stage="WARM", sentiment="NEGATIVE"), {_UA})

    def test_hot_plus_negative_is_empty(self):
        self.assertEqual(list_line_users(sb=self.sb, lead_stage="HOT",
                                         sentiment="NEGATIVE")["users"], [])

    def test_account_status_still_works_with_new_filters(self):
        self.assertEqual(self._ids(status="verified", sentiment="NEGATIVE"), {_UA})
        self.assertEqual(self._ids(status="unverified", sentiment="NEGATIVE"), {_UC})

    def test_search_plus_filters_intersect(self):
        self.assertEqual(self._ids(search="สมชาย", lead_stage="WARM", sentiment="NEGATIVE"), {_UA})
        self.assertEqual(self._ids(search="สมชาย", lead_stage="COLD"), set())      # wrong combo
        self.assertEqual(self._ids(search="anna", lead_stage="HOT"), {_UB})

    def test_reset_all_returns_every_real_user(self):
        self.assertEqual(self._ids(lead_stage="all", sentiment="all"), {_UA, _UB, _UC})
        self.assertEqual(self._ids(), {_UA, _UB, _UC})

    def test_summary_unchanged_by_filters(self):
        base = list_line_users(sb=self.sb)["summary"]
        filt = list_line_users(sb=self.sb, lead_stage="HOT", sentiment="NEGATIVE")["summary"]
        self.assertEqual(base, {"total": 3, "verified": 1, "unverified": 2})
        self.assertEqual(filt, base)

    def test_unknown_filter_value_ignored(self):
        self.assertEqual(self._ids(lead_stage="all"), {_UA, _UB, _UC})
        self.assertEqual(self._ids(sentiment=""), {_UA, _UB, _UC})


# ── P3.2 — Conversation History Viewer ────────────────────────────────

class SessionList(unittest.TestCase):
    def setUp(self):
        self.sb = _seed(_FakeSupabase())

    def test_lists_only_this_users_line_sessions(self):
        ids = [r["session_id"] for r in list_user_sessions(_UA, sb=self.sb)]
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
        self.assertEqual(list_user_sessions("U" + "f" * 32, sb=self.sb), [])


class Transcript(unittest.TestCase):
    def setUp(self):
        self.sb = _seed(_FakeSupabase())

    def test_returns_user_and_assistant_in_order(self):
        out = get_user_session_messages(_UA, "sessA1", sb=self.sb)
        self.assertEqual([(m["role"], m["content"]) for m in out["messages"]], [
            ("user", "อยากนำเข้าสินค้าจากจีน"),
            ("assistant", "ต้องการนำเข้าสินค้าประเภทไหนคะ"),
            ("user", "น้ำหอมครับ"),
            ("assistant", "น้ำหอมจัดเป็นของเหลว..."),
        ])

    def test_internal_roles_and_traces_excluded(self):
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
        self.assertNotIn("ค่าส่งเท่าไหร่", contents)
        self.assertNotIn("ขอเบอร์โกดังไทย", contents)

    def test_user_isolation_denied(self):
        self.assertIsNone(get_user_session_messages(_UB, "sessA1", sb=self.sb))

    def test_tenant_isolation_denied(self):
        self.assertIsNone(get_user_session_messages("U" + "9" * 32, "sessB1", sb=self.sb))

    def test_deleted_session_denied(self):
        self.assertIsNone(get_user_session_messages(_UA, "sessA_del", sb=self.sb))

    def test_non_line_session_denied(self):
        self.assertIsNone(get_user_session_messages(_UA, "sessA_pg", sb=self.sb))

    def test_unknown_session_denied(self):
        self.assertIsNone(get_user_session_messages(_UA, "nope", sb=self.sb))

    def test_session_with_no_messages_returns_empty_list(self):
        out = get_user_session_messages(_UC, "sessC1", sb=self.sb)
        self.assertEqual(out["messages"], [])
        self.assertEqual(out["session"]["session_id"], "sessC1")


class QueryShapeP32(unittest.TestCase):
    def _count_tables(self, fn):
        sb = _seed(_FakeSupabase())
        seen = []
        real = sb.table
        sb.table = lambda name: (seen.append(name), real(name))[1]
        fn(sb)
        return seen

    def test_session_list_is_one_read(self):
        self.assertEqual(self._count_tables(lambda sb: list_user_sessions(_UA, sb=sb)),
                         ["ai_sessions"])

    def test_transcript_is_two_reads_no_per_message_query(self):
        seen = self._count_tables(lambda sb: get_user_session_messages(_UA, "sessA1", sb=sb))
        self.assertEqual(seen, ["ai_sessions", "ai_session_messages"])
        self.assertNotIn("ai_session_traces", seen)
        self.assertNotIn("ai_session_events", seen)

    def test_denied_transcript_never_reads_messages(self):
        seen = self._count_tables(lambda sb: get_user_session_messages(_UB, "sessA1", sb=sb))
        self.assertEqual(seen, ["ai_sessions"])


class RoutesAreReadOnlyAndAuthGuarded(unittest.TestCase):
    def _line_user_routes(self):
        from admin.routes import app
        return [r for r in app.routes
                if getattr(r, "path", "").startswith("/admin/line-users")
                or getattr(r, "path", "").startswith("/admin/api/line-users")]

    def test_only_get_methods_registered(self):
        routes = self._line_user_routes()
        self.assertEqual(len(routes), 5)   # page + list + detail + sessions + transcript
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
