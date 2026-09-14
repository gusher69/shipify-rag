# -*- coding: utf-8 -*-
"""PHASE 6 POST-DEPLOY SYSTEMIC HARDENING — the three defect classes.

Every case here is GENERATED from orthogonal dimensions (product ×
quantity × unit × method × question × word order × spacing × particle),
never enumerated as sentences, so the suite measures the MECHANISM and
not the handful of phrasings that were reported.

A. ENTITY BOUNDARY / MULTI-INTENT SEPARATION
   services/conversation_semantics.py::split_question_clause — a
   trailing (or leading) interrogative/action clause is a closed-class
   run and is separated from the entity span structurally, so it can
   never be welded onto the product noun.
   Required: PRODUCT QUESTION-CLAUSE CONTAMINATION = 0, and every fact
   the turn stated survives (CURRENT_TURN_ENTITY_CANNOT_BE_DROPPED).

B. INTENT DISCRIMINATION
   services/operational_change_flow.py::classify_cancellation — the ONE
   place cancellation is named, now reached deterministically by
   _compose so the gated LLM is never consulted for it.
   Required: CANCELLATION->WITHDRAWAL = 0 and WITHDRAWAL->CANCELLATION = 0.

C. SLOT UNIT PRESERVATION
   Frame.unit + the typed quantity entities — the unit the customer
   supplied reaches the acknowledgement and survives a round trip.
   Required: SUPPLIED UNIT ECHO FIDELITY = 100%.

Test tier is pinned offline by tests/__init__.py — no paid API call.
"""
import re
import unittest

from services.conversation_semantics import (
    _compose, interpret, split_question_clause, derive_active_frame,
    Frame, frame_ack_reply, resolve_frame_correction,
)
from services.operational_change_flow import classify_cancellation
from services.service_intent_flow import import_interest_reply


# ── dimensions ───────────────────────────────────────────────────────
# compound nouns deliberately include ones that END in "ของ"/"ทาง" and
# ones that CONTAIN "เครื่อง" — the exact shapes a careless strip eats.
PRODUCTS = ["รองเท้า", "ชั้นวางของ", "กล่องพลาสติก", "เครื่องซีลถุง", "ของเล่น",
            "กระเป๋าเดินทาง", "โคมไฟตั้งโต๊ะ", "ผ้าม่าน", "เก้าอี้สำนักงาน", "ตู้เก็บของ"]
UNITS = ["ตัว", "ชิ้น", "คู่", "ชุด", "กล่อง", "ขวด", "โหล", "ลัง", "เครื่อง", "พาเลท"]
QUESTIONS = ["ราคาเท่าไหร่", "คิดค่าส่งยังไง", "ถึงไทยกี่วัน", "ส่งทางรถได้ไหม",
             "ใช้ขนส่งอะไร", "มีค่าอะไรบ้าง", "เท่าไร", "เมื่อไหร่ถึง",
             "ส่งถึงหน้าบ้านไหม", "คิดยังไง"]
QUANTITIES = [2, 3, 5, 10, 12, 20, 30, 50, 100, 500]


def _cases_entity_clause():
    """>=200 mixed cases across every axis the instruction names:
    product before/after quantity, method before/after product, question
    at the end, question at the beginning, no spaces, polite particles,
    compound Thai nouns."""
    out = []
    for i, p in enumerate(PRODUCTS):
        for j, u in enumerate(UNITS):
            n = QUANTITIES[(i + j) % len(QUANTITIES)]
            q = QUESTIONS[(i + j) % len(QUESTIONS)]
            # 1 product-first, question last
            out.append((f"อยากสั่ง{p}จากจีน {n} {u} {q}", p, n, u, None, q))
            # 2 quantity-first
            out.append((f"{n} {u} อยากสั่ง{p}จากจีน {q}", p, n, u, None, q))
            # 3 method BEFORE the question, glued quantity+unit
            out.append((f"อยากนำเข้า{p} {n}{u} ส่งเรือ {q}", p, n, u, "sea", q))
            # 4 question FIRST
            out.append((f"{q} อยากสั่ง{p} {n} {u}", p, n, u, None, q))
            # 5 no spaces anywhere
            out.append((f"อยากได้{p}จากจีน{n}{u}{q}", p, n, u, None, q))
            # 6 polite particle, method after quantity
            out.append((f"สั่ง{p} {n} {u} ส่งทางรถ {q} ครับ", p, n, u, "road", q))
    return out


ENTITY_CLAUSE_CASES = _cases_entity_clause()


class TestEntityQuestionBoundary(unittest.TestCase):
    """A — the product must never absorb the question/action clause."""

    def test_matrix_size(self):
        self.assertGreaterEqual(len(ENTITY_CLAUSE_CASES), 200)

    def test_product_carries_no_question_clause_contamination(self):
        """PRODUCT QUESTION-CLAUSE CONTAMINATION = 0."""
        bad = []
        for msg, product, _n, _u, _m, question in ENTITY_CLAUSE_CASES:
            ent = _compose(msg)[2]
            got = ent.get("product")
            if not got:
                continue
            # not one fragment of the question clause may appear in the
            # product, and the product may not be longer than the noun.
            for frag in re.findall(r"[ก-๙]{2,}", question):
                if frag in got and frag not in product:
                    bad.append((msg, got, frag))
                    break
        self.assertEqual(bad, [], f"{len(bad)} contaminated products, e.g. {bad[:3]}")

    def test_product_is_extracted_exactly(self):
        misses = [(m, _compose(m)[2].get("product"), p)
                  for m, p, _n, _u, _mm, _q in ENTITY_CLAUSE_CASES
                  if _compose(m)[2].get("product") != p]
        self.assertEqual(misses, [], f"{len(misses)} product misses, e.g. {misses[:3]}")

    def test_current_turn_entity_cannot_be_dropped(self):
        """Whatever family wins, the quantity / unit / method / question
        the turn stated are all still present."""
        dropped = []
        for msg, product, n, u, method, _q in ENTITY_CLAUSE_CASES:
            ent = _compose(msg)[2]
            if ent.get("quantity") != n or ent.get("quantity_unit") != u:
                dropped.append((msg, "quantity", ent.get("quantity"), ent.get("quantity_unit")))
            elif method and ent.get("method") != method:
                dropped.append((msg, "method", ent.get("method")))
            elif not ent.get("question_span"):
                dropped.append((msg, "question", None))
            elif ent.get("product") != product:
                dropped.append((msg, "product", ent.get("product")))
        self.assertEqual(dropped, [], f"{len(dropped)} dropped facts, e.g. {dropped[:3]}")

    def test_owner_reported_examples(self):
        for msg, product, n, u, method, qkind in [
                ("อยากสั่งรองเท้าจากจีน 30 คู่ ส่งเรือ ราคาเท่าไหร่", "รองเท้า", 30, "คู่", "sea", "PRICE"),
                ("อยากนำเข้าชั้นวางของ 10 ชิ้น คิดค่าส่งยังไง", "ชั้นวางของ", 10, "ชิ้น", None, "PRICE"),
                ("สั่งกล่องพลาสติก 5 ลัง ถึงไทยกี่วัน", "กล่องพลาสติก", 5, "ลัง", None, "DURATION"),
                ("อยากได้เครื่องซีลถุง 3 เครื่อง ส่งทางรถได้ไหม", "เครื่องซีลถุง", 3, "เครื่อง",
                 "road", "METHOD_ELIGIBILITY")]:
            with self.subTest(msg=msg):
                ent = _compose(msg)[2]
                self.assertEqual(ent.get("product"), product)
                self.assertEqual(ent.get("quantity"), n)
                self.assertEqual(ent.get("quantity_unit"), u)
                self.assertEqual(ent.get("question_kind"), qkind)
                if method:
                    self.assertEqual(ent.get("method"), method)

    def test_a_whole_message_question_is_never_split(self):
        """No entity to protect -> no split, so a pure question keeps
        its text and can never lose meaning to this mechanism."""
        for m in ["ส่งถึงบ้านไหม", "ราคาเท่าไหร่", "กี่วันถึงไทย", "คิดค่าส่งยังไง"]:
            with self.subTest(m=m):
                ent = _compose(m)[2]
                self.assertIsNone(ent.get("product"), m)

    def test_a_statement_with_no_question_is_never_split(self):
        for m in ["อยากสั่งรองเท้าจากจีน", "เป็นชั้นวางของ", "อยากสั่งของเล่นจากจีน"]:
            with self.subTest(m=m):
                entity, question = split_question_clause(m)
                self.assertEqual(entity, m)
                self.assertEqual(question, "")

    def test_typo_in_the_product_still_never_contaminates(self):
        """A typo'd product noun may not be canonicalised — but it must
        still never carry the question clause."""
        for typo in ["รองเท่า", "ชั้นวางของง", "กล่องพลาสติค", "เกาอี้สำนักงาน"]:
            msg = f"อยากสั่ง{typo}จากจีน 10 ชิ้น ราคาเท่าไหร่"
            with self.subTest(msg=msg):
                got = _compose(msg)[2].get("product") or ""
                self.assertNotIn("ราคา", got)
                self.assertNotIn("เท่าไหร่", got)


# ── B. cancellation vs withdrawal ────────────────────────────────────
CANCEL_OBJECTS = ["บิล", "บิลสั่งซื้อ", "คำสั่งซื้อ", "ออเดอร์", "order"]
CANCEL_Q_TAILS = ["ได้ไหม", "ได้ไหมคะ", "ได้หรือเปล่า", "ได้หรือไม่ครับ", "ได้มั้ย"]
WITHDRAW_VERBS = ["อยากถอนเงิน", "ถอนเครดิต", "ขอถอนยอด", "ถอนเงิน", "จะถอนยังไง"]
WITHDRAW_TAILS = ["", "ยังไง", "ในระบบ", "ได้ไหม", "ครับ"]


def _cancellation_cases():
    """>=100 contrast cases: policy shapes, operation shapes, and pure
    withdrawal shapes, each generated from its own dimensions."""
    out = []
    for obj in CANCEL_OBJECTS:                       # 25 POLICY
        for tail in CANCEL_Q_TAILS:
            out.append((f"ยกเลิก{obj}{tail}", "CANCELLATION_POLICY"))
    for obj in CANCEL_OBJECTS:                       # 25 OPERATION (imperative)
        for lead in ["ช่วย", "รบกวน", "ขอให้", "ต้องการให้", "จัดการ"]:
            out.append((f"{lead}ยกเลิก{obj} POS_TEST_001 ให้หน่อย", "CANCELLATION_OPERATION"))
    for obj in CANCEL_OBJECTS:                       # 25 OPERATION (bill named)
        for bill in ["POS_TEST_001", "PO318220260806008", "POS9001", "PA2026001", "POS_TEST_777"]:
            out.append((f"ยกเลิก{obj} {bill}", "CANCELLATION_OPERATION"))
    for verb in WITHDRAW_VERBS:                      # 25 WITHDRAWAL
        for tail in WITHDRAW_TAILS:
            out.append((f"{verb}{tail}", "PURCHASE_WITHDRAWAL"))
    # the owner's own adversarial list
    out += [("ยกเลิกบิลได้ไหม", "CANCELLATION_POLICY"),
            ("ยกเลิกคำสั่งซื้อได้ไหม", "CANCELLATION_POLICY"),
            ("ยกเลิกออเดอร์ได้หรือเปล่า", "CANCELLATION_POLICY"),
            ("ช่วยยกเลิกบิล POS_TEST_001", "CANCELLATION_OPERATION"),
            ("อยากถอนเงิน", "PURCHASE_WITHDRAWAL"),
            ("ถอนเครดิตยังไง", "PURCHASE_WITHDRAWAL"),
            ("ขอถอนยอดในระบบ", "PURCHASE_WITHDRAWAL"),
            ("ยกเลิกการถอนเงินได้ไหม", "CANCELLATION_POLICY"),
            ("ถอนเงินแล้วอยากยกเลิก", "CANCELLATION_POLICY"),
            ("อยากยกเลิกบิลแล้วถอนเงินคืนได้ไหม", "CANCELLATION_POLICY"),
            ("ถอนเงินค่าขนส่งยังไง", "SHIPPING_WITHDRAWAL"),
            ("ถอนเครดิตขนส่งได้ไหม", "SHIPPING_WITHDRAWAL")]
    return out


CANCELLATION_CASES = _cancellation_cases()


class TestCancellationWithdrawalDiscrimination(unittest.TestCase):
    """B — the two must never route into each other."""

    def test_matrix_size(self):
        self.assertGreaterEqual(len(CANCELLATION_CASES), 100)

    def test_every_case_resolves_to_its_own_family(self):
        wrong = [(m, interpret(m, []).intent_family, exp)
                 for m, exp in CANCELLATION_CASES
                 if interpret(m, []).intent_family != exp]
        self.assertEqual(wrong, [], f"{len(wrong)} misroutes, e.g. {wrong[:3]}")

    def test_cancellation_to_withdrawal_false_route_is_zero(self):
        bad = [m for m, exp in CANCELLATION_CASES
               if exp.startswith("CANCELLATION")
               and interpret(m, []).intent_family.endswith("WITHDRAWAL")]
        self.assertEqual(bad, [], f"cancellation->withdrawal: {bad[:5]}")

    def test_withdrawal_to_cancellation_false_route_is_zero(self):
        bad = [m for m, exp in CANCELLATION_CASES
               if exp.endswith("WITHDRAWAL")
               and interpret(m, []).intent_family.startswith("CANCELLATION")]
        self.assertEqual(bad, [], f"withdrawal->cancellation: {bad[:5]}")

    def test_no_cancellation_turn_consults_the_llm(self):
        """The root cause of G was that the deterministic tier had NO
        opinion, so the gated LLM answered PURCHASE_WITHDRAWAL. Every
        cancellation turn must now be resolved deterministically."""
        for m, exp in CANCELLATION_CASES:
            if not exp.startswith("CANCELLATION"):
                continue
            with self.subTest(m=m):
                self.assertEqual(interpret(m, []).source, "deterministic", m)
                self.assertGreaterEqual(interpret(m, []).confidence, 0.5, m)

    def test_bare_journey_cancel_is_still_neither(self):
        """A conversational 'never mind' keeps belonging to the frame
        reject path, not to the cancellation families."""
        for m in ["ยกเลิก", "ยกเลิกเลย", "ไม่เอาแล้ว", "พอแล้วค่ะ", "ยกเลิกรายการ"]:
            with self.subTest(m=m):
                self.assertIsNone(classify_cancellation(m), m)
                self.assertNotIn(interpret(m, []).intent_family,
                                 ("CANCELLATION_POLICY", "CANCELLATION_OPERATION"), m)

    def test_policy_and_operation_state_the_same_business_truth(self):
        from services.operational_change_flow import (
            CANCELLATION_POLICY_STATEMENT, CANCELLATION_POLICY_ANSWER, _KINDS)
        ack = [k[3] for k in _KINDS if k[0] == "cancel_purchase_bill"][0]
        self.assertIn(CANCELLATION_POLICY_STATEMENT, ack)
        self.assertIn(CANCELLATION_POLICY_STATEMENT, CANCELLATION_POLICY_ANSWER)

    def test_policy_answer_requires_no_identifier(self):
        from services.operational_change_flow import CANCELLATION_POLICY_ANSWER
        self.assertIsNone(re.search(r"รหัสลูกค้า|เลขบิล|เลขออเดอร์|เลขที่บิล",
                                    CANCELLATION_POLICY_ANSWER))


# ── C. unit fidelity ─────────────────────────────────────────────────
WEIGHTY = [("50 kg", None), ("500 กรัม", None)]   # weights are NOT count quantities


def _unit_cases():
    """>=100 quantity/unit fidelity cases."""
    out = []
    for u in UNITS:
        for n in QUANTITIES:
            out.append((f"อยากสั่งของจากจีน {n} {u}", n, u))
    for u in UNITS:                                  # glued, no space
        out.append((f"อยากสั่งของจากจีน{QUANTITIES[0]}{u}", QUANTITIES[0], u))
    return out


UNIT_CASES = _unit_cases()


class TestSuppliedUnitEchoFidelity(unittest.TestCase):
    """C — the unit the customer supplied is never replaced."""

    def test_matrix_size(self):
        self.assertGreaterEqual(len(UNIT_CASES), 100)

    def test_typed_quantity_is_extracted(self):
        bad = [(m, _compose(m)[2].get("quantity"), _compose(m)[2].get("quantity_unit"))
               for m, n, u in UNIT_CASES
               if _compose(m)[2].get("quantity") != n or _compose(m)[2].get("quantity_unit") != u]
        self.assertEqual(bad, [], f"{len(bad)} typed-quantity misses, e.g. {bad[:3]}")

    def test_supplied_unit_echo_fidelity_is_total(self):
        """SUPPLIED UNIT ECHO FIDELITY = 100%."""
        bad = []
        for m, n, u in UNIT_CASES:
            ent = _compose(m)[2]
            reply = import_interest_reply(ent.get("product"), quantity=ent.get("quantity"),
                                          method=ent.get("method"), unit=ent.get("quantity_unit"))
            if f"{n} {u}" not in reply:
                bad.append((m, reply))
        self.assertEqual(bad, [], f"{len(bad)} unit-echo failures, e.g. {bad[:3]}")

    def test_generic_fallback_only_when_no_unit_was_supplied(self):
        reply = frame_ack_reply(Frame(product="รองเท้า", quantity=20), changed="none")
        self.assertIn("20 ชิ้น", reply)
        reply2 = frame_ack_reply(Frame(product="รองเท้า", quantity=20, unit="คู่"), changed="none")
        self.assertIn("20 คู่", reply2)
        self.assertNotIn("20 ชิ้น", reply2)

    def test_unit_survives_a_conversation_round_trip(self):
        """The acknowledgement IS the state — a unit written into a reply
        must be readable back out of it on the next turn."""
        for u in UNITS:
            hist = [{"role": "user", "content": f"อยากสั่งของจากจีน 20 {u}"},
                    {"role": "assistant",
                     "content": frame_ack_reply(Frame(quantity=20, unit=u), changed="none")},
                    {"role": "user", "content": "เป็นชั้นวางของ"},
                    {"role": "assistant",
                     "content": frame_ack_reply(Frame(product="ชั้นวางของ", quantity=20, unit=u),
                                                changed="none")}]
            with self.subTest(u=u):
                f = derive_active_frame(hist)
                self.assertIsNotNone(f)
                self.assertEqual(f.quantity, 20)
                self.assertEqual(f.unit, u)

    def test_a_corrected_quantity_brings_its_own_unit(self):
        f = Frame(product="รองเท้า", quantity=20, unit="คู่")
        r = resolve_frame_correction("5 ลัง", f)
        self.assertEqual(r["op"], "SET_QUANTITY")
        self.assertEqual(r["quantity"], 5)
        self.assertEqual(r["unit"], "ลัง")

    def test_a_weight_is_never_read_as_a_count_quantity(self):
        for m, expected in [("อยากสั่งของจากจีน 50 kg", None),
                            ("อยากสั่งของจากจีน 500 กรัม", None)]:
            with self.subTest(m=m):
                self.assertEqual(_compose(m)[2].get("quantity_unit"), expected, m)


class TestNoKnownSlotIsEverReAsked(unittest.TestCase):
    """Cross-cutting: a slot stated in THIS turn is never asked for in
    the same turn's reply."""

    _ASK_PRODUCT_RE = re.compile(r"(แจ้ง|บอก|ระบุ|สนใจ).{0,20}(สินค้า|ประเภทสินค้า)|สินค้าอะไร")
    _ASK_QTY_RE = re.compile(r"(แจ้ง|บอก|ระบุ|ขอทราบ|รบกวน).{0,20}จำนวน|จำนวนเท่าไหร่")
    _ASK_METHOD_RE = re.compile(r"สนใจส่งทาง|ส่งทางรถหรือทางเรือ")

    def test_known_slot_reask_is_zero(self):
        bad = []
        for msg, product, n, u, method, _q in ENTITY_CLAUSE_CASES:
            ent = _compose(msg)[2]
            reply = import_interest_reply(ent.get("product"), quantity=ent.get("quantity"),
                                          method=ent.get("method"), unit=ent.get("quantity_unit"))
            if ent.get("product") and self._ASK_PRODUCT_RE.search(reply):
                bad.append((msg, "product", reply))
            elif ent.get("quantity") and self._ASK_QTY_RE.search(reply):
                bad.append((msg, "quantity", reply))
            elif ent.get("method") and self._ASK_METHOD_RE.search(reply):
                bad.append((msg, "method", reply))
        self.assertEqual(bad, [], f"{len(bad)} known-slot re-asks, e.g. {bad[:3]}")


if __name__ == "__main__":
    unittest.main()
