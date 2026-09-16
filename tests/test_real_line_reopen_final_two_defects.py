# -*- coding: utf-8 -*-
"""CUSTOMER 6-SOURCE MANUAL UAT REOPEN (2026-09-16) — three verified
shared-mechanism defects found by the owner's manual REAL LINE testing.
Two other reported items (warehouse-inbound honest fallback, duplicate-
bill/topup wording) were investigated against the authoritative source
and found to ALREADY match it exactly — no code change; see the session
report. A sixth (invoice concept vs document request) surfaced a genuine
conflict between two authoritative-source rows and is deliberately left
untouched (see report) rather than guessed.

1. TAOBAO HOMEPAGE / MARKDOWN-LINK ROUTING (link_conversion_flow.py):
   a URL wrapped in Markdown link syntax ("[www.taobao.com](https://
   www.taobao.com)") had its trailing ")" swept into the match, so
   classify_platform() found no known domain and the turn fell to the
   generic "unsupported domain" reply. Fixed by stripping wrapping/
   trailing punctuation from every extracted URL (never truncating a
   URL that legitimately ends in a parenthesised path) and by reading a
   bare "www.<platform>.com" mention (no scheme, no path) as the
   platform's home page, exactly like a real https:// home-page URL.

2. ANTI-LOOP COUNTER LEAKING ACROSS EXPLICIT PRIVATE-INTENT SWITCHES
   (decision_engine.py::_count_genuine_retries): a private-status
   inquiry ("ร้านส่งหรือยังคะ", "ขอแทรคไทยค่ะ", "วันนี้มีของเข้าไทยไหม",
   "คูปองผมมีไหม") resolves through the SEPARATE deterministic
   _classify_private_state_inquiry() classifier, never through
   _compose_intent_family()'s family table the retry-diversion check
   already consulted — so switching between different private-record
   questions counted every switch as a failed retry of the FIRST one,
   saturating max_retry after two turns and sending the customer's very
   first attempt at a brand-new private request straight to Human CS.
   Fixed by also consulting the SAME central private-state classifier
   already used everywhere else for this concept.

3. DAMAGE ("แตก") MISSING FROM THE CLAIM VOCABULARY, AND A SAME-MESSAGE
   FUTURE-CARRIER-PREFERENCE CLAUSE SILENTLY DROPPED
   (operational_change_flow.py): "แตก" (broken/shattered) — the
   commonest everyday wording for damaged goods — was missing from the
   missing_item_claim damage-signal regex (which already had "เสียหาย"),
   so "ของแตกมา..." fell through entirely to a bare no-info fallback.
   Added as a synonym of the SAME existing signal class. A same-message
   forward-looking carrier-preference mention ("...คราวหน้าส่งขนส่งอื่น
   ได้ไหม") is now acknowledged with one honest, non-committal extra
   sentence alongside the claim-evidence ask — never a promise the
   carrier will change, never silently dropped.

Test tier is pinned offline by tests/__init__.py — no paid API call.
"""
import unittest

from services.decision_engine import DecisionEngine
from services.link_conversion_flow import classify_link_request

CTX = {"channel": "line", "tenant_id": "default", "sample_source": "OWNER_TEST",
       "customer_context": {}, "developer_mode": True}


class TestTaobaoHomepageAndMarkdownLinkRouting(unittest.TestCase):

    def test_bare_domain_forms_are_platform_home(self):
        for text, platform in (("www.taobao.com", "taobao"), ("taobao.com", "taobao"),
                               ("tmall.com", "tmall"), ("1688.com", "1688")):
            with self.subTest(text=text):
                r = classify_link_request(text)
                self.assertEqual(r["state"], "PLATFORM_HOME_OR_NON_PRODUCT", (text, r))
                self.assertEqual(r["platform"], platform)

    def test_markdown_wrapped_homepage_url_is_recognised(self):
        r = classify_link_request("[www.taobao.com](https://www.taobao.com)")
        self.assertEqual(r["state"], "PLATFORM_HOME_OR_NON_PRODUCT")
        self.assertEqual(r["platform"], "taobao")

    def test_parenthesised_homepage_url_is_recognised(self):
        r = classify_link_request("ลองดูที่ (https://www.taobao.com) นะครับ")
        self.assertEqual(r["state"], "PLATFORM_HOME_OR_NON_PRODUCT")

    def test_real_product_urls_still_resolve_valid_even_when_wrapped(self):
        for text in ("https://item.taobao.com/item.htm?id=123456",
                    "[สินค้านี้](https://item.taobao.com/item.htm?id=123456)",
                    "https://item.taobao.com/item.htm?id=123456)"):
            with self.subTest(text=text):
                r = classify_link_request(text)
                self.assertEqual(r["state"], "VALID", (text, r))
                self.assertEqual(r["url"], "https://item.taobao.com/item.htm?id=123456")

    def test_end_to_end_reply_is_platform_guidance_not_generic_unsupported(self):
        eng = DecisionEngine()
        hist = [{"role": "user", "content": "ขอลิงก์เว็บ Taobao กับ Tmall หน่อย"},
                {"role": "assistant", "content": "ลิงก์เว็บไซต์หลักของแต่ละแพลตฟอร์มค่ะ"}]
        r = eng.decide("[www.taobao.com](https://www.taobao.com)", history=hist, context=dict(CTX))
        reply = (r.get("reply") or {}).get("text") or ""
        self.assertIn("Taobao", reply)
        self.assertNotIn("ยังไม่รองรับการแปลง", reply, "must not claim the domain is unsupported")


class TestAntiLoopCounterScopedToTheExplicitIntent(unittest.TestCase):

    def test_switching_private_intents_never_saturates_the_shared_counter(self):
        eng = DecisionEngine()
        hist = []
        for text in ("สินค้าจะเข้าไทยตอนไหน", "ร้านส่งหรือยังคะ", "ขอแทรคไทยค่ะ",
                     "วันนี้มีของเข้าไทยไหมคะ", "คูปองผมมีไหม"):
            r = eng.decide(text, history=hist, context=dict(CTX))
            reply = (r.get("reply") or {}).get("text") or ""
            with self.subTest(text=text):
                self.assertEqual((r.get("routing") or {}).get("type"), "WORKFLOW", (text, reply))
                self.assertIn("รหัสลูกค้า", reply, (text, reply))
                self.assertNotIn("ไม่สามารถขอข้อมูลที่จำเป็นได้ครบถ้วน", reply, (text, reply))
            hist = hist + [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]

    def test_a_genuinely_unanswered_repeated_ask_still_escalates(self):
        """The fix must not defeat the anti-loop guard itself — silence /
        off-topic non-answers to the SAME question still count. Uses a
        genuine non-answer ("ครับ") rather than re-sending the identical
        question text, since that literal phrase happens to keyword-
        match a real registered Business Action (searchdatashipmentlist)
        and is excluded from the count by the PRE-EXISTING, unrelated
        Business-Action-decisive-match check — not by this fix."""
        eng = DecisionEngine()
        hist = []
        replies = []
        for text in ("สินค้าจะเข้าไทยตอนไหน", "ครับ", "ครับ"):
            r = eng.decide(text, history=hist, context=dict(CTX))
            reply = (r.get("reply") or {}).get("text") or ""
            replies.append((r, reply))
            hist = hist + [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        self.assertEqual((replies[-1][0].get("routing") or {}).get("type"), "HUMAN_HANDOFF")


class TestDamageAndFutureCarrierPreference(unittest.TestCase):

    def test_broken_goods_wording_triggers_the_claim_flow(self):
        eng = DecisionEngine()
        for text in ("ของแตกมา แล้วคราวหน้าส่งขนส่งอื่นได้ไหม", "ของแตก รอบหน้าขอเปลี่ยนขนส่งได้ไหม",
                     "ของแตก", "สินค้าแตก"):
            with self.subTest(text=text):
                r = eng.decide(text, history=[], context=dict(CTX))
                reply = (r.get("reply") or {}).get("text") or ""
                self.assertIn("เลขบิลสั่งซื้อ", reply, (text, reply))
                self.assertNotIn("ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ", reply, (text, reply))

    def test_future_carrier_preference_is_acknowledged_not_dropped(self):
        eng = DecisionEngine()
        for text in ("ของแตกมา แล้วคราวหน้าส่งขนส่งอื่นได้ไหม", "ของเสียหาย คราวหน้าส่งเจ้าอื่นได้ปะ"):
            with self.subTest(text=text):
                r = eng.decide(text, history=[], context=dict(CTX))
                reply = (r.get("reply") or {}).get("text") or ""
                self.assertIn("เลขบิลสั่งซื้อ", reply, "the claim evidence ask stays primary")
                self.assertIn("เปลี่ยนบริษัทขนส่ง", reply, "the carrier preference must be acknowledged")
                self.assertNotIn("เปลี่ยนขนส่งให้แล้ว", reply, "must never promise it already happened")
                self.assertNotIn("จะเปลี่ยนขนส่งให้แน่นอน", reply, "must never promise future completion")

    def test_plain_claim_with_no_carrier_clause_is_unaffected(self):
        eng = DecisionEngine()
        r = eng.decide("ได้รับสินค้าไม่ครบ เคลมยังไงคะ", history=[], context=dict(CTX))
        reply = (r.get("reply") or {}).get("text") or ""
        self.assertIn("เลขบิลสั่งซื้อ", reply)
        self.assertNotIn("เปลี่ยนบริษัทขนส่ง", reply, "no carrier clause was in the message")


if __name__ == "__main__":
    unittest.main()
