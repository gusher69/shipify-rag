import unittest

from tests.golden.golden_assertions import CaseResult, TurnResult, evaluate_assertions


def _cr(**turn_kwargs) -> CaseResult:
    defaults = dict(turn_index=0, question="q", actual_route=None, actual_action=None, actual_answer="")
    defaults.update(turn_kwargs)
    return CaseResult(golden_id="GOLDEN-TEST", turns=[TurnResult(**defaults)])


class TestRouteActionAssertions(unittest.TestCase):
    def test_route_equals_pass(self):
        cr = _cr(actual_route="API")
        out = evaluate_assertions([{"type": "route_equals", "value": "API"}], cr)
        self.assertTrue(out[0].passed)

    def test_route_equals_fail(self):
        cr = _cr(actual_route="RAG")
        out = evaluate_assertions([{"type": "route_equals", "value": "API"}], cr)
        self.assertFalse(out[0].passed)

    def test_action_equals_case_insensitive(self):
        cr = _cr(actual_action="GetDataCustomer")
        out = evaluate_assertions([{"type": "action_equals", "value": "getdatacustomer"}], cr)
        self.assertTrue(out[0].passed)

    def test_similar_looking_answer_does_not_mask_wrong_action(self):
        # Regression for GOLDEN-024: a plausible-looking clarification reply
        # must NOT make action_equals pass if the wrong backend action was
        # actually selected.
        cr = _cr(actual_route="WORKFLOW", actual_action="searchdatashipmentlist",
                  actual_answer="กรุณาแจ้งรหัสลูกค้าค่ะ")
        out = evaluate_assertions(
            [{"type": "route_equals", "value": "WORKFLOW"},
             {"type": "action_equals", "value": "searchdatatracking"}], cr)
        self.assertTrue(out[0].passed)
        self.assertFalse(out[1].passed)

    def test_route_not_equals(self):
        cr = _cr(actual_route="RAG")
        out = evaluate_assertions([{"type": "route_not_equals", "value": "RAG"}], cr)
        self.assertFalse(out[0].passed)

    def test_action_not_equals(self):
        cr = _cr(actual_action="searchdataorderlist")
        out = evaluate_assertions([{"type": "action_not_equals", "value": "searchdataorderlist"}], cr)
        self.assertFalse(out[0].passed)


class TestAnswerTextAssertions(unittest.TestCase):
    def test_answer_contains(self):
        cr = _cr(actual_answer="พบคูปอง 2 รายการค่ะ")
        out = evaluate_assertions([{"type": "answer_contains", "value": "คูปอง"}], cr)
        self.assertTrue(out[0].passed)

    def test_answer_not_contains(self):
        cr = _cr(actual_answer="พบคูปอง 2 รายการค่ะ")
        out = evaluate_assertions([{"type": "answer_not_contains", "value": "error"}], cr)
        self.assertTrue(out[0].passed)

    def test_no_raw_json_detects_leak(self):
        cr = _cr(actual_answer='{"status": "success", "code": None}')
        out = evaluate_assertions([{"type": "no_raw_json"}], cr)
        self.assertFalse(out[0].passed)

    def test_no_raw_json_clean_answer_passes(self):
        cr = _cr(actual_answer="ยอดเงิน Purchase Wallet 21.94 บาทค่ะ")
        out = evaluate_assertions([{"type": "no_raw_json"}], cr)
        self.assertTrue(out[0].passed)

    def test_no_internal_source_detects_leak(self):
        cr = _cr(actual_answer="ตามที่ระบุใน Source: policy.xlsx หน้า 3")
        out = evaluate_assertions([{"type": "no_internal_source"}], cr)
        self.assertFalse(out[0].passed)

    def test_no_internal_architecture_label_detects_leak(self):
        cr = _cr(actual_answer="📦 ข้อมูลเฉพาะลูกค้า (ERP): ...")
        out = evaluate_assertions([{"type": "no_internal_architecture_label"}], cr)
        self.assertFalse(out[0].passed)

    def test_safety_assertions_scan_all_turns_by_default(self):
        cr = CaseResult(golden_id="GOLDEN-TEST", turns=[
            TurnResult(turn_index=0, question="q1", actual_route="API", actual_action="a",
                       actual_answer="clean answer"),
            TurnResult(turn_index=1, question="q2", actual_route="API", actual_action="a",
                       actual_answer='leaked {"raw": "json"}'),
        ])
        out = evaluate_assertions([{"type": "no_raw_json"}], cr)
        self.assertFalse(out[0].passed)
        self.assertEqual(out[0].turn_index, 1)


class TestContextAndClarificationAssertions(unittest.TestCase):
    def test_requires_clarification(self):
        cr = _cr(actual_route="WORKFLOW")
        out = evaluate_assertions([{"type": "requires_clarification"}], cr)
        self.assertTrue(out[0].passed)

    def test_context_reused_pass(self):
        cr = _cr(collected_parameters={"CustCode": "FT3182"})
        out = evaluate_assertions([{"type": "context_reused", "identifier": "FT3182"}], cr)
        self.assertTrue(out[0].passed)

    def test_context_reused_fail(self):
        cr = _cr(collected_parameters={"CustCode": "SP1014"})
        out = evaluate_assertions([{"type": "context_reused", "identifier": "FT3182"}], cr)
        self.assertFalse(out[0].passed)

    def test_no_fabricated_identifier_pass_when_no_execution(self):
        cr = _cr(erp_http_status=None)
        out = evaluate_assertions([{"type": "no_fabricated_identifier"}], cr)
        self.assertTrue(out[0].passed)

    def test_no_fabricated_identifier_fail_when_executed(self):
        cr = _cr(erp_http_status=200)
        out = evaluate_assertions([{"type": "no_fabricated_identifier"}], cr)
        self.assertFalse(out[0].passed)


class TestNotificationSafetyAssertions(unittest.TestCase):
    def test_notification_never_real_pass(self):
        cr = _cr(handoff_notification={"simulated_sent": True, "note": "SIMULATED ONLY -- no real call"})
        out = evaluate_assertions([{"type": "notification_never_real"}], cr)
        self.assertTrue(out[0].passed)

    def test_notification_never_real_fail_when_note_missing_simulated_marker(self):
        cr = _cr(handoff_notification={"simulated_sent": True, "note": "sent"})
        out = evaluate_assertions([{"type": "notification_never_real"}], cr)
        self.assertFalse(out[0].passed)

    def test_notification_simulated_sent_equals(self):
        cr = CaseResult(golden_id="GOLDEN-TEST", turns=[
            TurnResult(turn_index=0, question="q1", actual_route="HUMAN_HANDOFF", actual_action=None,
                       actual_answer="a", handoff_notification={"simulated_sent": True, "note": "SIMULATED ONLY"}),
            TurnResult(turn_index=1, question="q2", actual_route="HUMAN_HANDOFF", actual_action=None,
                       actual_answer="a", handoff_notification={"simulated_sent": False, "note": "duplicate suppressed"}),
        ])
        out = evaluate_assertions([
            {"type": "notification_simulated_sent_equals", "value": True, "turn_index": 0},
            {"type": "notification_simulated_sent_equals", "value": False, "turn_index": 1},
        ], cr)
        self.assertTrue(out[0].passed)
        self.assertTrue(out[1].passed)


class TestEngineIntegrity(unittest.TestCase):
    def test_unknown_assertion_type_raises(self):
        cr = _cr()
        with self.assertRaises(ValueError):
            evaluate_assertions([{"type": "not_a_real_assertion"}], cr)

    def test_no_golden_id_special_casing_in_module(self):
        import inspect
        from tests.golden import golden_assertions
        src = inspect.getsource(golden_assertions)
        self.assertNotIn("golden_id ==", src, "assertion engine must never branch on a specific golden_id")
        self.assertNotIn("cr.golden_id", src, "assertion engine must never read/branch on CaseResult.golden_id")


if __name__ == "__main__":
    unittest.main()
