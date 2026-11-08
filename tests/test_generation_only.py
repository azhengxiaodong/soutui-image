"""默认只生图的调度边界：全部成功图保存、无审核/补生、暂停续跑保持任务。"""

import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import test_streaming_pipeline as streaming_fixture
from soutui_image.generation_only import run_generation_only
from soutui_image.output_store import read_manifest
from soutui_image.review_images import ReviewImage
from soutui_image.saved_images import counts
from soutui_image.workflow_options import bind_options, read_options, options_path
from soutui_image.review_profiles import profile
from soutui_image.pipeline import UserPaused
from soutui_image.stage_a_inputs import scan_inputs


class GenerationOnlyTests(unittest.TestCase):
    def setUp(self):
        self.f = streaming_fixture.StreamingPipelineTests("test_first_image_review_and_save_overlap_unfinished_generation")
        self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def run_mode(self, rows=(1,), query=None, prepare=None, submit=None):
        f = self.f
        def download(pid, result, root):
            job = json.loads(f.job_path(int(result["result_id"].split('-')[1])).read_text())
            return ReviewImage(**f.outcome(f.product, job, result)["candidate"])
        with ExitStack() as stack:
            stack.enter_context(patch("soutui_image.generation_only.download_result", side_effect=download))
            stack.enter_context(patch("soutui_image.generation_only.query_job", side_effect=query or (lambda path, client: json.loads(path.read_text()))))
            stack.enter_context(patch("soutui_image.generation_only.prepare_job", side_effect=prepare or AssertionError("不能补生")))
            stack.enter_context(patch("soutui_image.generation_only.submit_job", side_effect=submit or AssertionError("不能重复提交")))
            stack.enter_context(patch("soutui_image.pipeline.review_candidate", side_effect=AssertionError("关闭审图不能调用模型")))
            return run_generation_only(f.pipeline, f.product, f.plan, f.upload,
                                       [f.plan.assignments[row - 1] for row in rows], f.client,
                                       poll_interval=.01, generation_timeout=3)

    def test_all_results_saved_and_resume_never_submits_or_audits(self):
        f = self.f
        f.write_job(1); f.write_job(2)
        self.assertTrue(all(x["state"] == "complete" for x in self.run_mode((1, 2))))
        record = read_manifest(Path(f.product.source_path).parent / "001", f.paths.tasks, f.product)
        self.assertEqual(sum(x["saved"] for x in counts(record).values()), 10)
        self.assertEqual(sum(x["pass"] for x in counts(record).values()), 0)
        self.run_mode((1, 2))
        self.assertEqual(len(list(f.paths.tasks.rglob("round_*.json"))), 2)

    def test_successful_image_saves_before_other_slots_finish(self):
        f = self.f
        f.write_job(1, ("done", "pending", "pending", "pending", "pending"))
        def query(path, client):
            if any(e["kind"] == "generated_saved" for e in f.events): return f.write_job(1)[1]
            return json.loads(path.read_text())
        self.run_mode(query=query)
        kinds = [e["kind"] for e in f.events]
        self.assertLess(kinds.index("generated_saved"), kinds.index("generation_saved_complete"))

    def test_failure_slots_reported_without_regeneration(self):
        self.f.write_job(1, ("done", "error", "error", "error", "error"))
        result = self.run_mode()[0]
        self.assertEqual(result["state"], "needs_attention")
        self.assertEqual((result["successful_count"], result["failed_count"]), (1, 4))
        self.assertEqual(len(list(self.f.paths.tasks.rglob("round_*.json"))), 1)

    def test_pause_preserves_downloads_then_resume_completes(self):
        f = self.f; f.write_job(1)
        original = f.pipeline.on_event
        def notify(event):
            original(event)
            if event["kind"] == "generated_saved": f.pipeline.stop.set()
        f.pipeline.on_event = notify
        with self.assertRaises(UserPaused): self.run_mode()
        self.assertGreater(len(list((Path(f.product.source_path).parent / "001").rglob("*.png"))), 0)
        f.pipeline.stop.clear(); f.pipeline.on_event = original
        self.assertEqual(self.run_mode()[0]["successful_count"], 5)

    def test_new_task_defaults_off_but_legacy_keeps_original_profile(self):
        f = self.f
        chosen = bind_options(f.product, f.paths.tasks, selected_profile=profile("gpt-6.1-terra"))
        self.assertFalse(chosen["review_enabled"])
        self.assertEqual(chosen["review_profile"]["model"], "gpt-6.1-terra")
        again = bind_options(f.product, f.paths.tasks, review_enabled=True, selected_profile=profile())
        self.assertEqual(again, chosen)
        from soutui_image.workflow_options import options_path
        options_path(f.product, f.paths.tasks).unlink()
        f.write_job(1)
        old = read_options(f.product, f.paths.tasks)
        self.assertTrue(old["review_enabled"])
        self.assertTrue(old["review_profile"]["legacy"])
        self.assertEqual(old["review_profile"]["locator_effort"], "xhigh")

    def test_changed_original_cannot_bind_legacy_and_restoring_original_can_resume(self):
        f = self.f
        job_path, _ = f.write_job(1)
        original_job = job_path.read_bytes()
        source = Path(f.product.source_path)
        original_image = source.read_bytes()
        Image.new("RGB", (12, 12), "black").save(source)
        replacement = scan_inputs(source.parent).products[0]
        self.assertNotEqual(replacement.sha256, f.product.sha256)
        with self.assertRaisesRegex(ValueError, "旧生成记录"):
            bind_options(replacement, f.paths.tasks, selected_profile=profile("gpt-6.1-terra"))
        self.assertFalse(options_path(f.product, f.paths.tasks).exists())
        self.assertEqual(job_path.read_bytes(), original_job)

        source.write_bytes(original_image)
        restored = scan_inputs(source.parent).products[0]
        actual = bind_options(restored, f.paths.tasks, review_enabled=False, selected_profile=profile("gpt-6.1-terra"))
        self.assertEqual(actual["source_sha256"], f.product.sha256)
        self.assertTrue(actual["review_enabled"])
        self.assertTrue(actual["review_profile"]["legacy"])
        self.assertEqual(actual["review_profile"]["model"], "gpt-6.1-sol")
        self.assertEqual(job_path.read_bytes(), original_job)
        self.assertEqual(source.read_bytes(), original_image)

    def test_legacy_binding_checks_every_existing_group_and_round(self):
        f = self.f
        f.write_job(1)
        later_path, later = f.write_job(9, round_index=1)
        for field, value in (("product_id", "002"), ("source_sha256", "f" * 64)):
            altered = {**later, field: value}
            later_path.write_text(json.dumps(altered), encoding="utf-8")
            before = later_path.read_bytes()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "旧生成记录"):
                bind_options(f.product, f.paths.tasks)
            self.assertFalse(options_path(f.product, f.paths.tasks).exists())
            self.assertEqual(later_path.read_bytes(), before)


if __name__ == "__main__": unittest.main()
