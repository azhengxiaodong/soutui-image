"""版本复核只验证记录/文件边界；模拟审核结果不作为真实视觉准确性验收。"""

import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from soutui_image.review_images import ReviewImage
from soutui_image.review_revision import execute_revision, plan_revision
from soutui_image.stage_a_inputs import ProductInput
from soutui_image.stage_e_codex_review import POLICY_VERSION, ImageReview
from soutui_image.output_store import manifest_path, save_manifest, MANIFEST_NAME


class RevisionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache, self.tasks = self.root / "cache", self.root / "tasks"
        self.source = self.root / "001.jpg"; self.source.write_bytes(b"source")
        sha = lambda raw: hashlib.sha256(raw).hexdigest()
        self.product = ProductInput("001", str(self.source), 1, 1, "JPEG", sha(b"source"))
        self.result_path = self.cache / "001/group_01/round_00/result.jpg"
        self.result_path.parent.mkdir(parents=True); self.result_path.write_bytes(b"result")
        self.candidate = ReviewImage("001", "task/slot-0", str(self.result_path), sha(b"result"), 1, 1, "JPEG", 6)
        self.old_review = {"policy_version": "2026-10-27.1", "product_id": "001", "result_id": "task/slot-0",
                           "source_sha256": self.product.sha256, "result_sha256": self.candidate.sha256, "decision": "pass"}
        self.job = {"product_id": "001", "source_sha256": self.product.sha256, "row": 1, "round_index": 0,
                    "folder_id": "folder", "reference_ids": [str(i) for i in range(5)], "task_id": "task",
                    "results": [{"result_id": "task/slot-0", "status": "done"}]}
        self.job_path = self.tasks / "001/group_01/round_00.json"; self.job_path.parent.mkdir(parents=True)
        self.job_path.write_text(json.dumps(self.job), encoding="utf-8")
        self.report_path = self.result_path.parent / "round_review.json"
        self.report = {**{k: v for k, v in self.job.items() if k != "results"}, "all_successful_images_reviewed": True,
                       "outcomes": [{"candidate": asdict(self.candidate), "review": self.old_review}],
                       "pass_count": 1, "reject_count": 0, "review_count": 0}
        self.report_path.write_text(json.dumps(self.report), encoding="utf-8")
        output = self.root / "001/组01"; output.mkdir(parents=True)
        self.output_photo = output / "old.jpg"; self.output_photo.write_bytes(b"result")
        self.manifest = manifest_path(output.parent, self.tasks)
        save_manifest(output.parent, self.tasks, {"app": "soutui-image", "schema_version": 1,
            "product_id": "001", "source_sha256": self.product.sha256,
            "groups": [{"row": row, "directory": f"组{row:02}", "folder_id": "folder",
                        "reference_ids": self.job["reference_ids"],
                        "saved": [{"filename": "old.jpg", "sha256": self.candidate.sha256}] if row == 1 else []}
                       for row in range(1, 10)]})

    def plan(self):
        return plan_revision([self.product], self.tasks, self.cache)

    def test_new_rejection_is_staged_without_changing_old_output_or_rounds(self):
        plan = self.plan(); self.assertEqual(plan["candidate_count"], 1)
        paths = (self.source, self.output_photo, self.manifest, self.report_path, self.job_path)
        before = {p: p.read_bytes() for p in paths}
        reviewed = ImageReview(POLICY_VERSION, "001", self.candidate.result_id, self.product.sha256,
                               self.candidate.sha256, "reject", "结构不符", "展示部分", (), "test-only")
        with patch("soutui_image.review_revision.review_candidate", return_value=reviewed):
            result = execute_revision([self.product], plan, self.root / "revision", self.root / "reviews")
        self.assertEqual(result["status"], "staged")
        self.assertEqual(result["reject_count"], 1)
        self.assertTrue(all(p.read_bytes() == raw for p, raw in before.items()))
        staged = json.loads(Path(result["reports"][0]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(staged["pass_count"], 0)
        self.assertEqual(staged["outcomes"][0]["review"]["policy_version"], POLICY_VERSION)

    def test_saved_content_change_blocks_plan(self):
        self.output_photo.write_bytes(b"changed")
        with self.assertRaises(ValueError): self.plan()

    def test_legacy_manifest_planning_is_read_only_and_uses_photo_output_root(self):
        record=json.loads(self.manifest.read_text(encoding="utf-8")); record.pop("output_directory")
        legacy=self.output_photo.parent.parent/MANIFEST_NAME
        legacy.write_text(json.dumps(record),encoding="utf-8"); self.manifest.unlink()
        before=legacy.read_bytes()
        plan=self.plan()
        self.assertEqual(plan["candidate_count"],1)
        self.assertEqual(legacy.read_bytes(),before)
        self.assertFalse(self.manifest.exists())
        self.assertEqual([x["path"] for x in plan["inputs"] if x.get("output_manifest")],[str(legacy)])

    def test_old_record_change_blocks_before_any_model_call(self):
        plan = self.plan(); self.report_path.write_bytes(b"changed")
        with patch("soutui_image.review_revision.review_candidate", side_effect=AssertionError("不得调用")):
            with self.assertRaises(ValueError):
                execute_revision([self.product], plan, self.root / "revision", self.root / "reviews")

    def test_failed_audit_never_produces_complete_replacement_reports(self):
        before = self.report_path.read_bytes()
        with patch("soutui_image.review_revision.review_candidate", side_effect=TimeoutError("测试超时")):
            result = execute_revision([self.product], self.plan(), self.root / "revision", self.root / "reviews")
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse((self.root / "revision/reports").exists())
        self.assertEqual(self.report_path.read_bytes(), before)

    def test_resuming_with_other_model_cannot_mix_reviews(self):
        plan = self.plan()
        with patch("soutui_image.review_revision.review_candidate", side_effect=TimeoutError("测试中断")):
            execute_revision([self.product], plan, self.root / "revision", self.root / "reviews")
        with patch("soutui_image.review_revision.review_candidate", side_effect=AssertionError("不得调用")):
            with self.assertRaises(ValueError):
                execute_revision([self.product], plan, self.root / "revision", self.root / "reviews", model="other-model")


if __name__ == "__main__":
    unittest.main()
