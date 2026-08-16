-- ============================================================
-- Migration 042: Business Action search_keywords vocabulary fixes
-- for two verified Golden Test Suite application defects.
--
-- Golden Application Defect Fixes (2026-08-16), following on from the
-- Golden Suite Independent Audit and the permanent Golden Test Harness
-- (migrations 040/041). This is a DATA change (Business Action Registry
-- config, services/business_action_registry.py), not a schema change --
-- committed as a migration purely for the same reproducible-audit-trail
-- reason the rest of this session's work has followed: "which commit
-- fixed the customer-facing vocabulary gap" should be answerable from
-- git history, not lost in an ad-hoc admin edit.
--
-- GOLDEN-024-TRACKING-THAI root cause: SearchDataTracking's own
-- search_keywords were 100% dependent on the literal English word
-- "tracking" appearing in the customer's message (['tracking จีน', 'เลข
-- tracking', 'tracking', ...]). A customer phrasing the exact same
-- intent in native Thai ("เลขพัสดุจีน...ถึงไหนแล้ว", no English word at
-- all) scored 0 on SearchDataTracking, while SearchDataShipmentList's
-- generic 'พัสดุ' keyword matched and won by default. Adding the native
-- Thai compound terms for "Chinese tracking/package number" closes that
-- gap without touching any scoring code -- the SAME generic, Registry-
-- driven services/action_selection_primitives.py::_keyword_score() now
-- has the vocabulary it needs to discriminate correctly.
--
-- GOLDEN-049-NATURAL-ORDER root cause: none of SearchDataOrderList's
-- search_keywords covered the common Thai-English loanword "ออเดอร์"
-- (order) -- only English "order"/"PO" and native "คำสั่งซื้อ" were
-- configured. A message using "ออเดอร์" scored 0 via search_keywords on
-- every ERP action, so the only signal left was the weak ai_description
-- word-overlap fallback (a bare "ของ" -- "of" -- happened to appear in
-- three unrelated actions' ai_description text and tied all three at an
-- equal, non-decisive score), producing a false CLARIFICATION_REQUIRED.
-- Adding "ออเดอร์" gives the genuinely decisive signal back.
-- ============================================================

UPDATE business_actions
SET search_keywords = (
    SELECT jsonb_agg(DISTINCT kw)
    FROM jsonb_array_elements_text(
        COALESCE(to_jsonb(search_keywords), '[]'::jsonb)
        || '["เลขพัสดุจีน", "เลขจีน", "พัสดุจีน", "หมายเลขพัสดุจีน"]'::jsonb
    ) AS kw
),
    updated_at = now()
WHERE action_key = 'searchdatatracking';

UPDATE business_actions
SET search_keywords = (
    SELECT jsonb_agg(DISTINCT kw)
    FROM jsonb_array_elements_text(
        COALESCE(to_jsonb(search_keywords), '[]'::jsonb)
        || '["ออเดอร์"]'::jsonb
    ) AS kw
),
    updated_at = now()
WHERE action_key = 'searchdataorderlist';
