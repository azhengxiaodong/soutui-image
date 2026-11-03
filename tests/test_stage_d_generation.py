"""D 的费用参数、补生上限、逐张失败及重复提交保护；真实生成另行验收。"""

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from soutui_image.stage_a_inputs import ProductInput
from soutui_image.stage_b_web_upload import OSS_HOST, WebUploadReceipt
from soutui_image.stage_c_web_plan import GroupAssignment, ReferenceImage
from soutui_image.stage_d_web_generation import _apply_task, build_payload, decide_next_round, quote_balanced_2k, submit_job, watch_job
from soutui_image.web_transport import RequestOutcomeUnknown


class GenerationRuleTests(unittest.TestCase):
    def test_first_round_and_at_most_three_regenerations(self):
        self.assertEqual(decide_next_round([], [], []).next_round, 0)
        for last in range(3):
            rounds = [{"round_index": i, "state": "awaiting_audit"} for i in range(last + 1)]
            choice = decide_next_round(rounds, ["a" * 64] * 5, list(range(last + 1)))
            self.assertEqual(choice.approved_count, 1)
            self.assertEqual(choice.next_round, last + 1)
        rounds = [{"round_index": i, "state": "awaiting_audit"} for i in range(4)]
        self.assertEqual(decide_next_round(rounds, [], [0, 1, 2, 3]).action, "paused")

    def test_all_successes_need_audit_even_if_three_already_pass(self):
        hashes = [letter * 64 for letter in ("a", "b", "c")]
        rounds = [{"round_index": 0, "state": "awaiting_audit"}]
        self.assertEqual(decide_next_round(rounds, hashes, []).action, "audit")
        self.assertEqual(decide_next_round(rounds, hashes, [0]).action, "complete")

    def test_unknown_submission_or_bad_rounds_cannot_regenerate(self):
        self.assertEqual(decide_next_round([{"round_index": 0, "state": "submission_unknown"}], [], []).action, "wait")
        for rounds in ([{"round_index": 1, "state": "awaiting_audit"}],
                       [{"round_index": i, "state": "awaiting_audit"} for i in range(5)]):
            with self.assertRaises(ValueError):
                decide_next_round(rounds, [], [])

    def test_quote_requires_available_balanced_2k(self):
        data = {"success": True, "enabled": True, "features": ["model-dressing"],
                "tiers": [{"id": "balanced", "available": True, "availableResolutions": ["2K"], "prices": {"2K": 6}}]}
        self.assertEqual(quote_balanced_2k(data), 6)
        data["tiers"][0]["available"] = False
        with self.assertRaises(ValueError): quote_balanced_2k(data)


class GenerationTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "001.jpg"; source.write_bytes(b"original")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.product = ProductInput("001", str(source), 1, 1, "JPEG", digest)
        self.upload = WebUploadReceipt("001", str(source), digest, "套装", f"https://{OSS_HOST}/inputs/115/test.jpg", "JPEG", 8, digest, .1)
        self.group = GroupAssignment(1, "17", "模特组17", tuple(ReferenceImage(str(i), f"https://example.test/{i}.jpg") for i in range(5)))
        self.payload = build_payload(self.product, self.upload, self.group, "mdtask-unit-test", 6)
        self.record = {"round_index": 0, "state": "prepared", "payload": self.payload,
                       "client_task_id": "mdtask-unit-test", "task_id": None, "reference_ids": [str(i) for i in range(5)]}
        self.path = self.root / "round_00.json"; self.path.write_text(json.dumps(self.record), encoding="utf-8")

    def test_fixed_parameters_and_one_result_per_reference(self):
        params = self.payload["params"]
        self.assertEqual(params["resolution"], "2K")
        self.assertEqual(params["aspectRatio"], "3:4")
        self.assertEqual(params["extraParams"]["imageTier"], "balanced")
        self.assertEqual(params["extraParams"]["resultsPerReference"], "1")
        self.assertEqual(len(params["inputImageUrls2"]), 5)
        self.assertEqual(params["creditsCharged"], 30)
        with self.assertRaises(ValueError):
            build_payload(self.product, replace(self.upload, source_sha256="wrong"), self.group, "test", 6)

    def test_partial_failure_does_not_become_five_successes(self):
        task = {"id": "mdtask-unit-test", "featureType": "model-dressing", "expectedResultCount": 5, "status": "done",
                **{k: self.payload["params"][k] for k in ("resolution", "aspectRatio", "inputImageUrls", "inputImageUrls2", "extraParams")},
                "resultSlots": [{"id": f"slot-{i}", "referenceIndex": i, "status": "done" if i < 4 else "error",
                                 "imageUrl": f"https://example.test/result{i}.jpg" if i < 4 else None} for i in range(5)]}
        record = _apply_task(self.path, self.record, task)
        self.assertEqual(record["successful_results"], 4)
        self.assertEqual(record["failed_results"], 1)
        self.assertEqual(record["state"], "awaiting_audit")
        self.assertEqual(record["results"][4]["reference_id"], "4")

    def test_timeout_records_unknown_then_next_call_only_queries(self):
        class Transport:
            def __init__(self): self.methods = []
            def request_json(inner, method, path, **kwargs):
                inner.methods.append(method)
                if method == "POST": raise RequestOutcomeUnknown("test timeout")
                return {"success": True, "tasks": [], "hasMore": False}
        transport = Transport()
        with self.assertRaises(RequestOutcomeUnknown): submit_job(self.path, transport)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["state"], "submission_unknown")
        with self.assertRaises(RequestOutcomeUnknown): submit_job(self.path, transport)
        self.assertEqual(transport.methods, ["POST", "GET"])

    def test_generation_watch_timeout_preserves_record_without_resubmission(self):
        item = {**self.record, "state": "generating"}
        with patch("soutui_image.stage_d_web_generation.query_job", return_value=item) as queried, \
                patch("soutui_image.stage_d_web_generation.time.monotonic", side_effect=[0, 2]):
            with self.assertRaises(TimeoutError):
                watch_job(self.path, object(), timeout=1)
            self.assertEqual(queried.call_count, 1)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["state"], "generation_timeout")


if __name__ == "__main__":
    unittest.main()
