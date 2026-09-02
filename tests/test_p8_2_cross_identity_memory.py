"""P8.2 — a cached account-scoped identifier learned under a DIFFERENT
(or unknown) customer must never satisfy a private ERP required
parameter alongside a verified CustCode.

Confirmed REAL LINE failure (HEAD a141b98): verified binding FT3182 +
stale user_profiles (cust_code=SP1008, last_shipment_code=
SP100820260817001) -> "ขอดูรายละเอียดพัสดุหน่อย" prefilled ShipmentCode
from the SP1008-era value and executed SearchDataShipment with
{CustCode: FT3182, ShipmentCode: SP100820260817001} -> HTTP 400.
"""
import unittest

from services.action_selection_primitives import (
    strip_cross_identity_identifier_memory, IDENTIFIER_MEMORY_FIELDS,
)
from services.decision_engine import _apply_identifier_memory

_SHIPMENT_ACTION = {
    "action_key": "searchdatashipment",
    "parameters": [
        {"name": "CustCode", "input_source": "customer_message", "required": True},
        {"name": "SecretCode", "input_source": "credential_store", "required": True},
        {"name": "ShipmentCode", "input_source": "customer_message", "required": True},
    ],
}
_ORDER_ACTION = {
    "action_key": "searchdataorder",
    "parameters": [
        {"name": "CustCode", "input_source": "customer_message", "required": True},
        {"name": "OrderCode", "input_source": "customer_message", "required": True},
    ],
}
_STALE_PROFILE_CTX = {
    "cust_code": "FT3182",                       # already replaced with the verified value upstream
    "last_shipment_code": "SP100820260817001",   # learned under SP1008, pre-binding
    "last_order_code": "POS100820260824001",
    "last_tracking": "9822950447648",
}


class Test1_ConfirmedProductionFailure(unittest.TestCase):
    def test_stale_cross_identity_shipment_code_is_not_prefilled(self):
        ctx = strip_cross_identity_identifier_memory(
            _STALE_PROFILE_CTX, profile_cust_code="SP1008", verified_cust_code="FT3182")
        self.assertNotIn("last_shipment_code", ctx)
        collected = _apply_identifier_memory(_SHIPMENT_ACTION, {"CustCode": "FT3182"}, ctx)
        self.assertEqual(collected, {"CustCode": "FT3182"})
        self.assertNotIn("ShipmentCode", collected)   # -> collection must ask, no ERP call


class Test2_NoCrossIdentityOrderOrTrackingReuse(unittest.TestCase):
    def test_all_account_scoped_identifiers_are_dropped_at_the_shared_boundary(self):
        ctx = strip_cross_identity_identifier_memory(
            _STALE_PROFILE_CTX, profile_cust_code="SP1008", verified_cust_code="FT3182")
        for pf in ("last_order_code", "last_shipment_code", "last_tracking"):
            self.assertNotIn(pf, ctx, msg=pf)

    def test_stale_order_code_does_not_satisfy_a_missing_order_slot(self):
        ctx = strip_cross_identity_identifier_memory(
            _STALE_PROFILE_CTX, profile_cust_code="SP1008", verified_cust_code="FT3182")
        collected = _apply_identifier_memory(_ORDER_ACTION, {"CustCode": "FT3182"}, ctx)
        self.assertNotIn("OrderCode", collected)


class Test3_ExplicitCurrentTurnValueWins(unittest.TestCase):
    def test_a_shipment_code_supplied_this_turn_is_never_overridden_or_dropped(self):
        ctx = strip_cross_identity_identifier_memory(
            _STALE_PROFILE_CTX, profile_cust_code="SP1008", verified_cust_code="FT3182")
        collected = _apply_identifier_memory(
            _SHIPMENT_ACTION, {"CustCode": "FT3182", "ShipmentCode": "FT318220260726001"}, ctx)
        self.assertEqual(collected["ShipmentCode"], "FT318220260726001")


class Test4_VerifiedCustCodeReuse(unittest.TestCase):
    def test_verified_custcode_survives_and_is_not_re_requested(self):
        ctx = strip_cross_identity_identifier_memory(
            _STALE_PROFILE_CTX, profile_cust_code="SP1008", verified_cust_code="FT3182")
        self.assertEqual(ctx.get("cust_code"), "FT3182")
        collected = _apply_identifier_memory(_SHIPMENT_ACTION, {}, ctx)
        self.assertEqual(collected.get("CustCode"), "FT3182")   # from verified binding, not asked


class Test5_LegitimateSameCustomerContinuation(unittest.TestCase):
    def test_same_customer_memory_is_preserved(self):
        # profile cust_code matches the verified binding -> the cached
        # identifiers were established by this same verified customer.
        ctx = strip_cross_identity_identifier_memory(
            _STALE_PROFILE_CTX, profile_cust_code="FT3182", verified_cust_code="FT3182")
        self.assertEqual(ctx.get("last_shipment_code"), "SP100820260817001")
        collected = _apply_identifier_memory(_SHIPMENT_ACTION, {"CustCode": "FT3182"}, ctx)
        self.assertEqual(collected.get("ShipmentCode"), "SP100820260817001")

    def test_case_and_whitespace_insensitive_match(self):
        ctx = strip_cross_identity_identifier_memory(
            _STALE_PROFILE_CTX, profile_cust_code="  ft3182 ", verified_cust_code="FT3182")
        self.assertEqual(ctx.get("last_shipment_code"), "SP100820260817001")


class Guards(unittest.TestCase):
    def test_no_verified_binding_leaves_context_unchanged(self):
        ctx = strip_cross_identity_identifier_memory(
            _STALE_PROFILE_CTX, profile_cust_code="SP1008", verified_cust_code=None)
        self.assertEqual(ctx, _STALE_PROFILE_CTX)

    def test_absent_profile_cust_code_fails_closed(self):
        ctx = strip_cross_identity_identifier_memory(
            {"last_shipment_code": "X", "last_tracking": "Y"},
            profile_cust_code=None, verified_cust_code="FT3182")
        self.assertEqual(ctx, {})

    def test_returns_a_new_dict_never_mutates_input(self):
        src = dict(_STALE_PROFILE_CTX)
        strip_cross_identity_identifier_memory(src, profile_cust_code="SP1008",
                                               verified_cust_code="FT3182")
        self.assertIn("last_shipment_code", src)   # original untouched

    def test_custcode_is_not_in_the_account_scoped_strip_set(self):
        # CustCode provenance is handled upstream (verified binding replace)
        # and never trusted for authorization — it is deliberately not
        # something this helper strips.
        keys = {pf for pf, _ in IDENTIFIER_MEMORY_FIELDS}
        self.assertEqual(keys - {"cust_code"},
                         {"last_order_code", "last_shipment_code", "last_tracking"})


if __name__ == "__main__":
    unittest.main()
