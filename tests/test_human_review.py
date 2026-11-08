"""人工裁定覆盖的保存与计数边界；不冒充模型审核证据。"""
import copy
import hashlib
import json
import unittest

import test_stage_f_outputs as output_tests
from soutui_image.human_review import record_path
from soutui_image.review_group_state import write_group_review_summary
from soutui_image.rule_runtime import current_policy


class HumanReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = output_tests.OutputTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def record(self, report, decisions):
        f = self.fixture
        values = {}
        for index, decision in decisions.items():
            c = report["outcomes"][index]["candidate"]
            values[c["result_id"]] = {"authority": "user", "result_sha256": c["sha256"],
                                     "decision": decision, "reason": "用户明确裁定", "policy_version": current_policy()}
        path = record_path(f.product, f.state)
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(json.dumps({"schema_version": 1, "authority": "user", "product_id": f.product.product_id,
                                   "source_sha256": f.product.sha256, "decisions": values}), encoding="utf-8")
        return path

    def test_accept_can_override_ai_reject_without_forging_double_pass(self):
        f = self.fixture; report = f.report(decisions=("reject",))
        original = copy.deepcopy(report)
        self.record(report, {0: "pass"})
        self.assertEqual(f.save([report])["total_saved"], 1)
        from soutui_image.output_store import read_manifest
        manifest = read_manifest(f.output, f.state, f.product)
        self.assertEqual(manifest["groups"][0]["saved"][0]["approval_source"], "human")
        self.assertEqual(report, original)
        self.assertEqual(len(report["outcomes"][0]["review"]["passes"]), 1)

    def test_rejected_image_never_reappears_on_repeated_save(self):
        f = self.fixture; report = f.report(decisions=("pass", "pass"))
        self.record(report, {0: "reject"})
        self.assertEqual(f.save([report])["total_saved"], 1)
        self.assertEqual(f.save([report])["saved_new"], 0)

    def test_existing_rejected_output_requires_explicit_withdrawal(self):
        f = self.fixture; report = f.report(decisions=("pass",))
        f.save([report]); self.record(report, {0: "reject"})
        with self.assertRaisesRegex(ValueError, "撤回"):
            f.save([report])

    def test_wrong_image_hash_cannot_override_model(self):
        f = self.fixture; report = f.report(decisions=("reject",))
        path = self.record(report, {0: "pass"})
        data = json.loads(path.read_text(encoding="utf-8"))
        next(iter(data["decisions"].values()))["result_sha256"] = "wrong"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "身份绑定"):
            f.save([report])

    def test_only_documented_human_rejection_can_reduce_group_count(self):
        f = self.fixture; report = f.report(decisions=("pass", "pass"))
        path = f.cache / "001/group_01/round_00/round_review.json"
        path.parent.mkdir(parents=True); path.write_text(json.dumps(report), encoding="utf-8")
        job_path = f.state / "001/group_01/round_00.json"
        job = json.loads(job_path.read_text(encoding="utf-8"))
        job.update(product_id="001", row=1, round_index=0)
        job_path.write_text(json.dumps(job), encoding="utf-8")
        original = hashlib.sha256(path.read_bytes()).hexdigest()
        first = write_group_review_summary(f.product, f.state, f.cache, 1)
        self.assertEqual(first["approved_count"], 2)
        self.record(report, {0: "reject"})
        second = write_group_review_summary(f.product, f.state, f.cache, 1)
        self.assertEqual(second["approved_count"], 1)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)
