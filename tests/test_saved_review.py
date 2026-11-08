"""单独审图编排的删留、配置续跑和排空边界；模拟模型不作为准确率验收。"""

import copy
import json
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_saved_images as fixtures
from soutui_image.output_store import read_manifest
from soutui_image.review_profiles import current_profile, profile
from soutui_image.saved_images import apply_saved_review
from soutui_image.saved_review import run_saved_review


class SavedReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SavedImagesTests("test_all_generated_images_saved_without_becoming_approved")
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.fixture
        self.paths = SimpleNamespace(root=self.f.root, tasks=self.f.state, cache=self.f.cache,
                                     reviews=self.f.root / "reviews")
        for index in range(3): self.fixture.save(index)
        self.events = []
        self.pipeline = SimpleNamespace(paths=self.paths, stop=threading.Event(), review_workers=1,
                                        emit=lambda kind, message, **data: self.events.append({"kind": kind, **data}))
        self.calls = []

    def verdict(self, product, candidate, directory, model=None):
        self.calls.append((candidate.result_id, model, current_profile().to_dict()))
        raw = next(copy.deepcopy(x["review"]) for x in self.fixture.report["outcomes"]
                   if x["candidate"]["result_id"] == candidate.result_id)
        raw["review_profile"] = current_profile().to_dict()
        return raw

    def run_review(self, model="gpt-6.1-sol"):
        return run_saved_review(self.pipeline, self.f.product, profile(model))

    def test_complete_keeps_pass_and_uncertain_deletes_only_reject_without_site(self):
        before={p:p.read_bytes() for p in self.f.output.rglob("*.png")}
        source=Path(self.f.product.source_path).read_bytes()
        with patch("soutui_image.saved_review.review_candidate",side_effect=self.verdict), \
                patch("soutui_image.web_transport.WebTransport.request_json",side_effect=AssertionError("不得连接网站")), \
                patch("soutui_image.stage_d_web_generation.submit_job",side_effect=AssertionError("不得补生")):
            result=self.run_review()
        self.assertEqual(result["status"],"complete")
        self.assertEqual(result["new_generation_submissions"],0)
        self.assertEqual(result["counts"][1],{"saved":2,"pass":1,"review":1,"unreviewed":0})
        self.assertEqual(len(self.calls),3)
        self.assertEqual(Path(self.f.product.source_path).read_bytes(),source)
        self.assertTrue(all(p.read_bytes()==raw for p,raw in before.items() if p.exists()))
        self.assertFalse((self.f.output/".搜推生图.json").exists())
        self.assertFalse((self.f.output/".soutui.lock").exists())

    def test_pause_resume_keeps_original_profile_then_completed_run_can_switch(self):
        def pause_first(kind,message,**data):
            self.events.append({"kind":kind,**data})
            if kind=="saved_review_image": self.pipeline.stop.set()
        self.pipeline.emit=pause_first
        with patch("soutui_image.saved_review.review_candidate",side_effect=self.verdict):
            paused=self.run_review()
        self.assertEqual(paused["status"],"paused"); self.assertEqual(len(self.calls),1)
        self.pipeline.stop.clear()
        self.pipeline.emit=lambda kind,message,**data:self.events.append({"kind":kind,**data})
        with patch("soutui_image.saved_review.review_candidate",side_effect=self.verdict):
            resumed=self.run_review("gpt-6.1-terra")
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["model"],"gpt-6.1-sol")
        self.assertEqual(resumed["status"],"complete")
        self.assertEqual(len(self.calls),3)  # 第1张由操作断点复用，未再次调用模型。
        with patch("soutui_image.saved_review.review_candidate",side_effect=self.verdict):
            fresh=self.run_review("gpt-6.1-terra")
        self.assertFalse(fresh["resumed"])
        self.assertEqual(fresh["model"],"gpt-6.1-terra")
        self.assertEqual(len(self.calls),5)  # 已删除图不在扫描集合，其余两张重新进入所选模型。
        self.assertNotEqual(paused["operation_path"],fresh["operation_path"])
        self.assertTrue(Path(paused["operation_path"]).exists())

    def test_network_error_preserves_images_and_stops_new_work_then_resumes(self):
        with patch("soutui_image.saved_review.review_candidate",side_effect=TimeoutError("模型超时")) as call:
            failed=self.run_review()
        self.assertEqual(call.call_count,1)
        self.assertEqual(failed["status"],"needs_attention")
        self.assertEqual(failed["errors"],1)
        self.assertEqual(len(list(self.f.output.rglob("*.png"))),3)
        with patch("soutui_image.saved_review.review_candidate",side_effect=self.verdict):
            result=self.run_review("gpt-6.1-terra")
        self.assertEqual(result["model"],"gpt-6.1-sol")
        self.assertEqual(result["errors"],0)
        self.assertEqual(result["status"],"complete")

    def test_wrong_profile_response_is_never_applied(self):
        def wrong(*args,**kwargs):
            raw=self.verdict(*args,**kwargs); raw["review_profile"]=profile("gpt-6.1-terra").to_dict()
            return raw
        with patch("soutui_image.saved_review.review_candidate",side_effect=wrong):
            result=self.run_review()
        self.assertEqual(result["status"],"needs_attention")
        self.assertEqual(result["reviewed"],0)
        self.assertEqual(len(list(self.f.output.rglob("*.png"))),3)

    def test_persistence_error_is_raised_after_draining_inflight_peer(self):
        self.pipeline.review_workers=2
        started=threading.Barrier(2)
        def model(product,candidate,directory,model=None):
            started.wait(timeout=3)
            if candidate.result_id.endswith("slot-1"): time.sleep(.1)
            return self.verdict(product,candidate,directory,model)
        def apply(product,paths,item,review=None,error=None):
            if item["candidate"]["result_id"].endswith("slot-0"):
                raise PermissionError("模拟清单占用")
            return apply_saved_review(product,paths,item,review,error)
        with patch("soutui_image.saved_review.review_candidate",side_effect=model), \
                patch("soutui_image.saved_review.apply_saved_review",side_effect=apply):
            with self.assertRaisesRegex(PermissionError,"模拟清单占用"):
                self.run_review()
        self.assertEqual(len(self.calls),2)
        record=read_manifest(self.f.output,self.paths.tasks,self.f.product)
        self.assertEqual(record["removed"][0]["deletion_status"],"complete")
        self.assertEqual(len(record["groups"][0]["saved"]),2)
        operation=next((self.paths.root/"saved_reviews").glob("*/runs/*.json"))
        self.assertEqual(json.loads(operation.read_text(encoding="utf-8"))["status"],"needs_attention")

    def test_empty_slot_starts_next_image_without_waiting_for_slow_peer(self):
        self.pipeline.review_workers=2
        first_started=threading.Event(); third_started=threading.Event()
        def model(product,candidate,directory,model=None):
            if candidate.result_id.endswith("slot-0"):
                first_started.set()
                if not third_started.wait(3): raise AssertionError("空闲位置没有补入第三张")
            elif candidate.result_id.endswith("slot-1"):
                self.assertTrue(first_started.wait(3))
            else:
                third_started.set()
            return self.verdict(product,candidate,directory,model)
        with patch("soutui_image.saved_review.review_candidate",side_effect=model):
            result=self.run_review()
        self.assertEqual(result["status"],"complete")
        self.assertTrue(third_started.is_set())
        self.assertEqual(len(self.calls),3)

    def test_review_cache_write_error_drains_peer_and_raises(self):
        self.pipeline.review_workers=2
        started=threading.Barrier(2)
        def model(product,candidate,directory,model=None):
            started.wait(timeout=3)
            if candidate.result_id.endswith("slot-0"):
                raise PermissionError("审核缓存不能写入")
            time.sleep(.1)
            return self.verdict(product,candidate,directory,model)
        with patch("soutui_image.saved_review.review_candidate",side_effect=model):
            with self.assertRaisesRegex(PermissionError,"审核缓存不能写入"):
                self.run_review()
        self.assertEqual(len(self.calls),1)
        record=read_manifest(self.f.output,self.paths.tasks,self.f.product)
        self.assertEqual(record["removed"][0]["deletion_status"],"complete")
        self.assertEqual(len(record["groups"][0]["saved"]),2)

    def test_model_result_for_another_image_never_deletes_current_image(self):
        def wrong(*args,**kwargs):
            raw=self.verdict(*args,**kwargs); raw["result_id"]="other-image"
            return raw
        with patch("soutui_image.saved_review.review_candidate",side_effect=wrong):
            with self.assertRaisesRegex(ValueError,"身份不符"):
                self.run_review()
        self.assertEqual(len(list(self.f.output.rglob("*.png"))),3)

    def test_missing_output_finishes_without_model_call(self):
        product=copy.copy(self.f.product)
        from dataclasses import replace
        product=replace(product,product_id="002")
        with patch("soutui_image.saved_review.review_candidate",side_effect=AssertionError("不应调用")):
            result=run_saved_review(self.pipeline,product,profile())
        self.assertEqual(result["status"],"complete")
        self.assertEqual(result["total"],0)


if __name__ == "__main__":
    unittest.main()
