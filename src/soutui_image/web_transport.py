"""后续接口模块的传输层：严格限制站内接口和方法，不重试可能已提交的请求。

复用已经验收的本机会话解密与禁止重定向机制，保留 C 的只读实现原样。
任务提交的实际业务验证由 D 负责；本文件提供传输，不在导入时发请求。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .web_session import MAX_RESPONSE_BYTES, ORIGIN, MusefitReadClient, SessionExpired, load_session


class RequestOutcomeUnknown(RuntimeError):
    """写入请求遇到网络中断，结果可能已到达服务器，调用方必须先查回而非重发。"""


class WebTransport:
    GET_PATHS = frozenset({"/api/web/oss/sign-url", "/api/web/tasks", "/api/web/image-tiers", "/api/auth/me"})
    POST_PATHS = frozenset({"/api/web/tasks/submit"})

    def __init__(self, session_path, *, timeout=30):
        self.session_path = session_path
        self.timeout = timeout
        self.opener = MusefitReadClient(session_path).opener

    def request_json(self, method, path, *, query=None, body=None):
        allowed = ((method == "GET" and (path in self.GET_PATHS
                    or re.fullmatch(r"/api/web/tasks/[A-Za-z0-9_.-]+", path)))
                   or (method == "POST" and path in self.POST_PATHS))
        if not allowed:
            raise ValueError("当前程序不允许这个接口或请求方法")
        token = load_session(self.session_path)["token"]
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json",
                   "User-Agent": "soutui-image/0.1", "Referer": ORIGIN + "/app/"}
        encoded = None
        if body is not None:
            if method != "POST":
                raise ValueError("GET 请求不能携带写入数据")
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        suffix = "?" + urllib.parse.urlencode(query) if query else ""
        request = urllib.request.Request(ORIGIN + path + suffix, data=encoded, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code in (401, 403):
                raise SessionExpired(f"网站会话或账号权限不可用（HTTP {code}）") from None
            if code >= 500 and method == "POST":
                raise RequestOutcomeUnknown(f"提交返回 HTTP {code}，需要先查询是否已产生任务") from None
            raise RuntimeError(f"网站接口返回 HTTP {code}，没有自动重发") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if method == "POST":
                raise RequestOutcomeUnknown("提交过程中网络中断；需要先查回任务，不能直接重发") from None
            raise RuntimeError("网站读取接口连接失败，没有自动重发") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("接口响应超出大小限制")
        if token.encode() in raw:
            raise RuntimeError("响应意外回显会话，已阻止保存")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            if method == "POST":
                raise RequestOutcomeUnknown("提交响应无法解析，需要先查询是否已产生任务") from None
            raise RuntimeError("接口响应不是有效 JSON") from None
        if not isinstance(data, dict):
            raise RuntimeError("接口响应不是对象")
        return data
