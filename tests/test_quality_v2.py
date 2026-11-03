"""新审核必须检查四项结构，可见范围仍严格保留；局部图不能改写原像素或漏掉下部。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from soutui_image.review_detail_views import prepare_detail_views, assert_detail_views_unchanged
from soutui_image.review_detail_locator import PARTS, validate_regions
from soutui_image.review_button_edges import prepare_button_segments, validate_button_check
from soutui_image.review_images import inspect_image
from soutui_image.stage_a_inputs import ProductInput, scan_inputs
from soutui_image.stage_e_codex_review import POLICY_VERSION, review_candidate, validate_review, combine_button_check
from test_stage_e_review import response


class CriticalStructureTests(unittest.TestCase):
    def test_completed_locator_run_is_reused_after_interrupted_first_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "001.jpg"; original.write_bytes(b"original")
            result = root / "result.jpg"; result.write_bytes(b"result")
            product = ProductInput("001", str(original), 1, 1, "JPEG", hashlib.sha256(original.read_bytes()).hexdigest())
            from soutui_image.review_images import ReviewImage
            candidate = ReviewImage("001", "task/slot-0", str(result), hashlib.sha256(result.read_bytes()).hexdigest(), 1, 1, "JPEG", 6)
            identity = "\0".join((POLICY_VERSION, "001", candidate.result_id, product.sha256, candidate.sha256, "gpt-6.1-sol"))
            latest = root / "reviews" / hashlib.sha256(identity.encode()).hexdigest() / "123"
            (latest / "detail_location").mkdir(parents=True)
            (latest / "detail_location/located.json").write_text("{}", encoding="utf-8")
            rejection = response("reject"); rejection["checks"][0]["comparison"] = "mismatch"
            with patch("soutui_image.stage_e_codex_review._audit_once", return_value=rejection) as audit:
                review_candidate(product, candidate, root / "reviews")
            self.assertEqual(audit.call_args.args[2], latest / "first")

    def test_button_uncertainty_cannot_be_overridden_by_overall_match(self):
        report = response()
        check = dict(next(c for c in report["checks"] if c["feature"] == "扣位边缘与凸片"))
        check["comparison"] = "uncertain"
        combined = combine_button_check(report, check, "001", "task/slot-0")
        self.assertEqual(combined["decision"], "review")
        self.assertEqual(report["decision"], "pass")

    def test_button_focus_match_never_cancels_existing_mismatch(self):
        report = response("reject")
        check = dict(next(c for c in report["checks"] if c["feature"] == "扣位边缘与凸片"))
        next(c for c in report["checks"] if c["feature"] == check["feature"])["comparison"] = "mismatch"
        self.assertEqual(combine_button_check(report, check, "001", "task/slot-0")["decision"], "reject")

    def test_each_new_visible_structure_mismatch_blocks_pass(self):
        for feature in ("袖口边缘结构", "门襟层次与边线", "扣位边缘与凸片", "裤长与脚踝位置"):
            with self.subTest(feature=feature):
                report = response()
                item = next(c for c in report["checks"] if c["feature"] == feature)
                item["comparison"] = "mismatch"
                with self.assertRaises(ValueError): validate_review(report, "001", "task/slot-0")
                report["decision"] = "reject"
                self.assertEqual(validate_review(report, "001", "task/slot-0")["decision"], "reject")

    def test_unshown_hem_does_not_force_ankle_length_review(self):
        report = response()
        item = next(c for c in report["checks"] if c["feature"] == "裤长与脚踝位置")
        item.update(visibility="not_visible", comparison="not_applicable", generated="没有展示裤脚", reason="仅核对可见部分")
        self.assertEqual(validate_review(report, "001", "task/slot-0")["decision"], "pass")

    def test_old_pass_cache_is_not_reused_by_new_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "001.jpg"; original.write_bytes(b"original")
            result = root / "result.jpg"; result.write_bytes(b"result")
            product = ProductInput("001", str(original), 1, 1, "JPEG", hashlib.sha256(original.read_bytes()).hexdigest())
            from soutui_image.review_images import ReviewImage
            candidate = ReviewImage("001", "task/slot-0", str(result), hashlib.sha256(result.read_bytes()).hexdigest(), 1, 1, "JPEG", 6)
            identity = "\0".join(("2026-10-27.1", "001", candidate.result_id, product.sha256, candidate.sha256, "default"))
            old = root / "reviews" / hashlib.sha256(identity.encode()).hexdigest()
            old.mkdir(parents=True); (old / "review.json").write_text('{"decision":"pass"}', encoding="utf-8")
            rejection = response("reject"); rejection["checks"][0]["comparison"] = "mismatch"
            with patch("soutui_image.stage_e_codex_review._audit_once", return_value=rejection) as audit:
                reviewed = review_candidate(product, candidate, root / "reviews")
            self.assertEqual(reviewed.decision, "reject")
            self.assertEqual(reviewed.policy_version, POLICY_VERSION)
            self.assertEqual(audit.call_count, 1)
            self.assertTrue((old / "review.json").exists())


class DetailViewTests(unittest.TestCase):
    def test_native_button_segments_include_material_beside_the_fold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "001.png"
            picture = Image.new("RGB", (200, 200), "white"); picture.putpixel((70,20),(40,90,110)); picture.save(source)
            product = scan_inputs(root).products[0]
            candidate = inspect_image("001", "task/slot-0", source)
            regions = [{"role": role, "part": "门襟与扣列", "visible": True,
                        "box": [450,100,550,900]} for role in ("original","generated")]
            views = prepare_detail_views(product, candidate, root / "views", regions=regions)
            segments = prepare_button_segments(views, root / "segments", product=product, candidate=candidate)
            self.assertEqual(len(segments), 6)
            for segment in segments:
                box = segment["native_context_box"]
                self.assertLess(box[0], 90); self.assertGreater(box[2], 110)
            with Image.open(segments[0]["path"]) as result:
                self.assertEqual(result.getpixel((0,0)), (40,90,110))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), product.sha256)

    def test_button_segments_cover_placket_and_keep_original_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "placket.png"
            picture = Image.new("RGB", (120, 301), "white")
            picture.putpixel((30, 100), (70, 40, 80)); picture.save(path)
            raw = path.read_bytes()
            views = [{"role": role, "region": "门襟与扣列", "path": str(path),
                      "sha256": hashlib.sha256(raw).hexdigest()} for role in ("original", "generated")]
            segments = prepare_button_segments(views, root / "segments")
            self.assertEqual(len(segments), 6)
            for role in ("original", "generated"):
                covered = set()
                for segment in (s for s in segments if s["role"] == role):
                    left, top, right, bottom = segment["box_in_parent"]
                    covered.update(range(top, bottom))
                    with Image.open(segment["path"]) as output:
                        self.assertEqual(output.getpixel((0, 0)), picture.getpixel((left, top)))
                self.assertEqual(covered, set(range(301)))
            self.assertEqual(path.read_bytes(), raw)

    def test_button_check_never_claims_match_for_unshown_edge(self):
        data = {"feature": "扣位边缘与凸片", "visibility": "not_visible", "comparison": "match",
                "original": "原图有扣子", "generated": "被遮挡", "reason": "未展示"}
        with self.assertRaises(ValueError): validate_button_check(data)
        data["comparison"] = "not_applicable"
        self.assertEqual(validate_button_check(data), data)

    def test_localized_regions_reject_out_of_bounds_and_duplicate_parts(self):
        regions = [{"role": role, "part": part, "visible": True, "box": [100, 200, 400, 800],
                    "description": "实际可见位置"} for role in ("original", "generated") for part in PARTS]
        self.assertEqual(len(validate_regions({"regions": regions})), 8)
        regions[0]["box"][0] = -1
        with self.assertRaises(ValueError): validate_regions({"regions": regions})
        regions[0]["box"][0] = 100
        regions[0]["part"] = regions[1]["part"]
        with self.assertRaises(ValueError): validate_regions({"regions": regions})

    def test_views_preserve_pixels_inputs_and_cover_entire_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "001.png"
            image = Image.new("RGB", (80, 100), "white")
            image.putpixel((0, 0), (30, 90, 170)); image.putpixel((79, 99), (200, 50, 10))
            image.save(source)
            result = root / "generated.png"; image.save(result)
            product = scan_inputs(root).products[0]
            candidate = inspect_image("001", "task/slot-0", result)
            before = {p: p.read_bytes() for p in (source, result)}
            views = prepare_detail_views(product, candidate, root / "views")
            for role in ("original", "generated"):
                coverage = set()
                for view in (v for v in views if v["role"] == role):
                    left, top, right, bottom = view["box_pixels"]
                    coverage.update((x, y) for x in range(left, right) for y in range(top, bottom))
                    with Image.open(view["path"]) as generated:
                        self.assertEqual(generated.getpixel((0, 0)), image.getpixel((left, top)))
                self.assertEqual(len(coverage), 80 * 100)
            self.assertTrue(all(p.read_bytes() == raw for p, raw in before.items()))
            assert_detail_views_unchanged(views)
            Path(views[0]["path"]).write_bytes(b"changed")
            with self.assertRaises(ValueError): assert_detail_views_unchanged(views)


if __name__ == "__main__":
    unittest.main()
