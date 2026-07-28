# Known Issues Register — AI Engine

Tracks known, accepted, or deferred issues across production baselines. Updated at each release.

---

## KI-001

| Field | Value |
|---|---|
| **Issue ID** | KI-001 |
| **Description** | Question "What countries does Shipify operate between?" fails Retrieval Only benchmark mode — the retriever does not reliably surface the exact expected_file for this abstractly-phrased, inferential question. |
| **Status** | **Accepted Known Issue** |
| **Severity** | Low |
| **Workaround** | None required — the underlying knowledge base content (warehouse/shipping data covering China and Thailand) still supports a correct answer in practice via Full RAG mode; this is a benchmark-assertion strictness issue on one specific case, not a customer-facing defect. |
| **Owner** | AI Engine maintainers |
| **Next Review** | Next Production Baseline promotion (whenever v1.0.2 or later is proposed) |

**Root cause**: this case's `expected_file` was set to a real KB file during the Production Cleanup dataset refresh, converting what was previously a vacuous pass (no expected_file asserted) into a real retrieval assertion. The question's phrasing ("what countries...") is abstract/inferential rather than a literal FAQ-style question, which the current hybrid retrieval scoring does not always rank the single best-matching chunk for.

**Decision**: Confirmed unrelated to the v1.0.1 Grounding/Citation Attribution fixes (present identically before and after those fixes). Does not block Production Baseline v1.0.1 approval. Retrieval behavior intentionally left unmodified per this release's scope (no Retrieval redesign permitted).

---

## Template for future entries

```
## KI-00X

| Field | Value |
|---|---|
| Issue ID | KI-00X |
| Description | |
| Status | Open / Accepted Known Issue / Resolved / Deferred |
| Severity | Low / Medium / High |
| Workaround | |
| Owner | |
| Next Review | |
```
