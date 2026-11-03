"""验证保护工具确实发现旧模块被修改、删除或记录越界。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.check_frozen import check_frozen


class FrozenGuardTests(unittest.TestCase):
    def test_modified_and_missing_source_detected(self):
        with tempfile.TemporaryDirectory(prefix="soutui_guard_") as temp:
            root = Path(temp)
            source = root / "a.py"
            source.write_bytes(b"original")
            baseline = root / "baseline.json"
            baseline.write_text(json.dumps({"schema_version": 1, "modules": {"A": {"files": {"a.py": hashlib.sha256(b"original").hexdigest()}}}}), encoding="utf-8")
            self.assertEqual(check_frozen(root, baseline), [])
            source.write_bytes(b"changed")
            self.assertEqual(len(check_frozen(root, baseline)), 1)
            source.unlink()
            self.assertEqual(len(check_frozen(root, baseline)), 1)

    def test_outside_source_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="soutui_guard_") as temp:
            root = Path(temp)
            baseline = root / "baseline.json"
            baseline.write_text(json.dumps({"schema_version": 1, "modules": {"A": {"files": {"../outside.py": "invalid"}}}}), encoding="utf-8")
            self.assertEqual(len(check_frozen(root, baseline)), 1)


if __name__ == "__main__":
    unittest.main()
