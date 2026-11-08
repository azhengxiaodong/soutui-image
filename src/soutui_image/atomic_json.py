"""运行记录原子写入：容忍 Windows 短暂共享占用，不重复任何业务请求。"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path


def atomic_write_json(path: Path, record: dict, *, retry_seconds=3.0, retry_interval=0.05):
    """完整候选关闭后替换正式文件；仅有界重试 Windows 占用错误，失败保留候选。"""
    if (not math.isfinite(retry_seconds) or retry_seconds < 0
            or not math.isfinite(retry_interval) or retry_interval <= 0):
        raise ValueError("文件替换重试上限须为非负有限值，间隔须为正有限值")
    path = Path(path)
    payload = json.dumps(record, ensure_ascii=False, indent=2)
    # 唯一名称避免旧失败候选或其他写入者互相覆盖；同目录保证同卷原子替换。
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            temporary.replace(path)
            return
        except OSError as error:
            # UI/审核读取及安全软件可能短暂禁止删除共享；权限永久错误仍在上限内报错。
            remaining = deadline - time.monotonic()
            if getattr(error, "winerror", None) not in (5, 32, 33) or remaining <= 0:
                raise
            time.sleep(min(retry_interval, remaining))
