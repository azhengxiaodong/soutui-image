"""首次连接剪贴板保护：独立复制实际数据，恢复后不依赖原所有者或连接进程。"""

import ctypes
import time


class ClipboardTransfer:
    """保留文本、DIB、PNG、HTML、文件列表等内存格式；无法保护时先停止。"""

    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = (
            (self.user, "OpenClipboard", [ctypes.c_void_p], ctypes.c_int),
            (self.user, "CloseClipboard", [], ctypes.c_int),
            (self.user, "EmptyClipboard", [], ctypes.c_int),
            (self.user, "EnumClipboardFormats", [ctypes.c_uint], ctypes.c_uint),
            (self.user, "IsClipboardFormatAvailable", [ctypes.c_uint], ctypes.c_int),
            (self.user, "GetClipboardFormatNameW", [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int),
            (self.user, "GetClipboardData", [ctypes.c_uint], ctypes.c_void_p),
            (self.user, "SetClipboardData", [ctypes.c_uint, ctypes.c_void_p], ctypes.c_void_p),
            (self.kernel, "GlobalAlloc", [ctypes.c_uint, ctypes.c_size_t], ctypes.c_void_p),
            (self.kernel, "GlobalFree", [ctypes.c_void_p], ctypes.c_void_p),
            (self.kernel, "GlobalLock", [ctypes.c_void_p], ctypes.c_void_p),
            (self.kernel, "GlobalUnlock", [ctypes.c_void_p], ctypes.c_int),
            (self.kernel, "GlobalSize", [ctypes.c_void_p], ctypes.c_size_t),
        )
        for lib, name, args, result in signatures:
            method = getattr(lib, name); method.argtypes = args; method.restype = result

    def _open(self):
        for attempt in range(20):
            if self.user.OpenClipboard(None): return
            time.sleep(.1)
        raise RuntimeError("剪贴板正被其他程序占用，请稍后再连接")

    def _bytes(self, fmt):
        handle = self.user.GetClipboardData(fmt)
        size = self.kernel.GlobalSize(handle) if handle else 0
        if not size or size > 64 * 1024 * 1024:
            raise RuntimeError("当前剪贴板格式无法完整保护；请先复制一段普通文字再连接")
        pointer = self.kernel.GlobalLock(handle)
        if not pointer: raise RuntimeError("无法保护剪贴板，未连接网站")
        try: return ctypes.string_at(pointer, size)
        finally: self.kernel.GlobalUnlock(handle)

    def __enter__(self):
        self._open()
        try:
            formats, current = [], 0
            while True:
                current = self.user.EnumClipboardFormats(current)
                if not current: break
                formats.append(current)
            if 2 in formats and 8 not in formats and 17 not in formats and self.user.IsClipboardFormatAvailable(8):
                formats.append(8)
            self.saved = {}
            for fmt in formats:
                # GDI 位图/调色板句柄改为其无损 DIB 内存表示，不能把句柄当数据复制。
                if fmt in (2, 9) and (8 in formats or 17 in formats): continue
                if fmt in (2, 3, 9, 14, 0x80):
                    raise RuntimeError("当前剪贴板对象无法保护；请先复制一段普通文字再连接")
                name = ctypes.create_unicode_buffer(256)
                self.user.GetClipboardFormatNameW(fmt, name, 256)
                if name.value in ("DataObject", "Ole Private Data"): continue
                self.saved[fmt] = self._bytes(fmt)
        finally: self.user.CloseClipboard()
        return self

    def __exit__(self, *exc):
        self._open()
        allocated = []
        try:
            for fmt, content in self.saved.items():
                handle = self.kernel.GlobalAlloc(0x0042, len(content))
                if not handle: raise RuntimeError("原剪贴板恢复分配失败")
                allocated.append([fmt, handle])
                pointer = self.kernel.GlobalLock(handle)
                if not pointer: raise RuntimeError("原剪贴板恢复锁定失败")
                try: ctypes.memmove(pointer, content, len(content))
                finally: self.kernel.GlobalUnlock(handle)
            if not self.user.EmptyClipboard(): raise RuntimeError("无法清理本次临时剪贴板数据")
            for item in allocated:
                if not self.user.SetClipboardData(item[0], item[1]): raise RuntimeError("原剪贴板恢复失败，请重新复制原内容")
                item[1] = None
            for fmt, content in self.saved.items():
                if self._bytes(fmt)[:len(content)] != content:
                    raise RuntimeError("原剪贴板恢复核对不一致，请重新复制原内容")
        finally:
            for fmt, handle in allocated:
                if handle: self.kernel.GlobalFree(handle)
            self.user.CloseClipboard()
