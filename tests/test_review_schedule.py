"""独立审核并行的正确性：上下文不串版，任何失败或不确定仍阻止通过。"""

import copy
import json
import threading
import unittest

from soutui_image.review_schedule import run_independent_checks
from soutui_image.rule_runtime import current_policy, use_rules
from soutui_image.rules_bundle import builtin_bundle, RuleBundle
from soutui_image.stage_e_codex_review import combine_button_check
from test_stage_e_review import response


class ReviewScheduleTests(unittest.TestCase):
    def test_both_checks_overlap_and_inherit_same_pinned_rules(self):
        barrier = threading.Barrier(2)
        data = builtin_bundle().data
        data["version"] = "2099-01-01.1"
        bundle = RuleBundle.parse(json.dumps(data).encode())

        def check():
            barrier.wait(timeout=3)
            return current_policy()

        with use_rules(bundle):
            expected = current_policy()
            body, buttons, timing = run_independent_checks(check, check)
        self.assertEqual((body, buttons), (expected, expected))
        self.assertLess(timing["buttons_started"], timing["body_finished"])

    def test_parallel_keeps_same_rejection_and_uncertainty_as_sequential(self):
        for comparison, expected in (("match", "pass"), ("uncertain", "review"), ("mismatch", "reject")):
            report = response()
            check = copy.deepcopy(next(c for c in report["checks"] if c["feature"] == "扣位边缘与凸片"))
            check["comparison"] = comparison
            for parallel in (False, True):
                body, button, _ = run_independent_checks(lambda: report, lambda: check, parallel=parallel)
                self.assertEqual(combine_button_check(body, button, "001", "task/slot-0")["decision"], expected)

    def test_failure_in_either_check_cannot_return_a_pass(self):
        def fail():
            raise TimeoutError("保留断点，不能通过")

        for body, buttons in ((fail, lambda: {}), (lambda: {}, fail)):
            with self.assertRaises(TimeoutError):
                run_independent_checks(body, buttons)
