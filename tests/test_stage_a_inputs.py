"""板块 A 独立验证：真实解码测试图片，检查误读输出、重复 ID 和原图变化。"""

import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from soutui_image.stage_a_inputs import assert_source_unchanged, main, scan_inputs


class InputScanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="soutui_a_")
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def picture(self, name, color="white"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (48, 64), color).save(path)
        return path

    def test_real_images_id_and_read_only(self):
        a = self.picture("00123.JPG")
        self.picture("456.png")
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()}
        batch = scan_inputs(self.root)
        self.assertTrue(batch.ready)
        self.assertEqual([p.product_id for p in batch.products], ["00123", "456"])
        self.assertEqual((batch.products[0].width, batch.products[0].height), (48, 64))
        self.assertEqual(batch.products[0].source_path, str(a.resolve()))
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()})

    def test_generated_subdirectories_are_not_inputs(self):
        self.picture("123.png")
        self.picture("123/组01/999.png")
        self.assertEqual([p.product_id for p in scan_inputs(self.root).products], ["123"])

    def test_duplicate_id_rejects_both_files(self):
        self.picture("123.png")
        self.picture("123.jpg")
        batch = scan_inputs(self.root)
        self.assertFalse(batch.ready)
        self.assertEqual(batch.products, ())
        self.assertEqual(len(batch.issues), 2)

    def test_bad_filename_and_corrupt_image_block_batch(self):
        self.picture("123.png")
        self.picture("商品图.png")
        (self.root / "456.jpg").write_bytes(b"broken JPEG")
        batch = scan_inputs(self.root)
        self.assertFalse(batch.ready)
        self.assertEqual([p.product_id for p in batch.products], ["123"])
        self.assertEqual({p.file_name for p in batch.issues}, {"商品图.png", "456.jpg"})

    def test_real_format_is_recorded_without_rewriting_original(self):
        path = self.picture("123.png")
        path.rename(self.root / "123.jpg")
        batch = scan_inputs(self.root)
        self.assertTrue(batch.ready)
        self.assertEqual(batch.products[0].image_format, "PNG")
        self.assertTrue((self.root / "123.jpg").read_bytes().startswith(b"\x89PNG"))

    def test_unsupported_format_disguised_as_jpeg_is_rejected(self):
        path = self.picture("123.gif")
        path.rename(self.root / "123.jpg")
        self.assertFalse(scan_inputs(self.root).ready)

    def test_empty_folder_is_not_ready(self):
        self.assertFalse(scan_inputs(self.root).ready)

    def test_changed_source_is_detected(self):
        self.picture("123.png")
        product = scan_inputs(self.root).products[0]
        assert_source_unchanged(product)
        self.picture("123.png", color="blue")
        with self.assertRaisesRegex(ValueError, "原图已变化"):
            assert_source_unchanged(product)

    def test_missing_directory_fails(self):
        with self.assertRaises(FileNotFoundError):
            scan_inputs(self.root / "missing")

    def test_report_cannot_overwrite_existing_file(self):
        self.picture("123.png")
        report = self.root / "report.json"
        report.write_text("original", encoding="utf-8")
        self.assertEqual(main([str(self.root), "--report", str(report)]), 2)
        self.assertEqual(report.read_text(encoding="utf-8"), "original")


if __name__ == "__main__":
    unittest.main()
