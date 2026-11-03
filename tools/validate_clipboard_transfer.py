"""真实核对首次连接的剪贴板保护：文本和位图经过临时替换后仍完整恢复。"""

import ctypes
import hashlib
import io
import json
import struct
import time
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.clipboard_transfer import ClipboardTransfer

user = ctypes.WinDLL("user32", use_last_error=True)
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
for lib, name, args, result in (
    (user, "OpenClipboard", [ctypes.c_void_p], ctypes.c_int),
    (user, "CloseClipboard", [], ctypes.c_int),
    (user, "EmptyClipboard", [], ctypes.c_int),
    (user, "GetClipboardData", [ctypes.c_uint], ctypes.c_void_p),
    (user, "SetClipboardData", [ctypes.c_uint, ctypes.c_void_p], ctypes.c_void_p),
    (kernel, "GlobalAlloc", [ctypes.c_uint, ctypes.c_size_t], ctypes.c_void_p),
    (kernel, "GlobalLock", [ctypes.c_void_p], ctypes.c_void_p),
    (kernel, "GlobalUnlock", [ctypes.c_void_p], ctypes.c_int),
    (kernel, "GlobalSize", [ctypes.c_void_p], ctypes.c_size_t),
):
    func = getattr(lib, name); func.argtypes = args; func.restype = result


def open_clipboard():
    for attempt in range(30):
        if user.OpenClipboard(None): return
        time.sleep(.1)
    raise RuntimeError("剪贴板持续被占用")


def snapshot():
    open_clipboard()
    try:
        values = {}
        for fmt in (8, 13):
            handle = user.GetClipboardData(fmt)
            if not handle: continue
            size = kernel.GlobalSize(handle)
            pointer = kernel.GlobalLock(handle)
            try:
                content = ctypes.string_at(pointer, size)
                # GlobalSize 包含分配器填充；只比较该格式定义的有效数据。
                if fmt == 13:
                    content = content.decode("utf-16-le").split("\0", 1)[0].encode("utf-16-le")
                elif fmt == 8 and len(content) >= 40:
                    header, width, height, planes, bits, compression, image_size, xppm, yppm, colors, important = struct.unpack("<IiiHHIIiiII", content[:40])
                    if compression == 0:
                        palette = colors or (1 << bits if bits <= 8 else 0)
                        pixels = image_size or (((width * bits + 31) // 32) * 4 * abs(height))
                        content = content[:header + palette * 4 + pixels]
                values[fmt] = content
            finally: kernel.GlobalUnlock(handle)
        return values
    finally: user.CloseClipboard()


def set_values(values):
    open_clipboard()
    try:
        assert user.EmptyClipboard()
        for fmt, content in values.items():
            handle = kernel.GlobalAlloc(0x0002, len(content))
            assert handle
            pointer = kernel.GlobalLock(handle)
            ctypes.memmove(pointer, content, len(content)); kernel.GlobalUnlock(handle)
            assert user.SetClipboardData(fmt, handle)
    finally: user.CloseClipboard()


if __name__ == "__main__":
    image = Image.new("RGB", (32, 24), (30, 120, 205))
    buffer = io.BytesIO(); image.save(buffer, "BMP")
    values = {8: buffer.getvalue()[14:], 13: "搜推生图剪贴板验证文字，不含账号数据\0".encode("utf-16-le")}
    before = snapshot()
    with ClipboardTransfer():
        set_values(values)
        expected = snapshot()
        with ClipboardTransfer():
            set_values({13: "temporary transfer\0".encode("utf-16-le")})
        after = snapshot()
        if expected != after:
            diagnostic = {"expected": {str(k): {"bytes": len(v), "sha256": hashlib.sha256(v).hexdigest()} for k, v in expected.items()},
                          "actual": {str(k): {"bytes": len(v), "sha256": hashlib.sha256(v).hexdigest()} for k, v in after.items()}}
            print(json.dumps(diagnostic))
            raise AssertionError("文本或位图没有恢复")
    assert before == snapshot(), "用户原剪贴板内容发生变化"
    report = {"actual_windows_clipboard_test": True, "unicode_text_restored": True,
              "bitmap_bytes_restored": True, "user_original_text_and_bitmap_unchanged": True,
              "fixture_bitmap_sha256": hashlib.sha256(values[8]).hexdigest(), "clipboard_contents_logged": False}
    target = ROOT / "validation" / "package_connection" / "clipboard_roundtrip.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
