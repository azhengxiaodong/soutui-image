"""输出目录的内部账本：图片留原处，清单和并发锁保存在软件数据目录。

旧版两个辅助文件只在持锁、核对图片哈希且内部清单回读成功后移除。
读取接口不迁移、不创建目录；迁移入口须在旧工作台已退出或暂停后调用。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from .atomic_json import atomic_write_json


MANIFEST_NAME = ".搜推生图.json"
LOCK_NAME = ".soutui.lock"
_process_locks = {}
_locks_guard = threading.Lock()


def _no_links(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("输出或数据路径含目录链接，未读写")
    return path.resolve()


def output_identity(output_root):
    """输出绝对路径是锁和账本的定位键；商品与原图身份在账本内再次核对。"""
    root = _no_links(output_root)
    canonical = os.path.normcase(str(root))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), canonical


def manifest_path(output_root, state_directory):
    """state_directory 为 data/tasks，返回 data/outputs/路径哈希/manifest.json。"""
    key, _ = output_identity(output_root)
    tasks = _no_links(state_directory)
    return _no_links(tasks.parent / "outputs" / key / "manifest.json")


def _validate_record(record, output_root, product=None, plan=None):
    if not isinstance(record, dict) or record.get("app") != "soutui-image" or record.get("schema_version") != 1:
        raise ValueError("输出清单格式不符，未覆盖")
    _, canonical = output_identity(output_root)
    if record.get("output_directory", canonical) != canonical:
        raise ValueError("内部输出清单绑定了其他目录")
    if product is not None and (record.get("product_id") != product.product_id
                                or record.get("source_sha256") != product.sha256):
        raise ValueError("输出清单不属于当前商品或原图")
    groups = record.get("groups")
    if not isinstance(groups, list) or len(groups) != 9:
        raise ValueError("输出清单缺少完整九组")
    for row, group in enumerate(groups, 1):
        if not isinstance(group, dict) or type(group.get("row")) is not int or group["row"] != row:
            raise ValueError("输出清单组号异常")
        if group.get("directory") != f"组{row:02}" or not isinstance(group.get("saved"), list):
            raise ValueError("输出清单分组路径异常")
    if plan is not None:
        actual = [(g["row"], g.get("folder_id"), g.get("reference_ids")) for g in groups]
        expected = [(g.row, g.folder_id, [r.image_id for r in g.references]) for g in plan.assignments]
        if actual != expected:
            raise ValueError("输出清单与当前九组参考计划不符")
    return record


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _business_record(record):
    return {k: v for k, v in record.items() if k != "output_directory"}


def read_manifest(output_root, state_directory, product=None):
    """只读内部清单；尚未迁移时兼容旧输出清单，不改任何图片或文件。"""
    root = _no_links(output_root)
    target = manifest_path(root, state_directory)
    legacy = _no_links(root / MANIFEST_NAME)
    internal = _validate_record(_read(target), root, product) if target.exists() else None
    old = _validate_record(_read(legacy), root, product) if legacy.exists() else None
    if internal is not None and old is not None and _business_record(internal) != _business_record(old):
        raise ValueError("内部与旧版输出清单冲突，未覆盖任何记录")
    return internal if internal is not None else old


def save_manifest(output_root, state_directory, record):
    """调用者持 output_lock 后保存；路径绑定随清单原子写入，保留全部业务字段。"""
    root = _no_links(output_root)
    record = copy.deepcopy(_validate_record(record, root))
    record["output_directory"] = output_identity(root)[1]
    target = manifest_path(root, state_directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    _no_links(target)
    atomic_write_json(target, record)
    if _read(target) != record:
        raise OSError("内部输出清单回读不一致")
    return target


@contextmanager
def _file_lock(path, *, existing_only=False):
    if existing_only and not path.exists():
        yield
        return
    path = _no_links(path)
    with path.open("r+b" if existing_only else "a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0"); handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("当前商品仍被其他保存操作占用") from None
            try:
                yield
            finally:
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("当前商品仍被其他保存操作占用") from None
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _windows_mutex(key):
    if os.name != "nt":
        yield
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, "Local\\soutui-output-" + key)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        status = kernel.WaitForSingleObject(handle, 0)
        if status not in (0, 0x80):
            if status == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            raise RuntimeError("当前输出目录正在被另一工作台处理")
        acquired = True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


@contextmanager
def output_lock(output_root, state_directory):
    """内部文件锁加 Windows 输出路径互斥，保护跨线程、进程及不同 data 根目录。"""
    key, _ = output_identity(output_root)
    with _locks_guard:
        local = _process_locks.setdefault(key, threading.Lock())
    if not local.acquire(blocking=False):
        raise RuntimeError("当前输出目录正在被另一个操作处理")
    try:
        with _windows_mutex(key):
            path = manifest_path(output_root, state_directory).parent / "output.lock"
            path.parent.mkdir(parents=True, exist_ok=True)
            with _file_lock(path):
                yield
    finally:
        local.release()


def _verify_saved_images(root, record):
    for group in record["groups"]:
        directory = _no_links(root / group["directory"])
        for item in group["saved"]:
            name = item.get("filename")
            if not isinstance(name, str) or Path(name).name != name or name in (".", ".."):
                raise ValueError("旧输出清单文件名越界")
            image = _no_links(directory / name)
            if image.parent != directory or not image.is_file():
                raise ValueError("旧输出图片缺失，未迁移清单")
            if hashlib.sha256(image.read_bytes()).hexdigest() != item.get("sha256"):
                raise ValueError("旧输出图片已改变，未迁移清单")


def migrate_manifest(output_root, state_directory, product, plan=None):
    """调用者持 output_lock；验证旧图片后迁移两辅助文件，原始图片字节始终不动。"""
    root = _no_links(output_root)
    legacy = _no_links(root / MANIFEST_NAME)
    legacy_lock = _no_links(root / LOCK_NAME)
    with _file_lock(legacy_lock, existing_only=True):
        record = read_manifest(root, state_directory, product)
        if record is not None:
            _validate_record(record, root, product, plan)
        if legacy.exists():
            raw = legacy.read_bytes()
            _verify_saved_images(root, record)
            target = save_manifest(root, state_directory, record)
            if legacy.read_bytes() != raw:
                raise ValueError("迁移期间旧输出清单发生变化，保留旧文件")
            # 内部回读验证已完成后才能移除旧清单；失败仍有双方完整记录可重试。
            legacy.unlink()
            record = _read(target)
    if legacy_lock.exists():
        legacy_lock.unlink()
    return record
