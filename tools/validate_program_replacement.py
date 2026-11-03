"""使用真实旧ZIP和新ZIP验证签名程序替换、旧版备份和本机记录保留，不伪造exe。"""

import hashlib
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/"src"))
from soutui_image.program_update import unpack_verified,apply_program_update
from soutui_image.rule_store import write_json
from soutui_image.update_security import sign_payload
from soutui_image.update_config import update_source,APP_VERSION


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path): return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--release-evidence",type=Path,required=True); args=parser.parse_args()
    target=args.output.resolve(); target.mkdir(parents=True,exist_ok=False)
    old_sha="a59d5ecb09c497a01675a4f0a39625364e6883637554fb1feef916789973625a"
    old=ROOT/"release/history"/("搜推生图-旧版-"+old_sha[:16]+".zip")
    assert sha(old)==old_sha
    old_app=unpack_verified(old,target/"old_install")
    old_exe_hash=sha(old_app/"搜推生图.exe")
    records=old_app/"运行记录"; records.mkdir(); (records/"progress.json").write_text('{"round":3,"sentinel":"keep"}',encoding="utf-8")
    record_hash=sha(records/"progress.json")
    release=read(args.release_evidence/"release.json")
    archive=Path(release["archive"]); assert sha(archive)==release["sha256"]
    staged=target/"staged"; staged.mkdir()
    archived=staged/"program.zip"; archived.write_bytes(archive.read_bytes())
    application=unpack_verified(archived,staged/"unpacked")
    payload={"format":1,"sequence":2,"repository_id":update_source()["repository_id"],"channel":"stable","rules":{},
             "program":{"id":0,"size":archive.stat().st_size,"sha256":release["sha256"],"version":APP_VERSION},"revoked":[]}
    envelope=sign_payload(payload,ROOT/"data/local/update_signing_key.dpapi")
    proof=staged/"install.json"; write_json(proof,{"envelope":envelope,"archive":str(archived),"application":str(application)})
    result=apply_program_update(proof,old_app,data_directory=records)
    assert sha(old_app/"搜推生图.exe")==release["executable_sha256"]
    assert sha(records/"progress.json")==record_hash
    assert sha(Path(result["backup"])/"搜推生图.exe")==old_exe_hash
    process=subprocess.run([str(old_app/"搜推生图.exe"),"--self-test","--data-dir",str(target/"after_diagnostic")],timeout=60,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    assert process.returncode==0
    diagnostic=read(target/"after_diagnostic/self_test_result.json")
    assert diagnostic["app_version"]==APP_VERSION and diagnostic["signature_support"]
    report={"status":"verified","real_old_executable_sha256":old_exe_hash,"real_new_executable_sha256":release["executable_sha256"],
            "old_program_backup_retained":True,"in_install_directory_user_data_preserved":True,"new_executable_self_test":diagnostic,
            "scope":"本机真实两版程序文件及签名ZIP的目录替换、记录保留与新exe启动；没有跨电脑验收或新增生图",
            "new_model_calls":0,"new_generation_submissions":0,"replacement":result}
    (target/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="new_executable_self_test"},ensure_ascii=True))


if __name__=="__main__": main()
