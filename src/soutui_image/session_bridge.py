"""桌面版会话连接变体；旧 C 连接工具保持冻结，新增剪贴板恢复兼容处理。"""

import json
import secrets
import time

import uiautomation as auto

from .clipboard_transfer import ClipboardTransfer
from .web_session import ORIGIN, save_session
from tools.connect_existing_browser import _check_site, _devtools


def connect_browser(browser, path):
    """仅从已核对的现有站点连接会话，加密保存；返回状态不含令牌。"""
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
    if prompt.GetValuePattern().Value.strip():
        raise RuntimeError("控制台有未提交内容，停止以免覆盖")
    nonce = secrets.token_hex(12)
    script = ('copy(JSON.stringify({bridge:"soutui-session-v1",nonce:"' + nonce
              + '",origin:location.origin,token:localStorage.getItem("genlight_token")||""}))')
    prompt.SetFocus()
    if not prompt.HasKeyboardFocus:
        raise RuntimeError("控制台未聚焦，停止输入")
    with ClipboardTransfer():
        prompt.GetValuePattern().SetValue(script)
        if prompt.GetValuePattern().Value.strip() != script:
            raise RuntimeError("控制台指令写入未通过核对，未执行")
        prompt.SendKeys("{Enter}", waitTime=.1)
        deadline, payload = time.monotonic() + 8, None
        while time.monotonic() < deadline:
            try:
                candidate = json.loads(auto.GetClipboardText())
                if isinstance(candidate, dict) and candidate.get("nonce") == nonce:
                    payload = candidate; break
            except (ValueError, TypeError): pass
            time.sleep(.15)
        if not payload:
            raise RuntimeError("未收到本次会话连接结果，未保存登录态")
        _check_site(browser)
        save_session(payload, path)
    return {"status": "connected", "origin": ORIGIN, "storage": "Windows DPAPI", "clipboard_restored": True}
