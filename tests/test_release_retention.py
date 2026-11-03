"""交付清理的失败保护：新包完整才删旧包；不触及用户数据，不跟随目录链接。"""

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import package_release


class ReleaseRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="soutui_release_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "dist/搜推生图"
        self.app.mkdir(parents=True)
        (self.app / "搜推生图.exe").write_bytes(b"new verified application")
        self.release = self.root / "release"
        (self.release / "history").mkdir(parents=True)
        (self.release / "history/old.zip").write_bytes(b"old")
        self.target = self.release / "current.zip"
        self.target.write_bytes(b"previous current")
        (self.release / "older.zip").write_bytes(b"older")
        self.data = self.root / "data/progress.json"
        self.data.parent.mkdir()
        self.data.write_bytes(b"keep actual progress")

    def test_only_current_verified_archive_survives(self):
        with patch.object(package_release, "ROOT", self.root):
            package_release.write_current_archive(self.app, self.target)
        with zipfile.ZipFile(self.target) as archive:
            self.assertEqual(archive.read("搜推生图/搜推生图.exe"), b"new verified application")
        self.assertEqual(list(self.release.iterdir()), [self.target])
        self.assertEqual(self.data.read_bytes(), b"keep actual progress")

    def test_failed_verification_keeps_old_archives(self):
        with patch.object(package_release, "ROOT", self.root), patch.object(zipfile.ZipFile, "testzip", return_value="broken"):
            with self.assertRaises(AssertionError):
                package_release.write_current_archive(self.app, self.target)
        self.assertEqual(self.target.read_bytes(), b"previous current")
        self.assertTrue((self.release / "history/old.zip").exists())
        self.assertTrue((self.release / "older.zip").exists())
        self.assertFalse(self.target.with_suffix(".partial.zip").exists())

    def test_outside_project_target_is_rejected(self):
        with patch.object(package_release, "ROOT", self.root):
            with self.assertRaises(ValueError):
                package_release.write_current_archive(self.app, self.root / "outside.zip")
        self.assertEqual(self.target.read_bytes(), b"previous current")
