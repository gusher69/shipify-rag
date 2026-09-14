# Customer Feedback Master Inventory — Phase 6

Canonical, single-file index of every customer-feedback / UAT / real-LINE
requirement known to this project as of this task, merging three
previously-separate sources into one acceptance surface. Full
machine-readable detail: [`customer_feedback_master_inventory.json`](customer_feedback_master_inventory.json)
(80 records). This document does not replace
`docs/customer_uat_sources/CUSTOMER_UAT_SOURCE_MANIFEST.md` or
`docs/customer_uat_sources/CUSTOMER_MASTER_FAILURE_INVENTORY.md` — it
indexes them, plus the engineering-log fixes and architecture program
this same session already shipped, plus today's new defect class.

## Source files found

| Named source | Found? | Location |
|---|---|---|
| `Ai.xlsx` | YES | `docs/customer_uat_sources/Ai.xlsx` |
| `ปัญหาที่เจอในการตอบ (1).xlsx` | YES | `docs/customer_uat_sources/ปัญหาที่เจอในการตอบ (1).xlsx` |
| `เคสที่ต้องแก้ใน 1.คำถามทั่วไป+2.ต้องเช็คในระบบ.pdf` | YES | `docs/customer_uat_sources/เคสที่ต้องแก้ใน 1.คำถามทั่วไป+2.ต้องเช็คในระบบ.pdf` |
| 5 LINE-chat screenshots | YES | `docs/customer_uat_sources/screenshot/*.jpg` |
| Human-CS style analysis (SP6918/FT1145/SA5061/SP7521) | YES | `docs/customer-service/01_HUMAN_CS_ANALYSIS.md` (analysis only — raw chat exports are, by design, never committed) |
| `tests/customer_uat/customer_uat_master.jsonl` (69 logical cases) | YES | already exists, already machine-readable |
| `docs/customer_uat_sources/CUSTOMER_MASTER_FAILURE_INVENTORY.md` (per-case classification) | YES | already exists |

## Source files NOT found (reported explicitly, not invented)

| Named source | Status | Nearest existing artifact (NOT assumed to be the same document) |
|---|---|---|
| "แก้ไขเคส Shipify Part 2" (`.docx`, cited by `services/service_intent_flow.py`'s own docstring as its customer-acceptance source) | **NOT FOUND** under that name or extension anywhere in the repo | `docs/customer_uat_sources/เคสที่ต้องแก้ใน 1.คำถามทั่วไป+2.ต้องเช็คในระบบ.pdf` — a case-fixing document covering the same two sheets ("1.คำถามทั่วไป" / "2.ต้องเช็คในระบบ") referenced elsewhere in the manifest, but a PDF, not a `.docx`, and under a different filename. Cannot confirm these are the same source. |
| `AI_API_Requirement_For_Client.xlsx` | **NOT FOUND** under that exact filename | `Ai.xlsx` — already cataloged, contains an `API Summary for Client` sheet and an `API` sheet (base_url + a real SecretCode, masked everywhere per the existing manifest). Plausibly related, not confirmed identical. |
| Raw SP7521 / SP6918 / SA5061 / FT1145 LINE chat exports | **NOT FOUND** (by design — never committed) | `docs/customer-service/01_HUMAN_CS_ANALYSIS.md` is the sanitized, already-existing style analysis derived from them; used as this task's Step 6 real-LINE-pattern source in place of the raw exports. |

## Coverage summary (80 records)

| Bucket | Count | Meaning |
|---|---|---|
| PASS today | 48 | Existing customer_uat cases, already passing (`CUSTOMER_MASTER_FAILURE_INVENTORY.md`) |
| FAIL — TEST_HARNESS_BUG | 9 | Behavior is correct; the routing-only measurement harness under-counts it |
| FAIL — STALE_EXPECTATION | 5 | A since-shipped, tested feature exceeds the master's older expectation |
| FAIL — BUSINESS_LOGIC_BUG | 5 | Real, pre-existing, out-of-scope-for-this-task product gaps |
| FAIL — DEFERRED_FEATURE | 2 | No Business Action / operational-change kind configured yet |
| FIXED_AND_TESTED (this task + this session) | 9 | FIX-03..06 (4) + PHASE6-SLOT-01..05 (5, today's defect) |
| SHIPPED_SHADOW_MODE | 1 | The P1→P2.1A conversation-intelligence program (additive, shadow-only, not yet read-authoritative) |
| KNOWN_GAP_NOT_FIXED | 1 | PHASE6-SLOT-06 — product-noun truncation via an unanchored filler particle; see Technical Debt in the final report |

**Nothing in this list is UNTESTED without an explicit reason** — every
FAIL row carries an evidenced classification in
`CUSTOMER_MASTER_FAILURE_INVENTORY.md`; the one genuinely new,
not-yet-fixed gap (PHASE6-SLOT-06) is called out by name, not silently
dropped.

## New records this task added (feedback_id prefix `PHASE6-SLOT-`)

Traced end-to-end from the owner's real LINE OWNER_TEST repro
(`20 คู่อยากสั่งของจากจีน`) to the exact function and line responsible —
see `customer_feedback_master_inventory.json` for full per-record detail,
and the Phase 6 FINAL REPORT (chat reply, this task) for the narrative
root-cause trace and the fix applied to each.
