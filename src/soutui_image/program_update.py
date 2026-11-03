"""程序更新：校验已签名的ZIP，准备同目录替换；保留旧程序和本机数据以便回退。"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
import uuid
import zipfile
from pathlib import Path,PurePosixPath

from .rule_store import read,write_json
from .update_config import update_source
from .update_security import verify_payload


def unpack_verified(archive,destination):
    """仅解压本程序的目录结构，限制文件数、总大小、链接与越界路径。"""
    destination=Path(destination).resolve(); destination.mkdir(parents=True,exist_ok=False)
    total=0
    with zipfile.ZipFile(archive) as source:
        if len(source.infolist())>6000: raise ValueError("程序包文件数量异常")
        for entry in source.infolist():
            parts=PurePosixPath(entry.filename).parts
            if not parts or parts[0]!="搜推生图" or any(p in ("..",".") or ":" in p or "\\" in p for p in parts):
                raise ValueError("程序包路径异常")
            if stat.S_ISLNK(entry.external_attr>>16): raise ValueError("程序包不能包含链接")
            total+=entry.file_size
            if total>400*1024*1024: raise ValueError("程序包解压总量过大")
            target=(destination/entry.filename).resolve()
            if not target.is_relative_to(destination): raise ValueError("程序包路径越界")
        if source.testzip() is not None: raise ValueError("程序包损坏")
        source.extractall(destination)
    result=destination/"搜推生图"
    if not (result/"搜推生图.exe").is_file() or not (result/"_internal").is_dir(): raise ValueError("程序包缺少可运行文件")
    return result


def stage_program(service,*,descriptor=None):
    """明确更新操作后下载程序；当前exe仍继续使用，绝不在后台覆盖正在运行的文件。"""
    descriptor=descriptor or service.program_offer()
    if descriptor is None: return None
    catalog=service.store.root/"available.json"
    if not catalog.exists(): catalog=service.store.root/"stable.json"
    envelope=read(catalog); payload=service.store._payload(envelope)
    if payload["program"]!=descriptor: raise ValueError("更新程序与签名清单不符")
    raw=service.reader().download_asset(descriptor)
    if hashlib.sha256(raw).hexdigest()!=descriptor["sha256"]: raise ValueError("程序包哈希不符")
    folder=service.data/"updates"/("program-"+uuid.uuid4().hex); folder.mkdir(parents=True)
    archive=folder/"program.zip"; archive.write_bytes(raw)
    application=unpack_verified(archive,folder/"unpacked")
    proof={"envelope":envelope,"archive":str(archive),"application":str(application)}
    write_json(folder/"install.json",proof)
    return {"version":descriptor["version"],"application":str(application),"proof":str(folder/"install.json")}


def _verify_stage(proof_path):
    config=update_source(); proof=read(proof_path); payload=verify_payload(proof["envelope"],config["public_key"])
    if payload["repository_id"]!=config["repository_id"] or payload["channel"]!="stable" or not isinstance(payload["program"],dict):
        raise ValueError("安装清单不属于此仓库的稳定程序")
    archive=Path(proof["archive"]).resolve(); application=Path(proof["application"]).resolve()
    if not application.is_relative_to(Path(proof_path).resolve().parent) or not archive.is_relative_to(Path(proof_path).resolve().parent): raise ValueError("程序更新目录越界")
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=payload["program"]["sha256"]: raise ValueError("安装前程序包已改变")
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            if entry.is_dir(): continue
            parts=PurePosixPath(entry.filename).parts
            if not parts or parts[0]!="搜推生图" or ".." in parts: raise ValueError("安装包路径不符")
            file=application.joinpath(*parts[1:])
            if file.is_symlink() or not file.resolve().is_relative_to(application): raise ValueError("安装文件存在链接")
            if hashlib.sha256(file.read_bytes()).digest()!=hashlib.sha256(source.read(entry)).digest(): raise ValueError("待安装文件已改变")
    return application


def apply_program_update(proof_path,target_directory,*,wait_pid=0,data_directory=None):
    """从新程序副本执行。原进程退出后在已检查的应用目录旁切换，失败恢复旧目录。"""
    source=_verify_stage(proof_path); target=Path(target_directory).resolve()
    if source==target or target.is_symlink() or not (target/"搜推生图.exe").is_file() or not (target/"_internal").is_dir():
        raise ValueError("更新目标不是已存在的搜推生图应用目录")
    if target.parent==target or target==Path.home().resolve(): raise ValueError("不能替换根目录或用户目录")
    if wait_pid:
        import ctypes
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.OpenProcess.restype=ctypes.c_void_p
        handle=kernel.OpenProcess(0x00100000,False,int(wait_pid))
        if handle:
            try:
                if kernel.WaitForSingleObject(ctypes.c_void_p(handle),60000)!=0: raise RuntimeError("原软件尚未关闭，保留当前版本")
            finally: kernel.CloseHandle(ctypes.c_void_p(handle))
    parent=target.parent; pending=parent/(target.name+"-new-"+uuid.uuid4().hex); backup=parent/(target.name+"-backup-"+str(time.time_ns()))
    # 两个移动目标的最终绝对路径必须位于应用原父目录，且是本次新建路径。
    if pending.resolve().parent!=parent or backup.resolve().parent!=parent or pending.exists() or backup.exists():
        raise ValueError("程序切换路径不符")
    shutil.copytree(source,pending)
    if data_directory is not None:
        local_data=Path(data_directory).resolve()
        if local_data.is_relative_to(target):
            relative=local_data.relative_to(target)
            if not relative.parts or (pending/relative).exists(): raise ValueError("运行记录与程序包冲突，原程序保留")
            if local_data.exists(): shutil.copytree(local_data,pending/relative)
    moved=False
    try:
        target.rename(backup); moved=True; pending.rename(target)
    except Exception:
        if moved and not target.exists(): backup.rename(target)
        raise
    return {"status":"installed","target":str(target),"backup":str(backup)}


def launch_installer(staged,target_directory,*,parent_pid,data_directory):
    """GUI空闲且用户明确更新后启动独立副本；所有路径使用独立参数，不经shell拼接。"""
    executable=Path(staged["application"])/"搜推生图.exe"
    return subprocess.Popen([str(executable),"--apply-program-update",staged["proof"],"--update-target",str(target_directory),
                             "--wait-pid",str(parent_pid),"--data-dir",str(data_directory)],creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
