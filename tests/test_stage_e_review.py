"""E 的可见范围、结论一致性、图片绑定和通过图片双次审核规则检查。"""

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from soutui_image.review_images import ReviewImage
from soutui_image.stage_a_inputs import ProductInput
from soutui_image.stage_e_codex_review import FEATURES, review_candidate, validate_review


def response(decision="pass"):
    return {"product_id": "001", "result_id": "task/slot-0", "decision": decision,
            "visible_scope": "上衣及部分裤子可见", "unshown_parts": ["裤脚"], "summary": "可见服装一致",
            "checks": [{"feature": feature, "visibility": "partial", "comparison": "match",
                        "original": "原款特征", "generated": "可见特征一致", "reason": "实际可见部分一致"}
                       for feature in FEATURES]}


class ReviewEvidenceTests(unittest.TestCase):
    def test_unshown_trouser_hems_do_not_reject_or_hold_visible_match(self):
        data = response()
        item = next(c for c in data["checks"] if c["feature"] == "裤子与裤脚")
        item.update(visibility="not_visible", comparison="not_applicable", generated="裤子未展示", reason="未展示，不参与判定")
        self.assertEqual(validate_review(data, "001", "task/slot-0")["decision"], "pass")

    def test_visible_mismatch_cannot_be_marked_pass(self):
        data = response()
        data["checks"][3]["comparison"] = "mismatch"
        with self.assertRaises(ValueError): validate_review(data, "001", "task/slot-0")
        data["decision"] = "reject"
        self.assertEqual(validate_review(data, "001", "task/slot-0")["decision"], "reject")

    def test_visible_uncertainty_requires_review_but_hidden_uncertainty_is_invalid(self):
        data = response("review")
        data["checks"][0]["comparison"] = "uncertain"
        self.assertEqual(validate_review(data, "001", "task/slot-0")["decision"], "review")
        data["checks"][0]["visibility"] = "not_visible"
        with self.assertRaises(ValueError): validate_review(data, "001", "task/slot-0")

    def test_missing_feature_or_wrong_result_stops(self):
        data = response(); data["checks"].pop()
        with self.assertRaises(ValueError): validate_review(data, "001", "task/slot-0")
        with self.assertRaises(ValueError): validate_review(response(), "002", "task/slot-0")


class ReviewPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "001.jpg"; source.write_bytes(b"original")
        generated = self.root / "generated.jpg"; generated.write_bytes(b"result")
        self.product = ProductInput("001", str(source), 1, 1, "JPEG", hashlib.sha256(source.read_bytes()).hexdigest())
        self.candidate = ReviewImage("001", "task/slot-0", str(generated), hashlib.sha256(generated.read_bytes()).hexdigest(), 1, 1, "JPEG", 6)

    def test_first_pass_requires_independent_second_pass(self):
        data = response()
        with patch("soutui_image.stage_e_codex_review._audit_once", side_effect=[data, copy.deepcopy(data)]) as audit:
            result = review_candidate(self.product, self.candidate, self.root / "reviews")
            self.assertEqual(result.decision, "pass")
            self.assertEqual(audit.call_count, 2)
        with patch("soutui_image.stage_e_codex_review._audit_once", side_effect=AssertionError("cached review should not call model")):
            cached = review_candidate(self.product, self.candidate, self.root / "reviews")
            self.assertEqual(cached, result)

    def test_second_pass_rejection_cannot_enter_approved_output(self):
        second = response("reject"); second["checks"][3]["comparison"] = "mismatch"
        with patch("soutui_image.stage_e_codex_review._audit_once", side_effect=[response(), second]):
            result = review_candidate(self.product, self.candidate, self.root / "reviews")
            self.assertEqual(result.decision, "reject")

    def test_changed_result_stops_before_model_call(self):
        Path(self.candidate.local_path).write_bytes(b"changed")
        with patch("soutui_image.stage_e_codex_review._audit_once", side_effect=AssertionError("must not call model")):
            with self.assertRaises(ValueError):
                review_candidate(self.product, self.candidate, self.root / "reviews")


if __name__ == "__main__":
    unittest.main()
