# Issue Report — SearchDataTracking returns HTTP 500

**Status**: CUSTOMER_API_DEFECT — confirmed on the live `fasttrade.in.th` API, not on the Shipify integration side.

## Summary
`POST /web-service/ai-chat/SearchDataTracking` returns a real server-side `500` for every real `Tracking` value tested, across 6 different values pulled from 5 different real shipments (`SearchDataShipmentList` output for customer `SP1014`). The `Tracking` parameter name itself appears correct — a missing/wrong parameter name produces a distinct `400` ("กรุณาระบุข้อมูลให้ครบถ้วน" / please provide complete information), which is NOT what happens here; validation passes and the request reaches a downstream query that then crashes.

## Request (sanitized — `SecretCode` never logged/shown)
```
POST https://fasttrade.in.th/web-service/ai-chat/SearchDataTracking
Content-Type: application/x-www-form-urlencoded

SecretCode=[MASKED]
CustCode=SP1014
Tracking=testsystem690522002
```

## Response
```
HTTP 500
{"status":"error","message":"Query : Trying to get property 'type_bill' of non-object"}
```

## Real values tested (all produced the identical error)
- `testsystem690806`
- `SPTestopen001`
- `SPTestopen002`
- `testsystem690522`
- `testsystem690522002`
- `testMao690514`

## Interpretation
The error message ("Trying to get property 'type_bill' of non-object") reads like a PHP null-object-access error — the backend query that's supposed to look up a bill record by tracking number appears to return no matching object, and the code then tries to read `type_bill` off that non-existent result without a null check. This suggests either: (a) a lookup/query bug independent of the tracking value, or (b) these particular tracking numbers aren't indexed the way this specific endpoint expects (worth checking whether `SearchDataTracking` expects a different bill-type scope than the ones these test tracking numbers belong to).

## What we need
Could your backend team check the `SearchDataTracking` handler for this null-check gap, and/or confirm whether these tracking numbers are the right kind of value for this specific endpoint (vs. `TrackingCH`/`TrackingTH` semantics elsewhere in the API)?

## Current state on our side
`SearchDataTracking` is registered in Shipify's Business Action Registry but **disabled** — it will not be selectable by the AI or execute in production until this is resolved and re-verified.
