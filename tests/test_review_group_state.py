"""审核汇总必须完整对应真实轮次，跨轮同一内容只能累计一次。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from soutui_image.review_group_state import write_group_review_summary
from soutui_image.stage_a_inputs import ProductInput
from soutui_image.stage_e_codex_review import FEATURES, POLICY_VERSION


class GroupReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "001.jpg"; source.write_bytes(b"original")
        self.product = ProductInput("001", str(source), 1, 1, "JPEG", hashlib.sha256(source.read_bytes()).hexdigest())
        self.state, self.cache = self.root / "state", self.root / "cache"
        self.jobs = self.state / "001" / "group_01"; self.jobs.mkdir(parents=True)

    def fixture(self, index, content_hash="b" * 64, *, complete=True):
        task_id = f"task-{index}"
        result_id = task_id + "/slot-0"
        job = {"product_id": "001", "source_sha256": self.product.sha256, "folder_id": "17", "row": 1,
               "reference_ids": list("01234"), "round_index": index, "task_id": task_id,
               "results": [{"result_id": result_id, "status": "done"}]}
        (self.jobs / f"round_{index:02}.json").write_text(json.dumps(job), encoding="utf-8")
        verdict = {"product_id": "001", "result_id": result_id, "decision": "pass", "visible_scope": "可见上衣",
                   "unshown_parts": [], "summary": "可见部分一致", "checks": [
                       {"feature": f, "visibility": "partial", "comparison": "match", "original": "原款",
                        "generated": "相同", "reason": "可见部分一致"} for f in FEATURES]}
        review = {"policy_version": POLICY_VERSION, "product_id": "001", "result_id": result_id,
                  "source_sha256": self.product.sha256, "result_sha256": content_hash,
                  "decision": "pass", "passes": [verdict, verdict]}
        report = {**{k: job[k] for k in ("product_id", "source_sha256", "folder_id", "row", "reference_ids", "round_index", "task_id")},
                  "all_successful_images_reviewed": complete, "outcomes": [
                      {"candidate": {"product_id": "001", "result_id": result_id, "sha256": content_hash}, "review": review}]}
        folder = self.cache / "001" / "group_01" / f"round_{index:02}"; folder.mkdir(parents=True)
        path = folder / "round_review.json"; path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return path, report

    def test_identical_approved_content_across_rounds_counts_once(self):
        self.fixture(0); self.fixture(1)
        result = write_group_review_summary(self.product, self.state, self.cache, 1)
        self.assertEqual(result["approved_count"], 1)
        self.assertEqual(result["reviewed_rounds"], [0, 1])

    def test_incomplete_round_does_not_unlock_regeneration(self):
        self.fixture(0, complete=False)
        result = write_group_review_summary(self.product, self.state, self.cache, 1)
        self.assertEqual(result["approved_count"], 0)
        self.assertEqual(result["reviewed_rounds"], [])

    def test_missing_success_or_wrong_source_in_report_is_rejected(self):
        path, report = self.fixture(0)
        report["outcomes"] = []
        path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(ValueError): write_group_review_summary(self.product, self.state, self.cache, 1)
        report["source_sha256"] = "wrong"
        path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(ValueError): write_group_review_summary(self.product, self.state, self.cache, 1)

    def test_missing_old_review_does_not_silently_lower_approved_count(self):
        path, report = self.fixture(0)
        write_group_review_summary(self.product, self.state, self.cache, 1)
        target = self.jobs / "review_summary.json"; before = target.read_bytes()
        path.unlink()
        with self.assertRaises(ValueError): write_group_review_summary(self.product, self.state, self.cache, 1)
        self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
