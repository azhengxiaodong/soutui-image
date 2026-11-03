"""重置仅清空业务记录，保留原图和本机账号；未知文件或正在运行时不得删除。"""

import json
import tempfile
import unittest
from pathlib import Path

from tools.reset_run_records import reset
from soutui_image.runtime_lock import pipeline_lock


class ResetRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "inputs"
        self.input.mkdir()
        (self.input / "001.jpg").write_bytes(b"original")
        (self.root / "data/local").mkdir(parents=True)
        (self.root / "data/local/session.dpapi").write_bytes(b"keep credentials")
        (self.root / "data/tasks").mkdir()
        (self.root / "data/tasks/old.json").write_bytes(b"old")
        output = self.input / "001"
        (output / "组01").mkdir(parents=True)
        (output / "组01/pass.jpg").write_bytes(b"result")
        (output / ".搜推生图.json").write_text(json.dumps({"app": "soutui-image", "product_id": "001",
            "groups": [{"row": 1, "directory": "组01", "saved": [{"filename": "pass.jpg"}]}]}), encoding="utf-8")

    def test_reset_preserves_original_and_credentials(self):
        reset(self.root, [self.input], apply=True)
        self.assertFalse((self.root / "data/tasks").exists())
        self.assertFalse((self.input / "001").exists())
        self.assertEqual((self.input / "001.jpg").read_bytes(), b"original")
        self.assertEqual((self.root / "data/local/session.dpapi").read_bytes(), b"keep credentials")

    def test_unknown_user_file_or_running_pipeline_blocks_reset(self):
        unknown = self.input / "001/user.png"
        unknown.write_bytes(b"user file")
        with self.assertRaises(ValueError):
            reset(self.root, [self.input], apply=True)
        unknown.unlink()
        with pipeline_lock(self.root / "data"):
            with self.assertRaises(RuntimeError):
                reset(self.root, [self.input], apply=True)
        self.assertTrue((self.root / "data/tasks/old.json").exists())
