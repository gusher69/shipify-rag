-- ============================================================
-- Migration 043: OrderCode pattern + SearchDataOrder keyword fixes
-- from real customer UAT feedback (2026-08-17).
--
-- Customer-Reported ERP Conversation Defects — root cause of a
-- misrouted "order detail" lookup: SearchDataOrder's OrderCode
-- validation_pattern was `^POS\d+$` (requires a literal "POS" prefix),
-- but the customer's real ERP issues BOTH "POS"-prefixed order codes
-- (e.g. POS100820260815001) AND "PO"-prefixed order codes with no "S"
-- (e.g. PO318220260806008 -- used throughout this platform's own real
-- UAT/Golden data all along, always reached via multi-turn context
-- continuation, never via a fresh single-message regex match, which is
-- why this gap was never exercised until now). A "PO"-prefixed code
-- never validly matched OrderCode at all -- it fell through to
-- CustCode's or SearchDataShipment's own, unrelated, much broader
-- patterns instead, causing the wrong Business Action to be selected
-- (or an unresolvable identifier) for a plain "order detail" question.
--
-- Fix: OrderCode's pattern now accepts an OPTIONAL "S" after "PO"
-- (`^POS?\d+$`), covering both real formats the ERP actually issues --
-- never narrowed to one specific code value, never customer-specific.
--
-- Compounding gap: SearchDataOrder's own search_keywords required the
-- literal English phrase "order detail" ('order detail', 'PO เดียว',
-- ...) -- a natural, mixed Thai-English customer message like "ขอ
-- รายละเอียด order PO318220260806008" never matched any of them, so once
-- the identifier-pattern signal above also ties with SearchDataShipment
-- (a "PO"+digits code can ALSO structurally match ShipmentCode's own
-- broad `^[A-Za-z]{2}\d{10,}$` pattern -- inherent, unavoidable overlap
-- between two independently-configured generic identifier shapes, not
-- a defect in either pattern individually), there was no other signal
-- left to break the tie in the right direction.
--
-- Adding the compound phrase "รายละเอียด order" (never a bare "order")
-- gives SearchDataOrder the decisive edge specifically for DETAIL-
-- shaped phrasing. A bare "order" was tried first and reverted after
-- live re-verification (tests/golden case A/D construction, 2026-08-17)
-- showed it regressed the ALREADY-CORRECT "ขอดู order ล่าสุด"-style
-- continuation (GOLDEN-011's own phrasing): a bare loanword collides
-- with SearchDataOrderList's own pre-existing "order" keyword (migration
-- 042) whenever no identifier is present in the message to break the
-- tie, forcing a false CLARIFICATION_REQUIRED where the LIST action was
-- previously the sole, correct, unambiguous winner. "รายละเอียด order"
-- only ever fires for genuinely DETAIL-shaped phrasing, never a plain
-- list/browse mention, so it cannot re-introduce that regression. A
-- bare identifier with NO topical wording at all (e.g. just
-- "PO318220260806008" alone) is still, correctly, left as a genuine
-- tie -- the same "ambiguity safety" standard already applied
-- everywhere else in this scoring system (a bare, contextless code
-- must not be silently guessed at).
-- ============================================================

UPDATE business_action_parameters bp
SET validation_pattern = '^POS?\d+$'
FROM business_actions ba
WHERE bp.action_id = ba.id
  AND ba.action_key = 'searchdataorder'
  AND bp.name = 'OrderCode'
  AND bp.validation_pattern = '^POS\d+$';

UPDATE business_actions
SET search_keywords = (
    SELECT jsonb_agg(DISTINCT kw)
    FROM jsonb_array_elements_text(
        COALESCE(to_jsonb(search_keywords), '[]'::jsonb)
        || '["รายละเอียด order"]'::jsonb
    ) AS kw
),
    updated_at = now()
WHERE action_key = 'searchdataorder';
