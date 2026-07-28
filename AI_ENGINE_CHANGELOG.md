# AI Engine Changelog

## v1.0.1 — Production Baseline (2026-07-17)

Official current baseline — supersedes v1.0.0. Promoted from Production Validation `9fbf0d50-756c-4434-8fe1-5bda45db41e1`. Release-management/bugfix release only — **no AI behavior changes, no redesign of any component.**

**Changes in this release:**
- **Grounding metric fix** — `services/benchmark_metrics.py::evaluate_grounding()` gained a dedicated correct-abstention branch: a case expecting `expected_answerability=no_information` whose answer is a genuine abstention now scores `supported` instead of `unsupported`. Fixes a benchmark-scoring false negative, not an AI behavior change.
- **Citation attribution fix** — `rag/evidence_classifier.py::select_citation_sources()` gained a cross-script fallback (scoped strictly to genuine Thai/non-Thai script mismatches) so a correctly-retrieved, correctly-used chunk is no longer left uncited purely because the question and the knowledge base are in different languages.
- **Production Validation Center** (new) — `services/validation_service.py` + `/admin/ai/validation` + `POST /admin/api/validation/run`: one-click orchestration of Query Understanding → Retrieval Only → Conversation Scenario → Full RAG → Compare vs. Baseline → Regression Analysis → Report. Pure orchestration over the existing benchmark framework — no duplicated logic.
- **Benchmark dataset refresh** — stale expected_file/expected_answer values in the "Initial Real-Data Benchmark" dataset (31 cases) corrected against the current knowledge base (`tools/refresh_benchmark_dataset_v1.py`); genuinely-unanswerable cases converted to honest negative controls rather than left pointing at deleted files.
- **Prompt Studio cleanup** — the active default template was renamed from "Test Customer Prompt QA" to "Production Default Prompt (v1.0)" (content unchanged, name only).

**Result**: Grounding score **54.8% → 100%**; Critical Facts unchanged at 100%. Zero new regressions — the one remaining regressed case ("What countries does Shipify operate between?") is a pre-existing, already-documented dataset/retrieval interaction unrelated to this release, marked **Accepted Known Issue**.

**Frozen as of this release:** see `baseline_v1.0.1.json` and `AI_ENGINE_VERSION.md`. **AI Core development is now closed** — see the "AI Core Freeze" section in `AI_ENGINE_VERSION.md`. Next phase: LINE OA Integration (see `AI_RELEASE_SUMMARY.md`).

---

## v1.0.0 — Production Baseline (2026-07-17)

Initial frozen release of the AI Engine before LINE OA integration. This release consolidates all AI Core development completed to date; no new features were added as part of this freeze itself.

**Included in this baseline:**
- Conversation Resolver 2.0 — follow-up detection, entity tracking (topic/location/transport/attribute), negation-aware exclusion, entity replacement.
- Conversation State Engine (Conversation Intelligence Phase 1) — normalized topic buckets, subtopic, transition detection (new/same/switch), conversation confidence.
- Canonical Query Rewrite.
- Unified Intent Classification (broad + actionable intent).
- Hybrid Retrieval — vector + keyword + heading + purpose-aware scoring, Knowledge Synonym Engine expansion, FAQ exact-match, negation-aware keyword scoring, excluded-term down-ranking, adaptive filtering.
- Answer Planner, Attachment Planner, Message Segmenter (multi-bubble replies).
- Prompt Builder / Prompt Studio, Policy Engine / AI Policies.
- AI Evaluation Framework — Retrieval Only / Full RAG / Query Understanding Only / Conversation Scenario benchmark modes, deterministic Query Understanding / Critical Fact / Grounding metrics, run comparison.

**Frozen as of this release:**
- Configuration snapshot: see `baseline_v1.0.json` and `AI_ENGINE_VERSION.md`.
- 4 benchmark runs recorded against the "Initial Real-Data Benchmark" dataset (31 cases) as Production Baseline v1.0 — run IDs in `baseline_v1.0.json`.

**Known limitations carried forward (documented, not fixed):** stale benchmark dataset relative to the live knowledge base, no dataset versioning/locking, no formal baseline DB table, no confidence calibration, no LLM-as-Judge, Synonym Engine DB table missing (JSON fallback active), Knowledge Graph retrieval boost disabled, active default prompt template named "Test Customer Prompt QA" (needs human confirmation). Full detail in `AI_ENGINE_VERSION.md`.

This marks the end of AI Core development before LINE OA integration work begins.
