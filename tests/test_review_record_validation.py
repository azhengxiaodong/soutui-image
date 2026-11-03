"""审核升级不复用旧通过、不篡改旧十项证据，并严格绑定每次审核对象。"""

import copy
import unittest
from types import SimpleNamespace

from soutui_image.review_record_validation import validate_bound_review, LEGACY_POLICY
from soutui_image.stage_e_codex_review import POLICY_VERSION, LEGACY_FEATURES
from test_stage_e_review import response


class RecordVersionTests(unittest.TestCase):
    def setUp(self):
        self.product = SimpleNamespace(product_id="001", sha256="a" * 64)
        self.candidate = {"product_id": "001", "result_id": "task/slot-0", "sha256": "b" * 64}

    def record(self, *, legacy=False, decision="pass"):
        first = response()
        if legacy:
            first["checks"] = [c for c in first["checks"] if c["feature"] in LEGACY_FEATURES]
        second = copy.deepcopy(first)
        if decision != "pass":
            second["decision"] = decision
            second["checks"][0]["comparison"] = "mismatch" if decision == "reject" else "uncertain"
        return {"policy_version": LEGACY_POLICY if legacy else POLICY_VERSION, "product_id": "001",
                "result_id": "task/slot-0", "source_sha256": self.product.sha256,
                "result_sha256": self.candidate["sha256"], "decision": decision, "passes": [first, second]}

    def test_legacy_pass_never_counts_as_current_pass(self):
        with self.assertRaises(ValueError): validate_bound_review(self.product, self.candidate, self.record(legacy=True))

    def test_legacy_rejection_preserves_ten_checks_without_relabeling(self):
        record = self.record(legacy=True, decision="reject")
        before = copy.deepcopy(record)
        self.assertEqual(validate_bound_review(self.product, self.candidate, record), "reject")
        self.assertEqual(record, before)
        self.assertEqual(len(record["passes"][0]["checks"]), 10)

    def test_each_legacy_pass_identity_is_checked_even_if_final_rejected(self):
        record = self.record(legacy=True, decision="reject")
        record["passes"][0]["result_id"] = "other-result"
        with self.assertRaises(ValueError): validate_bound_review(self.product, self.candidate, record)

    def test_current_double_pass_is_bound_to_both_images(self):
        record = self.record()
        self.assertEqual(validate_bound_review(self.product, self.candidate, record), "pass")
        record["source_sha256"] = "c" * 64
        with self.assertRaises(ValueError): validate_bound_review(self.product, self.candidate, record)

    def test_unknown_legacy_policy_is_rejected(self):
        record = self.record(legacy=True, decision="reject"); record["policy_version"] = "unknown"
        with self.assertRaises(ValueError): validate_bound_review(self.product, self.candidate, record)
