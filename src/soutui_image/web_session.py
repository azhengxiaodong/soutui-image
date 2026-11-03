"""网站会话接口基础：只使用用户已有的 MuseFit 会话，在固定原站点发起请求。

会话经 Windows DPAPI 加密保存在本机 data/local，不写入日志、不随软件分发。
本文件先提供只读 GET；上传和生成分别独立验收后再扩展，导入时无网络动作。
"""

from __future__ import annotations

import ctypes
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path


ORIGIN = "https://ai.51dingxiao.com"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class SessionExpired(RuntimeError):
    """会话无效时交给用户正常登录，不重复尝试或访问其他账号。"""


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def _protect(raw: bytes, *, decrypt=False) -> bytes:
    """调用当前 Windows 用户的 DPAPI；文件复制到别的电脑不能直接变成可用登录态。"""
    if os.name != "nt":
        raise RuntimeError("当前会话存储只支持 Windows")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_string_buffer(raw)
    source = _Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = _Blob()
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise RuntimeError("本机会话加密或解密失败；请在当前电脑重新连接网站")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def save_session(payload: dict, path: str | Path) -> None:
    """仅保存指定网站的已有会话，不接受密码、其他 localStorage 或新建的密钥。"""
    if payload.get("origin") != ORIGIN:
        raise ValueError("会话来源与当前生图站点不符")
    token = payload.get("token")
    if (not isinstance(token, str) or not token or len(token) > 8192
            or any(ord(c) < 33 or ord(c) > 126 for c in token)):
        raise SessionExpired("当前网站没有可用登录会话，请先正常登录")
    permitted = {"schema_version": 1, "origin": ORIGIN, "token": token, "captured_at": time.time()}
    encrypted = _protect(json.dumps(permitted, ensure_ascii=False).encode("utf-8"))
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(encrypted)
    temporary.replace(target)


def load_session(path: str | Path) -> dict:
    """只在需要请求时解密到内存；不返回给控制台或写入验收报告。"""
    data = json.loads(_protect(Path(path).read_bytes(), decrypt=True))
    token = data.get("token")
    if (data.get("origin") != ORIGIN or not isinstance(token, str) or not token
            or len(token) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token)):
        raise SessionExpired("本机会话记录无效，请重新连接")
    return data


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MusefitReadClient:
    """只读客户端：请求固定域名，令牌不随重定向发送，不自动重试失败操作。"""

    READ_PATHS = frozenset({"/api/web/favorites", "/api/auth/me", "/api/config"})

    def __init__(self, session_path: str | Path, *, timeout=25):
        self.session_path = Path(session_path)
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_NoRedirect())

    def get_json(self, path: str, query: dict | None = None) -> dict:
        if path not in self.READ_PATHS:
            raise ValueError("当前只读验证入口不允许该接口")
        session = load_session(self.session_path)
        token = session["token"]
        suffix = "?" + urllib.parse.urlencode(query) if query else ""
        request = urllib.request.Request(ORIGIN + path + suffix, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json",
            "User-Agent": "soutui-image/0.1", "Referer": ORIGIN + "/app/",
        })
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("接口响应过大，已停止读取")
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code in (401, 403):
                raise SessionExpired(f"网站会话或当前账号权限不可用（HTTP {code}），未继续操作") from None
            raise RuntimeError(f"网站接口返回 HTTP {code}，已停止；没有重复请求") from None
        except urllib.error.URLError:
            raise RuntimeError("网站接口网络连接失败，已停止；没有重复请求") from None
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise RuntimeError("网站接口没有返回有效 JSON") from None
        if not isinstance(data, dict):
            raise ValueError("网站接口响应不是对象")
        # 返回中若意外回显当前会话，也不能交给调用方保存到证据文件。
        if token.encode() in raw:
            raise RuntimeError("接口意外回显会话内容，已阻止保存响应")
        return data
