"""U_RULES数据边界：未知引擎、危险模板字段和未经支持的审核流程不能激活。"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from soutui_image.rules_bundle import RuleBundle, builtin_bundle


class RuleBundleTests(unittest.TestCase):
    def test_unsupported_engine_and_changed_flow_rejected(self):
        for field, value in (("engine", 2), ("timeout", 30), ("model", "other"), ("flow", "single")):
            data = builtin_bundle().data; data[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                RuleBundle.parse(json.dumps(data).encode())

    def test_template_traversal_and_missing_identity_rejected(self):
        for template in ("{product_id.__class__}", "只有一个固定结论", "{attachments!r}"):
            data = builtin_bundle().data; data["prompts"]["body"] = template
            with self.subTest(template=template), self.assertRaises(ValueError):
                RuleBundle.parse(json.dumps(data).encode())

    def test_content_identity_changes_with_rules(self):
        first = builtin_bundle(); data = first.data
        data["prompts"]["locator"] += "\n只作定位。"
        second = RuleBundle.parse(json.dumps(data).encode())
        self.assertNotEqual(first.sha256, second.sha256)
        self.assertEqual(first.raw, RuleBundle.parse(json.dumps(first.data, indent=2).encode()).raw)
