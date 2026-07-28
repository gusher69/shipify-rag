"""RAG Benchmark & Evaluation orchestration — evaluation infrastructure
ONLY. Never mutates embeddings, retrieval, chunking, or prompt-generation
behavior; it only CALLS the existing pipeline (RAGService, prompt_builder,
llm_service) exactly the way AI Playground already does, and records the
results for offline comparison.

Execution model deliberately mirrors admin/routes.py's existing sync-job
pattern (in-memory progress dict + threading.Thread + DB status column)
rather than introducing a new job-queue dependency.
"""
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import OPENAI_CHAT_MODEL
from services.embedding_service import get_embedding_provider
from services.rag_service import get_rag_service
from services.policy_engine import evaluate as evaluate_policies
from services.prompt_builder import build_prompt, DEFAULT_TEMPLATE_ID
from services.llm_service import get_llm_service, estimate_cost_usd
from rag.evidence_classifier import classify_evidence, select_citation_sources
from rag.confidence import compute_confidence
from services import benchmark_metrics as metrics

RETRIEVAL_ONLY = "RETRIEVAL_ONLY"
FULL_RAG = "FULL_RAG"
# Phase 2 — Conversation Intelligence evaluation. Both are pure Python,
# zero retrieval/LLM calls for QUERY_UNDERSTANDING; CONVERSATION_SCENARIO
# calls the real Full RAG pipeline per turn (needed to evaluate
# retrieval/answer/attachments across a real multi-turn session), same
# as FULL_RAG mode already does for a single question.
QUERY_UNDERSTANDING = "QUERY_UNDERSTANDING"
CONVERSATION_SCENARIO = "CONVERSATION_SCENARIO"
RUN_MODES = (RETRIEVAL_ONLY, FULL_RAG, QUERY_UNDERSTANDING, CONVERSATION_SCENARIO)

# Approx per-1K-token cost used ONLY for the pre-run cost estimate shown
# to the admin before a FULL_RAG run — the run's actual recorded cost
# always comes from services.llm_service.estimate_cost_usd on real
# token counts, never this constant.
_ESTIMATED_TOKENS_PER_CASE = 700  # rough input+output budget for a single Q&A turn


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ── In-memory run progress (mirrors admin/routes.py's `_sync` pattern) ──
_RUN_STATE: Dict[str, Dict] = {}
_RUN_LOCK = threading.Lock()


def get_run_progress(run_id: str) -> Optional[Dict]:
    with _RUN_LOCK:
        st = _RUN_STATE.get(run_id)
        return dict(st) if st else None


def cancel_run(run_id: str) -> bool:
    with _RUN_LOCK:
        if run_id in _RUN_STATE:
            _RUN_STATE[run_id]["cancelled"] = True
            return True
    return False


def estimate_full_rag_cost(case_count: int, llm_model: str = OPENAI_CHAT_MODEL, grader_enabled: bool = False) -> Dict:
    """Pre-run cost estimate shown to the admin before FULL_RAG starts
    (Part 12: cost safety) — a rough upper bound, not a guarantee."""
    calls = case_count * (2 if grader_enabled else 1)
    input_tokens = case_count * int(_ESTIMATED_TOKENS_PER_CASE * 0.7)
    output_tokens = case_count * int(_ESTIMATED_TOKENS_PER_CASE * 0.3)
    est_cost = estimate_cost_usd(llm_model, input_tokens, output_tokens) * (2 if grader_enabled else 1)
    return {
        "case_count": case_count,
        "estimated_llm_calls": case_count,
        "estimated_grader_calls": case_count if grader_enabled else 0,
        "estimated_max_cost_usd": round(est_cost, 4),
        "note": "Rough upper-bound estimate based on average token usage — actual cost is recorded per-run from real usage.",
    }


def run_retrieval_only_case(case: Dict, *, top_k: int = 3) -> Dict:
    """Question -> query expansion -> embedding -> retrieval -> hybrid
    ranking -> metrics. NEVER calls the LLM."""
    rag = get_rag_service()
    t0 = time.time()
    try:
        chunks = rag.retrieve(case["question"], top_k=top_k)
        error = None
    except Exception as e:
        chunks, error = [], str(e)
    latency_ms = (time.time() - t0) * 1000

    retrieved_files = [c.get("file_name") for c in chunks]
    retrieved_sections = [c.get("section_title") for c in chunks]

    r1 = metrics.recall_at_k(retrieved_files, case.get("expected_file"), 1)
    r3 = metrics.recall_at_k(retrieved_files, case.get("expected_file"), 3)
    r5 = metrics.recall_at_k(retrieved_files, case.get("expected_file"), 5)
    mrr = metrics.reciprocal_rank(retrieved_files, case.get("expected_file"))
    section_hit = metrics.expected_section_hit(retrieved_sections, case.get("expected_section"), top_k)
    prohibited_hits = metrics.prohibited_file_hit(retrieved_files, case.get("prohibited_files") or [], top_k)

    retrieval_metrics = {
        "recall_at_1": r1, "recall_at_3": r3, "recall_at_5": r5, "mrr": mrr,
        "precision_at_k": metrics.precision_at_k(retrieved_files, case.get("expected_file"), top_k),
        "expected_section_hit": section_hit,
        "prohibited_file_hits": prohibited_hits,
    }

    failure_type = None
    if error:
        failure_type = "system_error"
    elif not r5:
        failure_type = "retrieval_miss"
    elif not r1:
        failure_type = "ranking_failure"
    elif prohibited_hits:
        failure_type = "wrong_evidence"
    elif not section_hit:
        failure_type = "wrong_evidence"

    return {
        "actual_answer": None,
        "retrieved_chunks": [{"file_name": c.get("file_name"), "section_title": c.get("section_title"),
                               "score": c.get("score"), "hybrid_score": c.get("hybrid_score"),
                               "classification": c.get("classification")} for c in chunks],
        "selected_evidence": [], "citations": [],
        "prompt_snapshot": {},
        "answerability": None,
        "retrieval_metrics": retrieval_metrics,
        "answer_metrics": {},
        "latency_ms": round(latency_ms, 2),
        "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0.0,
        "status": "fail" if failure_type else "pass",
        "failure_type": failure_type,
        "failure_reason": error or (f"prohibited file(s) retrieved: {prohibited_hits}" if prohibited_hits else
                                     ("expected section not in top-k" if not section_hit else None)),
    }


def run_query_understanding_case(case: Dict) -> Dict:
    """Question -> Spell Correction -> Conversation Resolver ->
    Conversation State -> Canonical Rewrite -> Unified Intent
    Classification. NEVER calls retrieval, the prompt builder, or the
    LLM — mirrors exactly the pre-retrieval steps of
    services/playground_orchestrator.py::run_playground_turn (steps -1
    through 0c), calling the SAME modules rather than reimplementing
    any of them.

    `case["previous_messages"]`, if given, simulates conversation
    history for testing a follow-up in isolation (outside a full
    Conversation Scenario run) — see migrations/025_rag_benchmark_phase2.sql."""
    from rag.query_expansion import normalize_query
    from rag.spell_correction import correct_query
    from rag.query_resolution import resolve_conversation, extract_entities
    from rag.conversation_state import build_conversation_state, subtopic_for
    from rag.canonical_query import rewrite_canonical_query
    from rag.query_understanding import classify_actionable_intent

    question = case["question"]
    history = case.get("previous_messages") or []
    t0 = time.time()
    error = None
    resolved_question = canonical_question = None
    conv_state: Dict = {}
    intent_result: Dict = {"actionable_intent": None}
    conversation: Dict = {"excluded_entities": {"location": [], "transport": []}, "followup_type": None}
    try:
        normalized = normalize_query(question)
        corrected = correct_query(normalized)["corrected_query"]
        conversation = resolve_conversation(corrected, history)
        resolved_question = conversation["resolved_question"]
        conv_state = build_conversation_state(corrected, history)

        canonical_entities = {**conversation.get("entities_carried", {})}
        if conversation.get("prev_topic") and "topic" not in canonical_entities:
            canonical_entities["topic"] = conversation["prev_topic"]
        if conv_state.get("transition") == "switch_topic":
            for k in ("location", "transport", "topic"):
                canonical_entities.pop(k, None)

        canonical_result = rewrite_canonical_query(resolved_question, entities=canonical_entities)
        canonical_question = canonical_result["canonical_query"]

        current_entities = extract_entities(canonical_question)
        merged_entities = dict(canonical_entities)
        for k, v in current_entities.items():
            if v:
                merged_entities[k] = v
        intent_result = classify_actionable_intent(canonical_question, entities=merged_entities)
        conv_state["intent"] = intent_result["actionable_intent"]
        conv_state["subtopic"] = subtopic_for(intent_result["actionable_intent"], merged_entities.get("attribute"))
    except Exception as e:
        error = str(e)
    latency_ms = (time.time() - t0) * 1000

    actual_entities = {"location": conv_state.get("location"), "transport": conv_state.get("transport")}
    qu_metrics = {
        "topic": metrics.field_match(case.get("expected_topic"), conv_state.get("topic")),
        "subtopic": metrics.field_match(case.get("expected_subtopic"), conv_state.get("subtopic")),
        "intent": metrics.field_match(case.get("expected_intent"), conv_state.get("intent")),
        "entities": metrics.entity_accuracy(case.get("expected_entities"), actual_entities),
        "excluded_entities": metrics.excluded_entity_accuracy(case.get("expected_excluded_entities"),
                                                               conversation.get("excluded_entities")),
        "canonical_query": metrics.field_match(case.get("expected_canonical_query"), canonical_question),
        "resolved_query": metrics.field_match(case.get("expected_resolved_query"), resolved_question),
        "transition": metrics.field_match(case.get("expected_transition"), conv_state.get("transition")),
        "conversation_state": metrics.conversation_state_accuracy(case.get("expected_conversation_state"), conv_state),
    }
    failure_type = "system_error" if error else metrics.classify_query_understanding_failure(qu_metrics)
    failed_fields = [k for k, v in qu_metrics.items() if not (v.get("pass") if "pass" in v else v.get("exact_match"))]

    return {
        "actual_answer": None, "retrieved_chunks": [], "selected_evidence": [], "citations": [],
        "prompt_snapshot": {}, "answerability": None, "retrieval_metrics": {}, "answer_metrics": {},
        "query_understanding_metrics": qu_metrics, "conversation_metrics": {},
        "critical_fact_metrics": {}, "grounding_metrics": {},
        "latency_ms": round(latency_ms, 2), "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0.0,
        "status": "fail" if failure_type else "pass", "failure_type": failure_type,
        "failure_reason": error or (f"mismatched field(s): {', '.join(failed_fields)}" if failed_fields else None),
    }


def run_full_rag_case(case: Dict, *, top_k: int = 3, temperature: float = 0.3, max_tokens: int = 500) -> Dict:
    """Question -> retrieval -> prompt builder -> LLM -> citations ->
    evaluation. Calls the real LLM — costs real money, used only when
    the admin explicitly runs FULL_RAG mode."""
    rag = get_rag_service()
    question = case["question"]

    t0 = time.time()
    try:
        chunks = rag.retrieve(question, top_k=top_k)
    except Exception as e:
        return {
            "actual_answer": None, "retrieved_chunks": [], "selected_evidence": [], "citations": [],
            "prompt_snapshot": {}, "answerability": None, "retrieval_metrics": {}, "answer_metrics": {},
            "latency_ms": round((time.time() - t0) * 1000, 2), "input_tokens": 0, "output_tokens": 0,
            "estimated_cost": 0.0, "status": "error", "failure_type": "system_error",
            "failure_reason": f"retrieval failed: {e}",
        }

    from rag.query_expansion import expand_query
    classify_evidence(question, chunks, query_variants=expand_query(question))
    conf_result = compute_confidence(chunks)

    context = rag.build_context(chunks)
    policy = evaluate_policies(question)
    built_prompt = build_prompt(question, context, template_id=None, policy_notes=policy.notes)

    try:
        llm = get_llm_service()
        llm_response = llm.generate(built_prompt.messages, model=OPENAI_CHAT_MODEL,
                                     temperature=temperature, max_tokens=max_tokens)
        answer_text = llm_response.text
        input_tokens, output_tokens = llm_response.input_tokens, llm_response.output_tokens
    except Exception as e:
        latency_ms = (time.time() - t0) * 1000
        return {
            "actual_answer": None, "retrieved_chunks": [], "selected_evidence": [], "citations": [],
            "prompt_snapshot": {"final_prompt": built_prompt.final_prompt_text}, "answerability": None,
            "retrieval_metrics": {}, "answer_metrics": {}, "latency_ms": round(latency_ms, 2),
            "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0.0,
            "status": "error", "failure_type": "system_error", "failure_reason": f"LLM call failed: {e}",
        }

    latency_ms = (time.time() - t0) * 1000
    cost = estimate_cost_usd(OPENAI_CHAT_MODEL, input_tokens, output_tokens)

    citation_sources = select_citation_sources(question, answer_text, chunks)
    cited_files = [c.get("file_name") for c in citation_sources]

    retrieved_files = [c.get("file_name") for c in chunks]
    r1 = metrics.recall_at_k(retrieved_files, case.get("expected_file"), 1)
    r5 = metrics.recall_at_k(retrieved_files, case.get("expected_file"), 5)
    must_inc = metrics.must_include_check(answer_text, case.get("must_include") or [])
    must_not = metrics.must_not_include_check(answer_text, case.get("must_not_include") or [])
    citation_ok = metrics.citation_correct(cited_files, case.get("expected_file"))
    language_ok = metrics.language_correct(answer_text, case.get("language"))
    answerability_ok = metrics.answerability_correct(conf_result.answerability, case.get("expected_answerability"))

    failure_type = metrics.classify_failure(
        retrieval_r1=r1, retrieval_r5=r5, must_include_ok=must_inc["pass"],
        must_not_include_ok=must_not["pass"], citation_ok=citation_ok, language_ok=language_ok,
        answerability_ok=answerability_ok, system_error=False,
    )

    answer_metrics = {
        "must_include": must_inc, "must_not_include": must_not,
        "citation_correct": citation_ok, "language_correct": language_ok,
        "detected_language": metrics.detect_answer_language(answer_text),
        "answerability_correct": answerability_ok,
    }

    return {
        "actual_answer": answer_text,
        "retrieved_chunks": [{"file_name": c.get("file_name"), "section_title": c.get("section_title"),
                               "score": c.get("score"), "hybrid_score": c.get("hybrid_score"),
                               "classification": c.get("classification"),
                               "evidence_classification": c.get("evidence_classification")} for c in chunks],
        "selected_evidence": [{"file_name": c.get("file_name"), "section_title": c.get("section_title")}
                               for c in chunks if c.get("evidence_classification") in ("DIRECT_EVIDENCE", "PARTIAL_EVIDENCE")],
        "citations": [{"file_name": c.get("file_name"), "section_title": c.get("section_title")} for c in citation_sources],
        "prompt_snapshot": {
            "final_prompt": built_prompt.final_prompt_text,
            "template_id": built_prompt.template.id, "template_version": built_prompt.template.version,
            "policies_applied": [v.name for v in policy.verdicts if v.status == "triggered"],
        },
        "answerability": conf_result.answerability,
        "retrieval_metrics": {"recall_at_1": r1, "recall_at_5": r5,
                               "mrr": metrics.reciprocal_rank(retrieved_files, case.get("expected_file"))},
        "answer_metrics": answer_metrics,
        "latency_ms": round(latency_ms, 2),
        "input_tokens": input_tokens, "output_tokens": output_tokens, "estimated_cost": cost,
        "status": "fail" if failure_type else "pass",
        "failure_type": failure_type,
        "failure_reason": (", ".join(must_inc["missing"]) and f"missing required content: {must_inc['missing']}") or
                           (must_not["violations"] and f"contains prohibited content: {must_not['violations']}") or
                           (not citation_ok and "citation does not match expected file") or
                           (not language_ok and f"expected {case.get('language')}, got {answer_metrics['detected_language']}") or
                           None,
    }


def run_conversation_scenario(cases: List[Dict], *, top_k: int = 3) -> List[Dict]:
    """Runs every case sharing a scenario_key SEQUENTIALLY through the
    real end-to-end pipeline (services.playground_orchestrator.
    run_playground_turn — the exact same function AI Playground calls,
    never a second copy of retrieval/prompt/LLM/conversation-state
    logic), accumulating conversation history turn-by-turn exactly like
    a real multi-turn chat. Part 2's own rule: "do not evaluate turns
    independently" — `history` is real accumulated state, not simulated
    per-turn.

    `cases` must already be pre-filtered to one scenario_key; ordering
    is by turn_index."""
    from services.playground_orchestrator import run_playground_turn

    ordered = sorted(cases, key=lambda c: c.get("turn_index") if c.get("turn_index") is not None else 0)
    history: List[Dict] = []
    turn_results: List[Dict] = []

    for case in ordered:
        question = case["question"]
        t0 = time.time()
        try:
            result = run_playground_turn(question, top_k=top_k, history=history)
            error = None
        except Exception as e:
            result = None
            error = str(e)
        latency_ms = (time.time() - t0) * 1000

        if error:
            turn_result = {
                "case_id": case.get("id"), "turn_index": case.get("turn_index"), "question": question,
                "answer": None, "topic": None, "subtopic": None, "intent": None, "conversation_state": {},
                "expected_transition": case.get("expected_transition"), "transition": None,
                "topic_ok": False, "entity_ok": False,
                "expects_entity_carry": False, "expects_entity_replacement": False,
                "excluded_entity_check": None, "conversation_state_check": None,
                "retrieval_metrics": {}, "answer_metrics": {}, "critical_fact_metrics": {}, "grounding_metrics": {},
                "retrieved_chunks": [], "citations": [],
                "latency_ms": round(latency_ms, 2), "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0.0,
                "status": "error", "failure_type": "system_error", "failure_reason": error,
                "turn_pass": False,
            }
            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": ""})
            turn_results.append(turn_result)
            continue

        cs = result.conversation_state
        actual_entities = {"location": cs.get("location"), "transport": cs.get("transport")}
        answer_text = result.answer

        topic_row = metrics.field_match(case.get("expected_topic"), cs.get("topic"))
        entity_row = metrics.entity_accuracy(case.get("expected_entities"), actual_entities)
        excluded_check = metrics.excluded_entity_accuracy(case.get("expected_excluded_entities"),
                                                           cs.get("excluded_entities"))
        state_check = metrics.conversation_state_accuracy(case.get("expected_conversation_state"), cs)
        transition_row = metrics.field_match(case.get("expected_transition"), cs.get("transition"))

        retrieved_files = [c.get("file_name") for c in result.chunks]
        r1 = metrics.recall_at_k(retrieved_files, case.get("expected_file"), 1)
        r5 = metrics.recall_at_k(retrieved_files, case.get("expected_file"), 5)
        must_inc = metrics.must_include_check(answer_text, case.get("must_include") or [])
        must_not = metrics.must_not_include_check(answer_text, case.get("must_not_include") or [])
        critical = metrics.evaluate_critical_facts(case.get("critical_facts") or [], answer_text)
        citations = [{"file_name": c.get("file_name"), "section_title": c.get("section_title")}
                     for c in result.chunks if c.get("cited")]
        grounding = metrics.evaluate_grounding(
            answer_text=answer_text, citations=citations, retrieved_chunks=result.chunks,
            prohibited_files=case.get("prohibited_files") or [], critical_facts=case.get("critical_facts") or [],
            expected_answerability=case.get("expected_answerability"),
        )

        turn_pass = all([
            topic_row["pass"], entity_row["exact_match"], transition_row["pass"],
            excluded_check["exact_match"], state_check["exact_match"],
            must_inc["pass"], must_not["pass"], critical["pass"],
            r5 if case.get("expected_file") else True,
        ])

        turn_result = {
            "case_id": case.get("id"), "turn_index": case.get("turn_index"), "question": question,
            "answer": answer_text, "topic": cs.get("topic"), "subtopic": cs.get("subtopic"), "intent": cs.get("intent"),
            "conversation_state": cs, "expected_transition": case.get("expected_transition"),
            "transition": cs.get("transition"),
            "topic_ok": topic_row["pass"], "entity_ok": entity_row["exact_match"],
            "expects_entity_carry": bool(case.get("expected_entities")) and cs.get("transition") == "same_topic",
            "expects_entity_replacement": bool(case.get("expected_entities")) and bool(cs.get("state_changes")),
            "excluded_entity_check": excluded_check, "conversation_state_check": state_check,
            "retrieval_metrics": {"recall_at_1": r1, "recall_at_5": r5},
            "answer_metrics": {"must_include": must_inc, "must_not_include": must_not},
            "critical_fact_metrics": critical, "grounding_metrics": grounding,
            "retrieved_chunks": [{"file_name": c.get("file_name"), "section_title": c.get("section_title")}
                                  for c in result.chunks],
            "citations": citations,
            "latency_ms": round(latency_ms, 2), "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens, "estimated_cost": result.estimated_cost_usd,
            "status": "pass" if turn_pass else "fail",
            "failure_type": None if turn_pass else (metrics.classify_query_understanding_failure({
                "topic": topic_row, "entities": entity_row, "excluded_entities": excluded_check,
                "conversation_state": state_check, "transition": transition_row,
            }) or "wrong_evidence"),
            "failure_reason": None if turn_pass else "one or more turn-level checks failed — see field checks",
            "turn_pass": turn_pass,
        }
        turn_results.append(turn_result)
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer_text or ""})

    return turn_results


def _execute_conversation_scenario_run(sb, run_id: str, cases: List[Dict], *, top_k: int = 3, resume: bool = False):
    """CONVERSATION_SCENARIO's own execution path — groups `cases` by
    scenario_key and runs each group sequentially through
    run_conversation_scenario() (one real session per scenario), then
    persists one rag_benchmark_results row PER TURN (scenario_key +
    turn_index recorded on each row) so the Failure Inspector can show
    any single turn while conversation_scenario_metrics() gives the
    whole-scenario pass/fail. `total` in _RUN_STATE counts TURNS (every
    persisted result row), matching how progress is reported for every
    other mode."""
    scenarios: Dict[str, List[Dict]] = {}
    for c in cases:
        key = c.get("scenario_key") or c["id"]
        scenarios.setdefault(key, []).append(c)

    total_turns = sum(len(v) for v in scenarios.values())
    with _RUN_LOCK:
        _RUN_STATE[run_id] = {"status": "running", "completed": 0, "failed": 0, "total": total_turns,
                               "current_case": None, "cancelled": False, "started_at": time.time()}

    already_done_scenarios = set()
    if resume:
        try:
            existing = sb.table("rag_benchmark_results").select("scenario_key").eq("run_id", run_id).execute().data or []
            already_done_scenarios = {r["scenario_key"] for r in existing if r.get("scenario_key")}
        except Exception:
            pass

    latencies: List[float] = []
    total_input_tokens = total_output_tokens = 0
    total_cost = 0.0
    passed = failed = 0

    sb.table("rag_benchmark_runs").update({"status": "running", "started_at": _now_iso()}).eq("id", run_id).execute()

    for scenario_key, scenario_cases in scenarios.items():
        with _RUN_LOCK:
            if _RUN_STATE[run_id]["cancelled"]:
                break
        if scenario_key in already_done_scenarios:
            continue

        with _RUN_LOCK:
            _RUN_STATE[run_id]["current_case"] = f"scenario:{scenario_key}"

        try:
            turn_results = run_conversation_scenario(scenario_cases, top_k=top_k)
        except Exception as e:
            turn_results = [{
                "case_id": scenario_cases[0].get("id"), "turn_index": 0, "question": scenario_cases[0]["question"],
                "answer": None, "topic": None, "subtopic": None, "conversation_state": {},
                "retrieval_metrics": {}, "answer_metrics": {}, "critical_fact_metrics": {}, "grounding_metrics": {},
                "retrieved_chunks": [], "citations": [], "latency_ms": 0.0, "input_tokens": 0, "output_tokens": 0,
                "estimated_cost": 0.0, "status": "error", "failure_type": "system_error", "failure_reason": str(e),
            }]

        scenario_conv_metrics = metrics.conversation_scenario_metrics(turn_results)

        for turn in turn_results:
            latencies.append(turn["latency_ms"])
            total_input_tokens += turn.get("input_tokens", 0)
            total_output_tokens += turn.get("output_tokens", 0)
            total_cost += turn.get("estimated_cost", 0.0)
            if turn["status"] == "pass":
                passed += 1
            else:
                failed += 1
            try:
                sb.table("rag_benchmark_results").insert({
                    "run_id": run_id, "case_id": turn.get("case_id"), "question": turn["question"],
                    "actual_answer": turn.get("answer"),
                    "retrieved_chunks": turn.get("retrieved_chunks") or [], "selected_evidence": [],
                    "citations": turn.get("citations") or [], "prompt_snapshot": {},
                    "retrieval_metrics": turn.get("retrieval_metrics") or {},
                    "answer_metrics": turn.get("answer_metrics") or {},
                    "query_understanding_metrics": {
                        "topic": {"expected": None, "actual": turn.get("topic"), "pass": turn.get("topic_ok")},
                        "conversation_state": turn.get("conversation_state_check") or {},
                    },
                    "conversation_metrics": scenario_conv_metrics,
                    "critical_fact_metrics": turn.get("critical_fact_metrics") or {},
                    "grounding_metrics": turn.get("grounding_metrics") or {},
                    "latency_ms": turn["latency_ms"], "input_tokens": turn.get("input_tokens", 0),
                    "output_tokens": turn.get("output_tokens", 0), "estimated_cost": turn.get("estimated_cost", 0.0),
                    "status": turn["status"], "failure_type": turn.get("failure_type"),
                    "failure_reason": turn.get("failure_reason"),
                    "scenario_key": scenario_key, "turn_index": turn.get("turn_index"),
                }).execute()
            except Exception as e:
                print(f"[benchmark] failed to persist scenario turn result for case {turn.get('case_id')}: {e}")

            with _RUN_LOCK:
                _RUN_STATE[run_id]["completed"] += 1
                if turn["status"] != "pass":
                    _RUN_STATE[run_id]["failed"] += 1

    with _RUN_LOCK:
        cancelled = _RUN_STATE[run_id]["cancelled"]

    provider = get_embedding_provider()
    sb.table("rag_benchmark_runs").update({
        "status": "cancelled" if cancelled else "completed",
        "completed_at": _now_iso(),
        "total_cases": passed + failed, "passed_cases": passed, "failed_cases": failed,
        "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0,
        "p95_latency_ms": round(metrics.p95(latencies), 2),
        "input_tokens": total_input_tokens, "output_tokens": total_output_tokens,
        "estimated_cost": round(total_cost, 6),
        "embedding_provider": provider.provider_name, "embedding_model": provider.model_name(),
        "embedding_dimensions": provider.dimensions(),
    }).eq("id", run_id).execute()

    with _RUN_LOCK:
        _RUN_STATE[run_id]["status"] = "cancelled" if cancelled else "completed"


def execute_run(sb, run_id: str, cases: List[Dict], mode: str, *, top_k: int = 3, resume: bool = False):
    """Runs sequentially in a background thread — checks the cancellation
    flag between cases so a long FULL_RAG run can be stopped without
    losing already-completed results. `resume=True` skips cases that
    already have a result row for this run_id (Part 12: no repeated
    calls for completed cases)."""
    if mode == CONVERSATION_SCENARIO:
        return _execute_conversation_scenario_run(sb, run_id, cases, top_k=top_k, resume=resume)

    with _RUN_LOCK:
        _RUN_STATE[run_id] = {"status": "running", "completed": 0, "failed": 0, "total": len(cases),
                               "current_case": None, "cancelled": False, "started_at": time.time()}

    already_done_case_ids = set()
    if resume:
        try:
            existing = sb.table("rag_benchmark_results").select("case_id").eq("run_id", run_id).execute().data or []
            already_done_case_ids = {r["case_id"] for r in existing if r.get("case_id")}
        except Exception:
            pass

    latencies: List[float] = []
    total_input_tokens = total_output_tokens = 0
    total_cost = 0.0
    passed = failed = 0

    sb.table("rag_benchmark_runs").update({"status": "running", "started_at": _now_iso()}).eq("id", run_id).execute()

    for case in cases:
        with _RUN_LOCK:
            if _RUN_STATE[run_id]["cancelled"]:
                break
            _RUN_STATE[run_id]["current_case"] = case["question"]

        if case["id"] in already_done_case_ids:
            continue

        try:
            if mode == RETRIEVAL_ONLY:
                result = run_retrieval_only_case(case, top_k=top_k)
            elif mode == QUERY_UNDERSTANDING:
                result = run_query_understanding_case(case)
            else:
                result = run_full_rag_case(case, top_k=top_k)
        except Exception as e:
            result = {
                "actual_answer": None, "retrieved_chunks": [], "selected_evidence": [], "citations": [],
                "prompt_snapshot": {}, "answerability": None, "retrieval_metrics": {}, "answer_metrics": {},
                "query_understanding_metrics": {}, "conversation_metrics": {},
                "critical_fact_metrics": {}, "grounding_metrics": {},
                "latency_ms": 0.0, "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0.0,
                "status": "error", "failure_type": "system_error", "failure_reason": str(e),
            }

        latencies.append(result["latency_ms"])
        total_input_tokens += result["input_tokens"]
        total_output_tokens += result["output_tokens"]
        total_cost += result["estimated_cost"]
        if result["status"] == "pass":
            passed += 1
        else:
            failed += 1

        try:
            sb.table("rag_benchmark_results").insert({
                "run_id": run_id, "case_id": case["id"], "question": case["question"],
                "expected_answer": case.get("expected_answer"), "actual_answer": result["actual_answer"],
                "expected_file": case.get("expected_file"), "expected_section": case.get("expected_section"),
                "retrieved_chunks": result["retrieved_chunks"], "selected_evidence": result["selected_evidence"],
                "citations": result["citations"], "prompt_snapshot": result["prompt_snapshot"],
                "answerability": result["answerability"], "retrieval_metrics": result["retrieval_metrics"],
                "answer_metrics": result["answer_metrics"], "latency_ms": result["latency_ms"],
                "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"],
                "estimated_cost": result["estimated_cost"], "status": result["status"],
                "failure_type": result["failure_type"], "failure_reason": result["failure_reason"],
                "query_understanding_metrics": result.get("query_understanding_metrics") or {},
                "conversation_metrics": result.get("conversation_metrics") or {},
                "critical_fact_metrics": result.get("critical_fact_metrics") or {},
                "grounding_metrics": result.get("grounding_metrics") or {},
            }).execute()
        except Exception as e:
            print(f"[benchmark] failed to persist result for case {case['id']}: {e}")

        with _RUN_LOCK:
            _RUN_STATE[run_id]["completed"] += 1
            if result["status"] != "pass":
                _RUN_STATE[run_id]["failed"] += 1

    with _RUN_LOCK:
        cancelled = _RUN_STATE[run_id]["cancelled"]

    provider = get_embedding_provider()
    sb.table("rag_benchmark_runs").update({
        "status": "cancelled" if cancelled else "completed",
        "completed_at": _now_iso(),
        "total_cases": passed + failed, "passed_cases": passed, "failed_cases": failed,
        "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0,
        "p95_latency_ms": round(metrics.p95(latencies), 2),
        "input_tokens": total_input_tokens, "output_tokens": total_output_tokens,
        "estimated_cost": round(total_cost, 6),
        "embedding_provider": provider.provider_name, "embedding_model": provider.model_name(),
        "embedding_dimensions": provider.dimensions(),
    }).eq("id", run_id).execute()

    with _RUN_LOCK:
        _RUN_STATE[run_id]["status"] = "cancelled" if cancelled else "completed"


def start_run(sb, *, dataset_id: str, run_name: str, mode: str, top_k: int = 3, resume_run_id: Optional[str] = None) -> str:
    """Creates the run row (and result rows already existing if
    resuming) and launches execute_run() in a background thread. Returns
    the run_id immediately — never blocks the admin request."""
    cases = sb.table("rag_benchmark_cases").select("*").eq("dataset_id", dataset_id).eq("is_active", True).execute().data or []
    if not cases:
        raise ValueError("Dataset has no active cases")

    provider = get_embedding_provider()
    if resume_run_id:
        run_id = resume_run_id
    else:
        row = sb.table("rag_benchmark_runs").insert({
            "dataset_id": dataset_id, "run_name": run_name, "mode": mode, "status": "queued",
            "embedding_provider": provider.provider_name, "embedding_model": provider.model_name(),
            "embedding_dimensions": provider.dimensions(), "llm_model": OPENAI_CHAT_MODEL,
            "final_top_k": top_k, "config_snapshot": {"top_k": top_k, "mode": mode},
        }).execute().data[0]
        run_id = row["id"]

    thread = threading.Thread(target=lambda: execute_run(sb, run_id, cases, mode, top_k=top_k,
                                                          resume=bool(resume_run_id)), daemon=True)
    thread.start()
    return run_id


def compare_runs(sb, run_a_id: str, run_b_id: str) -> Dict:
    """Baseline (A) vs Candidate (B). Matches results by case_id — a case
    present in only one run is reported but not diffed."""
    run_a = sb.table("rag_benchmark_runs").select("*").eq("id", run_a_id).execute().data[0]
    run_b = sb.table("rag_benchmark_runs").select("*").eq("id", run_b_id).execute().data[0]
    results_a = {r["case_id"]: r for r in (sb.table("rag_benchmark_results").select("*").eq("run_id", run_a_id).execute().data or []) if r.get("case_id")}
    results_b = {r["case_id"]: r for r in (sb.table("rag_benchmark_results").select("*").eq("run_id", run_b_id).execute().data or []) if r.get("case_id")}

    improved, regressed, unchanged, case_diffs = [], [], [], []
    for case_id in set(results_a) & set(results_b):
        ra, rb = results_a[case_id], results_b[case_id]
        if ra["status"] != rb["status"]:
            entry = {
                "case_id": case_id, "question": rb["question"],
                "previous_answer": ra.get("actual_answer"), "new_answer": rb.get("actual_answer"),
                "previous_top_chunk": (ra.get("retrieved_chunks") or [{}])[0].get("file_name") if ra.get("retrieved_chunks") else None,
                "new_top_chunk": (rb.get("retrieved_chunks") or [{}])[0].get("file_name") if rb.get("retrieved_chunks") else None,
                "previous_status": ra["status"], "new_status": rb["status"],
            }
            case_diffs.append(entry)
            if ra["status"] != "pass" and rb["status"] == "pass":
                improved.append(case_id)
            elif ra["status"] == "pass" and rb["status"] != "pass":
                regressed.append(case_id)
        else:
            unchanged.append(case_id)

    def pass_rate(run):
        total = run.get("total_cases") or 0
        return (run.get("passed_cases") or 0) / total if total else 0.0

    def _direction(delta: float, higher_is_better: bool = True, eps: float = 1e-9) -> str:
        if abs(delta) < eps:
            return "unchanged"
        improved_flag = (delta > 0) if higher_is_better else (delta < 0)
        return "improved" if improved_flag else "regressed"

    # Phase 2 (Part 6) — aggregate accuracy deltas for the NEW metric
    # categories, over whichever results actually populated them (a
    # RETRIEVAL_ONLY/FULL_RAG run's results simply have {} for these
    # keys, so the aggregate is reported as "not_comparable" rather than
    # a misleading 0%).
    def _qu_accuracy(results: Dict) -> Optional[float]:
        scores = [metrics.query_understanding_score(r["query_understanding_metrics"])
                  for r in results.values() if r.get("query_understanding_metrics")]
        return round(sum(scores) / len(scores), 4) if scores else None

    def _scenario_pass_rate(results: Dict) -> Optional[float]:
        scores = [r["conversation_metrics"].get("turn_pass_rate") for r in results.values()
                  if r.get("conversation_metrics") and r["conversation_metrics"].get("turn_pass_rate") is not None]
        return round(sum(scores) / len(scores), 4) if scores else None

    def _critical_fact_pass_rate(results: Dict) -> Optional[float]:
        vals = [1.0 if r["critical_fact_metrics"].get("pass") else 0.0 for r in results.values()
                if r.get("critical_fact_metrics") and r["critical_fact_metrics"].get("total_count")]
        return round(sum(vals) / len(vals), 4) if vals else None

    def _grounded_rate(results: Dict) -> Optional[float]:
        vals = [1.0 if r["grounding_metrics"].get("status") in ("supported", "partially_supported") else 0.0
                for r in results.values() if r.get("grounding_metrics") and r["grounding_metrics"].get("status")
                and r["grounding_metrics"]["status"] != "not_applicable"]
        return round(sum(vals) / len(vals), 4) if vals else None

    metric_pairs = {
        "query_understanding_accuracy": (_qu_accuracy(results_a), _qu_accuracy(results_b)),
        "conversation_scenario_pass_rate": (_scenario_pass_rate(results_a), _scenario_pass_rate(results_b)),
        "critical_fact_pass_rate": (_critical_fact_pass_rate(results_a), _critical_fact_pass_rate(results_b)),
        "grounded_answer_rate": (_grounded_rate(results_a), _grounded_rate(results_b)),
    }
    metric_comparison = {}
    for name, (val_a, val_b) in metric_pairs.items():
        if val_a is None or val_b is None:
            metric_comparison[name] = {"baseline": val_a, "candidate": val_b, "direction": "not_comparable"}
        else:
            metric_comparison[name] = {"baseline": val_a, "candidate": val_b,
                                        "absolute_diff": round(val_b - val_a, 4),
                                        "direction": _direction(val_b - val_a)}

    return {
        "run_a": {"id": run_a["id"], "run_name": run_a["run_name"]},
        "run_b": {"id": run_b["id"], "run_name": run_b["run_name"]},
        "pass_rate_change": round(pass_rate(run_b) - pass_rate(run_a), 4),
        "latency_change_ms": round((run_b.get("average_latency_ms") or 0) - (run_a.get("average_latency_ms") or 0), 2),
        "cost_change": round(float(run_b.get("estimated_cost") or 0) - float(run_a.get("estimated_cost") or 0), 6),
        "cases_improved": improved, "cases_regressed": regressed, "cases_unchanged_count": len(unchanged),
        "case_diffs": case_diffs,
        "metric_comparison": metric_comparison,
    }
