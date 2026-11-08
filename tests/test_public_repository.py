"""公开检查器必须验证候选SHA连续性，不能将未知源码变化当成既有验收。"""
import json
import unittest

from tools.check_public_repository import registered_source_baseline, allowed_path, SECRET_PATTERNS


class PublicRepositoryTests(unittest.TestCase):
    def report(self, name, data):
        return {f"validation/public_handoff/{name}.json": ("100644", json.dumps(data).encode())}

    def test_frozen_only_is_unchanged(self):
        base = {"src/main.py": "a" * 64}
        self.assertEqual(registered_source_baseline({}, base), base)

    def test_registered_changes_and_new_files_require_continuous_hashes(self):
        files = self.report("workbench_028", {"changed_frozen_files": {
            "src/main.py": {"before": "a"*64, "after": "b"*64}}, "new_files": {"src/extra.py": "c"*64}})
        files.update(self.report("workbench_029", {"changed_from_028": {
            "src/main.py": {"before": "b"*64, "after": "d"*64}}, "new_files": {}}))
        base = {"src/main.py": "a"*64}
        self.assertEqual(registered_source_baseline(files, base), {"src/main.py": "d"*64, "src/extra.py": "c"*64})
        self.assertEqual(base["src/main.py"], "a"*64)
        with self.assertRaisesRegex(ValueError, "不连续"):
            registered_source_baseline(files, {"src/main.py": "e"*64})

    def test_missing_prior_candidate_and_invalid_or_private_entries_rejected(self):
        files = self.report("workbench_029", {"changed_from_028": {
            "src/new.py": {"before": "a"*64, "after": "b"*64}}})
        with self.assertRaises(ValueError): registered_source_baseline(files, {})
        for name, value in (("src/new.py", "bad"), ("data/session.json", "a"*64)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                registered_source_baseline(self.report("workbench_028", {"changed_frozen_files": {}, "new_files": {name: value}}), {})

    def test_label_patch_checks_before_hash_and_sensitive_paths_stay_excluded(self):
        files = self.report("label_0211", {"file":"src/app.py", "before_sha256":"a"*64, "after_sha256":"b"*64})
        self.assertEqual(registered_source_baseline(files, {"src/app.py":"a"*64})["src/app.py"], "b"*64)
        with self.assertRaises(ValueError): registered_source_baseline(files, {"src/app.py":"c"*64})
        for path in ("data/local/login.dpapi", "photo.jpg", ".env", "validation/current_quality/raw.json"):
            self.assertFalse(allowed_path(path))
        self.assertTrue(SECRET_PATTERNS)


if __name__ == "__main__": unittest.main()
