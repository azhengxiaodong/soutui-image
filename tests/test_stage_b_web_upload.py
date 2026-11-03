"""接口上传的目标、原图指纹与重复运行边界检查；真实上传另行验证。"""

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from soutui_image.codex_clothing import ClothingClassification
from soutui_image.stage_a_inputs import ProductInput
from soutui_image.stage_b_web_upload import OSS_HOST, upload_product, validate_upload_urls


class NoRequests:
    def __getattr__(self, name):
        raise AssertionError("拒绝条件下不应发起网络请求")


class UploadBoundaryTests(unittest.TestCase):
    def test_signed_and_public_urls_must_match_known_storage(self):
        public = f"https://{OSS_HOST}/inputs/115/test.jpg"
        validate_upload_urls(public + "?Signature=temporary", public)
        invalid = [(public.replace("https:", "http:"), public),
                   (public.replace(OSS_HOST, "other.example"), public),
                   (public + "?Signature=temporary", public.replace("test.jpg", "other.jpg")),
                   (public, public + "?Signature=temporary")]
        for signed, visible in invalid:
            with self.subTest(signed=signed), self.assertRaises(ValueError):
                validate_upload_urls(signed, visible)

    def test_wrong_product_or_changed_source_stops_before_request(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "001.jpg"
            source.write_bytes(b"source")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            product = ProductInput("001", str(source), 1, 1, "JPEG", digest)
            classification = ClothingClassification("001", digest, "套装", ("上衣", "裤子"), "可见两件", "", 0)
            with self.assertRaises(ValueError):
                upload_product(product, replace(classification, product_id="002"), folder, "unused", transport=NoRequests())
            source.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                upload_product(product, classification, folder, "unused", transport=NoRequests())


if __name__ == "__main__":
    unittest.main()
