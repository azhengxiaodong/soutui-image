"""手动补图盘点使用实际图片文件验证；不访问网站或调用模型。"""
import json
import unittest
from dataclasses import replace
from pathlib import Path

import test_streaming_pipeline as fixture
from soutui_image.output_store import read_manifest
from soutui_image.regeneration_files import original_plan, inspect_folders, reconcile_folders
from soutui_image.review_images import ReviewImage
from soutui_image.saved_images import save_generated_image, review_overrides


class RegenerationFilesTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.StreamingPipelineTests("test_first_image_review_and_save_overlap_unfinished_generation")
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        f = self.f
        groups = list(f.plan.assignments)
        groups[0] = replace(groups[0], folder_id="17", group_name="搜推模特17")
        f.plan = replace(f.plan, candidate_group_count=17, assignments=tuple(groups))
        f.paths.tasks.mkdir(parents=True, exist_ok=True)
        self.plan_path = f.paths.tasks / "product_001_references.json"
        self.plan_path.write_text(json.dumps(f.plan.to_dict()), encoding="utf-8")
        self.output = Path(f.product.source_path).parent / f.product.product_id
        self.output.mkdir()

    def save(self, row, number):
        f = self.f; _, job = f.write_job(row)
        for result in job["results"][:number]:
            candidate = ReviewImage(**f.outcome(f.product, job, result)["candidate"])
            save_generated_image(f.product, f.plan, job, result, candidate, f.paths)

    def test_actual_zero_two_three_counts_and_original_template(self):
        f = self.f
        f.write_job(1); self.save(2, 2); self.save(3, 3)
        plan = original_plan(f.product, f.paths)
        inventory = inspect_folders(f.product, f.paths, plan)
        self.assertEqual([x["count"] for x in inventory["groups"][:3]], [0, 2, 3])
        self.assertEqual([x["row"] for x in inventory["groups"] if x["eligible"]], [1, 2])
        self.assertEqual(plan.assignments[0].group_name, "搜推模特17")
        self.assertEqual(plan.assignments[0].references, f.plan.assignments[0].references)

    def test_missing_template_never_creates_random_plan(self):
        self.plan_path.unlink()
        with self.assertRaisesRegex(ValueError, "缺少原模特模板"):
            original_plan(self.f.product, self.f.paths)
        self.assertFalse(self.plan_path.exists())

    def test_external_deletion_is_counted_and_not_restored_or_called_a_reject(self):
        f = self.f; self.save(1, 3)
        path = sorted((self.output / "组01").glob("*.png"))[0]
        path.unlink()
        before = {p:p.read_bytes() for p in f.paths.tasks.rglob("*.json")}
        inventory = inspect_folders(f.product, f.paths, f.plan)
        self.assertEqual(inventory["groups"][0]["count"], 2)
        self.assertTrue(inventory["groups"][0]["eligible"])
        reconcile_folders(f.product, f.paths, f.plan)
        record = read_manifest(self.output, f.paths.tasks, f.product)
        self.assertEqual(len(record["groups"][0]["saved"]), 2)
        self.assertEqual(record["removed"][0]["deletion_status"], "external_missing")
        self.assertEqual(set(review_overrides(f.product, f.paths.tasks, 1).values()), {"review"})
        self.assertFalse(path.exists())
        self.assertEqual({p:p.read_bytes() for p in before}, before)
        self.save(1, 3)
        self.assertFalse(path.exists())

    def test_changed_image_and_pending_original_task_block(self):
        f = self.f; self.save(1, 2)
        f.write_job(1, ("pending",)*5)
        self.assertFalse(inspect_folders(f.product, f.paths, f.plan)["groups"][0]["eligible"])
        path = next((self.output / "组01").glob("*.png"))
        path.write_bytes(Path(f.product.source_path).read_bytes())
        with self.assertRaisesRegex(ValueError, "内容被更改"):
            inspect_folders(f.product, f.paths, f.plan)

    def test_deleted_group_directory_is_empty_without_recreating_deleted_results(self):
        f = self.f; self.save(1, 1)
        directory = self.output / "组01"
        for path in directory.iterdir(): path.unlink()
        directory.rmdir()
        self.assertEqual(reconcile_folders(f.product, f.paths, f.plan)["groups"][0]["count"], 0)
        self.assertFalse(directory.exists())


if __name__ == "__main__": unittest.main()
