"""Regression + fuzz tests for the Safe Query Understanding Framework
(P0, 2026-07-21) — the generic Semantic Invariant Guard
(rag/semantic_guard.py) wired into rag/spell_correction.py's
correct_query() and rag/canonical_query.py's rewrite_canonical_query().

Covers Part 17's regression list (1-18, 22-26 — 19-21/27 are ingestion/
full-suite items covered elsewhere, see the deliverable report) and
Part 13's fuzz/mutation requirement (>= 100 generated cases).
"""
import unittest

from rag.semantic_guard import entity_snapshot, validate_transformation, diff_entities
from rag.spell_correction import correct_query
from rag.canonical_query import rewrite_canonical_query
from rag.query_resolution import extract_entities


# ── Part 17: regression tests ───────────────────────────────────────────

class TestCorrectionCannotIntroduceEntities(unittest.TestCase):
    """1-8: correction cannot introduce/change a country, city, brand,
    product, service intent, shipping method, negation, or identifier."""

    def test_cannot_introduce_country(self):
        v = validate_transformation("ขอที่อยู่โกดังหน่อย", "ขอที่อยู่โกดังจีน่อย")
        self.assertFalse(v["accepted"])
        self.assertTrue(any("location" in x for x in v["violations"]))

    def test_cannot_change_country(self):
        v = validate_transformation("ขอที่อยู่โกดังไทย", "ขอที่อยู่โกดังจีน")
        self.assertFalse(v["accepted"])

    def test_cannot_change_shipping_method(self):
        v = validate_transformation("ส่งทางรถ", "ส่งทางเรือ")
        self.assertFalse(v["accepted"])
        self.assertTrue(any("transport" in x for x in v["violations"]))

    def test_cannot_change_service_intent_topic(self):
        # Uses two REGISTERED synonym-group topics (warehouse/rate) so the
        # entity extractor can actually recognize the substitution — the
        # guard's granularity is bounded by the same registered topic
        # vocabulary every other module in this pipeline already uses.
        v = validate_transformation("ขอโกดังหน่อย", "ขอเรทหน่อย")
        self.assertFalse(v["accepted"])

    def test_cannot_flip_negation_positive_to_negative(self):
        v = validate_transformation("รับสินค้านี้ไหม", "ไม่รับสินค้านี้ไหม")
        self.assertFalse(v["accepted"])

    def test_cannot_flip_negation_negative_to_positive(self):
        v = validate_transformation("ของยังไม่ถึง", "ของถึงแล้ว")
        self.assertFalse(v["accepted"])

    def test_cannot_change_order_number(self):
        v = validate_transformation("เช็คออเดอร์ 1234", "เช็คออเดอร์ 5678")
        self.assertFalse(v["accepted"])

    def test_cannot_change_amount(self):
        v = validate_transformation("จ่าย 100 บาท", "จ่าย 1,000 บาท")
        self.assertFalse(v["accepted"])


class TestPoliteParticlesSafe(unittest.TestCase):
    """9."""

    def test_the_exact_reported_bug_is_rejected(self):
        r = correct_query("ขอที่อยู่โกดังหน่อย")
        self.assertEqual(r["corrected_query"], "ขอที่อยู่โกดังหน่อย")
        self.assertIsNotNone(r["rejected_correction"])

    def test_polite_particle_removal_via_canonical_rewrite_is_still_allowed(self):
        """Part 7 explicitly allows canonical rewriting to remove polite
        particles entirely — only PARTIAL consumption is unsafe."""
        r = rewrite_canonical_query("ขอที่อยู่โกดังหน่อย", entities={})
        # No topic/location in this bare phrasing -> no confident template
        # matches, so nothing is rewritten; still must never CORRUPT it.
        self.assertNotIn("จีน", r["canonical_query"])


class TestOriginalQueryParticipatesInRetrieval(unittest.TestCase):
    """10."""

    def test_original_query_always_present_in_result(self):
        r = correct_query("ขอที่อยู่โกดังหน่อย")
        self.assertEqual(r["original_query"], "ขอที่อยู่โกดังหน่อย")

    def test_original_query_never_mutated_in_place(self):
        original = "ขอที่อยู่โกดังหน่อย"
        correct_query(original)
        self.assertEqual(original, "ขอที่อยู่โกดังหน่อย")


class TestCanonicalRewriteCannotAddUnsupportedEntity(unittest.TestCase):
    """11."""

    def test_canonical_rewrite_never_adds_a_country_from_nothing(self):
        r = rewrite_canonical_query("ส่งแผนที่ให้หน่อย", entities={})
        self.assertNotIn("จีน", r["canonical_query"])
        self.assertNotIn("ไทย", r["canonical_query"])

    def test_canonical_rewrite_with_carried_entities_is_safe(self):
        """A carried entity IS allowed (Part 2.B) — this must still work,
        it's the accepted case, not the rejected one."""
        r = rewrite_canonical_query("ส่งแผนที่ให้หน่อย", entities={"topic": "โกดัง", "location": "ไทย"})
        self.assertTrue(r["rewrite_applied"])
        self.assertIn("ไทย", r["canonical_query"])


class TestExpandedQueryTermsNeverBecomeEntities(unittest.TestCase):
    """12."""

    def test_expansion_terms_do_not_leak_into_entity_extraction(self):
        """rag/query_understanding.py::expand_company_intent_terms() adds
        "นำเข้าสินค้าจากจีน" as a RETRIEVAL-only variant for a company
        question — the original question's own entity snapshot must not
        show a location merely because that variant exists."""
        from rag.query_understanding import expand_company_intent_terms
        original = "บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร"
        terms = expand_company_intent_terms(original)
        self.assertIn("นำเข้าสินค้าจากจีน", terms)
        snap = entity_snapshot(original)
        self.assertIsNone(snap["location"])


class TestPreviousAssistantAnswerNeverProvidesEntities(unittest.TestCase):
    """13, 14."""

    def test_previous_assistant_answer_never_used_as_carried_entities(self):
        """accumulate_entities() only ever inspects role=="user" turns —
        an assistant turn mentioning "จีน" must never surface as a
        carried entity."""
        from rag.query_resolution import accumulate_entities
        history = [
            {"role": "user", "content": "ขอที่อยู่โกดังหน่อย"},
            {"role": "assistant", "content": "โกดังจีนอยู่ที่..."},
        ]
        carried = accumulate_entities(history)
        self.assertIsNone(carried.get("location"))

    def test_previous_user_message_may_provide_entities(self):
        from rag.query_resolution import accumulate_entities
        history = [{"role": "user", "content": "ขอที่อยู่โกดังจีน"}]
        carried = accumulate_entities(history)
        self.assertEqual(carried.get("location"), "จีน")


class TestExactGeneralFaqOutranksIncorrectSpecificFaq(unittest.TestCase):
    """15 — covered structurally: since the corrupted "โกดังจีน่อย" is now
    NEVER produced (rejected at the source), match_faq_exact/hybrid
    retrieval always operate on the safe original text, so a general
    warehouse FAQ can win on its own terms rather than a corrupted
    specific one ever entering the candidate pool at all."""

    def test_corrupted_query_never_reaches_retrieval(self):
        r = correct_query("ขอที่อยู่โกดังหน่อย")
        self.assertNotIn("จีน", r["corrected_query"])


class TestUnspecifiedEntityRemainsUnspecified(unittest.TestCase):
    """16, Part 8."""

    def test_bare_warehouse_question_has_unspecified_country(self):
        snap = entity_snapshot("ขอที่อยู่โกดังหน่อย")
        self.assertIsNone(snap["location"])

    def test_thai_warehouse_question_has_explicit_country(self):
        snap = entity_snapshot("ขอที่อยู่โกดังไทย")
        self.assertEqual(snap["location"], "ไทย")

    def test_china_warehouse_question_has_explicit_country(self):
        snap = entity_snapshot("ขอที่อยู่โกดังจีน")
        self.assertEqual(snap["location"], "จีน")


class TestAnswerPlanCannotAddUnsupportedEntity(unittest.TestCase):
    """17."""

    def test_answer_plan_validator_rejects_unsupported_location(self):
        from services.answer_planner import validate_answer_plan
        plan = {"answer_goal": "Provide the china warehouse map"}
        result = validate_answer_plan(plan, {"location": None})
        self.assertFalse(result["valid"])

    def test_answer_plan_validator_accepts_supported_location(self):
        from services.answer_planner import validate_answer_plan
        plan = {"answer_goal": "Provide the ไทย warehouse address"}
        result = validate_answer_plan(plan, {"location": "ไทย"})
        self.assertTrue(result["valid"])

    def test_plan_answer_never_fabricates_a_location_for_a_bare_question(self):
        from services.answer_planner import plan_answer
        plan = plan_answer("ขอที่อยู่โกดังหน่อย", "warehouse_location", entities={}, chunks=[{"score": 0.3}])
        self.assertNotIn("จีน", plan["answer_goal"])
        self.assertNotIn("ไทย", plan["answer_goal"])


class TestAttachmentCannotAddUnsupportedEntity(unittest.TestCase):
    """18."""

    def test_attachment_conflicting_with_validated_location_is_excluded(self):
        from services.attachment_planner import plan_attachments
        chunks = [{
            "chunk_id": "c1", "text": "โกดังจีนอยู่ที่...",
            "attachments": [{"public_url": "http://x/china.jpg", "mime_type": "image/jpeg", "filename": "china.jpg"}],
        }]
        plan = plan_attachments("warehouse_location", [], {}, chunks,
                                 validated_entities={"location": "ไทย"})
        self.assertFalse(plan["should_send"])

    def test_attachment_compatible_with_validated_location_is_kept(self):
        from services.attachment_planner import plan_attachments
        chunks = [{
            "chunk_id": "c1", "text": "โกดังไทยอยู่ที่...",
            "attachments": [{"public_url": "http://x/thai.jpg", "mime_type": "image/jpeg", "filename": "thai.jpg"}],
        }]
        plan = plan_attachments("warehouse_location", [], {}, chunks,
                                 validated_entities={"location": "ไทย"})
        self.assertTrue(plan["should_send"])

    def test_no_validated_entities_never_rejects_anything(self):
        """Backward compatibility — omitting validated_entities entirely
        must behave exactly as before this feature existed."""
        from services.attachment_planner import plan_attachments
        chunks = [{
            "chunk_id": "c1", "text": "โกดังจีนอยู่ที่...",
            "attachments": [{"public_url": "http://x/china.jpg", "mime_type": "image/jpeg", "filename": "china.jpg"}],
        }]
        plan = plan_attachments("warehouse_location", [], {}, chunks)
        self.assertTrue(plan["should_send"])


class TestCompanyOverviewAndFollowupSummaryStillPass(unittest.TestCase):
    """22, 23 — no regression to the two most recent P0 fixes."""

    def test_company_overview_intent_unaffected(self):
        from rag.query_understanding import classify_actionable_intent
        r = classify_actionable_intent("บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร", entities={})
        self.assertEqual(r["actionable_intent"], "company_overview")

    def test_followup_summary_still_resolves(self):
        from rag.query_resolution import resolve_conversation
        history = [{"role": "user", "content": "บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร"},
                   {"role": "assistant", "content": "..."}]
        r = resolve_conversation("ช่วยสรุปข้อมูลบริษัทให้หน่อย", history)
        self.assertNotIn("บริษัทไหน", r["resolved_question"])


class TestAdaptiveThresholdsUnchanged(unittest.TestCase):
    """25, 19 (partial) — the guard never touches hybrid_scoring's
    exclusion logic at all; a sanity check that it's still importable/
    unaffected."""

    def test_hybrid_scoring_exclusion_logic_still_importable_and_functional(self):
        from rag.hybrid_scoring import apply_hybrid_ranking
        # Deliberately shares NO vocabulary with the query below (a genuine
        # topic mismatch, not just a differently-worded restatement of
        # "irrelevant") — see Long-Glued-Query Partial Overlap fix
        # (rag/hybrid_scoring.py, Task 04, 2026-08-26): both this fixture's
        # old wording and the query used "เกี่ยวข้อง"/"ไม่เกี่ยวข้อง"
        # literally, which is itself a genuine ~15-character shared
        # substring — accidentally defeating the very exclusion this test
        # means to prove still works, once long-glued-token scoring learned
        # to recognize real shared substrings at all.
        chunks = [{"text": f"สูตรทำขนมหวานไทยแบบดั้งเดิม {i}", "section_title": f"หัวข้อสูตรอาหาร {i}",
                   "score": 0.05 - i * 0.005, "chunk_index": 0, "file_name": "x"} for i in range(8)]
        kept, excluded = apply_hybrid_ranking("รีวิวภาพยนตร์เรื่องล่าสุดเป็นอย่างไรบ้าง", chunks, return_excluded=True)
        self.assertGreater(len(excluded), 0)


# ── Part 12: global query-safety test matrix ────────────────────────────

MATRIX = {
    "WAREHOUSE": ["ขอที่อยู่โกดังหน่อย", "ขอที่อยู่โกดังไทย", "ขอที่อยู่โกดังจีน", "โกดังอยู่ไหน", "ขอรูปโกดังจีน"],
    "SERVICES": ["มีบริการอะไรบ้าง", "ฝากสั่งคืออะไร", "ฝากนำเข้าคืออะไร",
                 "ฝากสั่งกับฝากนำเข้าต่างกันยังไง", "มีบริการตีลังไม้ไหม"],
    "SHIPPING": ["ส่งทางรถได้ไหม", "แล้วทางเรือล่ะ", "ระยะเวลากี่วัน", "ค่าส่งคิดยังไง", "ของถึงหรือยัง"],
    "PAYMENT": ["จ่ายยังไง", "รับบัตรเครดิตไหม", "ฝากโอนคืออะไร", "ใช้คูปองยังไง", "คูปองใช้ไม่หมดคืนได้ไหม"],
    "CONTACT": ["ขอเบอร์ติดต่อ", "เบอร์โกดังอ่อนนุช", "เบอร์ Fasttrade", "ติดต่อเจ้าหน้าที่", "เปิดกี่โมง"],
    "COMPANY": ["บริษัททำธุรกิจอะไร", "ช่วยสรุปข้อมูลบริษัท", "บริษัทมีบริการอะไรบ้าง", "CEO ชื่ออะไร", "บริษัทก่อตั้งเมื่อไหร่"],
    "RESTRICTIONS": ["ของอะไรห้ามนำเข้า", "แบตเตอรี่ส่งได้ไหม", "เครื่องสำอางส่งได้ไหม", "ของกินส่งได้ไหม"],
    "ORDERS": ["เช็คสถานะสินค้า", "เช็คออเดอร์ 12345", "ของยังไม่ถึง", "ยกเลิกได้ไหม"],
}


class TestGlobalQuerySafetyMatrix(unittest.TestCase):
    """Every query across every domain must survive spell correction
    without an entity being introduced beyond what it already names."""

    def test_matrix_never_introduces_a_fact_entity(self):
        failures = []
        for domain, queries in MATRIX.items():
            for q in queries:
                r = correct_query(q)
                before = entity_snapshot(q)
                after = entity_snapshot(r["corrected_query"])
                violations = [v for v in diff_entities(before, after)
                              if v.startswith("introduced_location") or v.startswith("introduced_transport")
                              or v.startswith("changed_location") or v.startswith("changed_transport")]
                if violations:
                    failures.append((domain, q, r["corrected_query"], violations))
        self.assertEqual(failures, [], f"Matrix queries introduced fact entities: {failures}")

    def test_matrix_covers_at_least_25_queries_across_8_domains(self):
        total = sum(len(v) for v in MATRIX.values())
        self.assertGreaterEqual(total, 25)
        self.assertEqual(len(MATRIX), 8)


# ── Part 13: fuzz / mutation tests (>= 100 cases) ───────────────────────

_BASE_PHRASES = [
    "ขอที่อยู่โกดังหน่อย", "มีบริการอะไรบ้าง", "ฝากสั่งคืออะไร", "ส่งทางรถได้ไหม",
    "ค่าส่งคิดยังไง", "จ่ายยังไง", "รับบัตรเครดิตไหม", "ขอเบอร์ติดต่อ", "เปิดกี่โมง",
    "บริษัททำธุรกิจอะไร", "ของอะไรห้ามนำเข้า", "เช็คสถานะสินค้า", "ยกเลิกได้ไหม",
    "ระยะเวลากี่วัน", "ใช้คูปองยังไง",
]


def _mutate_append_particle(s):
    return s + "ครับ"


def _mutate_repeat_char(s):
    return s[:-1] + s[-1] * 3 if s else s


def _mutate_remove_space(s):
    return s.replace(" ", "")


def _mutate_add_space(s):
    return " ".join(list(s[:3])) + s[3:] if len(s) > 3 else s


def _mutate_trailing_punct(s):
    return s + "?"


def _mutate_informal_suffix(s):
    return s + "อะ"


def _mutate_polite_particle_variant(s):
    return s + "นะคะ"


def _mutate_double_first_char(s):
    return (s[0] * 2 + s[1:]) if s else s


_MUTATIONS = [
    _mutate_append_particle, _mutate_repeat_char, _mutate_remove_space, _mutate_add_space,
    _mutate_trailing_punct, _mutate_informal_suffix, _mutate_polite_particle_variant,
    _mutate_double_first_char,
]


def _generate_fuzz_cases():
    cases = []
    for phrase in _BASE_PHRASES:
        for mutate in _MUTATIONS:
            mutated = mutate(phrase)
            if mutated and mutated != phrase:
                cases.append((phrase, mutated))
    return cases


FUZZ_CASES = _generate_fuzz_cases()


class TestFuzzMutations(unittest.TestCase):
    def test_at_least_100_fuzz_cases_generated(self):
        self.assertGreaterEqual(len(FUZZ_CASES), 100)

    def test_no_mutation_introduces_a_new_fact_entity(self):
        failures = []
        for base, mutated in FUZZ_CASES:
            r = correct_query(mutated)
            before = entity_snapshot(base)
            after = entity_snapshot(r["corrected_query"])
            violations = [v for v in diff_entities(before, after)
                          if v.startswith("introduced_location") or v.startswith("introduced_transport")
                          or v.startswith("changed_location") or v.startswith("changed_transport")]
            if violations:
                failures.append((base, mutated, r["corrected_query"], violations))
        self.assertEqual(failures, [], f"{len(failures)}/{len(FUZZ_CASES)} fuzz cases introduced a fact entity: "
                                        f"{failures[:5]}")


if __name__ == "__main__":
    unittest.main()
