"""未审图保存与独立审图删除边界；仅本地假审核验证，不代表模型准确率。"""

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_stage_f_outputs as output_fixture
from soutui_image.output_store import read_manifest
from soutui_image.review_images import ReviewImage
from soutui_image.saved_images import (save_generated_image, scan_saved_images, apply_saved_review,
                                       counts, review_overrides)
from soutui_image.review_group_state import write_group_review_summary
from soutui_image.stage_d_web_generation import group_decision


class SavedImagesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = output_fixture.OutputTests("test_only_pass_saved_and_nine_group_directories_created")
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.paths = SimpleNamespace(tasks=f.state, cache=f.cache)
        self.report = f.report(decisions=("pass", "reject", "review"))
        self.job = {"row": 1, "round_index": 0, "product_id": f.product.product_id,
                    "source_sha256": f.product.sha256, "folder_id": "1",
                    "reference_ids": self.report["reference_ids"], "results": []}
        for item in self.report["outcomes"]:
            self.job["results"].append({"result_id": item["candidate"]["result_id"], "status": "done",
                                        "reference_id": item["reference_id"], "reference_index": item["reference_index"]})

    def save(self, index):
        f = self.fixture
        return save_generated_image(f.product, f.plan, self.job, self.job["results"][index],
                                    ReviewImage(**self.report["outcomes"][index]["candidate"]), self.paths)

    def scan(self):
        return scan_saved_images(self.fixture.product, self.paths, self.fixture.plan)

    def record(self):
        return read_manifest(self.fixture.output, self.paths.tasks, self.fixture.product)

    def test_all_generated_images_saved_without_becoming_approved(self):
        for index in range(3): self.save(index)
        self.assertEqual(counts(self.record())[1], {"saved": 3, "pass": 0, "review": 0, "unreviewed": 3})
        self.assertEqual(self.save(0)["saved_new"], 0)
        self.assertFalse((self.fixture.output / ".搜推生图.json").exists())
        self.assertFalse((self.fixture.output / ".soutui.lock").exists())
        self.assertEqual(len(self.scan()), 3)

    def test_reject_removes_only_matching_result_and_cannot_be_restored(self):
        self.save(1)
        item = self.scan()[0]
        result = apply_saved_review(self.fixture.product, self.paths, item, self.report["outcomes"][1]["review"])
        self.assertEqual(result["decision"], "reject")
        self.assertFalse(Path(item["candidate"]["local_path"]).exists())
        self.assertTrue(Path(self.fixture.product.source_path).exists())
        self.assertTrue(Path(self.report["outcomes"][1]["candidate"]["local_path"]).exists())
        self.assertEqual(self.save(1)["saved_new"], 0)
        self.assertEqual(self.record()["removed"][0]["deletion_status"], "complete")
        self.assertEqual(review_overrides(self.fixture.product, self.paths.tasks, 1)[item["candidate"]["sha256"]], "reject")

    def test_uncertain_and_call_failure_keep_original_bytes(self):
        self.save(2); item = self.scan()[0]
        path = Path(item["candidate"]["local_path"]); before = path.read_bytes()
        apply_saved_review(self.fixture.product, self.paths, item, self.report["outcomes"][2]["review"])
        self.assertEqual(counts(self.record())[1]["review"], 1)
        apply_saved_review(self.fixture.product, self.paths, item, error="测试超时")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.record()["groups"][0]["saved"][0]["review_status"], "error")

    def test_changed_image_is_never_deleted(self):
        self.save(1); item = self.scan()[0]
        path = Path(item["candidate"]["local_path"]); path.write_bytes(b"user changed bytes")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            apply_saved_review(self.fixture.product, self.paths, item, self.report["outcomes"][1]["review"])
        self.assertEqual(path.read_bytes(), b"user changed bytes")
        self.assertFalse(self.record().get("removed"))

    def test_interrupted_delete_can_resume_from_persisted_evidence(self):
        self.save(1); item = self.scan()[0]
        path = Path(item["candidate"]["local_path"])
        original = Path.unlink
        def fail(target, *args, **kwargs):
            if target == path: raise PermissionError("模拟占用")
            return original(target, *args, **kwargs)
        with patch.object(Path, "unlink", fail), self.assertRaises(PermissionError):
            apply_saved_review(self.fixture.product, self.paths, item, self.report["outcomes"][1]["review"])
        self.assertTrue(path.exists())
        self.assertEqual(self.record()["removed"][0]["deletion_status"], "pending")
        self.assertEqual(self.scan(), [])
        self.assertFalse(path.exists())

    def test_wrong_reference_or_review_identity_cannot_save_or_delete(self):
        original = copy.deepcopy(self.job["results"][0])
        self.job["results"][0]["reference_id"] = "other"
        with self.assertRaises(ValueError): self.save(0)
        self.job["results"][0] = original
        self.save(0); item = self.scan()[0]
        wrong = copy.deepcopy(self.report["outcomes"][1]["review"])
        with self.assertRaises(ValueError):
            apply_saved_review(self.fixture.product, self.paths, item, wrong)
        self.assertTrue(Path(item["candidate"]["local_path"]).exists())

    def _assert_failed_recheck_drops_approved_count(self, *, previously_rechecked):
        f = self.fixture
        report = f.report(decisions=("pass",) * 3)
        f.save([report])
        job_path = f.state / f.product.product_id / "group_01" / "round_00.json"
        job = json.loads(job_path.read_text(encoding="utf-8"))
        job.update(product_id=f.product.product_id, row=1, round_index=0)
        job_path.write_text(json.dumps(job), encoding="utf-8")
        report_path = f.cache / f.product.product_id / "group_01" / "round_00" / "round_review.json"
        report_path.parent.mkdir(parents=True)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        write_group_review_summary(f.product, f.state, f.cache, 1)
        self.assertEqual(group_decision(f.state, f.product, f.plan.assignments[0]).action, "complete")
        original_job, original_report = job_path.read_bytes(), report_path.read_bytes()
        item = self.scan()[0]
        review = report["outcomes"][0]["review"]
        if previously_rechecked:
            apply_saved_review(f.product, self.paths, item, review)
        image_path = Path(item["candidate"]["local_path"])
        original_image = image_path.read_bytes()

        apply_saved_review(f.product, self.paths, item, error="模拟模型调用失败")
        decision = group_decision(f.state, f.product, f.plan.assignments[0])
        self.assertEqual((decision.approved_count, decision.action), (2, "generate"))
        summary = write_group_review_summary(f.product, f.state, f.cache, 1)
        self.assertEqual(summary["approved_count"], 2)
        self.assertNotIn(item["candidate"]["sha256"], summary["approved_sha256"])

        # 继续正常流程的 F 会重读旧通过报告，不能覆盖最新的失败/待确认标记。
        saved = f.save([report])
        entry = next(x for x in self.record()["groups"][0]["saved"] if x["sha256"] == item["candidate"]["sha256"])
        self.assertEqual(entry["review_status"], "error")
        self.assertEqual(saved["approved_counts"]["组01"], 2)
        self.assertEqual(counts(self.record())[1], {"saved": 3, "pass": 2, "review": 1, "unreviewed": 0})
        self.assertEqual(entry.get("audit_review"), review if previously_rechecked else None)
        self.assertEqual(image_path.read_bytes(), original_image)
        self.assertEqual(job_path.read_bytes(), original_job)
        self.assertEqual(report_path.read_bytes(), original_report)

    def test_original_pass_then_recheck_error_is_not_approved_by_group(self):
        self._assert_failed_recheck_drops_approved_count(previously_rechecked=False)

    def test_saved_recheck_pass_then_error_does_not_reuse_old_pass(self):
        self._assert_failed_recheck_drops_approved_count(previously_rechecked=True)


if __name__ == "__main__":
    unittest.main()
