"""ERP Adapter Interface — the ONLY seam the Slot Filling Engine
(services/slot_filling_engine.py) talks to for actual ERP execution.
Today this is a mock (`MockERPAdapter`); swapping in a real ERP later
means implementing `ERPAdapter` and changing `get_erp_adapter()`'s
return value — the workflow layer that calls it never changes.

This module is deliberately separate from the legacy `erp/bridge.py`
stub (a pre-existing, unfinished MySQL-based scaffold for a different
integration) — it is not touched or reused here, since it is not wired
into any part of this workflow and swapping its unfinished credentials
in was explicitly out of scope ("Do NOT implement ERP").
"""
from abc import ABC, abstractmethod
from typing import Dict


class ERPAdapter(ABC):
    """One method per ERP-backed intent (services.slot_filling_engine
    .ERP_INTENTS) — every method receives the FINAL collected slots dict
    (already validated complete by the Slot Filling Engine) and returns
    a result dict with at least {"status": ..., "message": ...}."""

    @abstractmethod
    def lookup_tracking(self, slots: Dict[str, str]) -> Dict:
        ...

    @abstractmethod
    def lookup_order(self, slots: Dict[str, str]) -> Dict:
        ...

    @abstractmethod
    def lookup_customer(self, slots: Dict[str, str]) -> Dict:
        ...

    @abstractmethod
    def lookup_warranty(self, slots: Dict[str, str]) -> Dict:
        ...

    @abstractmethod
    def lookup_invoice(self, slots: Dict[str, str]) -> Dict:
        ...

    @abstractmethod
    def lookup_payment(self, slots: Dict[str, str]) -> Dict:
        ...


# One acknowledgement message per intent — worded exactly like the
# task's own example ("ได้รับเลขพัสดุเรียบร้อยแล้วค่ะ ... เมื่อเชื่อมต่อระบบหลังบ้าน
# ระบบจะใช้เลขนี้เพื่อตรวจสอบสถานะสินค้า"), generalized to each intent's own
# collected slot rather than hardcoding "เลขพัสดุ" everywhere.
_ACK_LABEL_BY_INTENT = {
    "tracking": "เลขพัสดุ", "order": "เลขออเดอร์", "customer": "ข้อมูลลูกค้า",
    "warranty": "ข้อมูลการรับประกัน", "invoice": "เลขใบกำกับภาษี/ใบสั่งซื้อ", "payment": "รหัสลูกค้า",
}


def _mock_result(intent: str) -> Dict:
    label = _ACK_LABEL_BY_INTENT[intent]
    return {
        "status": "pending_integration",
        "message": f"ได้รับ{label}เรียบร้อยแล้วค่ะ เมื่อเชื่อมต่อระบบหลังบ้าน ระบบจะใช้ข้อมูลนี้เพื่อตรวจสอบให้อัตโนมัติค่ะ",
    }


class MockERPAdapter(ERPAdapter):
    """Current implementation — never calls a real ERP. Every method
    simply acknowledges that the required information was collected and
    explains that the actual lookup happens once ERP integration exists.
    This is what makes the workflow layer "already complete" per the
    task's own framing — only the return value of these 6 methods
    changes when a real ERP adapter replaces this one."""

    def lookup_tracking(self, slots: Dict[str, str]) -> Dict:
        return _mock_result("tracking")

    def lookup_order(self, slots: Dict[str, str]) -> Dict:
        return _mock_result("order")

    def lookup_customer(self, slots: Dict[str, str]) -> Dict:
        return _mock_result("customer")

    def lookup_warranty(self, slots: Dict[str, str]) -> Dict:
        return _mock_result("warranty")

    def lookup_invoice(self, slots: Dict[str, str]) -> Dict:
        return _mock_result("invoice")

    def lookup_payment(self, slots: Dict[str, str]) -> Dict:
        return _mock_result("payment")


_INTENT_TO_METHOD = {
    "tracking": "lookup_tracking", "order": "lookup_order", "customer": "lookup_customer",
    "warranty": "lookup_warranty", "invoice": "lookup_invoice", "payment": "lookup_payment",
}


def execute_erp_intent(adapter: ERPAdapter, intent: str, slots: Dict[str, str]) -> Dict:
    """Dispatches to the right adapter method by intent name — the one
    place callers (services/playground_orchestrator.py) need to know
    about, so a future real adapter's method signatures never leak into
    the orchestrator itself."""
    method_name = _INTENT_TO_METHOD.get(intent)
    if not method_name:
        return {"status": "unknown_intent", "message": ""}
    return getattr(adapter, method_name)(slots)


_instance: ERPAdapter = MockERPAdapter()


def get_erp_adapter() -> ERPAdapter:
    """Named factory, same pattern as services/rag_service.py::
    get_rag_service() and services/llm_service.py::get_llm_service() —
    swap the module-level `_instance` to a real adapter once ERP
    integration exists; every caller keeps working unchanged."""
    return _instance
