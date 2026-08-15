-- Migration 039: Conversation Resolver — last_business_action (2026-08-15)
--
-- Extends Customer Intelligence V1's Identifier Memory (migration 038:
-- cust_code/last_order_code/last_shipment_code/last_tracking) with ONE
-- more remembered field: which Business Action the customer was last
-- using. services/decision_engine.py's Conversation Resolver
-- (_resolve_conversation_reference) reads this back, as a last resort
-- before falling to RAG, so a generic referring-expression follow-up
-- ("แล้วของถึงหรือยัง", "ของผม") with no topic word of its own resumes
-- the SAME action instead of guessing via a knowledge-base search. Never
-- a second routing system — purely a remembered pointer into the SAME
-- Business Action Registry every other routing path already reads from.

ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS last_business_action TEXT;
