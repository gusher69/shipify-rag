# SEMANTIC LIBRARY BENCHMARK — EXPERIMENT ONLY

- **Control:** current production semantic behaviour, candidate `afbeb28` (real `services.decision_engine` helpers — `_is_social_only`, `_CONVERSATION_CANCEL_RE`, `_classify_private_state_inquiry`, `classify_turn_intent`, `classify_question`).
- **Isolation:** library experiments ran in a throw-away venv. **Production code changed: NO · Production dependencies changed: NO · Deployed: NO.**
- **Dataset:** 178 labelled examples — 39 seed, 43 held-out (disjoint wordings incl. the 10 critical cases + typo/spacing paraphrases), 96 Public/Private items derived from the 69-case UAT master + variants.
- **Library versions:** `{'semantic_router_version': '0.1.2', 'encoder': 'openai/text-embedding-3-small', 'op_routes': ['GREETING', 'CONTINUE', 'CANCEL', 'CHANGE_TARGET', 'CORRECT_VALUE', 'CHANGE_TOPIC', 'NEW_ACTION'], 'scope_routes': ['PUBLIC_INFO', 'PRIVATE_UNSPECIFIED', 'PRIVATE_LATEST', 'PRIVATE_EXPLICIT', 'PRIVATE_LIST_ALL'], 'seed_utterances': 94, 'pythainlp_version': '5.3.7', 'rapidfuzz_version': '3.14.6'}`

## Scoreboard (held-out unless noted)

| Config | op acc (held-out) | op acc (all) | Public/Private (UAT69) | record-scope | held-out paraphrase | typo | crit-10 ops | Pub→Priv FP | median ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|
| 1. CURRENT (control) | 79.1 | 90.4 | 70.8 | 71.4 | 100.0 | 100.0 | 8/10 | 0.0 | 0.04 | 0.081 |
| 2. PyThaiNLP → current | 69.8 | 86.0 | 67.7 | 21.4 | 100.0 | 100.0 | 7/10 | 0.0 | 0.042 | 0.066 |
| 3. RapidFuzz → current | 69.8 | 86.0 | 67.7 | 21.4 | 100.0 | 100.0 | 7/10 | 0.0 | 0.042 | 0.067 |
| 4. Semantic Router | 81.4 | 64.6 | 46.9 | 67.9 | 73.3 | 100.0 | 10/10 | 15.8 | 454.716 | 556.17 |
| 5. PyThaiNLP + Router | 79.1 | 65.2 | 44.8 | 53.6 | 73.3 | 80.0 | 10/10 | 22.4 | 453.576 | 548.34 |
| 6. RapidFuzz + Router | 79.1 | 64.6 | 43.8 | 53.6 | 73.3 | 80.0 | 10/10 | 22.4 | 453.481 | 526.667 |
| 7. PyThaiNLP + RapidFuzz + Router | 79.1 | 64.6 | 43.8 | 53.6 | 73.3 | 80.0 | 10/10 | 22.4 | 468.278 | 627.809 |

## Per-operation op-accuracy

| Config | GREETING | CONTINUE | NEW_ACTION | CHANGE_TOPIC | CHANGE_TARGET | CORRECT_VALUE | CANCEL |
|---|---|---|---|---|---|---|---|
| 1. CURRENT (control) | 80.0 | 100.0 | 100.0 | 0.0 | 0.0 | 0.0 | 100.0 |
| 2. PyThaiNLP → current | 50.0 | 100.0 | 100.0 | 0.0 | 0.0 | 0.0 | 28.6 |
| 3. RapidFuzz → current | 50.0 | 100.0 | 100.0 | 0.0 | 0.0 | 0.0 | 28.6 |
| 4. Semantic Router | 80.0 | 77.8 | 57.7 | 83.3 | 100.0 | 100.0 | 100.0 |
| 5. PyThaiNLP + Router | 80.0 | 77.8 | 58.4 | 83.3 | 100.0 | 100.0 | 100.0 |
| 6. RapidFuzz + Router | 80.0 | 77.8 | 57.7 | 83.3 | 100.0 | 100.0 | 100.0 |
| 7. PyThaiNLP + RapidFuzz + Router | 80.0 | 77.8 | 57.7 | 83.3 | 100.0 | 100.0 | 100.0 |

## record_scope accuracy

| Config | UNSPECIFIED | LATEST | EXPLICIT |
|---|---|---|---|
| 1. CURRENT (control) | 88.9 | 80.0 | 0.0 |
| 2. PyThaiNLP → current | 33.3 | 0.0 | 0.0 |
| 3. RapidFuzz → current | 33.3 | 0.0 | 0.0 |
| 4. Semantic Router | 50.0 | 100.0 | 100.0 |
| 5. PyThaiNLP + Router | 50.0 | 60.0 | 100.0 |
| 6. RapidFuzz + Router | 50.0 | 60.0 | 100.0 |
| 7. PyThaiNLP + RapidFuzz + Router | 50.0 | 60.0 | 100.0 |

## Install / dependency footprint

| Library | Version | Installed clean on Python 3.13 + Windows | Footprint |
|---|---|---|---|
| PyThaiNLP | 5.3.7 | YES | ~40 MB (+ data corpora on first use); pure-Python |
| RapidFuzz | 3.14.6 | YES | ~1 MB C-extension |
| semantic-router | 0.1.2 | **NO — needs a shim** | pulls `litellm` (unconditional import), `tokenizers`, `aurelio-sdk`, `numpy`, `openai`; `semantic-router==0.1.16` requires `litellm` whose compatible older builds need a **Rust toolchain** to compile, and current `litellm` (1.99) dropped the top-level `EmbeddingResponse` symbol the package imports at load time — the benchmark had to monkey-patch a stub to import it at all |

## Latency (per message, this machine)

| Step | median | p95 |
|---|---|---|
| CURRENT semantic layer (regex only) | 0.04 ms | 0.081 ms |
| PyThaiNLP tokenize + normalize | 1.659 ms | — |
| PyThaiNLP + spell-correct | **448.5 ms** | — |
| RapidFuzz canonicalize | 0.1692 ms | — |
| Semantic Router (2 sequential OpenAI embeddings) | **454.716 ms** | **556.17 ms** |

## Acceptance-rule evaluation

- **PyThaiNLP → current / RapidFuzz → current:** op-accuracy (held-out) *regresses* 79.1% → 69.8% and record-scope *collapses* 71.4% → 21.4% — word-tokenization inserts spaces that break the current engine's contiguous-Thai patterns. Adding spell-correction costs ~448 ms/msg. **REJECT.**
- **RapidFuzz as a primitive** (0.17 ms fuzzy token match) is cheap and could help a *targeted* typo-normalization of a known small vocabulary, but as a full-text canonicalizer it inherits the same tokenization damage. **REJECT as a text layer; keep as a possible narrow helper.**
- **Semantic Router:** held-out op-accuracy +2.3% and it *does* fix the one critical failure family the current engine has no detector for (CHANGE_TARGET 0→100, CORRECT_VALUE 0→100, CHANGE_TOPIC 0→83). **But it triggers every hard-reject condition:** Public/Private *regresses* 70.8% → 46.9%; public-question over-gating (Pub→Priv false positives) rises 0.0% → 15.8%; held-out paraphrase pass drops 100.0% → 73.3%; latency +454.716 ms/msg (2 embedding round-trips); dependency graph does not install cleanly on the target stack. **REJECT.**

## Recommendation

**Do NOT integrate any of the three libraries into production.** The current `afbeb28` deterministic semantic layer is faster by ~4 orders of magnitude (0.04 ms vs 450 ms), never over-gates a public question as private (0% vs 15.8% false positives), and scores higher on the bulk Public/Private and NEW_ACTION classification. The only real gap the libraries expose — no dedicated CHANGE_TARGET / CORRECT_VALUE / CHANGE_TOPIC-during-pending detector — is best closed the same way SEM-1.2 closed CANCEL and greeting: a small deterministic detector in `services/decision_engine.py`, reviewed case-by-case, at ~0 ms and 0 new dependencies. A semantic-embedding router remains an option ONLY if a future requirement genuinely needs open-vocabulary intent understanding that regex cannot express, and only with a local (no-API) encoder to remove the latency and the API round-trip — which is a separate, larger evaluation (Torch / local transformer models) explicitly out of scope here.

## Machine-readable results

Per-example gold/pred for every config is in `tests/customer_uat/semantic_library_benchmark.json` → `per_example`.

**PRODUCTION CODE CHANGED: NO · PRODUCTION DEPENDENCIES CHANGED: NO · DEPLOYED: NO**

_Generated by tests/customer_uat/semantic_library_benchmark.py — experiment only, no production integration._