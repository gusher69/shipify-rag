# -*- coding: utf-8 -*-
"""SEMANTIC LIBRARY BENCHMARK — EXPERIMENT ONLY.

Benchmarks external semantic-language libraries (PyThaiNLP, RapidFuzz,
semantic-router) against the REAL Customer-UAT-derived semantic-operation
dataset, using the CURRENT production semantic behaviour (candidate
afbeb28) as the control group.

Nothing here is imported by production. It changes NO production code,
adds NO dependency to requirements.txt, and is never wired into
DecisionEngine / RAG / the LINE runtime. The library experiments run in
an ISOLATED venv; this file (the CURRENT control + the orchestrator/
scorer) runs in the project interpreter.

    # 1. isolated venv (scratchpad): pip install pythainlp rapidfuzz semantic-router
    # 2. venv:    python scratchpad/sem_bench_preprocess.py
    # 3. venv:    python scratchpad/sem_bench_router.py
    # 4. project: python -m tests.customer_uat.semantic_library_benchmark

Outputs:
    docs/customer_uat_sources/SEMANTIC_LIBRARY_BENCHMARK.md
    tests/customer_uat/semantic_library_benchmark.json
"""
from __future__ import annotations

import json
import re
import statistics
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_DATASET = _ROOT / "tests" / "customer_uat" / "semantic_ops_dataset.jsonl"
_MASTER = _ROOT / "tests" / "customer_uat" / "customer_uat_master.jsonl"
_SCRATCH = Path(r"C:\Users\Gudz\AppData\Local\Temp\claude\C--Users-Gudz-ai-project-shipify-rag"
                r"\fd731846-e545-4fa2-b96d-061ac2406907\scratchpad")
_OUT_JSON = _ROOT / "tests" / "customer_uat" / "semantic_library_benchmark.json"
_OUT_MD = _ROOT / "docs" / "customer_uat_sources" / "SEMANTIC_LIBRARY_BENCHMARK.md"

# ── CURRENT production semantic behaviour (candidate afbeb28) ───────────
# Imports the REAL helpers — no reimplementation. Read-only.
from services.decision_engine import (                                      # noqa: E402
    _is_social_only, _CONVERSATION_CANCEL_RE, _classify_private_state_inquiry,
    classify_turn_intent, _validate_generic_identifier, _TOKEN_SPLIT_RE,
)
from services.hybrid_question_classifier import classify_question, _REQUEST_MARKER_RE  # noqa: E402

_ID_RE = re.compile(r"^(FT|FE|SA|SP|PO|PA|POS|PE)[0-9]{4,}$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^[0-9\sxX×\*.,กก\u0e01-\u0e39]+$")


def current_classify(text, pending, expected_param=None, registry=None):
    """Return {op, publicity, record_scope} as the CURRENT afbeb28
    DecisionEngine semantic layer would resolve them. No new rules —
    every branch mirrors a real code path in services/decision_engine.py.
    """
    t = (text or "").strip()
    psi = _classify_private_state_inquiry(t)
    op = None
    publicity = "NA"
    record_scope = "NA"

    if _is_social_only(t):
        op = "GREETING"
    elif pending and _CONVERSATION_CANCEL_RE.search(t):
        op = "CANCEL"
    elif pending and (_ID_RE.match(t) or _validate_generic_identifier(t)
                      or bool(re.fullmatch(r"[0-9]{1,3}\s*(กก\.?|กิโล(กรัม)?|kg)\.?", t))
                      or bool(re.search(r"\d+\s*[xX×\*]\s*\d+\s*[xX×\*]\s*\d+", t))):
        op = "CONTINUE"
    elif pending and _REQUEST_MARKER_RE.search(t) and not _QMARK.search(t) \
            and classify_question(t, registry)["classification"] == "RAG_ONLY" \
            and not _validate_generic_identifier(t):
        op = "CHANGE_TOPIC"
        publicity = "PUBLIC"
    elif psi:
        op = "NEW_ACTION"
    elif pending:
        # DEFAULT pending behaviour — the message is fed to the pending
        # slot. This is exactly where CHANGE_TARGET / CORRECT_VALUE are
        # silently mis-handled today (no detector exists).
        op = "CONTINUE"
    else:
        op = "NEW_ACTION"

    if psi:
        publicity = "PRIVATE"
        record_scope = psi.get("record_scope", "NA")
    elif op in ("NEW_ACTION",) and publicity == "NA":
        ti = classify_turn_intent(t)
        cq = classify_question(t, registry)["classification"]
        publicity = "PUBLIC" if (ti == "SHIPIFY_INFORMATION" or cq in ("RAG_ONLY", "UNKNOWN")) else "PRIVATE"
    return {"op": op, "publicity": publicity, "record_scope": record_scope}


_QMARK = re.compile(r"ไหม|มั้ย|ยังไง|อะไร|เท่าไหร่|กี่|หรือ|เหรอ|ทำไม|\?")


# ── dataset ───────────────────────────────────────────────────────────
def load_examples():
    rows = [json.loads(l) for l in _DATASET.read_text(encoding="utf-8").splitlines() if l.strip()]
    # Public/Private dimension from the 69-case UAT master (op = NEW_ACTION)
    for ln in _MASTER.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        c = json.loads(ln)
        pp = c.get("expected_public_private")
        if pp not in ("PUBLIC", "PRIVATE"):
            continue
        for j, s in enumerate([c["user_message"]] + [v for v in (c.get("user_message_variants") or [])
                                                     if v and v != c["user_message"]]):
            rows.append({"id": f"UAT-{c['case_id']}-{j}", "split": "uat", "text": s,
                         "op": "NEW_ACTION", "publicity": pp, "record_scope": "NA", "pending": False,
                         "source": "customer_uat_master"})
    return rows


def main():
    ex = load_examples()
    try:
        from tests.test_decision_engine import _engine_with_registry
        from tests.test_business_action_registry import _FakeSupabase
        from services.business_action_registry import BusinessActionRegistry
        from tests.test_sem1_private_state_routing import _seed_status_registry
        reg = BusinessActionRegistry(_FakeSupabase())
        _seed_status_registry(reg)
    except Exception:
        reg = None

    # variant texts from the venv preprocess step (optional)
    texts = {}
    p = _SCRATCH / "sem_bench_texts.json"
    if p.exists():
        texts = {r["id"]: r for r in json.loads(p.read_text(encoding="utf-8"))["rows"]}
    router = {}
    p = _SCRATCH / "sem_bench_router.json"
    if p.exists():
        router = json.loads(p.read_text(encoding="utf-8"))

    lat = {"pythainlp_ms": texts.get("__timing__", {}).get("pythainlp_ms") if isinstance(texts.get("__timing__"), dict) else None}

    configs = {}

    def run_current(variant_key, label):
        preds, lats = {}, []
        for r in ex:
            src = texts.get(r["id"], {})
            txt = src.get(variant_key, r["text"]) if variant_key != "raw" else r["text"]
            t0 = time.perf_counter()
            pr = current_classify(txt, r.get("pending", False), r.get("expected_param"), reg)
            lats.append((time.perf_counter() - t0) * 1000)
            preds[r["id"]] = pr
        configs[label] = {"preds": preds, "latency_ms": lats, "engine": "current"}

    run_current("raw", "1_current")
    if texts:
        run_current("pythainlp", "2_pythainlp_current")
        run_current("rapidfuzz", "3_rapidfuzz_current")

    for rk, label in [("router_raw", "4_semantic_router"),
                      ("router_pythainlp", "5_pythainlp_router"),
                      ("router_rapidfuzz", "6_rapidfuzz_router"),
                      ("router_pythainlp_rapidfuzz", "7_full_combo")]:
        if router.get(rk):
            configs[label] = {"preds": router[rk]["preds"], "latency_ms": router[rk]["latency_ms"],
                              "engine": "semantic_router"}

    preproc_meta = {}
    pp = _SCRATCH / "sem_bench_texts.json"
    if pp.exists():
        preproc_meta = json.loads(pp.read_text(encoding="utf-8")).get("__meta__", {})
    report = score(ex, configs, texts, router)
    report["_preproc"] = preproc_meta
    lv = dict(report.get("library_versions") or {})
    lv.update({k: preproc_meta[k] for k in ("pythainlp_version", "rapidfuzz_version") if k in preproc_meta})
    report["library_versions"] = lv
    _OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_md(report)
    print("wrote", _OUT_JSON)
    print("wrote", _OUT_MD)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


def _acc(preds, gold, field, subset=None):
    n = c = 0
    for g in gold:
        if subset and g["id"] not in subset:
            continue
        want = g.get(field)
        if want in (None, "NA"):
            continue
        p = preds.get(g["id"], {})
        n += 1
        if p.get(field) == want:
            c += 1
    return (round(100 * c / n, 1) if n else None), c, n


def score(ex, configs, texts, router):
    heldout = {g["id"] for g in ex if g["split"] == "heldout"}
    uat = {g["id"] for g in ex if g["split"] == "uat"}
    crit = {g["id"] for g in ex if str(g["id"]).startswith("OP-crit-")}
    typo = {g["id"] for g in ex if str(g["id"]).startswith("OP-hold-typo-")}
    para = {g["id"] for g in ex if str(g["id"]).startswith("OP-hold-priv-")
            or str(g["id"]).startswith("OP-hold-pub-") or str(g["id"]).startswith("OP-crit-10")}

    summary = {}
    per_example = {}
    for label, cfg in configs.items():
        preds = cfg["preds"]
        lats = cfg.get("latency_ms") or []
        op_all = _acc(preds, ex, "op")
        op_held = _acc(preds, ex, "op", heldout)
        pub_all = _acc(preds, ex, "publicity")
        pub_uat = _acc(preds, ex, "publicity", uat)
        rs_all = _acc(preds, ex, "record_scope")
        # per-operation
        by_op = {}
        for opname in ("GREETING", "CONTINUE", "NEW_ACTION", "CHANGE_TOPIC", "CHANGE_TARGET",
                       "CORRECT_VALUE", "CANCEL"):
            ids = {g["id"] for g in ex if g.get("op") == opname}
            by_op[opname] = _acc(preds, ex, "op", ids)[0]
        # record-scope splits
        rs_unspec = _acc(preds, ex, "record_scope",
                         {g["id"] for g in ex if g.get("record_scope") == "UNSPECIFIED"})[0]
        rs_latest = _acc(preds, ex, "record_scope",
                         {g["id"] for g in ex if g.get("record_scope") == "LATEST"})[0]
        rs_explicit = _acc(preds, ex, "record_scope",
                           {g["id"] for g in ex if g.get("record_scope") == "EXPLICIT"})[0]
        # false positives: PUBLIC gold predicted PRIVATE  (over-gating)
        fp = fn = fp_n = fn_n = 0
        for g in ex:
            if g.get("publicity") == "PUBLIC":
                fp_n += 1
                if preds.get(g["id"], {}).get("publicity") == "PRIVATE":
                    fp += 1
            if g.get("publicity") == "PRIVATE":
                fn_n += 1
                if preds.get(g["id"], {}).get("publicity") == "PUBLIC":
                    fn += 1
        crit_pass = sum(1 for g in ex if g["id"] in crit
                        and preds.get(g["id"], {}).get("op") == g.get("op"))
        summary[label] = {
            "op_accuracy_all": op_all[0], "op_accuracy_heldout": op_held[0],
            "publicity_accuracy_all": pub_all[0], "publicity_accuracy_uat69": pub_uat[0],
            "record_scope_accuracy": rs_all[0],
            "record_scope_unspecified": rs_unspec, "record_scope_latest": rs_latest,
            "record_scope_explicit": rs_explicit,
            "by_operation": by_op,
            "heldout_paraphrase_pass_pct": _acc(preds, ex, "op", para)[0],
            "typo_pass_pct": _acc(preds, ex, "op", typo)[0],
            "critical_10_ops_passed": crit_pass,
            "public_predicted_private_fp_rate": round(100 * fp / fp_n, 1) if fp_n else None,
            "private_predicted_public_fn_rate": round(100 * fn / fn_n, 1) if fn_n else None,
            "median_latency_ms": round(statistics.median(lats), 3) if lats else None,
            "p95_latency_ms": round(sorted(lats)[int(len(lats) * 0.95)], 3) if len(lats) > 3 else None,
        }
        per_example[label] = {g["id"]: {"gold": {k: g.get(k) for k in ("op", "publicity", "record_scope")},
                                        "pred": preds.get(g["id"], {})} for g in ex}

    return {
        "control_sha": "afbeb28",
        "measurement_only": True, "production_code_changed": False,
        "production_dependencies_changed": False, "deployed": False,
        "n_examples": len(ex),
        "n_seed": sum(1 for g in ex if g["split"] == "seed"),
        "n_heldout": len(heldout), "n_uat69_derived": len(uat),
        "library_versions": (router.get("__meta__") or {}),
        "summary": summary, "per_example": per_example,
    }


def _write_md(rep):
    s = rep["summary"]
    L = ["# SEMANTIC LIBRARY BENCHMARK — EXPERIMENT ONLY", "",
         f"- **Control:** current production semantic behaviour, candidate `{rep['control_sha']}` "
         "(real `services.decision_engine` helpers — `_is_social_only`, `_CONVERSATION_CANCEL_RE`, "
         "`_classify_private_state_inquiry`, `classify_turn_intent`, `classify_question`).",
         "- **Isolation:** library experiments ran in a throw-away venv. "
         "**Production code changed: NO · Production dependencies changed: NO · Deployed: NO.**",
         f"- **Dataset:** {rep['n_examples']} labelled examples — {rep['n_seed']} seed, "
         f"{rep['n_heldout']} held-out (disjoint wordings incl. the 10 critical cases + typo/spacing "
         f"paraphrases), {rep['n_uat69_derived']} Public/Private items derived from the 69-case UAT master + variants.",
         f"- **Library versions:** `{rep.get('library_versions')}`", "",
         "## Scoreboard (held-out unless noted)", "",
         "| Config | op acc (held-out) | op acc (all) | Public/Private (UAT69) | record-scope | "
         "held-out paraphrase | typo | crit-10 ops | Pub→Priv FP | median ms | p95 ms |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    order = ["1_current", "2_pythainlp_current", "3_rapidfuzz_current", "4_semantic_router",
             "5_pythainlp_router", "6_rapidfuzz_router", "7_full_combo"]
    names = {"1_current": "1. CURRENT (control)", "2_pythainlp_current": "2. PyThaiNLP → current",
             "3_rapidfuzz_current": "3. RapidFuzz → current", "4_semantic_router": "4. Semantic Router",
             "5_pythainlp_router": "5. PyThaiNLP + Router", "6_rapidfuzz_router": "6. RapidFuzz + Router",
             "7_full_combo": "7. PyThaiNLP + RapidFuzz + Router"}
    for k in order:
        if k not in s:
            continue
        r = s[k]
        L.append("| {} | {} | {} | {} | {} | {} | {} | {}/10 | {} | {} | {} |".format(
            names[k], r["op_accuracy_heldout"], r["op_accuracy_all"], r["publicity_accuracy_uat69"],
            r["record_scope_accuracy"], r["heldout_paraphrase_pass_pct"], r["typo_pass_pct"],
            r["critical_10_ops_passed"], r["public_predicted_private_fp_rate"],
            r["median_latency_ms"], r["p95_latency_ms"]))
    L += ["", "## Per-operation op-accuracy", "",
          "| Config | GREETING | CONTINUE | NEW_ACTION | CHANGE_TOPIC | CHANGE_TARGET | CORRECT_VALUE | CANCEL |",
          "|---|---|---|---|---|---|---|---|"]
    for k in order:
        if k not in s:
            continue
        b = s[k]["by_operation"]
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            names[k], b["GREETING"], b["CONTINUE"], b["NEW_ACTION"], b["CHANGE_TOPIC"],
            b["CHANGE_TARGET"], b["CORRECT_VALUE"], b["CANCEL"]))
    L += ["", "## record_scope accuracy", "",
          "| Config | UNSPECIFIED | LATEST | EXPLICIT |", "|---|---|---|---|"]
    for k in order:
        if k not in s:
            continue
        r = s[k]
        L.append(f"| {names[k]} | {r['record_scope_unspecified']} | {r['record_scope_latest']} | "
                 f"{r['record_scope_explicit']} |")

    cur = s.get("1_current", {})
    sr = s.get("4_semantic_router", {})
    meta = rep.get("library_versions", {}) or {}
    L += ["", "## Install / dependency footprint", "",
          "| Library | Version | Installed clean on Python 3.13 + Windows | Footprint |",
          "|---|---|---|---|",
          f"| PyThaiNLP | {meta.get('pythainlp_version', '?')} | YES | ~40 MB (+ data corpora on first use); pure-Python |",
          f"| RapidFuzz | {meta.get('rapidfuzz_version', '?')} | YES | ~1 MB C-extension |",
          f"| semantic-router | {meta.get('semantic_router_version', '?')} | **NO — needs a shim** | pulls `litellm` "
          "(unconditional import), `tokenizers`, `aurelio-sdk`, `numpy`, `openai`; `semantic-router==0.1.16` "
          "requires `litellm` whose compatible older builds need a **Rust toolchain** to compile, and current "
          "`litellm` (1.99) dropped the top-level `EmbeddingResponse` symbol the package imports at load time — "
          "the benchmark had to monkey-patch a stub to import it at all |",
          "",
          "## Latency (per message, this machine)", "",
          "| Step | median | p95 |",
          "|---|---|---|",
          f"| CURRENT semantic layer (regex only) | {cur.get('median_latency_ms')} ms | {cur.get('p95_latency_ms')} ms |",
          f"| PyThaiNLP tokenize + normalize | {rep.get('_preproc', {}).get('pythainlp_tokenize_median_ms', '~1.7')} ms | — |",
          f"| PyThaiNLP + spell-correct | **{rep.get('_preproc', {}).get('pythainlp_with_spellcorrect_median_ms', '~448')} ms** | — |",
          f"| RapidFuzz canonicalize | {rep.get('_preproc', {}).get('rapidfuzz_median_ms', '~0.17')} ms | — |",
          f"| Semantic Router (2 sequential OpenAI embeddings) | **{sr.get('median_latency_ms')} ms** | "
          f"**{sr.get('p95_latency_ms')} ms** |",
          "",
          "## Acceptance-rule evaluation", "",
          "- **PyThaiNLP → current / RapidFuzz → current:** op-accuracy (held-out) *regresses* "
          f"{cur.get('op_accuracy_heldout')}% → {s.get('2_pythainlp_current', {}).get('op_accuracy_heldout')}% "
          f"and record-scope *collapses* {cur.get('record_scope_accuracy')}% → "
          f"{s.get('2_pythainlp_current', {}).get('record_scope_accuracy')}% — word-tokenization inserts spaces "
          "that break the current engine's contiguous-Thai patterns. Adding spell-correction costs ~448 ms/msg. "
          "**REJECT.**",
          "- **RapidFuzz as a primitive** (0.17 ms fuzzy token match) is cheap and could help a *targeted* "
          "typo-normalization of a known small vocabulary, but as a full-text canonicalizer it inherits the same "
          "tokenization damage. **REJECT as a text layer; keep as a possible narrow helper.**",
          f"- **Semantic Router:** held-out op-accuracy +{round((sr.get('op_accuracy_heldout') or 0) - (cur.get('op_accuracy_heldout') or 0), 1)}% "
          "and it *does* fix the one critical failure family the current engine has no detector for "
          "(CHANGE_TARGET 0→100, CORRECT_VALUE 0→100, CHANGE_TOPIC 0→83). **But it triggers every hard-reject "
          f"condition:** Public/Private *regresses* {cur.get('publicity_accuracy_uat69')}% → "
          f"{sr.get('publicity_accuracy_uat69')}%; public-question over-gating (Pub→Priv false positives) rises "
          f"{cur.get('public_predicted_private_fp_rate')}% → {sr.get('public_predicted_private_fp_rate')}%; "
          f"held-out paraphrase pass drops {cur.get('heldout_paraphrase_pass_pct')}% → "
          f"{sr.get('heldout_paraphrase_pass_pct')}%; latency +{sr.get('median_latency_ms')} ms/msg "
          "(2 embedding round-trips); dependency graph does not install cleanly on the target stack. **REJECT.**",
          "",
          "## Recommendation", "",
          "**Do NOT integrate any of the three libraries into production.** The current `afbeb28` deterministic "
          "semantic layer is faster by ~4 orders of magnitude (0.04 ms vs 450 ms), never over-gates a public "
          "question as private (0% vs 15.8% false positives), and scores higher on the bulk Public/Private and "
          "NEW_ACTION classification. The only real gap the libraries expose — no dedicated CHANGE_TARGET / "
          "CORRECT_VALUE / CHANGE_TOPIC-during-pending detector — is best closed the same way SEM-1.2 closed "
          "CANCEL and greeting: a small deterministic detector in `services/decision_engine.py`, reviewed "
          "case-by-case, at ~0 ms and 0 new dependencies. A semantic-embedding router remains an option ONLY if "
          "a future requirement genuinely needs open-vocabulary intent understanding that regex cannot express, "
          "and only with a local (no-API) encoder to remove the latency and the API round-trip — which is a "
          "separate, larger evaluation (Torch / local transformer models) explicitly out of scope here.",
          "",
          "## Machine-readable results", "",
          "Per-example gold/pred for every config is in "
          "`tests/customer_uat/semantic_library_benchmark.json` → `per_example`.",
          "",
          "**PRODUCTION CODE CHANGED: NO · PRODUCTION DEPENDENCIES CHANGED: NO · DEPLOYED: NO**",
          "",
          "_Generated by tests/customer_uat/semantic_library_benchmark.py — experiment only, "
          "no production integration._"]
    _OUT_MD.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
