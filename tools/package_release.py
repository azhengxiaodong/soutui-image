"""用本次exe绑定的自检及回归证据制作交付包，验证成功后仅保留当前ZIP。"""

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as handle: return hashlib.file_digest(handle,"sha256").hexdigest()
def read(path): return json.loads(path.read_text(encoding="utf-8"))


def write_current_archive(app, target):
    """先逐文件回读校验新ZIP，再原子替换当前包；成功后删除项目内旧程序包。"""
    app, target = Path(app).resolve(), Path(target).resolve()
    release = (ROOT / "release").resolve()
    if target.parent != release or app != (ROOT / "dist/搜推生图").resolve():
        raise ValueError("程序包路径必须位于本项目的 dist/release")
    history = release / "history"
    old_archives = [p for p in release.glob("*.zip") if p != target]
    # 删除前解析所有目标，拒绝链接目录或越界文件。
    for path in [history, *old_archives]:
        if (not path.resolve().is_relative_to(release) or path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                or path.is_dir() and any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in path.rglob("*"))):
            raise ValueError("旧程序包路径不安全")
    files = sorted(p for p in app.rglob("*") if p.is_file())
    temporary = target.with_suffix(".partial.zip")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in files:
                archive.write(path, path.relative_to(app.parent).as_posix())
        with zipfile.ZipFile(temporary) as archive:
            assert archive.testzip() is None and len(archive.namelist()) == len(files)
            for path in files:
                name = path.relative_to(app.parent).as_posix()
                assert hashlib.sha256(archive.read(name)).hexdigest() == digest(path), "新ZIP内容校验失败"
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    # 新包成功前不删除旧包；正式记录、任务绑定的规则及用户素材不在清理范围。
    if history.exists():
        shutil.rmtree(history)
    for path in old_archives:
        if path.exists():
            path.unlink()
    return files


def main():
    """验收文件必须对应当前exe；保留历史连接证据的范围，不冒用为新exe连接实测。"""
    parser=argparse.ArgumentParser()
    parser.add_argument("--build-evidence",type=Path,required=True)
    parser.add_argument("--runtime-evidence",type=Path,required=True)
    parser.add_argument("--output-evidence",type=Path,required=True)
    args=parser.parse_args()
    app=ROOT/"dist/搜推生图"; exe=app/"搜推生图.exe"
    manifest=read(args.build_evidence/"build_manifest.json")
    runtime=read(args.runtime_evidence/"result.json")
    assert runtime["status"]=="verified" and runtime["executable_sha256"]==digest(exe)
    assert runtime["review_model"]=="gpt-6.1-sol" and runtime["policy_version"]=="2026-11-03.3"
    assert runtime["self_test"]["audit_timeout_per_call_seconds"]==600
    assert runtime["self_test"]["https_verification_required"]
    for item in manifest["files"]: assert digest(app/item["path"])==item["sha256"], "构建文件改变"
    for path,value in manifest["source_sha256"].items(): assert digest(ROOT/path)==value, "构建后的源码改变"
    # 浏览器连接实现没有修改；旧真实证据继续只表示登记时的Edge连接结果。
    frozen=read(ROOT/"validation/frozen_modules.json")["modules"]["DESKTOP_CONNECT"]
    for path,value in frozen["files"].items(): assert digest(ROOT/path)==value
    shutil.copyfile(ROOT/"docs/Windows版使用说明.md",app/"使用说明.txt")
    release=ROOT/"release"; release.mkdir(exist_ok=True)
    target=release/"搜推生图-Windows测试版.zip"
    files=sorted(p for p in app.rglob("*") if p.is_file())
    assert all(p.suffix.lower() not in (".dpapi",".jpg",".jpeg",".png") for p in files)
    assert all("musefit_session" not in p.name.lower() for p in files)
    assert all(part.lower() not in ("data","review_cache","reviews","quality_withdrawn") for p in files for part in p.relative_to(app).parts)
    files=write_current_archive(app,target)
    report={"archive":str(target),"bytes":target.stat().st_size,"sha256":digest(target),"file_count":len(files),
            "executable_sha256":digest(exe),"policy_version":runtime["policy_version"],"review_model":runtime["review_model"],
            "includes_local_session":False,"includes_product_images":False,"requires_separate_python_installation":False,
            "requires_own_codex_login":True,"audit_timeout_per_call_seconds":600,"local_executable_smoke_verified":True,
            "existing_browser_connection_code_unchanged":True,"new_executable_browser_connection_retested":False,
            "build_evidence":str(args.build_evidence),"runtime_evidence":str(args.runtime_evidence),"other_computer_verified":False}
    args.output_evidence.mkdir(parents=True,exist_ok=False)
    (args.output_evidence/"release.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    (release/"SHA256.txt").write_text(report["sha256"]+"  "+target.name+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=True))


if __name__=="__main__": main()
