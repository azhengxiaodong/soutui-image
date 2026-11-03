"""将本次ZIP解压到新目录，实际启动exe自检和窗口；只证明本机迁移，不代表其他电脑。"""

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def read(path): return json.loads(path.read_text(encoding="utf-8"))
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    """解压自己的交付包，核对每个构建文件，再验证新路径中的真实exe。"""
    parser=argparse.ArgumentParser(); parser.add_argument("--release-evidence",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True); args=parser.parse_args()
    release=read(args.release_evidence/"release.json"); archive=Path(release["archive"])
    assert sha(archive)==release["sha256"]
    target=args.output.resolve(); target.mkdir(parents=True,exist_ok=False)
    extraction=target/"迁移后的软件目录"; extraction.mkdir()
    with zipfile.ZipFile(archive) as source:
        assert source.testzip() is None
        for entry in source.namelist():
            assert (extraction/entry).resolve().is_relative_to(extraction)
        source.extractall(extraction)
    app=extraction/"搜推生图"; exe=app/"搜推生图.exe"
    assert sha(exe)==release["executable_sha256"]
    build=read(Path(release["build_evidence"])/"build_manifest.json")
    for entry in build["files"]: assert sha(app/entry["path"])==entry["sha256"]
    for arguments in (("--self-test","--data-dir",str(target/"diagnostic")),
                      ("--gui","--data-dir",str(target/"fresh_data"),"--ui-smoke-output",str(target/"gui"))):
        result=subprocess.run([str(exe),*arguments],timeout=60,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        assert result.returncode==0
    diagnostic=read(target/"diagnostic/self_test_result.json"); gui=read(target/"gui/smoke.json")
    assert diagnostic["status"]=="ok" and diagnostic["policy_version"]==release["policy_version"]
    assert gui["window_created"] and gui["group_rows"]==9 and gui["recheck_button_present"]
    assert not gui["generation_started"] and gui["products"]==0
    report={"status":"verified","executable_sha256":sha(exe),"archive_sha256":sha(archive),
            "self_test":diagnostic,"gui":gui,"zip_files_verified":True,"same_computer_relocation":True,
            "other_computer_verified":False,"new_generation_submissions":0,"new_model_calls":0}
    (target/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in ("self_test","gui")},ensure_ascii=True))


if __name__=="__main__": main()
