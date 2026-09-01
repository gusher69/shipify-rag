"""P5.2 — a self-contained current-turn request stays authoritative;
history/context must not rewrite or weaken it.

Root cause of the real-LINE 4-turn repro was a HISTORY-INDEPENDENT false
spell correction: "การสั่ง" ("the ordering", the ordinary Thai gerund
การ + verb) was fuzzy-corrected into the vocabulary term "ฝากสั่ง" (the
specific proxy-buy service), silently rewriting a generic ordering-process
question and producing a false no_information. Deterministic; reproduces
on an empty conversation.
"""
import unittest

from rag.spell_correction import correct_query
from rag.query_resolution import resolve_conversation, accumulate_entities

_Q = "ขอขั้นตอนในการสั่งซื้อสินค้าหน่อย"          # "please give me the steps to order goods"
_INTERVENING = [
    {"role": "user", "content": "ค่าขนส่งเท่าไหร่"},
    {"role": "assistant", "content": "ทางรถ 35 บาท/กก. ทางเรือ 19 บาท/กก. ค่ะ"},
    {"role": "user", "content": "โกดังอ่อนนุชเปิดกี่โมง"},
    {"role": "assistant", "content": "จันทร์-ศุกร์ 9:00-18:00 น. ค่ะ"},
]


class SpellCorrectionDoesNotInjectFaksang(unittest.TestCase):
    def test_generic_ordering_is_not_rewritten_to_faksang(self):
        r = correct_query(_Q)
        self.assertNotIn("ฝากสั่ง", r["corrected_query"])
        self.assertEqual(r["corrected_query"], _Q)
        self.assertEqual(r["corrections"], [])

    def test_other_การสั่ง_phrasings_are_left_alone(self):
        for q in ("ขั้นตอนการสั่งของจากจีน", "อยากทราบการสั่งซื้อ",
                  "การสั่งซื้อสินค้าจากจีนทำยังไง"):
            out = correct_query(q)["corrected_query"]
            self.assertNotIn("ฝากสั่ง", out, msg=q)

    def test_customer_may_still_name_the_faksang_service(self):
        # an EXPLICIT "ฝากสั่ง" from the customer is preserved untouched
        for q in ("ขอขั้นตอนฝากสั่งซื้อสินค้า", "ฝากสั่งของจากจีนยังไง"):
            self.assertIn("ฝากสั่ง", correct_query(q)["corrected_query"], msg=q)

    def test_unrelated_fuzzy_guards_still_active(self):
        # the pre-existing guards in the same list must not have regressed
        for q in ("ขั้นตอนการนำเข้าสินค้าจากจีน", "เริ่มนำเข้าสินค้าจากจีนยังไง",
                  "ทางรถกับทางเรือกี่วัน", "สนใจนำเข้าสินค้าจากจีน"):
            out = correct_query(q)["corrected_query"]
            self.assertNotIn("เรท", out, msg=q)
            self.assertNotIn("นำเข้าจีน", out.replace("นำเข้าจีน", "") or out, msg=q)


class HistoryDoesNotRewriteASelfContainedTurn(unittest.TestCase):
    def test_resolver_uses_the_self_contained_query_as_is(self):
        res = resolve_conversation(_Q, _INTERVENING)
        self.assertEqual(res["resolved_question"], _Q)
        self.assertIsNone(res["followup_type"])

    def test_spell_correction_is_idempotent_under_carried_context(self):
        carried = accumulate_entities(_INTERVENING)     # {'topic': 'โกดัง', 'attribute': 'rate', ...}
        standalone = correct_query(_Q, carried_entities={})["corrected_query"]
        with_history = correct_query(_Q, carried_entities=carried)["corrected_query"]
        self.assertEqual(standalone, with_history)
        self.assertEqual(with_history, _Q)

    def test_idempotence_Q_then_unrelated_then_Q(self):
        # the resolved/corrected target for Q is the same the 2nd time,
        # regardless of the two unrelated answerable turns in between.
        first = correct_query(_Q)["corrected_query"]
        again_res = resolve_conversation(correct_query(_Q)["corrected_query"], _INTERVENING)
        self.assertEqual(again_res["resolved_question"], first)


if __name__ == "__main__":
    unittest.main()
