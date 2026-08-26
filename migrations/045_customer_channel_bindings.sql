-- Migration 045: customer_channel_bindings (Task 06B, 2026-08-26)
--
-- The dedicated, persistent source of truth for "which real customer
-- account (CustCode) does this channel identity (e.g. a LINE user id)
-- actually belong to, VERIFIED" -- resolving Task 06's own confirmed
-- gap ("VERIFIED CUSTOMER BINDING DOES NOT EXIST"). A customer-typed
-- CustCode/OrderCode/ShipmentCode is still never authorization by
-- itself (see services/authorization_service.py); only a row in THIS
-- table, in status='verified', is.
--
-- Deliberately never reuses user_profiles.cust_code/last_order_code/
-- last_shipment_code/last_tracking as an authorization source -- those
-- were a customer-typed convenience cache with no ownership proof
-- (Task 06's own root cause), and Task 06 already stopped writing them.
--
-- No trusted self-service verification channel exists yet in this
-- codebase (no OTP provider, no customer login/portal, no LINE Login --
-- see Task 06B's investigation) -- verification_method is 'staff_assisted'
-- only today: an authenticated admin (admin/routes.py's existing
-- auth(request) session-cookie gate) records a binding after verifying
-- the customer out-of-band. Real self-service verification is future
-- work once a real OTP/email/SMS provider or LINE Login is integrated;
-- this schema does not need to change for that -- only
-- verification_method gains a new value and a new admin/service caller.
--
-- Business rule (confirmed 2026-08-26): strictly 1:1 -- one LINE user
-- may hold only one VERIFIED CustCode at a time, and one CustCode may
-- be VERIFIED-bound to only one LINE user at a time. Enforced by the
-- two partial unique indexes below (scoped to status='verified' only --
-- a revoked/expired/pending row must never block a fresh relink).

CREATE TABLE IF NOT EXISTS customer_channel_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL DEFAULT 'default',
    channel TEXT NOT NULL,
    -- The channel's own opaque user identity -- the LINE user id for
    -- channel='line', server-derived from the webhook's own HMAC-
    -- verified signature (line_bot/webhook.py), never customer-message
    -- content.
    external_user_id TEXT NOT NULL,
    -- The VERIFIED customer identity this binding proves ownership of.
    -- NOT the same thing as a customer merely typing this value in
    -- chat (see services/authorization_service.py's module docstring).
    cust_code TEXT NOT NULL,
    -- pending | verified | revoked | expired
    status TEXT NOT NULL DEFAULT 'pending',
    -- 'staff_assisted' only today -- see module comment above.
    verification_method TEXT,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    revoked_reason TEXT,
    -- Best-effort audit label, same limitation as every other admin
    -- audit field in this project (e.g. business_actions.created_by via
    -- admin/routes.py::_current_admin_user) -- no per-staff login exists
    -- yet, so this is the literal string "admin", not a real identity.
    created_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_customer_channel_bindings_lookup
    ON customer_channel_bindings (tenant_id, channel, external_user_id, status);

CREATE INDEX IF NOT EXISTS idx_customer_channel_bindings_custcode
    ON customer_channel_bindings (tenant_id, cust_code, status);

-- One VERIFIED binding per LINE user at a time.
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_channel_bindings_active_user
    ON customer_channel_bindings (tenant_id, channel, external_user_id)
    WHERE status = 'verified';

-- One VERIFIED binding per CustCode at a time (per tenant+channel) --
-- a second LINE user can never simultaneously claim the same verified
-- customer identity.
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_channel_bindings_active_custcode
    ON customer_channel_bindings (tenant_id, channel, cust_code)
    WHERE status = 'verified';
