"""生成尺寸的请求/报价/续跑边界；全部网站响应均隔离，不提交真实生图。"""

import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock

import test_stage_d_generation as task_fixture
from soutui_image.generation_settings import existing_resolution, job_resolution, validate_resolution
from soutui_image.review_profiles import profile
from soutui_image.rule_runtime import current_policy
from soutui_image.stage_c_web_plan import ProductReferencePlan
from soutui_image.stage_d_web_generation import (
    _verify_task_parameters, build_payload, prepare_job, quote_balanced, quote_balanced_2k, submit_job,
)
from soutui_image.web_transport import RequestOutcomeUnknown
from soutui_image.workflow_options import bind_options, options_path, read_options


def prices():
    return {"success": True, "enabled": True, "features": ["model-dressing"], "tiers": [
        {"id": "balanced", "available": True, "availableResolutions": ["2K", "4K"],
         "prices": {"2K": 6, "4K": 10}}]}


class GenerationResolutionTests(unittest.TestCase):
    def setUp(self):
        self.f = task_fixture.GenerationTaskTests("test_fixed_parameters_and_one_result_per_reference")
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.tasks = self.f.root / "tasks"
        self.plan = ProductReferencePlan(1, self.f.product.product_id, self.f.product.source_path,
                                        self.f.product.sha256, 9,
                                        tuple(replace(self.f.group, row=row, folder_id=str(row)) for row in range(1, 10)))
        self.transport = Mock()
        self.transport.request_json.return_value = prices()

    def prepare(self, resolution="2K", row=1):
        return prepare_job(self.f.product, self.f.upload, self.plan, row, self.tasks,
                           self.transport, resolution=resolution)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_explicit_resolution_and_matching_current_quote(self):
        self.assertEqual(quote_balanced_2k(prices()), 6)
        self.assertEqual(quote_balanced(prices(), "4K"), 10)
        path, job = self.prepare("4K")
        params = job["payload"]["params"]
        self.assertEqual(params["resolution"], "4K")
        self.assertEqual(params["creditsCharged"], 50)
        self.assertEqual(job["quoted_credits"], 50)
        self.assertEqual(params["aspectRatio"], "3:4")
        self.assertEqual(params["extraParams"]["imageTier"], "balanced")
        self.assertEqual(params["extraParams"]["resultsPerReference"], "1")
        self.assertEqual(len(params["inputImageUrls2"]), 5)
        self.transport.request_json.assert_called_once_with("GET", "/api/web/image-tiers")

    def test_unsupported_or_unpriced_4k_never_falls_back_or_writes_job(self):
        for mutate in (lambda data: data["tiers"][0].update(availableResolutions=["2K"]),
                       lambda data: data["tiers"][0]["prices"].pop("4K"),
                       lambda data: data["tiers"][0]["prices"].update({"4K": 0}),
                       lambda data: data["tiers"][0]["prices"].update({"4K": True})):
            data = prices(); mutate(data)
            self.transport.request_json.return_value = data
            with self.assertRaises(ValueError): self.prepare("4K")
            self.assertFalse(list(self.tasks.rglob("round_*.json")))

    def test_invalid_resolution_rejected_before_network_or_write(self):
        for value in ("8K", "4k", "", None, True, ["4K"]):
            with self.subTest(value=value), self.assertRaises(ValueError): self.prepare(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_payload(self.f.product, self.f.upload, self.f.group, "id", 10, resolution=value)
        self.transport.request_json.assert_not_called()
        self.assertEqual(validate_resolution("2K"), "2K")

    def test_prepared_and_unknown_jobs_keep_resolution_and_task_id(self):
        path, job = self.prepare("4K")
        for state in ("prepared", "submission_unknown"):
            job["state"] = state; self.write(path, job)
            original = path.read_bytes(); self.transport.reset_mock()
            same_path, same_job = self.prepare("4K")
            self.assertEqual(same_path, path)
            self.assertEqual(same_job["client_task_id"], job["client_task_id"])
            self.assertEqual(path.read_bytes(), original)
            self.transport.request_json.assert_not_called()
            with self.assertRaisesRegex(ValueError, "固定分辨率"): self.prepare("2K")
            self.assertEqual(path.read_bytes(), original)
            self.transport.request_json.assert_not_called()

    def test_unknown_submit_still_only_queries(self):
        path, job = self.prepare("4K"); job["state"] = "submission_unknown"; self.write(path, job)
        self.transport.reset_mock()
        self.transport.request_json.return_value = {"success": True, "tasks": [], "hasMore": False}
        with self.assertRaises(RequestOutcomeUnknown): submit_job(path, self.transport)
        self.assertTrue(all(call.args[0] == "GET" for call in self.transport.request_json.call_args_list))
        self.assertEqual(job_resolution(json.loads(path.read_text(encoding="utf-8"))), "4K")

    def test_regeneration_and_other_groups_cannot_change_resolution(self):
        path, job = self.prepare("4K"); original = path.read_bytes()
        self.transport.reset_mock()
        with self.assertRaises(ValueError): self.prepare("2K", row=2)
        self.transport.request_json.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        job["state"] = "awaiting_audit"; self.write(path, job)
        summary = {k: job[k] for k in ("product_id", "source_sha256", "folder_id", "reference_ids")}
        summary.update(policy_version=current_policy(), approved_sha256=[], reviewed_rounds=[0])
        self.write(path.parent / "review_summary.json", summary)
        before = path.read_bytes()
        next_path, next_job = self.prepare("4K")
        self.assertEqual(next_job["round_index"], 1)
        self.assertEqual(job_resolution(next_job), "4K")
        self.assertEqual(path.read_bytes(), before)
        self.assertNotEqual(next_path, path)

    def test_server_cannot_silently_return_2k_for_4k_job(self):
        _, job = self.prepare("4K")
        params = job["payload"]["params"]
        task = {k: copy.deepcopy(params[k]) for k in ("resolution", "aspectRatio", "inputImageUrls", "inputImageUrls2", "extraParams")}
        task.update(featureType="model-dressing", expectedResultCount=5)
        _verify_task_parameters(task, job)
        task["resolution"] = "2K"
        with self.assertRaisesRegex(ValueError, "resolution"): _verify_task_parameters(task, job)

    def test_new_workflow_pins_choice_and_prepare_enforces_it(self):
        chosen = bind_options(self.f.product, self.tasks, resolution="4K")
        self.assertEqual(chosen["resolution"], "4K")
        original = options_path(self.f.product, self.tasks).read_bytes()
        resumed = bind_options(self.f.product, self.tasks, resolution="2K", selected_profile=profile("gpt-6.1-terra"))
        self.assertEqual(resumed, chosen)
        self.assertEqual(options_path(self.f.product, self.tasks).read_bytes(), original)
        with self.assertRaisesRegex(ValueError, "任务配置"): self.prepare("2K")
        self.transport.request_json.assert_not_called()
        self.assertEqual(job_resolution(self.prepare("4K")[1]), "4K")

    def test_028_workflow_missing_resolution_is_read_only_2k(self):
        chosen = bind_options(self.f.product, self.tasks)
        chosen.pop("resolution"); path = options_path(self.f.product, self.tasks); self.write(path, chosen)
        original = path.read_bytes()
        result = bind_options(self.f.product, self.tasks, resolution="4K")
        self.assertEqual(result["resolution"], "2K")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(job_resolution(self.prepare()[1]), "2K")

    def test_legacy_job_resolution_is_inferred_without_modifying_rounds(self):
        path, job = self.prepare()
        original = path.read_bytes()
        result = bind_options(self.f.product, self.tasks, resolution="4K")
        self.assertEqual(result["resolution"], "2K")
        self.assertTrue(result["legacy"])
        self.assertEqual(path.read_bytes(), original)

    def test_any_group_mismatch_or_missing_payload_blocks_binding(self):
        path, job = self.prepare("4K")
        original = path.read_bytes()
        for changed in ({**job, "payload": {"params": {"resolution": "2K"}}},
                        {k: v for k, v in job.items() if k != "payload"},
                        {**job, "source_sha256": "a" * 64}):
            other = path.parent.parent / "group_09" / "round_01.json"
            self.write(other, changed); before = other.read_bytes()
            with self.assertRaises(ValueError): bind_options(self.f.product, self.tasks, resolution="4K")
            self.assertFalse(options_path(self.f.product, self.tasks).exists())
            self.assertEqual(other.read_bytes(), before)
        self.assertEqual(path.read_bytes(), original)

    def test_workflow_job_disagreement_and_invalid_payload_cannot_submit(self):
        path, job = self.prepare("4K")
        config = bind_options(self.f.product, self.tasks)
        config["resolution"] = "2K"; self.write(options_path(self.f.product, self.tasks), config)
        with self.assertRaisesRegex(ValueError, "分辨率不一致"): read_options(self.f.product, self.tasks)
        job["payload"]["params"]["resolution"] = "8K"; self.write(path, job)
        self.transport.reset_mock(); before = path.read_bytes()
        with self.assertRaises(ValueError): submit_job(path, self.transport)
        self.transport.request_json.assert_not_called()
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__": unittest.main()
