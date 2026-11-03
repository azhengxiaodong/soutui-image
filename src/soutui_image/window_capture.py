"""界面验收只抓取本程序自己的窗口，即使被其他软件遮挡也不截取桌面内容。"""

import ctypes
from ctypes import wintypes


def capture_own_window(root, path):
    """输入本程序创建的 Tk 根窗口和图片路径，输出窗口客户区截图。"""
    from PIL import Image
    user, gdi = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("gdi32", use_last_error=True)
    signatures = (
        (user, "GetClientRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        (user, "GetDC", [wintypes.HWND], wintypes.HDC),
        (user, "ReleaseDC", [wintypes.HWND, wintypes.HDC], ctypes.c_int),
        (user, "PrintWindow", [wintypes.HWND, wintypes.HDC, wintypes.UINT], wintypes.BOOL),
        (gdi, "CreateCompatibleDC", [wintypes.HDC], wintypes.HDC),
        (gdi, "CreateCompatibleBitmap", [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP),
        (gdi, "SelectObject", [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
        (gdi, "DeleteObject", [wintypes.HANDLE], wintypes.BOOL),
        (gdi, "DeleteDC", [wintypes.HDC], wintypes.BOOL),
        (gdi, "GetDIBits", [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT], ctypes.c_int),
    )
    for lib, name, args, result in signatures:
        method = getattr(lib, name); method.argtypes = args; method.restype = result
    handle = root.winfo_id()
    rect = wintypes.RECT()
    if not user.GetClientRect(handle, ctypes.byref(rect)): raise RuntimeError("无法取得本程序窗口尺寸")
    width, height = rect.right, rect.bottom
    display = user.GetDC(handle)
    memory = gdi.CreateCompatibleDC(display)
    bitmap = gdi.CreateCompatibleBitmap(display, width, height)
    previous = gdi.SelectObject(memory, bitmap)
    try:
        if not user.PrintWindow(handle, memory, 3): raise RuntimeError("本程序窗口抓图失败")
        gdi.SelectObject(memory, previous); previous = None
        # BITMAPINFOHEADER：32 位、无压缩、从上往下排列。
        import struct
        info = ctypes.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0))
        pixels = ctypes.create_string_buffer(width * height * 4)
        if gdi.GetDIBits(display, bitmap, 0, height, pixels, info, 0) != height:
            raise RuntimeError("本程序窗口像素读取不完整")
        Image.frombytes("RGB", (width, height), pixels.raw, "raw", "BGRX").save(path)
    finally:
        if previous: gdi.SelectObject(memory, previous)
        gdi.DeleteObject(bitmap); gdi.DeleteDC(memory); user.ReleaseDC(handle, display)
