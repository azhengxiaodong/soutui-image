"""程序升级需保留旧版；签名数据处理前先拦截ZIP越界与链接。"""

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from soutui_image.program_update import unpack_verified


class ProgramArchiveTests(unittest.TestCase):
    def test_path_escape_and_link_are_rejected(self):
        for name in ("搜推生图/../../outside.exe","/absolute.exe","other/app.exe","搜推生图/a:b"):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                archive=Path(tmp)/"bad.zip"
                with zipfile.ZipFile(archive,"w") as z: z.writestr(name,b"test")
                with self.assertRaises(ValueError): unpack_verified(archive,Path(tmp)/"out")
        with tempfile.TemporaryDirectory() as tmp:
            archive=Path(tmp)/"link.zip"; item=zipfile.ZipInfo("搜推生图/link"); item.create_system=3; item.external_attr=(0o120777<<16)
            with zipfile.ZipFile(archive,"w") as z: z.writestr(item,b"../outside")
            with self.assertRaises(ValueError): unpack_verified(archive,Path(tmp)/"out")

    def test_missing_runtime_is_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive=Path(tmp)/"incomplete.zip"
            with zipfile.ZipFile(archive,"w") as z: z.writestr("搜推生图/搜推生图.exe",b"test")
            with self.assertRaises(ValueError): unpack_verified(archive,Path(tmp)/"out")
