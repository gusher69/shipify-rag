# API-Gap Safe-Fallback Evidence (Phase 6 closure gate B)

All **26** API_GAP requirements, each with an executable safe-fallback turn. PASS means all five safety properties held.

| FEEDBACK_ID | Required API capability | Fallback behaviour | Test IDs | PASS/FAIL |
|---|---|---|---|---|
| AI-API-S1-11.0 | GET /orders/{bill}/items + POST /claims | collects the identifier then hands off | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S1-12.0 | GET /shipments/tracking/{tracking_cn} | routed WORKFLOW (private_state_inquiry) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S1-16.0 | GET /orders/{bill}/status | routed WORKFLOW (private_state_inquiry) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S1-17.0 | GET /shipments/{bill}/tracking_th | routed WORKFLOW (None) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S1-18.0 | GET /wallet/{cust}/transactions | collects the identifier then hands off | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S1-21.0 | GET /orders/{bill}/status (payment_status) | routed RAG (fresh_search) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S1-3.0 | GET /orders/{bill}/tracking + ETA | routed WORKFLOW (private_state_inquiry) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-1.0 | GET /orders/{bill}/tracking | routed WORKFLOW (private_state_inquiry) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-11.0 | PUT /shipments/{bill}/carrier | collects the identifier then hands off | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-12.0 | GET+POST /wallet/{cust}/withdraw (shipment) | routed WORKFLOW (shipping_withdrawal_kb) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-13.0 | DELETE /shipments/{bill} (+ human confirm) | collects the identifier then hands off | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-15.0 | GET /warehouse/cn/address?customer_id= | collects the identifier then hands off | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-17.0 | GET /shipments/{bill}/tracking_th | routed WORKFLOW (private_state_inquiry) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-18.0 | GET /shipments?arriving_today=true | routed WORKFLOW (fresh_search) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-2.0 | PUT /orders/{bill}/items | collects the identifier then hands off | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-3.0 | PUT /orders/{bill}/shipping_type | collects the identifier then hands off | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-4.0 | PUT /orders/{bill}/vat | collects the identifier then hands off | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-5.0 | GET /wallet/{cust} (purchase credit) | routed WORKFLOW (purchase_withdrawal_kb) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-7.0 | GET /documents/{bill}/invoice | routed RAG (fresh_search) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-8.0 | GET /shipments/map?tracking_cn= | collects the identifier then hands off | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| AI-API-S2-9.0 | PUT /shipments/{bill}/address | routed WORKFLOW (fresh_search) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| LINE-02 | batch tracking lookup (multi-record) | routed WORKFLOW (fresh_search) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| LINE-04 | PUT shipping_type after payment | collects the identifier then hands off | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| LINE-13 | typed wallet/credit pools | routed RAG (fresh_search) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| LINE-15 | tracking->bill mapping | routed RAG (fresh_search) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
| LINE-22 | tracking de-duplication | routed RAG (fresh_search) | tests/test_phase6_api_gap_fallback.py::TestApiGapSafeFallback::test_1..test_5 | PASS |
