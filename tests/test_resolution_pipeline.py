"""出图尺寸贯穿统一入口和两种调度；不向网站提交实际任务。"""

import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import test_streaming_pipeline as fixtures
from soutui_image.pipeline import Pipeline
from soutui_image.generation_only import run_generation_only
from soutui_image.streaming_pipeline import run_streaming_product
from soutui_image.review_images import ReviewImage


class ResolutionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.StreamingPipelineTests("test_first_image_review_and_save_overlap_unfinished_generation")
        self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def test_product_pins_resolution_before_work_and_keeps_it_on_resume(self):
        f = self.f
        first = Pipeline(f.paths, resolution="4K", on_event=f.events.append)
        def work(product, rows):
            return first.active_options["resolution"]
        with patch.object(first, "_run_product_with_rules", side_effect=work):
            self.assertEqual(first.run_product(f.product, (1,)), "4K")
        second = Pipeline(f.paths, resolution="2K")
        with patch.object(second, "_run_product_with_rules", side_effect=lambda p,r:second.active_options["resolution"]):
            self.assertEqual(second.run_product(f.product, (1,)), "4K")
        self.assertEqual(next(e for e in f.events if e["kind"]=="run_options")["resolution"], "4K")

    def _run_mode(self, mode, prepared=False):
        f = self.f
        f.pipeline.active_options={"resolution":"4K", "legacy":False}
        module="soutui_image."+("generation_only" if mode=="raw" else "streaming_pipeline")
        supplied=[]
        def prepare(product, upload, plan, row, directory, client, *, resolution):
            supplied.append(resolution)
            return f.write_job(row, ("pending",)*5, state="prepared", resolution=resolution)
        def submit(path, client):
            job=json.loads(path.read_text())
            self.assertEqual(job["payload"]["params"]["resolution"],"4K")
            return f.write_job(job["row"], resolution="4K")[1]
        def download(pid,result,root):
            job=json.loads(f.job_path(1).read_text())
            return ReviewImage(**f.outcome(f.product,job,result)["candidate"])
        with ExitStack() as stack:
            stack.enter_context(patch(module+".prepare_job",side_effect=prepare))
            submitted=stack.enter_context(patch(module+".submit_job",side_effect=submit))
            stack.enter_context(patch(module+".query_job",side_effect=AssertionError("本例提交后直接完成")))
            if mode=="raw":
                stack.enter_context(patch(module+".download_result",side_effect=download))
                run=run_generation_only
            else:
                stack.enter_context(patch.object(f.pipeline,"_review_one",side_effect=f.outcome))
                run=run_streaming_product
            result=run(f.pipeline,f.product,f.plan,f.upload,[f.plan.assignments[0]],f.client,poll_interval=.01,generation_timeout=3)
        self.assertEqual(supplied,["4K"])
        self.assertEqual(submitted.call_count,1)
        self.assertEqual(result[0]["state"],"complete")

    def test_generation_only_submits_selected_4k(self):
        self._run_mode("raw")

    def test_generation_with_review_submits_selected_4k(self):
        self._run_mode("review")

    def test_mismatched_existing_task_stops_before_site_or_review(self):
        f=self.f
        f.write_job(1,("pending",)*5,state="prepared",resolution="4K")
        f.pipeline.active_options={"resolution":"2K"}
        for name,run in (("generation_only",run_generation_only),("streaming_pipeline",run_streaming_product)):
            with self.subTest(mode=name), ExitStack() as stack:
                for action in ("submit_job","query_job","prepare_job"):
                    stack.enter_context(patch("soutui_image."+name+"."+action,side_effect=AssertionError("不应请求网站")))
                stack.enter_context(patch.object(f.pipeline,"_review_one",side_effect=AssertionError("不应审图")))
                with self.assertRaises(ValueError):
                    run(f.pipeline,f.product,f.plan,f.upload,[f.plan.assignments[0]],f.client,poll_interval=.01,generation_timeout=3)


if __name__=="__main__":
    unittest.main()
