"""整批流程运行锁：同一数据目录只允许一条流程，进程退出由系统释放锁。"""

import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def pipeline_lock(directory):
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "pipeline.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0"); handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError: raise RuntimeError("已有流程在运行，请等待或在原窗口暂停") from None
            try: yield
            finally:
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError: raise RuntimeError("已有流程在运行") from None
            try: yield
            finally: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
