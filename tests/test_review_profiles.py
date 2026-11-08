"""模型配置边界检查：模拟模型只验证路由/缓存/双审，不冒充真实图片准确率验收。"""

import copy
import hashlib
import json
import tempfile
import unittest
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from soutui_image.review_detail_locator import PARTS, locate_detail_regions
from soutui_image.review_images import inspect_image
from soutui_image.review_profiles import (ReviewProfile, current_profile, legacy_profile, profile,
    reasoning_arguments, review_cache_key, use_review_profile, validate_review_profile)
from soutui_image.review_schedule import run_independent_checks
from soutui_image.rule_runtime import current_policy
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_e_codex_review import _audit_once, review_candidate
from test_stage_e_review import response


class ReviewProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "001.png"
        Image.new("RGB", (100, 140), "white").save(source)
        self.product = scan_inputs(self.root).products[0]
        self.candidate = inspect_image("001", "task/slot-0", source)

    def regions(self):
        return {"regions": [{"role": role, "part": part, "visible": True,
                            "box": [200, 100, 800, 900], "description": "测试定位"}
                           for role in ("original", "generated") for part in PARTS]}

    def model_calls(self, calls):
        def invoke(command, **kwargs):
            calls.append(list(command))
            schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text(encoding="utf-8"))
            if "regions" in schema["properties"]:
                verdict = self.regions()
            elif "product_id" in schema["properties"]:
                verdict = response()
            else:
                verdict = copy.deepcopy(next(c for c in response()["checks"] if c["feature"] == "扣位边缘与凸片"))
            Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(verdict), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="测试模拟输出")
        return invoke

    def test_strict_serialization_and_legacy_constraint(self):
        for chosen in (profile(), profile("gpt-6.1-terra"), legacy_profile()):
            self.assertEqual(ReviewProfile.from_dict(chosen.to_dict()), chosen)
        for change in ({"extra": 1}, {"schema_version": True}, {"locator_effort": "none"}, {"model": "unknown"}):
            with self.assertRaises(ValueError):
                ReviewProfile.from_dict({**profile().to_dict(), **change})
        missing = profile().to_dict(); del missing["button_effort"]
        with self.assertRaises(ValueError): ReviewProfile.from_dict(missing)
        with self.assertRaises(ValueError): ReviewProfile("gpt-6.1-terra", "xhigh", "xhigh", "xhigh", legacy=True)
        with self.assertRaises(ValueError): replace(legacy_profile(), body_effort="high")

    def test_context_model_binding_restores_and_parallel_checks_inherit(self):
        self.assertEqual(current_profile(), legacy_profile())
        chosen = profile("gpt-6.1-terra")
        with use_review_profile(chosen):
            self.assertEqual(current_profile(), chosen)
            with self.assertRaises(ValueError): current_profile("gpt-6.1-sol")
            body, buttons, _ = run_independent_checks(current_profile, current_profile)
            self.assertEqual((body, buttons), (chosen, chosen))
            self.assertEqual(copy_context().run(current_profile), chosen)
        self.assertEqual(current_profile(), legacy_profile())

    def test_cache_keys_bind_model_effort_and_preserve_legacy(self):
        original = "\0".join((current_policy(), self.product.product_id, self.candidate.result_id,
                              self.product.sha256, self.candidate.sha256, "gpt-6.1-sol"))
        old_key = review_cache_key(self.product, self.candidate)
        self.assertEqual(old_key, hashlib.sha256(original.encode()).hexdigest())
        keys = {old_key}
        for chosen in (profile(), profile("gpt-6.1-terra"), replace(profile(), body_effort="medium"),
                       replace(profile(), locator_effort="high"), replace(profile(), button_effort="medium")):
            with use_review_profile(chosen):
                keys.add(review_cache_key(self.product, self.candidate))
        self.assertEqual(len(keys), 6)

    def test_legacy_record_only_reused_by_legacy_profile(self):
        self.assertEqual(validate_review_profile({}), legacy_profile())
        with use_review_profile(profile()):
            with self.assertRaises(ValueError): validate_review_profile({})
            with self.assertRaises(ValueError): validate_review_profile({"review_profile": profile("gpt-6.1-terra").to_dict()})
            self.assertEqual(validate_review_profile({"review_profile": profile().to_dict()}), profile())

    def test_complete_audit_routes_all_four_calls_with_explicit_effort(self):
        calls = []
        chosen = profile("gpt-6.1-terra")
        with use_review_profile(chosen), \
             patch("soutui_image.stage_e_codex_review.find_codex", return_value=Path("codex.exe")), \
             patch("soutui_image.review_detail_locator.find_codex", return_value=Path("codex.exe")), \
             patch("soutui_image.review_button_edges.find_codex", return_value=Path("codex.exe")), \
             patch("soutui_image.codex_runtime.subprocess.run", side_effect=self.model_calls(calls)):
            result = review_candidate(self.product, self.candidate, self.root / "reviews", model=chosen.model)
            self.assertEqual(result.decision, "pass")
            self.assertEqual(len(result.passes), 2)
            self.assertEqual(result.review_profile, chosen.to_dict())
            self.assertEqual(len(calls), 4)
            second = review_candidate(self.product, self.candidate, self.root / "reviews", model=chosen.model)
            self.assertEqual(second, result)
            self.assertEqual(len(calls), 4)
        efforts = [next(x for x in call if x.startswith("model_reasoning_effort=")) for call in calls]
        self.assertEqual(efforts.count('model_reasoning_effort="medium"'), 1)
        self.assertEqual(efforts.count('model_reasoning_effort="high"'), 3)
        for call in calls:
            self.assertEqual(call[call.index("--model") + 1], chosen.model)
            self.assertFalse(any("model_provider" in x or "websocket" in x or "base_url" in x for x in call))
            self.assertEqual(call[call.index("--image") + 1], self.product.source_path)

    def test_legacy_calls_explicitly_pin_xhigh(self):
        for stage in ("locator", "body", "buttons"):
            self.assertEqual(reasoning_arguments(stage), ["-c", 'model_reasoning_effort="xhigh"'])

    def test_locator_cache_rejects_changed_effort_and_rule_identity(self):
        calls = []
        root = self.root / "location"
        with use_review_profile(profile()), \
             patch("soutui_image.review_detail_locator.find_codex", return_value=Path("codex.exe")), \
             patch("soutui_image.codex_runtime.subprocess.run", side_effect=self.model_calls(calls)):
            locate_detail_regions(self.product, self.candidate, root)
            cached = json.loads((root / "located.json").read_text(encoding="utf-8"))
            self.assertIn("prompt", cached)
            self.assertIn("schema", cached)
            self.assertEqual(cached["policy_version"], current_policy())
            with patch("soutui_image.review_detail_locator.current_policy", return_value="changed"):
                with self.assertRaises(ValueError): locate_detail_regions(self.product, self.candidate, root)
        with use_review_profile(replace(profile(), locator_effort="high")):
            with self.assertRaises(ValueError): locate_detail_regions(self.product, self.candidate, root)
        self.assertEqual(len(calls), 1)

    def test_final_cache_rejects_legacy_report_copied_to_new_profile(self):
        root = self.root / "reviews"
        with patch("soutui_image.stage_e_codex_review._audit_once", side_effect=[response(), response()]):
            review_candidate(self.product, self.candidate, root)
        old = root / review_cache_key(self.product, self.candidate) / "review.json"
        record = json.loads(old.read_text(encoding="utf-8")); record.pop("review_profile")
        with use_review_profile(profile()):
            copied = root / review_cache_key(self.product, self.candidate) / "review.json"
            copied.parent.mkdir()
            copied.write_text(json.dumps(record), encoding="utf-8")
            with patch("soutui_image.stage_e_codex_review._audit_once", side_effect=AssertionError("不得调用")):
                with self.assertRaises(ValueError): review_candidate(self.product, self.candidate, root)

    def test_profile_change_does_not_reuse_successful_full_review(self):
        root = self.root / "reviews"
        with use_review_profile(profile()), \
             patch("soutui_image.stage_e_codex_review._audit_once", side_effect=[response(), response()]) as audit:
            first = review_candidate(self.product, self.candidate, root)
            self.assertEqual(audit.call_count, 2)
        with use_review_profile(profile("gpt-6.1-terra")), \
             patch("soutui_image.stage_e_codex_review._audit_once", side_effect=[response(), response()]) as audit:
            second = review_candidate(self.product, self.candidate, root)
            self.assertEqual(audit.call_count, 2)
        self.assertNotEqual(first.evidence_directory, second.evidence_directory)
        self.assertNotEqual(first.review_profile, second.review_profile)

    def test_new_profile_resumes_successful_body_after_specialty_failure(self):
        calls = []
        attempt = self.root / "run" / "verification"
        chosen = profile("gpt-6.1-terra")
        with use_review_profile(chosen), \
             patch("soutui_image.stage_e_codex_review.find_codex", return_value=Path("codex.exe")), \
             patch("soutui_image.review_detail_locator.find_codex", return_value=Path("codex.exe")), \
             patch("soutui_image.codex_runtime.subprocess.run", side_effect=self.model_calls(calls)), \
             patch("soutui_image.stage_e_codex_review.review_button_edges", side_effect=TimeoutError("测试专项中断")):
            with self.assertRaises(TimeoutError):
                _audit_once(self.product, self.candidate, attempt, verification=True)
        checkpoint = json.loads((attempt / "body_checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["identity"]["review_profile"], chosen.to_dict())
        self.assertEqual(len(calls), 2)
        with use_review_profile(chosen), \
             patch("soutui_image.stage_e_codex_review.find_codex", return_value=Path("codex.exe")), \
             patch("soutui_image.review_button_edges.find_codex", return_value=Path("codex.exe")), \
             patch("soutui_image.codex_runtime.subprocess.run", side_effect=self.model_calls(calls)):
            result = _audit_once(self.product, self.candidate, attempt, verification=True)
        self.assertEqual(result["decision"], "pass")
        self.assertEqual(len(calls), 3)
        checkpoint = json.loads((attempt / "button_checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["identity"]["review_profile"], chosen.to_dict())


if __name__ == "__main__":
    unittest.main()
