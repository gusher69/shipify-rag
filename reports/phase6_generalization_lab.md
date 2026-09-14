# Phase 6 Generalization Lab

**1517 conversation turns** — generated combinatorially from structure (noun x unit x ordering x spacing x particle x correction form), not from a list of customer sentences. Regenerate with `python -m tests.phase6_lab.run_lab`.

## Hard requirements (all must be 0)

| Metric | Result |
|---|---|
| product_quantity_collision | **0** |
| product_noun_truncation | **0** |
| known_slot_reask | **0** |
| stale_journey_takeover | **0** |
| auth_violation | **0** |
| private_data_leak | **0** |
| hallucinated_business_fact | **0** |
| false_action_completion | **0** |

## Accuracy

| Metric | Result |
|---|---|
| INTENT/PRODUCT accuracy | 98.06% (1265/1290) |
| QUANTITY accuracy | 99.56% (906/910) |
| UNIT preservation | 100.0% |
| METHOD accuracy | 100.0% |
| CORRECTION accuracy | 100.0% |

## Tiers

- entity/state tier: 1385 turns (deterministic interpreter + frame planner)
- journey tier: 100 turns across 25 multi-turn journeys — known-slot re-ask 0, stale takeover 0
- safety tier: 32 turns through the REAL DecisionEngine — false action completion 0, invented private state 0, auth violation 0, public-question identity demand 0, dead end 0

## Residual misses

29 entity-tier misses remain (0 of them truncations or collisions), 0 journey, 0 safety. See failure_samples in the JSON.
