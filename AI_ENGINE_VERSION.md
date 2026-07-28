# AI Engine — Production v1.0.1

**Current official baseline.** Supersedes v1.0.0 below (kept for history). This document is a **freeze/audit record** — it describes the current production state; it does not change any behavior.

## v1.0.1 Release Summary

- **Release date**: 2026-07-17
- **Promoted from Production Validation**: `9fbf0d50-756c-4434-8fe1-5bda45db41e1`
- **Run IDs**: Query Understanding `3653cf3b-89d1-4b94-844c-be61388ba8a4` · Retrieval Only `211202ed-374a-465a-ab29-1aea242cae6e` · Conversation Scenario `cfab6680-21e1-4985-955f-a9e03d90eeb3` · Full RAG `9559ecb9-e6d3-4855-ada2-19772bb30207`
- **Scorecard**: Conversation 93.5% · Retrieval 93.5% · **Grounding 100%** (was 54.8% in v1.0.0) · Critical Facts 100%
- **What changed since v1.0.0**: `evaluate_grounding()` correct-abstention branch (services/benchmark_metrics.py), cross-script citation attribution fallback (rag/evidence_classifier.py), and the new Production Validation Center (services/validation_service.py) — see AI_ENGINE_CHANGELOG.md for full detail. **No AI behavior changed** — both fixes are benchmark-scoring/citation-attribution corrections only.
- **Known Accepted Issue carried forward**: "What countries does Shipify operate between?" (RETRIEVAL_ONLY) — pre-existing, unrelated to this release's fixes, does not block promotion. Full machine-readable snapshot: [baseline_v1.0.1.json](baseline_v1.0.1.json).
- **AI Core status**: **FROZEN** as of this release — see "AI Core Freeze" section below.

---

# AI Engine — Production v1.0.0 (superseded, kept for history)

Frozen baseline before LINE OA integration. This document is a **freeze/audit record** — it describes the current production state; it does not change any behavior.

## Architecture

```
User Question
   │
   ▼
Spell Correction (rag/spell_correction.py)
   │
   ▼
Conversation Resolver 2.0 (rag/query_resolution.py)
   — follow-up detection, entity tracking, negation/exclusion, replacement
   │
   ▼
Conversation State Engine (rag/conversation_state.py) — Phase 1
   — topic bucket, subtopic, transition (new/same/switch), confidence
   │
   ▼
Canonical Query Rewrite (rag/canonical_query.py)
   │
   ▼
Unified Intent Classification (rag/query_understanding.py)
   — broad_intent + actionable_intent + requested_attributes
   │
   ▼
Hybrid Retrieval (rag/searcher.py + rag/hybrid_scoring.py)
   — Knowledge Synonym Engine expansion, FAQ exact-match, vector +
     keyword + heading + purpose-aware scoring, adaptive filtering,
     negation-aware keyword scoring, excluded-term down-ranking
   │
   ▼
Context Builder (rag/context_builder.py) — dedup/merge/compress
   │
   ▼
Answer Planner (services/answer_planner.py)
   — fact-label selection, deterministic clarification detection
   │
   ▼
Prompt Builder (services/prompt_builder.py) — Prompt Studio template
   │
   ▼
LLM (services/llm_service.py)
   │
   ▼
Policy Engine (services/policy_engine.py) — escalation/business rules
   │
   ▼
Attachment Planner (services/attachment_planner.py)
   │
   ▼
Message Segmenter (services/message_segmenter.py) — multi-bubble replies
   │
   ▼
Final Answer (+ citations, attachments, message_parts)
```

AI Evaluation (`services/benchmark_service.py`, `services/benchmark_metrics.py`, `/admin/ai/benchmark`) sits alongside this pipeline as an offline harness — it calls the exact same production functions (never a second implementation) to measure quality without being part of the live request path.

## Component Versions

| Component | Version / Identity | Status |
|---|---|---|
| Retrieval Engine (Hybrid Search) | `rag/hybrid_scoring.py` + `rag/searcher.py` — purpose-aware boost, negation-aware keyword scoring, excluded-term down-ranking, adaptive filtering | Implemented |
| Conversation Resolver | Conversation Resolver 2.0 (`rag/query_resolution.py`) | Implemented |
| Conversation State | Conversation Intelligence Phase 1 v1.0 (`rag/conversation_state.py`) | Implemented |
| Query Understanding | Unified Intent Classification (`rag/query_understanding.py`) | Implemented |
| Canonical Rewrite | `rag/canonical_query.py` | Implemented |
| Synonym Engine | Knowledge Synonym Engine (`rag/synonym_service.py`) | Implemented, **degraded**: live DB table `public.synonym_groups` is missing from schema cache — silently falls back to its JSON file (confirmed in server logs every run) |
| FAQ Matcher | `is_faq_exact` near-exact matching (`rag/searcher.py` / `rag/evidence_classifier.py`) | Implemented |
| Hybrid Search | `rag/hybrid_scoring.py::apply_hybrid_ranking()` | Implemented |
| Answer Planner | `services/answer_planner.py` v1.0 | Implemented |
| Attachment Planner | `services/attachment_planner.py` v1.0 | Implemented |
| Prompt Builder | `services/prompt_builder.py` + Prompt Studio (DB templates) | Implemented — active default template renamed to **"Production Default Prompt (v1.0)"** (id `55956fa1-...`, v1) during Production Cleanup v1.0; content unchanged, only the test-sounding name was fixed |
| Policy Engine | `services/policy_engine.py` + Policy Studio — active set "Standard Policy" | Implemented |
| Message Segmenter | `services/message_segmenter.py` | Implemented |
| AI Evaluation | `services/benchmark_service.py` + `services/benchmark_metrics.py` — Phase 2 (Query Understanding / Retrieval Only / Full RAG / Conversation Scenario modes) | Implemented |
| Knowledge Graph search boost | `rag/hybrid_scoring.py` graph_weight | **Disabled** (`graph_search_enabled: false` in active retrieval settings) |
| LLM-as-Judge | — | **Not implemented** (out of scope by design — deterministic-only evaluation) |
| Confidence Calibration | — | **Not implemented** |
| Dataset versioning/locking | — | **Not implemented** |
| Tools menu (Excel Engine / Attachment Manager / Storage) | `admin/sidebar_config.py` | **Hidden** — placeholders, `comingSoon`, unrelated to the AI reasoning pipeline |

Nothing above was modified as part of this freeze — this table is a read-only audit of the state already in production.

## Configuration Snapshot (frozen with this baseline)

See [baseline_v1.0.json](baseline_v1.0.json) for the full machine-readable snapshot. Summary:

- **Embedding**: OpenAI `text-embedding-3-large`, 3072 dimensions
- **Retriever**: hybrid strategy, candidate_top_k=12, final_context_top_k=5, weights semantic/keyword/heading/graph = 0.5/0.2/0.2/0.1, reranker=`heuristic`, graph search **disabled**
- **Conversation**: Resolver 2.0, Conversation State Phase 1 v1.0
- **Prompt**: template id `55956fa1-8ff4-4c0f-a5fd-3ab55bde3a03`, version 1, name "Test Customer Prompt QA"
- **Policies**: policy set id `0f924254-3e3c-462c-a577-0d6541fcd5a3` ("Standard Policy")
- **Planner**: Answer Planner v1.0, Attachment Planner v1.0
- **Evaluation**: AI Evaluation Phase 2 (migration `025_rag_benchmark_phase2.sql`)
- **Application**: chat model `gpt-4o`; git commit not tracked (no `.git` repository in this working directory); highest applied migration `025_rag_benchmark_phase2.sql`

## Benchmark Summary (Production Baseline v1.0)

Dataset: **Initial Real-Data Benchmark** (31 cases, pre-existing seed from `tools/seed_benchmark_dataset.py` — no new dataset created for this freeze).

| Mode | Run ID | Total | Passed | Failed | Notes |
|---|---|---|---|---|---|
| Query Understanding | `9e24ea25-e213-45a4-a2bf-6e4b80c617fb` | 31 | 31 | 0 | Dataset predates Phase 2 fields — every field check is a vacuous pass; confirms zero retrieval/LLM calls |
| Retrieval Only | `f75a1160-8a58-42c7-a117-1387178fc566` | 31 | 6 | 25 | See root cause below |
| Full RAG | `f603a04f-7285-4611-8bcd-46adac1b341a` | 31 | 0 | 31 | Cost: $0.1399. Failure breakdown: retrieval_miss ×25, partial_answer ×3, language_failure ×2, wrong_evidence ×1 |
| Conversation Scenario | `be72d2db-f0e7-4b38-9664-204ef1409d1a` | 31 | 4 | 27 | Cost: $0.1423. Dataset has no scenario_key groupings — ran as 31 independent 1-turn scenarios (functionally identical to Full RAG here) |

**Root cause of the low Retrieval/Full RAG pass rate**: the seed dataset's expected values were written against `knowledge/company-profile-test.md`, `graph-test.md`, `requirements (1).txt`, `flexible-attachment-test.xlsx`, `preview-test.xlsx`, `full-auto-test.md`. The knowledge base actually live in Supabase today is driven by a different source ("AI Knowledge Master.xlsx" — warehouse/shipping/payment FAQ rows), confirmed during live testing in the AI Evaluation Phase 0/2 sessions. **This is reported exactly as measured — no result was adjusted, hidden, or reinterpreted to look better.** It means the seed dataset needs to be refreshed against the current knowledge base before it's useful as a day-to-day regression signal; it does not indicate a broken pipeline (see Known Limitations).

## Known Limitations

(Documented only — none of these were fixed in this freeze, per instruction.)

1. **Stale benchmark dataset** — see Benchmark Summary root cause above; the seeded dataset's expected files no longer match the live knowledge base content.
2. **Dataset versioning/locking not implemented** — editing a dataset after a baseline is promoted can silently change what "the baseline" means; no lock/version field exists yet.
3. **No formal baseline table** — "promotion to baseline" in this project is currently a naming/documentation convention (run_name prefix + this file + `baseline_v1.0.json`), not a dedicated DB row with `approved_by`/`locked` fields.
4. **Confidence calibration not implemented** — no calibration-bucket analysis (Part 10 of the AI Evaluation spec) exists yet.
5. **LLM-as-Judge not implemented** — by design, per every phase's "no additional LLM calls, no LLM judge" instruction; all scoring is deterministic.
6. **Synonym Engine degraded** — `public.synonym_groups` table is missing from the Supabase schema cache; the engine silently falls back to its bundled JSON glossary every request (confirmed in logs on every run in this session). Functionally fine (fallback works), but the DB-backed path has never actually been exercised in this environment.
7. **Active default prompt template is named "Test Customer Prompt QA"** — worth a human review in Prompt Studio before LINE OA goes live, to confirm this is intentionally the production template and not a leftover test artifact.
8. **Knowledge Graph retrieval boost is disabled** (`graph_search_enabled: false`) in the active retrieval settings — graph-based evidence never contributes to ranking in the current configuration.
9. **Conversation Scenario mode has not yet been exercised against a real multi-turn scenario dataset** in this baseline — the seed dataset has no `scenario_key` groupings, so this run degenerated to 31 independent single-turn calls.

## Deployment Notes

- No code, schema, or configuration was changed as part of this freeze — this is a read/document-only exercise plus running the existing benchmark against the existing dataset.
- Before LINE OA integration, address Known Limitations #1 (refresh/replace the benchmark dataset to match the live KB) and #7 (confirm the production prompt template) — both are pre-existing gaps, surfaced but not fixed here per instruction.
- Every future benchmark run should be compared against the 4 run IDs recorded in `baseline_v1.0.json` via `/admin/api/benchmark/compare` to catch regressions in retrieval, conversation intelligence, or answer quality.

---

## AI Core Freeze Policy (effective with v1.0.1)

As of Production Baseline v1.0.1, **AI Core development is closed**. The following modules are **Feature Frozen** — this is a governance status, not a total prohibition; see the Allowed/Requires New Release distinction below.

**Feature Frozen modules:**

- Retrieval (`rag/searcher.py`, `rag/hybrid_scoring.py`)
- Conversation Engine (`rag/query_resolution.py`, `rag/conversation_state.py`, `rag/canonical_query.py`, `rag/query_understanding.py`)
- Prompt Builder / Prompt Studio (`services/prompt_builder.py`, `services/prompt_studio_service.py`)
- AI Policies (`services/policy_engine.py`, `services/policy_studio_service.py`)
- Benchmark Engine (`services/benchmark_service.py`, `services/benchmark_metrics.py`)
- Production Validation (`services/validation_service.py`)
- Grounding (`services/benchmark_metrics.py::evaluate_grounding()`, `rag/evidence_classifier.py`)
- Evaluation framework as a whole (`/admin/ai/benchmark`, `/admin/ai/validation`)

### Allowed without a new release approval

- Bug Fix (a confirmed implementation defect, not a behavior redesign — e.g. the v1.0.1 Grounding/Citation fixes)
- Security Fix
- Performance Improvement (no output/behavior change)
- Documentation Update
- Test Improvement (new/expanded test coverage, no production code change)

### Requires a new, separately-approved release

- AI Logic Changes
- Prompt Changes
- Retrieval Changes
- Conversation Changes
- Policy Changes
- Benchmark Changes
- Grounding Logic Changes

Any change in the "Requires New Release" list must go through its own release-approval document (see `AI_RELEASE_APPROVAL.md` as the template) before being merged — it cannot ride along inside an unrelated fix or a LINE OA integration change.

The next development phase is **LINE OA Integration** (see `AI_RELEASE_SUMMARY.md`) — new work happens in the messaging/channel layer, not inside the Feature Frozen modules above.
