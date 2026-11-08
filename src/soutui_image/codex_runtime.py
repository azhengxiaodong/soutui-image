"""图片专用 Codex 调用：减少无关启动连接，记录阶段耗时并恢复短暂网络失败。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


_observer = ContextVar("image_call_observer", default=None)


@contextmanager
def observe_calls(callback):
    """把阶段进度带入现有审核线程；不参与审核结论或改变业务断点。"""
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


def _emit(**event):
    callback = _observer.get()
    if callback:
        callback(event)


def _system_proxy():
    """只读取本机用户已启用的 Windows 代理；不配置新代理或更改系统设置。"""
    if os.name != "nt":
        return {}
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return {}
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
    except OSError:
        return {}
    if not isinstance(server, str) or not server.strip():
        return {}
    # 不把 PAC、SOCKS 或不明协议猜成 HTTP；已有显式进程代理优先。
    entries = dict(part.split("=", 1) for part in server.split(";") if "=" in part)
    result = {}
    for scheme in ("http", "https"):
        value = entries.get(scheme) if entries else server
        if value and ":" in value and not any(c.isspace() for c in value):
            if "://" not in value:
                value = "http://" + value
            if value.startswith(("http://", "https://")):
                result[scheme.upper() + "_PROXY"] = value
    return result


def image_process_environment():
    """沿用登录、TLS 和服务端点；仅让子进程明确继承已有系统网络通道。"""
    env = os.environ.copy()
    if not any(env.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                   "http_proxy", "https_proxy", "all_proxy")):
        proxy = _system_proxy()
        env.update(proxy)
        if proxy and not env.get("NO_PROXY") and not env.get("no_proxy"):
            env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    return env


def transient_failure(stderr):
    """只重试连接/路由类故障；登录、额度、配置、图片和结构校验错误不盲重试。"""
    value = stderr.lower()
    if (any(word in value for word in ("usage limit", "quota", "unauthorized", "forbidden",
                                      "invalid api key", "authentication"))
            or re.search(r"\b(?:401|403|429)\b", value)):
        return False
    return any(word in value for word in ("workspace routing discovery failed",
               "workspace routing discovery timed out", "error sending request for url",
               "connection reset", "connection closed", "stream disconnected", "502 bad gateway",
               "503 service unavailable", "504 gateway timeout"))


def run_image_call(command, *, stage, **kwargs):
    """保留全部图片、提示词、模型与推理配置；每个网络尝试保留独立响应和计时。"""
    command = list(command)
    # 图片已由 --image 附带，审核提示词禁止工具。只关闭此子进程的无关集成。
    # 项目 AGENTS 可能记载人工样本标签，不可作为审图依据；保留用户级 HTTPS 配置。
    command[2:2] = ["--disable", "apps", "--disable", "plugins",
                    "--disable", "multi_agent", "--disable", "shell_tool",
                    "-c", "project_doc_max_bytes=0"]
    root = Path(kwargs["cwd"]).resolve()
    response_index = command.index("--output-last-message") + 1
    response = Path(command[response_index]).resolve()
    kwargs["env"] = image_process_environment()
    timings = []
    call_id = str(time.time_ns())
    for index in range(1, 3):
        attempt = root / f"transport_{call_id}_{index}"
        attempt.mkdir(parents=True)
        attempt_response = attempt / "response.json"
        command[response_index] = str(attempt_response)
        started = time.monotonic()
        _emit(stage=stage, state="started", attempt=index)
        try:
            result = subprocess.run(command, **kwargs)
        except subprocess.TimeoutExpired as error:
            for name, value in (("stdout", error.stdout), ("stderr", error.stderr)):
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                (attempt / f"{name}.txt").write_text(value or "", encoding="utf-8")
            timings.append({"attempt": index, "seconds": round(time.monotonic() - started, 2), "timeout": True})
            (root / f"transport_{call_id}.json").write_text(json.dumps(timings), encoding="utf-8")
            _emit(stage=stage, state="timeout", seconds=timings[-1]["seconds"])
            raise
        (attempt / "stdout.txt").write_text(result.stdout, encoding="utf-8")
        (attempt / "stderr.txt").write_text(result.stderr, encoding="utf-8")
        # 返回码成功也必须有本次响应，不能误用上次留下的 response.json。
        if result.returncode == 0 and not attempt_response.is_file():
            result = subprocess.CompletedProcess(command, 1, result.stdout,
                                                  result.stderr + "\n本次调用缺少完整响应")
        timings.append({"attempt": index, "seconds": round(time.monotonic() - started, 2),
                        "returncode": result.returncode})
        (root / f"transport_{call_id}.json").write_text(json.dumps(timings), encoding="utf-8")
        if result.returncode == 0:
            response.write_bytes(attempt_response.read_bytes())
            _emit(stage=stage, state="finished", seconds=timings[-1]["seconds"])
            return result
        if index == 2 or not transient_failure(result.stderr):
            _emit(stage=stage, state="failed", seconds=timings[-1]["seconds"])
            return result
        _emit(stage=stage, state="retry", attempt=index + 1)
    raise AssertionError("不可达的调用状态")
