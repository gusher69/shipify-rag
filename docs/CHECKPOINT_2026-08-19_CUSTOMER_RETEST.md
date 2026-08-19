# Checkpoint — Customer Retest Baseline (2026-08-19)

**Status: FROZEN — awaiting customer feedback. No further fixes or migrations until feedback arrives.**

## Baseline

| | |
|---|---|
| Baseline commit | `5898073` — "fix: GetUrlProductDetail no longer misroutes or drops the URL across turns" |
| Local repo | clean, `HEAD == origin/main == 5898073` |
| Server (`root@10.0.1.243:/opt/shipify-ai`) | `git HEAD == 5898073`, in sync with `origin/main` |
| Containers | `shipify-ai-admin-1` and `shipify-ai-line-webhook-1`, both rebuilt from this commit, both healthy (`admin` → 302, `line-webhook` → 400 on an unsigned probe, both expected) |

## What this baseline fixes (GetUrlProductDetail / "แปลงลิงค์")

Customer reported the product-link-conversion feature wasn't working. Root cause turned out to be two Decision Engine bugs (not the CustCode requirement, which is genuine per the real FastTrade ERP — verified empirically: URL-only → HTTP 400, URL+real CustCode → HTTP 200):

1. `_bind_message_to_action`'s generic identifier scanner was tokenizing digits from *inside a URL* (e.g. `1688` from the domain) as a plausible value for an unrelated sibling action's parameter, which silently misrouted the customer's next reply away from `geturlproductdetail` right after asking for CustCode.
2. `_extract_system_values()` only read the URL from the current turn's message, so a URL given earlier in the conversation vanished by the time CustCode arrived on a later turn.
3. (Minor, same fix) the polite `ค่ะ` particle was glued directly onto a trailing URL with no space, corrupting the link.

Fix is entirely in `services/decision_engine.py` — no parameter/action metadata was changed; CustCode stays required.

## Evidence preserved

- **Golden Test Suite**: `tests/golden/golden_cases.json`, dataset v1.3.0, 59 cases (58 prior + `GOLDEN-058-URL-CONVERSION-CUSTCODE-CONTINUITY`), committed at `5898073`.
- **Unit regression**: `tests/test_decision_engine.py::TestUrlConversionActionAndCrossActionIdentifierReuse` (7 focused tests) + full suite 1959/1959 passing except 2 pre-existing, unrelated failures (`test_hybrid_playground_route.py`, reproduced identically on the pre-fix baseline).
- **Production AI Playground UAT (real ERP calls, real browser session)**: 6/6 natural-conversation journeys PASS — URL-first, CustCode-first, natural multi-turn conversation, URL/identifier isolation, cross-session isolation, and a 5-turn long-context journey. Full turn-by-turn transcript (session IDs, `production_trace`, `collected_parameters`, `erp_http_status`) is recorded in this session's own chat log; sessions were left **visible, not cleared**, in the AI Playground's session list on the server for independent audit.
- **Live empirical ERP contract verification**: direct calls to `https://fasttrade.in.th/web-service/ai-chat/GetUrlProductDetail` proving CustCode is genuinely required and that it determines the output link's domain (`SP1014` → shipify.co.th, `FT1004` → fasttrade.in.th).

## Explicit freeze conditions

- No application logic, parameters, or Business Action metadata changed since `5898073`.
- No database migrations run.
- No Playground/Conversation sessions cleared — the UAT trail from this checkpoint remains visible in the Admin UI (`/admin/preview`, `/admin/conversations`).
- Next action is customer retest of the "แปลงลิงค์" flow; only resume fixing on new customer feedback.

## Final Report

```
Baseline Commit: 5898073
Server HEAD: 5898073 (in sync with origin/main)
Containers: admin=healthy, line-webhook=healthy (both rebuilt from 5898073)
Golden/UAT evidence preserved: YES (GOLDEN-058 committed, 6/6 Playground UAT journeys, sessions left visible)
READY FOR CUSTOMER RETEST: YES
```
