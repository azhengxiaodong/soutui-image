"""流式编排控制验证：假网络/审核只用于调度边界，真实保存逻辑仍运行。"""

import copy
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from soutui_image.pipeline import Pipeline, PipelinePaths, UserPaused
from soutui_image.review_images import inspect_image
from soutui_image.rule_runtime import current_policy
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_c_web_plan import GroupAssignment, ProductReferencePlan, ReferenceImage
from soutui_image.stage_d_web_generation import group_decision
from soutui_image.stage_e_codex_review import FEATURES
from soutui_image.streaming_pipeline import run_streaming_product
from soutui_image.output_store import read_manifest


class StreamingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        inputs = self.root / "input"
        inputs.mkdir()
        Image.new("RGB", (12, 12), "white").save(inputs / "001.png")
        self.product = scan_inputs(inputs).products[0]
        self.paths = PipelinePaths(self.root / "state")
        groups = tuple(GroupAssignment(row, str(row), f"测试组{row}", tuple(
            ReferenceImage(f"ref-{row}-{slot}", f"https://example.test/{row}/{slot}.png")
            for slot in range(5))) for row in range(1, 10))
        self.plan = ProductReferencePlan(1, "001", self.product.source_path, self.product.sha256, 9, groups)
        self.events = []
        self.pipeline = Pipeline(self.paths, on_event=self.events.append)
        self.upload = object()
        self.client = object()

    def job_path(self, row, round_index=0):
        return self.paths.tasks / "001" / f"group_{row:02}" / f"round_{round_index:02}.json"

    def cache_path(self, row, round_index=0):
        return self.paths.cache / "001" / f"group_{row:02}" / f"round_{round_index:02}"

    def write_job(self, row, statuses=("done",) * 5, *, state=None, round_index=0, resolution="2K"):
        group = self.plan.assignments[row - 1]
        task = f"task-{row}-{round_index}"
        record = {"schema_version": 1, "product_id": "001", "source_sha256": self.product.sha256,
                  "row": row, "round_index": round_index, "task_id": task, "client_task_id": task,
                  "folder_id": group.folder_id, "group_name": group.group_name,
                  "reference_ids": [r.image_id for r in group.references], "expected_results": 5,
                  "quoted_credits": 30, "successful_results": statuses.count("done"),
                  "payload": {"params": {"resolution": resolution}},
                  "failed_results": statuses.count("error"), "state": state or (
                      "generating" if "pending" in statuses else "awaiting_audit" if "done" in statuses else "generation_failed"),
                  "results": [{"result_id": f"{task}/slot-{i}", "slot_id": f"slot-{i}",
                               "reference_index": i, "reference_id": group.references[i].image_id,
                               "status": status, "image_url": f"https://example.test/{task}/{i}.png" if status == "done" else None}
                              for i, status in enumerate(statuses)]}
        path = self.job_path(row, round_index)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(path)
        return path, record

    def outcome(self, product, job, result):
        """产生显式测试证据及实际小图，使身份校验和保存边界可正常运行。"""
        directory = self.cache_path(job["row"], job["round_index"])
        directory.mkdir(parents=True, exist_ok=True)
        image = directory / f"test-{result['reference_index']}.png"
        Image.new("RGB", (12, 12), (job["row"] * 17, result["reference_index"] * 35, job["round_index"] * 30)).save(image)
        candidate = inspect_image(product.product_id, result["result_id"], image)
        verdict = {"product_id": product.product_id, "result_id": candidate.result_id, "decision": "pass",
                   "visible_scope": "仅单元测试假证据", "unshown_parts": [], "summary": "仅单元测试假证据",
                   "checks": [{"feature": feature, "visibility": "partial", "comparison": "match",
                               "original": "测试原款", "generated": "测试结果", "reason": "测试调度边界"}
                              for feature in FEATURES]}
        review = {"policy_version": current_policy(), "product_id": product.product_id,
                  "result_id": candidate.result_id, "source_sha256": product.sha256,
                  "result_sha256": candidate.sha256, "decision": "pass", "summary": "仅单元测试假证据",
                  "passes": [verdict, copy.deepcopy(verdict)]}
        return {"candidate": asdict(candidate), "review": review,
                "reference_id": result["reference_id"], "reference_index": result["reference_index"]}

    def run_stream(self, rows, *, review=None, query=None, prepare=None, submit=None, generation_timeout=5):
        def read(path, client):
            return json.loads(path.read_text(encoding="utf-8"))
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.pipeline, "_review_one", side_effect=review or self.outcome))
            stack.enter_context(patch("soutui_image.streaming_pipeline.query_job", side_effect=query or read))
            stack.enter_context(patch("soutui_image.streaming_pipeline.prepare_job", side_effect=prepare or AssertionError("不应额外生图")))
            stack.enter_context(patch("soutui_image.streaming_pipeline.submit_job", side_effect=submit or AssertionError("不应额外提交")))
            return run_streaming_product(self.pipeline, self.product, self.plan, self.upload,
                                         [self.plan.assignments[row - 1] for row in rows], self.client,
                                         poll_interval=0.01, generation_timeout=generation_timeout)

    def test_first_image_review_and_save_overlap_unfinished_generation(self):
        self.write_job(1, ("done", "pending", "pending", "pending", "pending"))
        review_started, polling_continues, first_saved = (threading.Event() for _ in range(3))
        original_save = self.pipeline._save_all

        def review(product, job, result):
            if result["reference_index"] == 0:
                self.assertEqual(job["state"], "generating")
                review_started.set()
                self.assertTrue(polling_continues.wait(3), "审图运行时后台轮询被阻塞")
            return self.outcome(product, job, result)

        def query(path, client):
            if first_saved.is_set():
                return self.write_job(1)[1]
            if review_started.is_set():
                polling_continues.set()
            return json.loads(path.read_text(encoding="utf-8"))

        def save(product, plan):
            result = original_save(product, plan)
            if not first_saved.is_set():
                self.assertEqual(json.loads(self.job_path(1).read_text())["state"], "generating")
                self.assertFalse((self.cache_path(1) / "round_review.json").exists())
                self.assertTrue((self.cache_path(1) / "partial_review.json").exists())
                self.assertEqual(result["total_saved"], 1)
                first_saved.set()
            return result

        with patch.object(self.pipeline, "_save_all", side_effect=save):
            result = self.run_stream([1], review=review, query=query)
        self.assertTrue(first_saved.is_set())
        self.assertEqual(result[0]["approved_count"], 5)

    def test_free_workers_take_other_groups_before_slow_image_finishes(self):
        self.write_job(1)
        self.write_job(2)
        four_started, other_group_started = threading.Event(), threading.Event()
        lock = threading.Lock()
        active = peak = 0

        def review(product, job, result):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 4:
                    four_started.set()
            try:
                if job["row"] == 1 and result["reference_index"] < 4:
                    self.assertTrue(four_started.wait(3), "没有使用全局四个审核位置")
                if job["row"] == 1 and result["reference_index"] == 0:
                    self.assertTrue(other_group_started.wait(3), "其他组被首组慢图挡住")
                elif job["row"] == 2:
                    other_group_started.set()
                return self.outcome(product, job, result)
            finally:
                with lock:
                    active -= 1

        result = self.run_stream([1, 2], review=review)
        self.assertEqual(peak, 4)
        self.assertEqual([item["approved_count"] for item in result], [5, 5])

    def test_pause_drains_current_image_but_starts_no_waiting_image(self):
        self.write_job(1)
        self.pipeline.review_workers = 1
        reviewed = []

        def review(product, job, result):
            reviewed.append(result["reference_index"])
            self.pipeline.stop.set()
            return self.outcome(product, job, result)

        with self.assertRaises(UserPaused):
            self.run_stream([1], review=review)
        self.assertEqual(reviewed, [0])
        partial = json.loads((self.cache_path(1) / "partial_review.json").read_text(encoding="utf-8"))
        self.assertEqual(len(partial["outcomes"]), 1)
        self.assertFalse(partial["all_successful_images_reviewed"])
        self.assertFalse((self.cache_path(1) / "round_review.json").exists())
        saved = read_manifest(Path(self.product.source_path).parent / "001", self.paths.tasks, self.product)
        self.assertEqual(len(saved["groups"][0]["saved"]), 1)

    def test_review_failure_still_saves_running_peer_without_completing_round(self):
        self.write_job(1)
        self.pipeline.review_workers = 2
        peer_started, error_observed = threading.Event(), threading.Event()
        self.pipeline.on_event = lambda event: error_observed.set() if event["kind"] == "review_image_error" else None
        reviewed = []

        def review(product, job, result):
            reviewed.append(result["reference_index"])
            if result["reference_index"] == 0:
                self.assertTrue(peer_started.wait(3))
                raise RuntimeError("测试网络中断")
            peer_started.set()
            self.assertTrue(error_observed.wait(3))
            return self.outcome(product, job, result)

        with self.assertRaisesRegex(RuntimeError, "测试网络中断"):
            self.run_stream([1], review=review)
        self.assertEqual(sorted(reviewed), [0, 1])
        partial = json.loads((self.cache_path(1) / "partial_review.json").read_text(encoding="utf-8"))
        self.assertEqual([item["reference_index"] for item in partial["outcomes"]], [1])
        self.assertFalse((self.cache_path(1) / "round_review.json").exists())
        self.assertFalse((self.job_path(1).parent / "review_summary.json").exists())

    def test_resume_partial_does_not_repeat_completed_image(self):
        _, job = self.write_job(1)
        completed = self.outcome(self.product, job, job["results"][0])
        self.pipeline._write_round_report(self.product, job, [completed], self.cache_path(1), complete=False)
        self.pipeline._save_all(self.product, self.plan)
        reviewed = []

        def review(product, current_job, result):
            reviewed.append(result["reference_index"])
            return self.outcome(product, current_job, result)

        result = self.run_stream([1], review=review)
        self.assertEqual(sorted(reviewed), [1, 2, 3, 4])
        self.assertEqual(result[0]["approved_count"], 5)
        self.assertFalse((self.cache_path(1) / "partial_review.json").exists())

    def test_notification_failure_drains_reviews_saves_results_and_keeps_first_error(self):
        for row, trigger in enumerate(("stream_progress", "image_reviewed", "review_image_error", "reviewing"), 1):
            with self.subTest(notification=trigger):
                self.write_job(row)
                self.pipeline.review_workers = 2
                peer_started, notification_failed = threading.Event(), threading.Event()
                reviewed = []

                def notify(event):
                    if event["kind"] != trigger or notification_failed.is_set():
                        return
                    if trigger == "reviewing" and event["result_id"].endswith("/slot-0"):
                        return
                    notification_failed.set()
                    raise OSError("测试进度日志写入失败")

                def review(product, job, result):
                    index = result["reference_index"]
                    reviewed.append(index)
                    self.assertLess(index, 2, "通知失败后不应再启动等待中的审核")
                    if index == 0 and trigger in ("image_reviewed", "review_image_error"):
                        self.assertTrue(peer_started.wait(3))
                        if trigger == "review_image_error":
                            raise RuntimeError("测试原始审核错误")
                    else:
                        if index == 1:
                            peer_started.set()
                        self.assertTrue(notification_failed.wait(3))
                    return self.outcome(product, job, result)

                self.pipeline.on_event = notify
                error_type, message = ((RuntimeError, "测试原始审核错误") if trigger == "review_image_error"
                                       else (OSError, "测试进度日志写入失败"))
                with self.assertRaisesRegex(error_type, message):
                    self.run_stream([row], review=review,
                                    query=lambda *args: self.fail("终态任务不应查询网站"))
                self.assertTrue(notification_failed.is_set())
                self.assertEqual(sorted(reviewed), [0] if trigger == "reviewing" else [0, 1])
                expected = [1] if trigger == "review_image_error" else [0] if trigger == "reviewing" else [0, 1]
                partial = json.loads((self.cache_path(row) / "partial_review.json").read_text(encoding="utf-8"))
                self.assertEqual(sorted(item["reference_index"] for item in partial["outcomes"]), expected)
                self.assertFalse((self.cache_path(row) / "round_review.json").exists())
                saved = read_manifest(Path(self.product.source_path).parent / "001", self.paths.tasks, self.product)
                self.assertEqual(len(saved["groups"][row - 1]["saved"]), len(expected))

    def test_corrupt_partial_slot_binding_stops_before_new_review(self):
        _, job = self.write_job(1)
        completed = self.outcome(self.product, job, job["results"][0])
        completed["reference_index"] = 4
        self.pipeline._write_round_report(self.product, job, [completed], self.cache_path(1), complete=False)
        with self.assertRaisesRegex(ValueError, "断点图片"):
            self.run_stream([1], review=lambda *args: self.fail("损坏断点不得触发新审核"),
                            query=lambda *args: self.fail("损坏断点不得触发网站查询"))
        self.assertFalse((self.cache_path(1) / "round_review.json").exists())

    def test_polling_failure_drains_running_review_without_resubmission(self):
        self.write_job(1, ("done", "pending", "pending", "pending", "pending"))
        review_started, query_failed = threading.Event(), threading.Event()

        def review(product, job, result):
            review_started.set()
            self.assertTrue(query_failed.wait(3))
            return self.outcome(product, job, result)

        def query(path, client):
            self.assertTrue(review_started.wait(3))
            query_failed.set()
            raise RuntimeError("测试网站查询失败")

        with self.assertRaisesRegex(RuntimeError, "测试网站查询失败"):
            self.run_stream([1], review=review, query=query)
        partial = json.loads((self.cache_path(1) / "partial_review.json").read_text(encoding="utf-8"))
        self.assertEqual(len(partial["outcomes"]), 1)
        self.assertFalse((self.cache_path(1) / "round_review.json").exists())
        self.assertEqual(json.loads(self.job_path(1).read_text())["task_id"], "task-1-0")

    def test_generation_timeout_drains_slow_review_and_preserves_partial(self):
        self.write_job(1, ("done", "pending", "pending", "pending", "pending"))
        review_started, deadline_observed = threading.Event(), threading.Event()

        def monotonic():
            # 仅推进调度器的时钟；不修改并发库或真实审核/保存所用的全局时间。
            if review_started.is_set():
                deadline_observed.set()
                return 100.0
            return 0.0

        def review(product, job, result):
            review_started.set()
            self.assertTrue(deadline_observed.wait(3), "调度器没有在慢审核期间检查生成期限")
            return self.outcome(product, job, result)

        with patch("soutui_image.streaming_pipeline.time", SimpleNamespace(monotonic=monotonic)):
            with self.assertRaisesRegex(TimeoutError, "生成跟踪"):
                self.run_stream([1], review=review, generation_timeout=5,
                                query=lambda *args: self.fail("生成期限已到，不得再查询或提交"))
        partial = json.loads((self.cache_path(1) / "partial_review.json").read_text(encoding="utf-8"))
        self.assertEqual(len(partial["outcomes"]), 1)
        self.assertFalse((self.cache_path(1) / "round_review.json").exists())
        saved = read_manifest(Path(self.product.source_path).parent / "001", self.paths.tasks, self.product)
        self.assertEqual(len(saved["groups"][0]["saved"]), 1)

    def test_terminal_generation_does_not_time_out_while_review_is_slow(self):
        self.write_job(1)
        self.pipeline.review_workers = 1
        current_time = [0.0]
        reviewed = []

        def review(product, job, result):
            # 生成已终态，后续所有审核的模拟时刻均超过原生成期限。
            current_time[0] = 100.0
            reviewed.append(result["reference_index"])
            return self.outcome(product, job, result)

        with patch("soutui_image.streaming_pipeline.time", SimpleNamespace(monotonic=lambda: current_time[0])):
            result = self.run_stream([1], review=review, generation_timeout=5,
                                     query=lambda *args: self.fail("终态任务不应继续轮询"))
        self.assertEqual(reviewed, list(range(5)))
        self.assertEqual(result[0]["approved_count"], 5)
        report = json.loads((self.cache_path(1) / "round_review.json").read_text(encoding="utf-8"))
        self.assertTrue(report["all_successful_images_reviewed"])

    def test_replenishment_waits_for_terminal_slots_and_full_review(self):
        self.write_job(1, ("error",) * 5)
        submitted = []

        def prepare(product, upload, plan, row, directory, client, *, resolution="2K"):
            decision = group_decision(directory, product, plan.assignments[row - 1])
            self.assertEqual(decision.next_round, 1)
            report = json.loads((self.cache_path(1) / "round_review.json").read_text(encoding="utf-8"))
            self.assertTrue(report["all_successful_images_reviewed"])
            self.assertEqual(report["outcomes"], [])
            return self.write_job(1, ("pending",) * 5, state="prepared", round_index=1)

        def submit(path, client):
            submitted.append(path)
            return self.write_job(1, round_index=1)[1]

        result = self.run_stream([1], prepare=prepare, submit=submit)
        self.assertEqual(submitted, [self.job_path(1, 1)])
        self.assertEqual(result[0]["approved_count"], 5)
        self.assertEqual(len(list(self.job_path(1).parent.glob("round_*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
