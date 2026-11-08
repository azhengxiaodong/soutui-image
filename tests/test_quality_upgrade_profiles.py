"""规则升级仅验证执行配置与续跑边界，不调用模型，不代替视觉质量验收。"""

import hashlib
import json
import unittest
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import test_review_revision as fixtures
from soutui_image.quality_upgrade import recheck_saved_product
from soutui_image.review_profiles import legacy_profile, profile, profile_fingerprint, use_review_profile
from soutui_image.rule_runtime import current_policy


class QualityUpgradeProfileTests(unittest.TestCase):
    setUp = fixtures.RevisionBoundaryTests.setUp

    def prepare_staged(self, chosen, saved_profile=None):
        """构造本地已完成待应用阶段，保留真实旧报告及生成轮次以验证写入边界。"""
        identity = current_policy() + self.product.product_id + self.product.sha256 + chosen.model
        if not chosen.legacy:
            identity += profile_fingerprint(chosen)
        root = self.root / "quality_revisions" / hashlib.sha256(identity.encode()).hexdigest()[:24]
        source = root / "reports" / "001" / "group_01" / "round_00" / "round_review.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        report["quality_revision"] = {"policy_version": current_policy()}
        source.write_text(json.dumps(report), encoding="utf-8")
        sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        staged = {"status": "staged", "policy_version": current_policy(), "model": chosen.model,
                  "total": 1, "reports": [{"path": str(source), "sha256": sha(source),
                      "old_path": str(self.report_path), "old_sha256": sha(self.report_path)}]}
        if saved_profile is not None:
            staged["review_profile"] = saved_profile
        (root / "result.json").write_text(json.dumps(staged), encoding="utf-8")
        (root / "plan.json").write_text(json.dumps({"inputs": [{
            "job_path": str(self.job_path), "job_sha256": sha(self.job_path)}]}), encoding="utf-8")
        paths = SimpleNamespace(root=self.root, tasks=self.tasks, cache=self.cache,
                                reviews=self.root / "reviews")
        return root, paths

    def dependencies(self, stack):
        """隔离业务执行，仅允许被测函数检查并应用已经落盘的审核阶段。"""
        stack.enter_context(patch("soutui_image.quality_upgrade.needs_upgrade", return_value=True))
        stack.enter_context(patch("soutui_image.quality_upgrade.load_plan",
                                 return_value=SimpleNamespace(assignments=[])))
        stack.enter_context(patch("soutui_image.quality_upgrade.withdraw_legacy_outputs", return_value={}))
        execute = stack.enter_context(patch("soutui_image.quality_upgrade.execute_revision"))
        qualify = stack.enter_context(patch("soutui_image.quality_upgrade._qualified_items"))
        save = stack.enter_context(patch("soutui_image.quality_upgrade.save_approved_reports", return_value={}))
        return execute, qualify, save

    def test_legacy_staged_without_profile_resumes_existing_operation(self):
        chosen = legacy_profile()
        root, paths = self.prepare_staged(chosen)
        before = {p: p.read_bytes() for p in (self.source, self.job_path, self.output_photo, self.manifest)}
        old_report = self.report_path.read_bytes()
        with ExitStack() as stack:
            execute, _, save = self.dependencies(stack)
            stack.enter_context(use_review_profile(chosen))
            result = recheck_saved_product(self.product, paths, model=chosen.model)
        self.assertEqual(result["status"], "rechecked")
        self.assertNotIn("review_profile", result)
        self.assertEqual(json.loads((root / "apply_result.json").read_text(encoding="utf-8")), result)
        self.assertEqual(next((root / "history").rglob("round_review.json")).read_bytes(), old_report)
        self.assertTrue(all(p.read_bytes() == raw for p, raw in before.items()))
        self.assertEqual(list((self.root / "quality_revisions").iterdir()), [root])
        execute.assert_not_called()
        save.assert_called_once()

    def test_new_profile_is_preserved_in_applied_result_and_event(self):
        chosen = profile("gpt-6.1-terra")
        root, paths = self.prepare_staged(chosen, chosen.to_dict())
        events = []
        with ExitStack() as stack:
            execute, _, save = self.dependencies(stack)
            stack.enter_context(use_review_profile(chosen))
            result = recheck_saved_product(self.product, paths, model=chosen.model, on_event=events.append)
        self.assertEqual(result["review_profile"], chosen.to_dict())
        self.assertEqual(json.loads((root / "apply_result.json").read_text(encoding="utf-8")), result)
        self.assertEqual(events[-1]["review_profile"], chosen.to_dict())
        execute.assert_not_called()
        save.assert_called_once()

    def test_wrong_or_missing_staged_profile_cannot_apply_reports(self):
        chosen = profile()
        cases = ((legacy_profile(), chosen.to_dict()),
                 (chosen, None),
                 (chosen, profile("gpt-6.1-terra").to_dict()),
                 (chosen, replace(chosen, body_effort="xhigh").to_dict()),
                 (chosen, {**chosen.to_dict(), "schema_version": 99}))
        for active, stored in cases:
            with self.subTest(active=active, stored=stored):
                root, paths = self.prepare_staged(active, stored)
                before = {p: p.read_bytes() for p in
                          (self.source, self.job_path, self.output_photo, self.manifest, self.report_path)}
                with ExitStack() as stack:
                    execute, qualify, save = self.dependencies(stack)
                    stack.enter_context(use_review_profile(active))
                    with self.assertRaises(ValueError):
                        recheck_saved_product(self.product, paths, model=active.model)
                self.assertFalse((root / "apply_result.json").exists())
                self.assertFalse((root / "history").exists())
                self.assertTrue(all(p.read_bytes() == raw for p, raw in before.items()))
                execute.assert_not_called()
                qualify.assert_not_called()
                save.assert_not_called()

    def test_model_context_mismatch_fails_before_already_current_shortcut(self):
        with patch("soutui_image.quality_upgrade.needs_upgrade", return_value=False) as needs:
            with use_review_profile(profile("gpt-6.1-terra")):
                with self.assertRaisesRegex(ValueError, "当前任务固定配置"):
                    recheck_saved_product(self.product, SimpleNamespace(), model="gpt-6.1-sol")
        needs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
