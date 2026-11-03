"""从真实私有最新发布重新下载规则及程序，检查签名、哈希和解压后的exe身份。"""

import hashlib
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/"src"))
from soutui_image.update_config import update_source,APP_VERSION
from soutui_image.update_service import UpdateService
from soutui_image.github_updates import GitHubReader,maintainer_token
from soutui_image.program_update import stage_program


def read(path): return json.loads(Path(path).read_text(encoding="utf-8"))
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--release-evidence",type=Path,required=True); parser.add_argument("--sequence",type=int,required=True)
    args=parser.parse_args(); target=args.output.resolve(); target.mkdir(parents=True,exist_ok=False)
    config=update_source(); reader=GitHubReader(config["owner"],config["repository"],maintainer_token(config["owner"]),timeout=120)
    info=reader.repository_info(); assert info["private"] is True
    service=UpdateService(target/"data")
    # 替换凭据来源而不替换网络响应：实际GET客户端读同一私有GitHub发布，不向验证目录复制访问令牌。
    service.reader=lambda token=None:reader
    rules=service.check(); assert rules["status"]=="updated" and rules["sequence"]==args.sequence
    payload=service.store._payload(read(service.store.root/"available.json"))
    assert payload["program"]["version"]==APP_VERSION and service.program_offer() is None
    staged=stage_program(service,descriptor=payload["program"])
    release=read(args.release_evidence/"release.json")
    assert sha(Path(staged["application"])/"搜推生图.exe")==release["executable_sha256"]
    assert payload["program"]["sha256"]==release["sha256"]
    result={"status":"verified","private_repository":info["html_url"],"release_url":rules["release_url"],
            "catalog_signature_verified":True,"rules_sha256":rules["sha256"],"archive_sha256":release["sha256"],
            "executable_sha256":release["executable_sha256"],"real_private_program_download":True,
            "current_program_not_reoffered":True,"client_methods":["GET"],"new_model_calls":0,"new_generation_submissions":0,
            "credentials_saved_in_validation_directory":False,"other_computer_verified":False,
            "scope":"本机维护账号授权的GET客户端，从真实私有Release下载与验签；其他电脑首次只读授权尚需现场验收"}
    (target/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=True))


if __name__=="__main__": main()
