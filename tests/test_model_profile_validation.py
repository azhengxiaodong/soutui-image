"""隔离复验工具边界：仅使用临时合成图片和模拟审核，不调用网络。"""

import copy
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from soutui_image.review_images import inspect_image
from soutui_image.review_profiles import profile
from soutui_image.rules_bundle import builtin_bundle
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_e_codex_review import ImageReview
from test_stage_e_review import response
from tools import validate_model_profiles as validation


class ModelValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        sources = self.root / "sources"; sources.mkdir()
        Image.new("RGB", (100, 140), "white").save(sources / "001.png")
        self.product = scan_inputs(sources).products[0]
        image = self.root / "business" / "result.png"; image.parent.mkdir()
        Image.new("RGB", (100, 140), "gray").save(image)
        self.candidate = inspect_image("001", "task/slot-0", image)
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"product": asdict(self.product), "samples": [
            {"sample": "private-sample-name", "decision": "pass", "candidate": asdict(self.candidate)}]}), encoding="utf-8")

    def metrics(self):
        return {"by_stage": {stage: {"attempts": 1, "seconds_sum": 2.0, "missing_timing_count": 0,
                                      "tokens_used_sum": 3, "missing_token_count": 0} for stage in validation.STAGES},
                "attempts": [], "logged_tokens_used_sum": 12, "missing_token_count": 0,
                "configuration_verified": True}

    def test_clarification_overrides_stale_overall_and_feature_label(self):
        clarification = self.root / "clarification.json"
        clarification.write_text(json.dumps({"sample": "private-sample-name", "overall_expected": "reject",
            "feature_expectations": {"袖口边缘结构": "match", "门襟层次与边线": "mismatch"}}), encoding="utf-8")
        _, samples, protected, _ = validation.load_plan(self.manifest, clarification)
        self.assertEqual(samples[0]["expected"], "reject")
        self.assertEqual(samples[0]["feature_expectations"]["门襟层次与边线"], "mismatch")
        self.assertEqual(len(protected), 4)

    def test_transport_count_excludes_parent_copy_and_preserves_missing_usage(self):
        cache = self.root / "cache"
        stage = cache / "1" / "detail_location"; stage.mkdir(parents=True)
        text = 'model: gpt-6.1-terra\nprovider: openai_https\nreasoning effort: medium\ntokens used\n1,234\n'
        (stage / "stderr.txt").write_text(text, encoding="utf-8")
        for index in (1, 2):
            path = stage / f"transport_123_{index}"; path.mkdir()
            (path / "stderr.txt").write_text(text if index == 2 else text.split("tokens used")[0], encoding="utf-8")
        (stage / "transport_123.json").write_text(json.dumps([
            {"attempt": 1, "seconds": 4, "returncode": 1}, {"attempt": 2, "seconds": 5, "returncode": 0}]), encoding="utf-8")
        result = validation.transport_statistics(cache, profile("gpt-6.1-terra"))
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(result["logged_tokens_used_sum"], 1234)
        self.assertEqual(result["missing_token_count"], 1)
        self.assertEqual(result["by_stage"]["locator"]["seconds_sum"], 9)
        self.assertTrue(result["configuration_verified"])

    def test_main_writes_safe_summary_and_rejects_changed_input_on_resume(self):
        output = self.root / "validation"
        evidence = "private-raw-evidence"
        body = response()
        review = ImageReview(validation.policy_id(builtin_bundle()), "001", self.candidate.result_id,
                             self.product.sha256, self.candidate.sha256, "pass", evidence, "展示部分",
                             (body, copy.deepcopy(body)), evidence, profile().to_dict())
        before = {p: p.read_bytes() for p in (self.manifest, Path(self.product.source_path), Path(self.candidate.local_path))}
        args = ["--manifest", str(self.manifest), "--output", str(output), "--models", "gpt-6.1-sol", "--workers", "1"]
        with patch.object(validation, "review_candidate", return_value=review) as calls, \
             patch.object(validation, "transport_statistics", return_value=self.metrics()):
            self.assertEqual(validation.main(args), 0)
            self.assertEqual(calls.call_count, 1)
            positional, keywords = calls.call_args
            self.assertEqual(positional[:2], (self.product, self.candidate))
            self.assertTrue(positional[2].is_relative_to(output))
            self.assertEqual(keywords, {"model": "gpt-6.1-sol"})
        public_text = (output / "public_summary.json").read_text(encoding="utf-8")
        for sensitive in (str(self.root), "private-sample-name", evidence, self.product.sha256, self.candidate.result_id):
            self.assertNotIn(sensitive, public_text)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in before.items()))
        (output / "private_run_config.json").write_text("{}", encoding="utf-8")
        with patch.object(validation, "review_candidate", side_effect=AssertionError("不得调用")):
            with self.assertRaises(ValueError): validation.main(args)

    def test_business_cache_cannot_be_selected_as_validation_output(self):
        args = ["--manifest", str(self.manifest), "--output", str(Path(self.candidate.local_path).parent)]
        with patch.object(validation, "review_candidate", side_effect=AssertionError("不得调用")):
            with self.assertRaises(ValueError): validation.main(args)


if __name__ == "__main__":
    unittest.main()
