"""板块 B 的边界检查：拒绝不确定分类、串商品、改动原图及重复创建。

这些检查验证停止条件；网站业务验收另外使用真实页面，不能由本文件代替。
"""

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from soutui_image.codex_clothing import ClothingClassification, parse_classification
from soutui_image.stage_a_inputs import ProductInput
from soutui_image.stage_b_projects import ProjectBuilder, verify_one_row


class NoBrowserActions:
    def __getattr__(self, name):
        raise AssertionError(f"拒绝条件下不应调用浏览器：{name}")


class StageBBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "00123.jpg"
        source.write_bytes(b"original fingerprint")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.product = ProductInput("00123", str(source), 1, 1, "JPEG", digest)
        self.classification = ClothingClassification("00123", digest, "套装", ("上衣", "裤子"), "两件都可见", "", 0)

    def data(self, category="套装"):
        return {"product_id": "00123", "category": category,
                "visible_garments": ["上衣", "裤子"], "reason": "可见两件服装"}

    def test_supported_types_and_id_preserved(self):
        for category in ("套装", "上装", "下装"):
            with self.subTest(category=category):
                result = parse_classification(self.data(category), self.product)
                self.assertEqual(result.product_id, "00123")
                self.assertEqual(result.source_sha256, self.product.sha256)
                self.assertEqual(result.category, category)

    def test_unknown_or_ambiguous_category_stops(self):
        for category in ("待确认", "连衣裙", "", None, []):
            with self.subTest(category=category), self.assertRaises(ValueError):
                parse_classification(self.data(category), self.product)

    def test_incomplete_or_wrong_product_response_stops(self):
        for data in ({**self.data(), "product_id": "123"},
                     {**self.data(), "reason": ""},
                     {**self.data(), "visible_garments": []},
                     {**self.data(), "extra": "ignored?"}, {}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_classification(data, self.product)

    def test_source_changed_stops_before_browser_action(self):
        Path(self.product.source_path).write_bytes(b"changed source")
        with self.assertRaises(ValueError):
            ProjectBuilder(NoBrowserActions()).create(self.product, self.classification, self.root / "state")

    def test_wrong_classification_binding_stops_before_browser_action(self):
        for classification in (replace(self.classification, product_id="999"),
                               replace(self.classification, source_sha256="other"),
                               replace(self.classification, category="待确认")):
            with self.subTest(classification=classification), self.assertRaises(ValueError):
                ProjectBuilder(NoBrowserActions()).create(self.product, classification, self.root / "state")

    def test_existing_run_stops_duplicate_project(self):
        state = self.root / "state"
        state.mkdir()
        record = state / "project_00123.json"
        record.write_text('{"status":"needs_attention"}', encoding="utf-8")
        with self.assertRaises(FileExistsError):
            ProjectBuilder(NoBrowserActions()).create(self.product, self.classification, state)
        self.assertEqual(record.read_text(encoding="utf-8"), '{"status":"needs_attention"}')

    def test_ready_state_rejects_wrong_project_category_or_row_count(self):
        def control(name, role="TextControl"):
            return {"name": name, "type": role, "offscreen": False}
        rows = [control("模特换衣｜00123"), control("重命名服装", "ButtonControl"),
                control("套装"), control("未生成"), control("批量生成", "ButtonControl")]
        self.assertTrue(verify_one_row(rows, "00123", "套装"))
        self.assertFalse(verify_one_row(rows, "00124", "套装"))
        self.assertFalse(verify_one_row(rows, "00123", "上装"))
        self.assertFalse(verify_one_row(rows + [control("重命名服装", "ButtonControl")], "00123", "套装"))
        self.assertFalse(verify_one_row([x for x in rows if x["name"] != "未生成"], "00123", "套装"))


if __name__ == "__main__":
    unittest.main()
