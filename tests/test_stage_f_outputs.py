"""F 的文件安全与计数检查；假审核仅用于验证保存边界，真实合格图另行验收。"""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from soutui_image.review_images import inspect_image
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_c_web_plan import GroupAssignment, ProductReferencePlan, ReferenceImage
from soutui_image.stage_e_codex_review import FEATURES, POLICY_VERSION
from soutui_image.stage_f_outputs import save_approved_reports


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.inputs = self.root / "input"; self.inputs.mkdir()
        Image.new("RGB", (20, 20), "white").save(self.inputs / "001.jpg")
        self.product = scan_inputs(self.inputs).products[0]
        assignments = tuple(GroupAssignment(i, str(i), f"模特组{i}", tuple(
            ReferenceImage(f"{i}-{j}", f"https://example.test/{i}/{j}.jpg") for j in range(5))) for i in range(1, 10))
        self.plan = ProductReferencePlan(1, "001", self.product.source_path, self.product.sha256, 17, assignments)
        self.cache, self.state = self.root / "cache", self.root / "state"
        self.cache.mkdir(); self.output = self.inputs / "001"

    def report(self, index=0, decisions=("pass", "reject", "review"), same_color=False):
        task_id = f"task-{index}"; group = self.plan.assignments[0]
        report = {"status": "reviewed", "all_successful_images_reviewed": True, "product_id": "001",
                  "source_sha256": self.product.sha256, "row": 1, "round_index": index, "task_id": task_id,
                  "folder_id": "1", "reference_ids": [r.image_id for r in group.references], "outcomes": []}
        for slot, decision in enumerate(decisions):
            image = self.cache / f"r{index}_s{slot}.png"
            Image.new("RGB", (20, 20), (30 if same_color else slot * 30, 100, 120)).save(image)
            candidate = inspect_image("001", f"{task_id}/slot-{slot}", image)
            from dataclasses import asdict
            verdict = {"product_id": "001", "result_id": candidate.result_id, "decision": decision,
                       "visible_scope": "单元测试假证据", "unshown_parts": [], "summary": "单元测试假证据",
                       "checks": [{"feature": feature, "visibility": "partial", "comparison": "match",
                                   "original": "测试原款", "generated": "测试结果", "reason": "测试边界"} for feature in FEATURES]}
            if decision != "pass": verdict["checks"][0]["comparison"] = "mismatch" if decision == "reject" else "uncertain"
            review = {"policy_version": POLICY_VERSION, "product_id": "001", "result_id": candidate.result_id,
                      "source_sha256": self.product.sha256, "result_sha256": candidate.sha256,
                      "decision": decision, "summary": "测试保存边界", "passes": [verdict, copy.deepcopy(verdict)] if decision == "pass" else [verdict]}
            report["outcomes"].append({"candidate": asdict(candidate), "review": review,
                                       "reference_id": group.references[slot].image_id, "reference_index": slot})
        jobs = self.state / "001" / "group_01"; jobs.mkdir(parents=True, exist_ok=True)
        job = {"task_id": task_id, "source_sha256": self.product.sha256, "folder_id": "1",
               "reference_ids": report["reference_ids"], "results": [
                   {"result_id": item["candidate"]["result_id"], "status": "done"} for item in report["outcomes"]]}
        (jobs / f"round_{index:02}.json").write_text(json.dumps(job), encoding="utf-8")
        return report

    def save(self, reports):
        return save_approved_reports(self.product, self.plan, reports, self.cache, self.state)

    def test_only_pass_saved_and_nine_group_directories_created(self):
        report = self.report()
        result = self.save([report])
        self.assertEqual(result["total_saved"], 1)
        self.assertEqual(sorted(p.name for p in self.output.iterdir() if p.is_dir()), [f"组{i:02}" for i in range(1, 10)])
        files = list((self.output / "组01").glob("*.png"))
        self.assertEqual(len(files), 1)
        self.assertEqual(hashlib.sha256(files[0].read_bytes()).hexdigest(), report["outcomes"][0]["candidate"]["sha256"])
        self.assertEqual(scan_inputs(self.inputs).products[0].sha256, self.product.sha256)

    def test_rerun_does_not_duplicate_saved_image(self):
        report = self.report(); first = self.save([report]); second = self.save([report])
        self.assertEqual(first["total_saved"], second["total_saved"])
        self.assertEqual(second["saved_new"], 0)
        self.assertEqual(second["duplicates_skipped"], 1)

    def test_identical_content_across_rounds_counts_once(self):
        first = self.report(0, ("pass",), same_color=True)
        second = self.report(1, ("pass",), same_color=True)
        result = self.save([first, second])
        self.assertEqual(result["total_saved"], 1)
        self.assertEqual(result["duplicates_skipped"], 1)

    def test_keep_all_five_passes_not_only_three(self):
        result = self.save([self.report(decisions=("pass",) * 5)])
        self.assertEqual(result["total_saved"], 5)

    def test_changed_review_image_never_saved(self):
        report = self.report()
        Path(report["outcomes"][0]["candidate"]["local_path"]).write_bytes(b"changed")
        with self.assertRaises(ValueError): self.save([report])
        self.assertFalse(self.output.exists())

    def test_unmanaged_existing_product_folder_is_preserved(self):
        report = self.report(); self.output.mkdir()
        old = self.output / "用户原图.jpg"; old.write_bytes(b"keep")
        with self.assertRaises(FileExistsError): self.save([report])
        self.assertEqual(old.read_bytes(), b"keep")

    def test_incomplete_or_wrong_source_audit_does_not_save(self):
        report = self.report(); report["all_successful_images_reviewed"] = False
        with self.assertRaises(ValueError): self.save([report])
        report["all_successful_images_reviewed"] = True; report["source_sha256"] = "other"
        with self.assertRaises(ValueError): self.save([report])
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
