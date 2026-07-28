# Release Notes — AI Engine v1.0.1

**Release date**: 2026-07-17 · **Validation ID**: `9fbf0d50-756c-4434-8fe1-5bda45db41e1`

## New Features

- **Production Validation Center** (`services/validation_service.py`, `/admin/ai/validation`, `POST /admin/api/validation/run`) — one-click orchestration of the full evaluation pipeline: Query Understanding → Retrieval Only → Conversation Scenario → Full RAG → Compare vs. Baseline → Regression Analysis → Report, with Markdown/JSON/HTML export.

## Improvements

- Benchmark dataset ("Initial Real-Data Benchmark", 31 cases) refreshed against the current knowledge base — stale expected_file/expected_answer values corrected or converted to honest negative controls (`tools/refresh_benchmark_dataset_v1.py`).
- Prompt Studio default template renamed from "Test Customer Prompt QA" to "Production Default Prompt (v1.0)" (content unchanged — naming clarity only).

## Bug Fixes

### Grounding Fix

`services/benchmark_metrics.py::evaluate_grounding()` — added a dedicated correct-abstention branch. Previously, any case expecting `expected_answerability=no_information` whose answer correctly declined to invent information was scored `unsupported` purely because it had nothing to cite. Now scored `supported`. Result: Grounding score **54.8% → 100%**.

### Citation Attribution Fix

`rag/evidence_classifier.py::select_citation_sources()` — added a scoped cross-script fallback. A question and its correctly-retrieved knowledge-base chunk in different scripts (e.g. an English question against Thai-only content) could have zero literal token overlap, leaving a genuinely-used chunk uncited. Fixed with a fallback that cites the best-supported chunk when an answer was actually generated from it — scoped strictly to genuine script mismatches to avoid over-crediting unrelated same-script substrings (e.g. an entity name inside an email domain).

**Both fixes are benchmark-scoring/citation-attribution corrections only — no AI behavior, retrieval ranking, prompt, or policy logic was changed.**

## Known Accepted Issues

- "What countries does Shipify operate between?" (Retrieval Only mode) — pre-existing dataset/retrieval interaction, unrelated to this release. See `KNOWN_ISSUES.md`.

## Breaking Changes

(None)

## Migration

(None) — no new database migration in this release; highest applied migration remains `migrations/025_rag_benchmark_phase2.sql`.

## Deployment Notes

- No code path affecting live LINE OA behavior changes with this release — all changes are in the evaluation/benchmark layer.
- Follow `DEPLOYMENT_CHECKLIST.md` before promoting to any new environment.
- Recommended Git tag: `ai-engine-v1.0.1` (see `GIT_RELEASE.md`).
