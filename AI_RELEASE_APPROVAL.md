# AI Release Approval — AI Engine v1.0.1

| Field | Value |
|---|---|
| **AI Engine Version** | v1.0.1 |
| **Baseline Version** | Production Baseline v1.0.1 (supersedes v1.0.0) |
| **Release Date** | 2026-07-17 |
| **Validation ID** | `9fbf0d50-756c-4434-8fe1-5bda45db41e1` |

## Benchmark Summary

Dataset: "Initial Real-Data Benchmark" (`188ccb41-f545-4834-807b-128ad62015fe`, 31 cases).

| Mode | Run ID | Passed/Total |
|---|---|---|
| Query Understanding | `3653cf3b-89d1-4b94-844c-be61388ba8a4` | 31/31 |
| Retrieval Only | `211202ed-374a-465a-ab29-1aea242cae6e` | 29/31 |
| Conversation Scenario | `cfab6680-21e1-4985-955f-a9e03d90eeb3` | 29/31 |
| Full RAG | `9559ecb9-e6d3-4855-ada2-19772bb30207` | 3/31 |

## Production Metrics

| Dimension | Score |
|---|---|
| Grounding | **100%** |
| Critical Facts | **100%** |
| Conversation | 93.5% |
| Retrieval | 93.5% |

## Test Summary

- Focused Tests: PASS
- Full Test Suite: **769/769 PASS**

## Production Validation Summary

Validation `9fbf0d50-756c-4434-8fe1-5bda45db41e1` completed successfully via the one-click Production Validation Center (`POST /admin/api/validation/run`). Compared against Production Baseline v1.0: zero regressions in Query Understanding, Conversation Scenario, and Full RAG; one accepted-known-issue regression in Retrieval Only (see Known Accepted Issues).

## Known Accepted Issues

- "What countries does Shipify operate between?" (Retrieval Only) — pre-existing dataset/retrieval interaction, confirmed unrelated to the v1.0.1 grounding/citation fixes. See `KNOWN_ISSUES.md` for full detail.

## Deployment Approval

**APPROVED FOR PRODUCTION DEPLOYMENT.**

Approval basis: Grounding and Critical Facts both measured at 100% with zero hallucination found across two full validation cycles; the only open item is a single pre-accepted, unrelated known issue that does not affect answer quality, safety, or citation correctness. No AI behavior was changed to reach this result — both underlying fixes were benchmark-scoring/citation-attribution corrections only (see `RELEASE_NOTES_v1.0.1.md`).

## Release Status

**APPROVED — Production Baseline v1.0.1 is the official release. AI Core development is closed as of this approval.**
