# -*- coding: utf-8 -*-
"""PHASE 6 FINAL SAFETY CLOSURE (A/B/C/E) — private-inquiry authority.

The rule under test is the INVERSION:

    A PRIVATE INQUIRY REQUIRES POSITIVE PRIVATE-OWNERSHIP EVIDENCE.
    An LLM family name is EVIDENCE, never authorization.

Before this change the runtime asked "did the LLM say this looks private,
and does the wording miss my list of public exceptions?" — an open-ended
blocklist, where every unanticipated public phrasing became a demand for
the customer's identity. Now it asks "is there positive evidence this is
the customer's OWN record?" and answers publicly when there is none.

Measured here (all must be 0):
  PUBLIC -> PRIVATE FALSE POSITIVE
  PRIVATE -> PUBLIC FALSE NEGATIVE
  STALE PRIVATE TAKEOVER
"""
import unittest

from services.decision_engine import (
    _private_ownership_evidence, _classify_private_state_inquiry,
)


# ── 50 public service / capability questions ─────────────────────────
PUBLIC = [
    # the owner's own must-stay-public list
    "จัดส่งถึงหน้าบ้านไหม", "ส่งถึงบ้านหรือเปล่า", "ใช้ขนส่งอะไร",
    "ของถึงไทยกี่วัน", "ถอนเงินใช้เวลากี่วัน", "มีค่าจัดส่งเท่าไหร่",
    "ถ้าของถึงโกดังแล้วทำยังไง", "สามารถเปลี่ยนที่อยู่ได้ไหม", "ออกใบกำกับได้ไหม",
    # paraphrases deliberately outside the old exception regexes
    "ส่งเข้าคอนโดได้ไหมคะ", "ไปส่งต่างจังหวัดด้วยไหม", "มีบริการส่งถึงที่ไหม",
    "ขนส่งเอกชนมีเจ้าไหนบ้าง", "เลือกขนส่งเองได้ไหม", "ส่งของเข้าโกดังยังไง",
    "ที่อยู่โกดังจีนอยู่ตรงไหน", "โกดังไทยเปิดกี่โมง", "วันหยุดส่งของไหม",
    "ค่าขนส่งคิดยังไง", "ทางเรือกับทางรถต่างกันยังไง", "ทางเรือใช้เวลานานไหม",
    "นำเข้าขั้นต่ำเท่าไหร่", "มีขั้นต่ำในการสั่งไหม", "รับนำเข้าของเหลวไหม",
    "แบตเตอรี่ส่งได้ไหม", "สินค้าต้องห้ามมีอะไรบ้าง", "ตีลังไม้คิดเงินเพิ่มไหม",
    "มีบริการรีแพ็คไหม", "เหมารถคิดราคายังไง", "CBM คำนวณยังไง",
    "กี่กิโลถึงจะคุ้มส่งทางเรือ", "ชำระเงินมีกี่ช่องทาง", "จ่ายบัตรเครดิตได้ไหม",
    "คูปองใช้ยังไง", "คูปองหมดอายุกี่วัน", "ขอใบกำกับภาษีต้องทำยังไง",
    "VAT คิดกี่เปอร์เซ็นต์", "เปลี่ยนขนส่งภายหลังได้ไหม", "ยกเลิกบิลได้ไหม",
    "เคลมสินค้าต้องใช้เอกสารอะไร", "ถ้าของเสียหายทำยังไง", "ติดต่อเจ้าหน้าที่ยังไง",
    "มีไลน์ให้ติดต่อไหม", "เปิดบริการวันไหนบ้าง", "รับสั่งของจาก 1688 ไหม",
    "สั่งของจากเถาเป่าได้ไหม", "ผมอยากทราบว่าส่งถึงบ้านไหม",
    "เราต้องเตรียมอะไรบ้างก่อนสั่ง", "ลูกค้าใหม่เริ่มยังไง",
    "ส่งของไปต่างประเทศได้ไหม",
]

# ── 50 genuine private-ownership questions ───────────────────────────
PRIVATE = [
    # the owner's own required set
    "ยอดเงินในกระเป๋าฉันเหลือเท่าไหร่", "ของฉันถึงไทยหรือยัง",
    "ออเดอร์ของฉันจ่ายแล้วหรือยัง", "บิล POS_TEST_001 ของฉันสถานะอะไร",
    "คูปองของฉันมีอะไรบ้าง",
    # paraphrases
    "ของผมถึงโกดังหรือยังครับ", "พัสดุของฉันอยู่ไหนแล้ว",
    "บิลของผมชำระไปหรือยัง", "ยอดของฉันเข้าระบบหรือยัง",
    "เครดิตของฉันเหลือเท่าไหร่", "วอลเล็ทของผมมีเงินเท่าไหร่",
    "แทรคของฉันอัปเดตหรือยัง", "ล็อตของผมออกจากจีนหรือยัง",
    "สินค้าของฉันเข้าไทยวันไหน", "รายการของผมมีกี่บิล",
    "บัญชีของฉันผูกกับเบอร์ไหน", "ใบกำกับของฉันออกหรือยัง",
    "ใบเสร็จของผมโหลดได้ไหม", "คำสั่งซื้อของฉันถึงขั้นตอนไหน",
    "เงินของผมคืนมาหรือยัง", "กระเป๋าฉันถูกหักเงินไปเท่าไหร่",
    "สถานะของฉันตอนนี้เป็นยังไง", "ของผมค้างที่ด่านหรือเปล่า",
    "ออเดอร์ฉันร้านส่งหรือยัง", "บิลขนส่งของผมมีกี่ใบ",
    "เลขบิลของฉันคืออะไร", "แพ็กเกจของผมถึงไหนแล้ว",
    "ของฉันตีลังไม้ไปหรือยัง", "บิลของฉันรวมเหมารถหรือยัง",
    "ยอดค้างของผมเท่าไหร่", "รายการของฉันที่ยังไม่จ่ายมีอะไรบ้าง",
    "ของผมที่สั่งเมื่อวานถึงไหน", "คูปองของผมใช้กับบิลนี้ได้ไหม",
    "เครดิตสั่งซื้อของฉันถอนได้เท่าไหร่", "เงินของฉันถอนถึงบัญชีหรือยัง",
    "สินค้าของผมครบไหม", "ของฉันหายไปหนึ่งกล่อง",
    "พัสดุของผมเสียหายต้องทำยังไง", "บิลของฉันโดนตีกลับหรือเปล่า",
    "ออเดอร์ของผมยกเลิกไปแล้วใช่ไหม", "ของฉันเปลี่ยนเป็นทางเรือหรือยัง",
    "ที่อยู่จัดส่งของฉันอัปเดตหรือยัง", "แทรคไทยของผมคือเลขอะไร",
    "ยอดของผมวันนี้เข้าไหม", "บัญชีของผมมีกี่บิลค้าง",
    "วอลเล็ทฉันถอนได้เมื่อไหร่", "ของผมรอยืนยันเข้าไทยอยู่ใช่ไหม",
    "สินค้าของฉันอยู่ขั้นตอนไหนแล้ว", "รายการสั่งซื้อของผมล่าสุดคือบิลไหน",
    "บิลของฉันมีค่าใช้จ่ายเพิ่มไหม", "ของผมมาถึงยัง",
]

# ── 30 stale private history, then a NEW public topic ────────────────
STALE_THEN_PUBLIC = PUBLIC[:30]
_STALE_HISTORY = [
    {"role": "user", "content": "ขอเช็กออเดอร์ของฉันหน่อย"},
    {"role": "assistant", "content": "รบกวนขอเลขบิลสั่งซื้อหน่อยนะคะ"},
]

# ── 30 private contextual follow-ups (bare answers in a private journey)
FOLLOWUPS = [
    "POS_TEST_001", "POS123456", "PO987654", "123456", "7788990",
    "FT31822026", "SA5061001", "SP7521002", "FE1145003", "PE2024001",
    "POS_TEST_002", "PO111222", "334455", "998877", "TH1234567890",
    "POS-2024-01", "PO/9911", "556677", "PA778899", "PE445566",
    "POS_TEST_003", "PO222333", "667788", "889900", "FT9988776",
    "SA1122334", "SP5566778", "FE2233445", "PE6677889", "112233",
]

# ── 20 typo / noisy public + 20 typo / noisy private ─────────────────
NOISY_PUBLIC = [
    "จัดส่งถึงหน้าบ้านไหมคับ", "สงถึงบ้านมั้ย", "ค่าสงเท่าไหร่",
    "ถึงไทยกี่วันคับ", "ถอนเงินนานมั้ย", "ขนสงใช้เจ้าไหน",
    "ออกใบกำกับไดไหม", "เปลียนที่อยู่ได้ปะ", "มีขันต่ำไหม",
    "ทางเรือนานมั้ยครับ", "ตีลังไม้ได้ปะ", "คูปองใช้ไง",
    "โกดังเปิดกี่โมงอะ", "รับของเหลวปะ", "แบตส่งได้ปะ",
    "vat กี่%", "เหมารถเท่าไร", "cbm คิดไง",
    "สั่งจาก1688ได้มะ", "จ่ายบัตรได้ปะ",
]
NOISY_PRIVATE = [
    "ของฉันถึงยัง", "ของผมถึงไทยยัง", "บิลผมจ่ายยัง",
    "ยอดฉันเหลือเท่าไร", "กระเป๋าผมมีเท่าไร", "แทรคฉันอัพเดทยัง",
    "ออเดอร์ฉันไปถึงไหน", "ของผมค้างมั้ย", "เครดิตฉันเหลือเท่าไร",
    "วอลเลทผมถอนได้ยัง", "คูปองฉันมีอะไรมั่ง", "ใบกำกับฉันออกยัง",
    "สินค้าผมครบมั้ย", "พัสดุฉันหายอะ", "บิลฉันมีกี่ใบ",
    "รายการผมค้างกี่บิล", "ของฉันเปลี่ยนเรือยัง", "ยอดผมเข้ายัง",
    "เงินฉันคืนยัง", "ล็อตผมออกจีนยัง",
]


def _is_private(msg, history=None, ctx=None):
    """The system treats a turn as private if EITHER the deterministic
    recogniser fires, or the inverted evidence gate finds positive
    ownership evidence. (The LLM family name alone can no longer.)"""
    return (_classify_private_state_inquiry(msg) is not None
            or _private_ownership_evidence(msg, history, ctx or {}) is not None)


class TestPublicNeverBecomesPrivateFromLlmFamily(unittest.TestCase):
    """PUBLIC -> PRIVATE FALSE POSITIVE = 0, measured on the gate this
    change owns: the LLM-family synthesis path."""

    def test_no_public_question_yields_ownership_evidence(self):
        bad = [m for m in PUBLIC + NOISY_PUBLIC
               if _private_ownership_evidence(m, None, {}) is not None]
        self.assertEqual(bad, [], f"{len(bad)} public turns produced ownership evidence:\n"
                                  + "\n".join(bad[:15]))

    def test_public_corpus_size(self):
        self.assertGreaterEqual(len(PUBLIC), 50)
        self.assertGreaterEqual(len(NOISY_PUBLIC), 20)


class TestGenuinePrivateStillRecognised(unittest.TestCase):
    """PRIVATE -> PUBLIC FALSE NEGATIVE = 0."""

    def test_every_private_ownership_question_is_recognised(self):
        bad = [m for m in PRIVATE + NOISY_PRIVATE if not _is_private(m)]
        self.assertEqual(bad, [], f"{len(bad)} private turns were NOT recognised:\n"
                                  + "\n".join(bad[:15]))

    def test_private_corpus_size(self):
        self.assertGreaterEqual(len(PRIVATE), 50)
        self.assertGreaterEqual(len(NOISY_PRIVATE), 20)


class TestStalePrivateHistoryDoesNotCapturePublicTurns(unittest.TestCase):
    """STALE PRIVATE TAKEOVER = 0 — current explicit intent beats stale
    history."""

    def test_new_public_topic_after_a_private_prompt_stays_public(self):
        bad = [m for m in STALE_THEN_PUBLIC
               if _private_ownership_evidence(m, _STALE_HISTORY, {}) is not None]
        self.assertEqual(bad, [], f"{len(bad)} stale takeovers:\n" + "\n".join(bad[:15]))

    def test_corpus_size(self):
        self.assertGreaterEqual(len(STALE_THEN_PUBLIC), 30)


class TestPrivateContextualFollowUp(unittest.TestCase):
    """A bare value answering an already-established private journey stays
    private (owner section C)."""

    def test_bare_answers_inside_a_private_journey_stay_private(self):
        bad = [m for m in FOLLOWUPS
               if _private_ownership_evidence(m, _STALE_HISTORY, {}) is None]
        self.assertEqual(bad, [], f"{len(bad)} private follow-ups lost context:\n"
                                  + "\n".join(bad[:15]))

    def test_the_same_bare_answers_without_a_private_journey_are_not_private(self):
        """Without the preceding private prompt, a bare token must not
        manufacture a private inquiry on its own."""
        loose = [m for m in FOLLOWUPS if not m[0].isalpha()]   # pure-digit shapes
        bad = [m for m in loose if _private_ownership_evidence(m, None, {}) is not None]
        self.assertEqual(bad, [], "\n".join(bad[:10]))

    def test_corpus_size(self):
        self.assertGreaterEqual(len(FOLLOWUPS), 30)


class TestTargetedCorpusTotal(unittest.TestCase):
    def test_at_least_200_targeted_cases(self):
        total = (len(PUBLIC) + len(PRIVATE) + len(STALE_THEN_PUBLIC)
                 + len(FOLLOWUPS) + len(NOISY_PUBLIC) + len(NOISY_PRIVATE))
        self.assertGreaterEqual(total, 200, f"only {total} targeted cases")


if __name__ == "__main__":
    unittest.main()
