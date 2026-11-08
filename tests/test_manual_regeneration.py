"""手动补图调度与恢复边界，假网络返回配合真实本地图片保存；不消耗网站积分。"""
import copy
import hashlib
import json
import unittest
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch
from PIL import Image

import test_regeneration_files as fixture
from soutui_image.manual_regeneration import run_regeneration
from soutui_image.output_store import read_manifest
from soutui_image.pipeline import UserPaused
from soutui_image.review_images import inspect_image
from soutui_image.stage_b_web_upload import WebUploadReceipt
from soutui_image.stage_c_web_plan import FavoriteGroup


class ManualRegenerationTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.RegenerationFilesTests("test_actual_zero_two_three_counts_and_original_template")
        self.fx.setUp(); self.addCleanup(self.fx.doCleanups)
        self.f = f = self.fx.f
        self.fx.save(1, 2); self.fx.save(2, 3)
        receipt = WebUploadReceipt(f.product.product_id, f.product.source_path, f.product.sha256, "套装",
             "https://wxhook.oss-cn-shenzhen.aliyuncs.com/inputs/test-original.png", "PNG", Path(f.product.source_path).stat().st_size,
             f.product.sha256, 0.1)
        (f.paths.tasks / "product_001_upload.json").write_text(json.dumps({"receipt":asdict(receipt)}), encoding="utf-8")
        self.client = Mock()
        self.client.request_json.return_value = {"success":True,"enabled":True,"features":["model-dressing"],
            "tiers":[{"id":"balanced","available":True,"availableResolutions":["2K","4K"],"prices":{"2K":6,"4K":10}}]}
        self.sent = []
        self.original = {p:p.read_bytes() for p in f.paths.tasks.rglob("*.json")}

    def result(self, path, statuses=("done",)*5):
        job = json.loads(path.read_text(encoding="utf-8"))
        job.update(state="generating" if "pending" in statuses else "awaiting_audit" if "done" in statuses else "generation_failed",
                   task_id=job["client_task_id"], successful_results=statuses.count("done"), failed_results=statuses.count("error"))
        job["results"] = [{"result_id":job["client_task_id"]+f"/slot-{i}","slot_id":f"slot-{i}","reference_index":i,
                           "reference_id":job["reference_ids"][i],"status":status,"image_url":"https://example.test/result.png"}
                          for i,status in enumerate(statuses)]
        path.write_text(json.dumps(job), encoding="utf-8")
        return job

    def submit(self, path, client):
        self.sent.append(copy.deepcopy(json.loads(path.read_text(encoding="utf-8"))))
        return self.result(path)

    def download(self, pid, result, root):
        root.mkdir(parents=True, exist_ok=True)
        path = root / (str(result["reference_index"]) + ".png")
        color = tuple(hashlib.sha256(result["result_id"].encode()).digest()[:3])
        Image.new("RGB", (16,16), color).save(path)
        return inspect_image(pid, result["result_id"], path)

    def run_manual(self, *, submit=None, query=None):
        f = self.f
        with ExitStack() as s:
            for name in ("classify_clothing", "review_candidate"):
                s.enter_context(patch("soutui_image.pipeline."+name, side_effect=AssertionError("重新生图不能调用模型")))
            s.enter_context(patch("soutui_image.manual_regeneration.WebTransport", return_value=self.client))
            s.enter_context(patch("soutui_image.manual_regeneration.MusefitReadClient"))
            s.enter_context(patch("soutui_image.manual_regeneration.parse_favorites", return_value=tuple(
                FavoriteGroup(g.folder_id,g.group_name,g.references) for g in f.plan.assignments)))
            s.enter_context(patch("soutui_image.manual_regeneration.verify_remote_image"))
            s.enter_context(patch("soutui_image.manual_regeneration.submit_job", side_effect=submit or self.submit))
            s.enter_context(patch("soutui_image.manual_regeneration.query_job", side_effect=query or (lambda p,c:self.result(p))))
            s.enter_context(patch("soutui_image.manual_regeneration.download_result", side_effect=self.download))
            return run_regeneration(f.pipeline,f.product,poll_interval=.01,generation_timeout=3)

    def test_shortage_only_original_template_all_saved_without_review_and_second_click_no_work(self):
        self.f.pipeline.review_enabled = True
        result = self.run_manual()
        self.assertEqual(result["status"],"complete")
        self.assertEqual(len(self.sent),1)
        job = self.sent[0]
        self.assertEqual((job["row"],job["folder_id"],job["group_name"]),(1,"17","搜推模特17"))
        self.assertEqual(job["payload"]["params"]["inputImageUrls2"], [r.url for r in self.f.plan.assignments[0].references])
        record = read_manifest(self.fx.output,self.f.paths.tasks,self.f.product)
        self.assertEqual(len(record["groups"][0]["saved"]),7)
        self.assertTrue(all(i["review_status"]=="unreviewed" for i in record["groups"][0]["saved"]))
        self.assertEqual(self.run_manual()["new_rounds"],0)
        self.assertEqual(len(self.sent),1)
        self.assertEqual({p:p.read_bytes() for p in self.original}, self.original)

    def test_manual_can_exceed_three_and_never_resets_original_rounds(self):
        for i in (1,2,3): self.f.write_job(1,round_index=i)
        originals = {p:p.read_bytes() for p in self.f.paths.tasks.rglob("*.json")}
        self.run_manual()
        self.assertEqual(self.sent[0]["round_index"],4)
        for path in sorted((self.fx.output/"组01").glob("*.png"))[2:]: path.unlink()
        self.run_manual()
        self.assertEqual(self.sent[1]["round_index"],5)
        self.assertNotEqual(self.sent[0]["client_task_id"], self.sent[1]["client_task_id"])
        self.assertEqual(self.sent[0]["reference_ids"], self.sent[1]["reference_ids"])
        self.assertEqual({p:p.read_bytes() for p in originals}, originals)

    def test_unknown_submission_resume_only_queries_never_reposts(self):
        def unknown(path, client):
            self.sent.append(json.loads(path.read_text()))
            job=json.loads(path.read_text()); job["state"]="submission_unknown"
            path.write_text(json.dumps(job),encoding="utf-8")
            raise TimeoutError("模拟已发出POST但响应未知")
        with self.assertRaises(TimeoutError): self.run_manual(submit=unknown)
        query = Mock(side_effect=lambda p,c:self.result(p))
        self.run_manual(submit=Mock(side_effect=AssertionError("禁止重复POST")),query=query)
        self.assertEqual(len(self.sent),1); self.assertEqual(query.call_count,1)

    def test_partial_generation_saves_before_final_slot_and_queries_continue(self):
        def submit(path,client):
            self.sent.append(json.loads(path.read_text()))
            return self.result(path,("done","pending","pending","pending","pending"))
        def query(path,client):
            return self.result(path) if any(e["kind"]=="generated_saved" for e in self.f.events) else json.loads(path.read_text())
        self.run_manual(submit=submit,query=query)
        kinds=[e["kind"] for e in self.f.events]
        self.assertLess(kinds.index("generated_saved"),kinds.index("generation_saved_complete"))

    def test_pause_then_resume_same_round_and_corrupt_pointer_blocks(self):
        def notify(event):
            self.f.events.append(event)
            if event["kind"]=="generated_saved": self.f.pipeline.stop.set()
        self.f.pipeline.on_event=notify
        with self.assertRaises(UserPaused): self.run_manual()
        self.f.pipeline.stop.clear(); self.f.pipeline.on_event=self.f.events.append
        self.run_manual(submit=Mock(side_effect=AssertionError("恢复不能重发")))
        self.assertEqual(len(self.sent),1)
        pointer=self.f.paths.root/"manual_regeneration"/"001"/"active.json"
        pointer.write_text('{"run_id":"../../escape"}',encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"指针损坏"): self.run_manual()

    def test_failed_slots_end_one_round_without_automatic_next_round(self):
        def fail(path,client):
            self.sent.append(json.loads(path.read_text()))
            return self.result(path,("error",)*5)
        self.run_manual(submit=fail)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(len(list((self.fx.output/"组01").glob("*.png"))),2)


if __name__ == "__main__": unittest.main()
