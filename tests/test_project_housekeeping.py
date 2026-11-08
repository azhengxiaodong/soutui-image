"""全新克隆的清理回归：能保存清单，预览不删除，实际删除保留有效业务文件。"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import project_housekeeping


class ProjectHousekeepingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="soutui_housekeeping_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "validation").mkdir()
        (self.root / "validation/frozen_modules.json").write_text(
            json.dumps({"modules": {}}), encoding="utf-8")
        (self.root / "build").mkdir()
        (self.root / "build/intermediate.txt").write_text("rebuildable", encoding="utf-8")
        (self.root / "data").mkdir()
        self.progress = self.root / "data/progress.json"
        self.progress.write_bytes(b'{"keep": true}')

    def run_cleanup(self, *arguments):
        with patch.object(project_housekeeping, "ROOT", self.root), \
                patch("sys.argv", ["project_housekeeping.py", *arguments]), \
                contextlib.redirect_stdout(io.StringIO()):
            project_housekeeping.main()

    def test_fresh_clone_preview_creates_report_without_deleting(self):
        self.run_cleanup()
        report = json.loads((self.root / "migration_backup/cleanup_preview.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["file_count"], 1)
        self.assertTrue((self.root / "build/intermediate.txt").is_file())
        self.assertEqual(self.progress.read_bytes(), b'{"keep": true}')

    def test_fresh_clone_apply_removes_only_planned_files_and_is_repeatable(self):
        self.run_cleanup("--apply")
        report_path = self.root / "migration_backup/cleanup_latest.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "deleted")
        self.assertEqual(report["file_count"], 1)
        self.assertFalse((self.root / "build").exists())
        self.assertEqual(self.progress.read_bytes(), b'{"keep": true}')
        self.run_cleanup("--apply")
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)

    def test_frozen_file_in_cleanup_target_stops_before_deletion(self):
        (self.root / "validation/frozen_modules.json").write_text(
            json.dumps({"modules": {"protected": {"files": {"build/intermediate.txt": "baseline"}}}}),
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "冻结文件"):
            self.run_cleanup("--apply")
        self.assertTrue((self.root / "build/intermediate.txt").is_file())
        self.assertFalse((self.root / "migration_backup").exists())
