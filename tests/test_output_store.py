"""内部输出账本迁移验证：使用临时真实图片及 Windows 实锁，不调用网站或模型。"""

import copy
import hashlib
import json
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_stage_f_outputs as fixtures
from soutui_image import output_store as store
from soutui_image.stage_f_outputs import save_approved_reports


class OutputStoreTests(unittest.TestCase):
    setUp = fixtures.OutputTests.setUp
    report = fixtures.OutputTests.report
    save = fixtures.OutputTests.save

    def legacy_output(self):
        self.save([self.report(decisions=("pass",) * 5)])
        internal = store.manifest_path(self.output, self.state)
        record = json.loads(internal.read_text(encoding="utf-8"))
        record.pop("output_directory")
        legacy = self.output / store.MANIFEST_NAME
        legacy.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        (self.output / store.LOCK_NAME).write_bytes(b"0")
        internal.unlink()
        return legacy, record

    def migrate(self):
        with store.output_lock(self.output, self.state):
            return store.migrate_manifest(self.output, self.state, self.product, self.plan)

    def test_new_outputs_contain_only_group_directories(self):
        self.save([self.report()])
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), [f"组{i:02}" for i in range(1, 10)])
        internal = store.manifest_path(self.output, self.state)
        self.assertTrue(internal.is_relative_to(self.state.parent / "outputs"))
        self.assertEqual(store.read_manifest(self.output, self.state, self.product)["product_id"], "001")

    def test_read_legacy_does_not_migrate_or_touch_files(self):
        legacy, record = self.legacy_output()
        self.assertEqual(store.read_manifest(self.output, self.state, self.product), record)
        self.assertTrue(legacy.exists())
        self.assertFalse(store.manifest_path(self.output, self.state).exists())

    def test_migration_preserves_every_image_and_business_field_and_is_repeatable(self):
        legacy, record = self.legacy_output()
        record["withdrawn"] = [{"reason": "历史证据", "retained_path": "保留原有路径字段"}]
        legacy.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        photos = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.output.rglob("*.png")}
        migrated = self.migrate()
        self.assertEqual({k: v for k, v in migrated.items() if k != "output_directory"}, record)
        self.assertFalse(legacy.exists())
        self.assertFalse((self.output / store.LOCK_NAME).exists())
        self.assertEqual(self.migrate(), migrated)
        self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest() for p in photos}, photos)

    def test_changed_photo_stops_before_legacy_removed(self):
        legacy, _ = self.legacy_output()
        image = next(self.output.rglob("*.png")); image.write_bytes(b"user-changed")
        with self.assertRaisesRegex(ValueError, "图片已改变"):
            self.migrate()
        self.assertTrue(legacy.exists())
        self.assertFalse(store.manifest_path(self.output, self.state).exists())
        self.assertEqual(image.read_bytes(), b"user-changed")

    def test_wrong_product_identity_and_output_path_are_rejected(self):
        self.save([self.report()])
        with self.assertRaisesRegex(ValueError, "当前商品或原图"):
            store.read_manifest(self.output, self.state, replace(self.product, sha256="other"))
        path = store.manifest_path(self.output, self.state)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["output_directory"] = str(self.root / "other-output")
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "其他目录"):
            store.read_manifest(self.output, self.state)

    def test_conflicting_internal_and_legacy_records_are_preserved(self):
        legacy, old = self.legacy_output()
        conflicting = copy.deepcopy(old); conflicting["note"] = "different"
        store.save_manifest(self.output, self.state, conflicting)
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.migrate()
        self.assertEqual(json.loads(legacy.read_text(encoding="utf-8")), old)
        self.assertEqual(json.loads(store.manifest_path(self.output, self.state).read_text(encoding="utf-8"))["note"], "different")

    def test_failed_internal_write_preserves_legacy_and_all_images(self):
        legacy, _ = self.legacy_output()
        with patch("soutui_image.output_store.atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.migrate()
        self.assertTrue(legacy.exists())
        self.assertEqual(len(list(self.output.rglob("*.png"))), 5)

    def test_interruption_after_copy_before_legacy_delete_can_resume(self):
        legacy, _ = self.legacy_output()
        original = Path.unlink
        def fail_legacy(path, *args, **kwargs):
            if path == legacy:
                raise PermissionError("temporary sharing violation")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_legacy):
            with self.assertRaises(PermissionError):
                self.migrate()
        self.assertTrue(legacy.exists())
        self.assertTrue(store.manifest_path(self.output, self.state).exists())
        self.assertEqual(sum(len(g["saved"]) for g in self.migrate()["groups"]), 5)
        self.assertFalse(legacy.exists())

    def test_active_legacy_lock_prevents_migration(self):
        legacy, _ = self.legacy_output()
        with store._file_lock(self.output / store.LOCK_NAME, existing_only=True):
            with self.assertRaisesRegex(RuntimeError, "占用"):
                self.migrate()
        self.assertTrue(legacy.exists())

    def test_same_output_lock_blocks_another_data_root_in_process(self):
        with store.output_lock(self.output, self.state):
            with self.assertRaises(RuntimeError):
                with store.output_lock(self.output, self.root / "different-data" / "tasks"):
                    self.fail("same output must be locked")

    @unittest.skipUnless(os.name == "nt", "Windows 命名互斥")
    def test_windows_same_output_lock_blocks_another_process_and_data_root(self):
        code = ("import sys; from soutui_image.output_store import output_lock\n"
                "try:\n with output_lock(sys.argv[1], sys.argv[2]): pass\n"
                "except RuntimeError:\n sys.exit(23)\n")
        with store.output_lock(self.output, self.state):
            child = subprocess.run([sys.executable, "-B", "-c", code, str(self.output),
                                    str(self.root / "different-data" / "tasks")],
                                   capture_output=True, timeout=15,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(child.returncode, 23, child.stderr.decode(errors="replace"))

    def test_pending_saved_only_when_explicit_and_not_counted_approved(self):
        report = self.report()
        old = self.save([report])
        self.assertEqual(old["total_saved"], 1)
        result = save_approved_reports(self.product, self.plan, [report], self.cache, self.state,
                                       include_pending=True)
        self.assertEqual(result["total_saved"], 2)
        self.assertEqual(result["approved_counts"]["组01"], 1)
        self.assertEqual(sorted(x["review_status"] for x in store.read_manifest(self.output, self.state)["groups"][0]["saved"]), ["pass", "review"])

    def test_deleted_tombstone_prevents_legacy_report_restoring_photo(self):
        report = self.report()
        self.save([report])
        with store.output_lock(self.output, self.state):
            record = store.read_manifest(self.output, self.state)
            item = record["groups"][0]["saved"].pop()
            (self.output / "组01" / item["filename"]).unlink()
            record["removed"] = [{"row": 1, "sha256": item["sha256"], "reason": "独立审核拒绝"}]
            store.save_manifest(self.output, self.state, record)
        result = self.save([report])
        self.assertEqual(result["total_saved"], 0)
        self.assertEqual(len(list(self.output.rglob("*.png"))), 0)


if __name__ == "__main__":
    unittest.main()
