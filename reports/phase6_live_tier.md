# Live-Tier Evaluation — Phase 6 Post-Deploy Systemic Hardening

Explicitly invoked (spends real API credits):

    SHIPIFY_LIVE_TIER=1 python -m tests.phase6_lab.run_live_tier

**Status: PASS** — 199 live turns (136 through the central interpreter, 63 through the full DecisionEngine), of which **32** actually consulted the model. Violations recorded: **0**.

## Hard requirements (all must be 0)

| Metric | Result |
|---|---|
| high_confidence_deterministic_override_by_llm | **0** |
| auth_violation | **0** |
| private_data_leak | **0** |
| false_action_completion | **0** |
| hallucinated_business_fact | **0** |
| public_to_private_false_positive | **0** |
| private_to_public_false_negative | **0** |
| cancellation_to_withdrawal | **0** |
| withdrawal_to_cancellation | **0** |
| product_clause_contamination | **0** |
| unit_echo_failure | **0** |

## The three post-deploy defect classes, measured live

| Defect class | Live metric | Result |
|---|---|---|
| A — entity boundary / multi-intent | product_clause_contamination | **0** |
| B — intent discrimination | cancellation_to_withdrawal | **0** |
| B — intent discrimination | withdrawal_to_cancellation | **0** |
| C — slot unit preservation | unit_echo_failure | **0** |

Class A is measured by asking the SAME import-interest turn twice — once without a trailing question clause and once with one — and requiring the product entity to be identical. Class B asserts an expected family against the LIVE interpretation for every cancellation-policy, cancellation-operation and withdrawal wording, so a model that wants to call a cancellation question a withdrawal is caught here and not only offline. Class C renders the acknowledgement for each of the ten count units and requires the customer's own unit to appear in it.

## Grounding check

Every factual claim is classified rather than skipped, whatever the route:

| Classification | Count |
|---|---|
| SUPPORTED_FACT | 12 |
| UNSUPPORTED_ADDITION | 0 |
| NON_FACTUAL_LANGUAGE | 2 |

A claim counts as SUPPORTED when it appears in an authoritative Knowledge Base chunk (read live from `knowledge_chunks`) **or** in an approved committed business constant. Support is semantic — no verbatim sentence match is required.

## Interpreter authority

**0** high-confidence deterministic families were overridden by the gated LLM across 136 interpreter turns (32 of which consulted the model). The cancellation families resolve deterministically at confidence 0.85, so the LLM is no longer consulted for them at all — which is the whole of root cause G.

Public->private false positive **0**, private->public false negative **0**, entity mismatch **0**.

