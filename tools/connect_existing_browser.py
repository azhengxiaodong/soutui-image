"""一次性连接现有 MuseFit 登录态；后续 HTTP 请求不再依赖鼠标或打开的网页。

仅从已核对域名的页面读取 genlight_token，不访问密码或其他站点的存储。
经临时剪贴板传入当前 Python 进程，立即以 DPAPI 加密，并恢复原剪贴板。
遇到 DevTools 粘贴安全提示时停止，不能输入允许粘贴或更改安全设置。
"""

from __future__ import annotations

import ctypes
import json
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import uiautomation as auto

from soutui_image.browser_ui import ExistingBrowser
from soutui_image.web_session import ORIGIN, save_session


def _devtools(browser):
    items = [c.control for c in browser.walk(browser.window(), max_depth=16)
             if c.control.ControlTypeName == "DocumentControl" and c.control.Name == "DevTools"]
    if len(items) != 1:
        raise RuntimeError("请先在现有 MuseFit 标签打开开发者工具控制台")
    return items[0]


def _check_site(browser):
    docs = [c.control for c in browser.walk(browser.window(), max_depth=16)
            if c.control.ControlTypeName == "DocumentControl" and "MuseFit AI" in c.control.Name]
    if len(docs) != 1:
        raise RuntimeError("需要当前已选中的唯一 MuseFit 网页")
    address = urlsplit(docs[0].GetLegacyIAccessiblePattern().Value)
    if f"{address.scheme}://{address.netloc}" != ORIGIN or not (
            address.path == "/app" or address.path.startswith("/app/")):
        raise RuntimeError("目标网页地址不符，未读取任何会话")


class _ClipboardBackup:
    """保留 OLE 剪贴板数据对象，避免只恢复文本而丢失用户原来的图片等格式。"""

    def __enter__(self):
        self.ole = ctypes.OleDLL("ole32")
        self.ole.OleInitialize.argtypes = [ctypes.c_void_p]
        self.ole.OleInitialize.restype = ctypes.c_long
        self.ole.OleGetClipboard.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.ole.OleGetClipboard.restype = ctypes.c_long
        self.ole.OleSetClipboard.argtypes = [ctypes.c_void_p]
        self.ole.OleSetClipboard.restype = ctypes.c_long
        status = self.ole.OleInitialize(None)
        if status not in (0, 1):
            raise RuntimeError("无法初始化剪贴板保护")
        self.original = ctypes.c_void_p()
        status = self.ole.OleGetClipboard(ctypes.byref(self.original))
        if status != 0:
            self.ole.OleUninitialize()
            raise RuntimeError("无法保留原剪贴板，未读取会话")
        return self

    def __exit__(self, *exc):
        status = self.ole.OleSetClipboard(self.original)
        if self.original:
            table = ctypes.cast(self.original, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])
            release(self.original)
        self.ole.OleUninitialize()
        if status != 0:
            raise RuntimeError("原剪贴板恢复失败，请重新复制需要的内容")


def connect(path=ROOT / "data" / "local" / "musefit_session.dpapi"):
    browser = ExistingBrowser()
    _check_site(browser)
    dev = _devtools(browser)
    controls = browser.walk(dev, max_depth=32)
    for item in controls:
        name = item.control.Name.lower()
        if any(word in name for word in ("allow pasting", "允许粘贴", "don't paste", "不要粘贴")):
            raise RuntimeError("开发者工具出现粘贴安全提示，已停止；不更改安全保护")
    prompts = [c.control for c in controls if c.control.ControlTypeName == "EditControl"
               and c.control.Name in ("控制台提示", "Console prompt") and not c.control.IsOffscreen]
    if len(prompts) != 1:
        raise RuntimeError("未找到唯一控制台输入框")
    prompt = prompts[0]
    current = prompt.GetValuePattern().Value
    if current.strip():
        raise RuntimeError("控制台有未提交内容，停止以免覆盖")
    nonce = secrets.token_hex(12)
    script = ('copy(JSON.stringify({bridge:"soutui-session-v1",nonce:"' + nonce
              + '",origin:location.origin,token:localStorage.getItem("genlight_token")||""}))')
    prompt.SetFocus()
    if not prompt.HasKeyboardFocus:
        raise RuntimeError("控制台未聚焦，停止输入")
    with _ClipboardBackup():
        prompt.GetValuePattern().SetValue(script)
        if prompt.GetValuePattern().Value.strip() != script:
            raise RuntimeError("控制台指令写入未通过核对，未执行")
        prompt.SendKeys("{Enter}", waitTime=.1)
        deadline = time.monotonic() + 8
        payload = None
        while time.monotonic() < deadline:
            try:
                candidate = json.loads(auto.GetClipboardText())
                if isinstance(candidate, dict) and candidate.get("nonce") == nonce:
                    payload = candidate
                    break
            except (ValueError, TypeError):
                pass
            time.sleep(.15)
        if not payload:
            raise RuntimeError("未收到本次会话连接结果，未保存登录态")
        _check_site(browser)
        save_session(payload, path)
    # 只报告成功与存储方式；令牌与剪贴板内容绝不进入标准输出。
    return {"status": "connected", "origin": ORIGIN, "storage": "Windows DPAPI", "clipboard_restored": True}


if __name__ == "__main__":
    try:
        print(json.dumps(connect(), ensure_ascii=True))
    except Exception as error:
        print(json.dumps({"status": "needs_attention", "error": str(error)}, ensure_ascii=True))
        raise SystemExit(2)
