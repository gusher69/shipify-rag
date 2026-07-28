"""Business Action Provider abstraction (Milestone: Business Action
Framework + ERP Sync Preparation, Phase 5).

Extends the EXISTING Business Action Registry -> Action Executor chain —
no new framework. A Provider is the seam the Action Executor's TOOL
adapter (services/action_executor.py::_execute_tool, via TOOL_REGISTRY)
calls into for one of five data categories: customer / order / tracking
/ finance / product. The Action itself (a business_actions row) never
knows or cares which provider implementation is active — only
get_provider() below does, via a per-category, environment-configurable
registration:

    Business Action Registry -> Action Executor -> Provider (this module)

Today only MockProvider implementations exist for every category
(Phase 3/6 — safe to exercise end-to-end from the Playground). A future
real ERP connector (Phase 7) is added by implementing BusinessDataProvider
for that category and registering it in _PROVIDER_IMPLEMENTATIONS below
— every Action/Executor/Playground call site is unchanged.

Standardized shape (Phase 4) — every provider's lookup() returns EITHER:
    {"ok": True,  "data": {...}, "source": str, "error": None}
    {"ok": False, "data": None,  "source": str, "error": {"code": str, "message": str}}
never raises for an ordinary "not found"/"missing parameter" case (only a
genuine programming error would raise) — this is the SAME "ok"-flagged
shape services/action_executor.py's existing _tool_calculator/
_tool_url_converter already use, so _execute_tool's status mapping
(`"success" if outcome.get("ok", True) else "error"`) needs no changes.
"""
import os
from abc import ABC, abstractmethod
from typing import Dict, Optional

PROVIDER_CATEGORIES = ("customer", "order", "tracking", "finance", "product")


class BusinessDataProvider(ABC):
    """One implementation per category. `lookup(params)` receives the
    resolved parameter values dict (whatever the Action Executor/caller
    collected — e.g. Action Executor's _collect_parameter_values, or a
    manual Playground test payload) and returns a normalized result —
    see normalize_result/normalize_error below. Must never raise for a
    missing/invalid input; that's an ordinary error result, not an
    exception."""

    @abstractmethod
    def lookup(self, params: Dict[str, str]) -> Dict:
        ...


def normalize_result(data: Dict, *, source: str) -> Dict:
    """The standardized SUCCESS shape every provider returns (Phase 4:
    "Normalized Response")."""
    return {"ok": True, "data": data, "source": source, "error": None}


def normalize_error(message: str, *, source: str, code: str = "not_found") -> Dict:
    """The standardized ERROR shape (Phase 4: "Error Schema") — never a
    raised exception, always a plain, inspectable result."""
    return {"ok": False, "data": None, "source": source,
            "error": {"code": code, "message": message}}


def _first_present(params: Dict[str, str], *names: str) -> Optional[str]:
    """Accepts several historically-used parameter name spellings for the
    same field (e.g. a REST-style "CustCode" vs. a Playground manual-test
    field "customer_code") without the provider needing to know which
    naming convention the caller used."""
    for name in names:
        value = params.get(name)
        if value not in (None, ""):
            return value
    return None


# ── Mock Providers (Phase 3) — one per category, deterministic, no
# network call, safe to exercise end-to-end from the Playground. ────────

class MockCustomerProvider(BusinessDataProvider):
    """Input Schema: at least one of CustCode/CustEmail/CustName/CustPhone
    (mirrors the existing customer_data_lookup ("GetDataCustomer")
    Business Action's own AT_LEAST_ONE parameter group — see
    tools/seed_business_action_getdatacustomer.py)."""

    def lookup(self, params: Dict[str, str]) -> Dict:
        code = _first_present(params, "CustCode", "customer_code")
        email = _first_present(params, "CustEmail", "customer_email")
        name = _first_present(params, "CustName", "customer_name")
        phone = _first_present(params, "CustPhone", "customer_phone")
        if not any([code, email, name, phone]):
            return normalize_error(
                "ต้องระบุอย่างน้อยหนึ่งค่า: รหัสลูกค้า, อีเมล, ชื่อ หรือเบอร์โทร",
                source="mock_customer", code="missing_identifier")
        return normalize_result({
            "customer_code": code or "MOCK-CUST-0001",
            "name": name or "ลูกค้าทดสอบ (ม็อก)",
            "email": email or "mock.customer@example.com",
            "phone": phone or "080-000-0000",
            "status": "active",
            "member_since": "2024-01-01",
        }, source="mock_customer")


class MockOrderProvider(BusinessDataProvider):
    """Input Schema: OrderCode (required)."""

    def lookup(self, params: Dict[str, str]) -> Dict:
        order_code = _first_present(params, "OrderCode", "order_code")
        if not order_code:
            return normalize_error("ต้องระบุเลขออเดอร์ (OrderCode)",
                                    source="mock_order", code="missing_order_code")
        return normalize_result({
            "order_code": order_code,
            "status": "shipped",
            "total_amount": 1250.00,
            "currency": "THB",
            "items": [{"sku": "MOCK-SKU-001", "name": "สินค้าทดสอบ", "qty": 2}],
            "created_at": "2026-07-15",
        }, source="mock_order")


class MockTrackingProvider(BusinessDataProvider):
    """Input Schema: TrackingNumber (required)."""

    def lookup(self, params: Dict[str, str]) -> Dict:
        tracking_no = _first_present(params, "TrackingNumber", "tracking_number")
        if not tracking_no:
            return normalize_error("ต้องระบุเลขพัสดุ (TrackingNumber)",
                                    source="mock_tracking", code="missing_tracking_number")
        return normalize_result({
            "tracking_number": tracking_no,
            "status": "in_transit",
            "location": "Bangkok Sorting Center",
            "estimated_delivery": "2026-07-25",
            "history": [{"status": "picked_up", "date": "2026-07-20"},
                        {"status": "in_transit", "date": "2026-07-22"}],
        }, source="mock_tracking")


class MockFinanceProvider(BusinessDataProvider):
    """Input Schema: at least one of InvoiceNumber / OrderCode."""

    def lookup(self, params: Dict[str, str]) -> Dict:
        invoice_no = _first_present(params, "InvoiceNumber", "invoice_number")
        order_code = _first_present(params, "OrderCode", "order_code")
        if not (invoice_no or order_code):
            return normalize_error("ต้องระบุเลขใบกำกับภาษี (InvoiceNumber) หรือเลขออเดอร์ (OrderCode)",
                                    source="mock_finance", code="missing_reference")
        return normalize_result({
            "invoice_number": invoice_no or f"INV-{order_code}",
            "order_code": order_code,
            "amount_due": 0.0,
            "status": "paid",
            "payment_method": "bank_transfer",
            "paid_at": "2026-07-16",
        }, source="mock_finance")


class MockProductProvider(BusinessDataProvider):
    """Input Schema: SKU (required)."""

    def lookup(self, params: Dict[str, str]) -> Dict:
        sku = _first_present(params, "SKU", "sku", "ProductCode", "product_code")
        if not sku:
            return normalize_error("ต้องระบุรหัสสินค้า (SKU)",
                                    source="mock_product", code="missing_sku")
        return normalize_result({
            "sku": sku,
            "name": "สินค้าทดสอบ (ม็อก)",
            "in_stock": True,
            "quantity_available": 42,
            "price": 199.0,
            "currency": "THB",
        }, source="mock_product")


_PROVIDER_IMPLEMENTATIONS: Dict[str, Dict[str, type]] = {
    "customer": {"mock": MockCustomerProvider},
    "order": {"mock": MockOrderProvider},
    "tracking": {"mock": MockTrackingProvider},
    "finance": {"mock": MockFinanceProvider},
    "product": {"mock": MockProductProvider},
}

_provider_instances: Dict[str, BusinessDataProvider] = {}


def get_provider(category: str) -> BusinessDataProvider:
    """The ONE configurable seam (Phase 5): which implementation backs a
    category is chosen here, via env var
    `BUSINESS_ACTION_PROVIDER_<CATEGORY>` (default "mock"), never inside
    the Action or the Executor — a future real ERP connector (Phase 7)
    is added by registering it in _PROVIDER_IMPLEMENTATIONS[category]
    under a new key (e.g. "erp") and setting the env var; every caller
    (Action Executor's TOOL adapter, the Playground manual tester)
    keeps working completely unchanged. Requesting a provider kind that
    isn't registered raises immediately — a misconfiguration is never
    silently downgraded to the mock."""
    if category not in _PROVIDER_IMPLEMENTATIONS:
        raise ValueError(f"Unknown business action provider category: {category!r} "
                          f"(expected one of {PROVIDER_CATEGORIES})")
    cache_key = category
    if cache_key in _provider_instances:
        return _provider_instances[cache_key]
    kind = os.getenv(f"BUSINESS_ACTION_PROVIDER_{category.upper()}", "mock").strip().lower()
    implementations = _PROVIDER_IMPLEMENTATIONS[category]
    if kind not in implementations:
        raise NotImplementedError(
            f"Provider kind {kind!r} for category {category!r} is not implemented — "
            f"available: {list(implementations)}. Real ERP connectors are Phase 7 future work.")
    instance = implementations[kind]()
    _provider_instances[cache_key] = instance
    return instance


def reset_provider_cache() -> None:
    """Test-only helper — clears cached provider instances so a test can
    change BUSINESS_ACTION_PROVIDER_<CATEGORY> and see the effect."""
    _provider_instances.clear()
