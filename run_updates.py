"""私有更新独立入口：维护端创建/发布，客户端明确检查；导入不联网、不执行生图。"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/"src"))
from soutui_image.github_updates import GitHubReader, GitHubPublisher, maintainer_token, UpdateUnavailable
from soutui_image.update_security import create_signing_key, sign_payload, load_read_token, verify_payload
from soutui_image.rules_bundle import RuleBundle, canonical
from soutui_image.rule_store import RuleStore, write_json
from soutui_image.update_config import APP_VERSION


def read(path): return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    """发布前要求规则验证证据与内容哈希一致，客户端检查只使用GET。"""
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=("init-private","publish-rules","check","publish-program"))
    parser.add_argument("--owner",default="wanghua361"); parser.add_argument("--repo",default="soutui-image-updates")
    parser.add_argument("--data-dir",type=Path,default=ROOT/"data")
    parser.add_argument("--source",type=Path,default=ROOT/"src/soutui_image/resources/update_source.json")
    parser.add_argument("--rules",type=Path); parser.add_argument("--evidence",type=Path)
    parser.add_argument("--tag"); parser.add_argument("--sequence",type=int); parser.add_argument("--program",type=Path)
    parser.add_argument("--program-version",default=APP_VERSION); parser.add_argument("--validation",action="store_true")
    parser.add_argument("--maintainer",action="store_true",help="仅维护机用已有Git凭据；普通客户端使用本机只读授权")
    args=parser.parse_args(); data=args.data_dir.resolve()
    private=data/"local/update_signing_key.dpapi"
    if args.action=="init-private":
        writer=GitHubPublisher(args.owner,args.repo,maintainer_token(args.owner))
        info=writer.create_private_repository(); public=create_signing_key(private)
        source={"format":1,"owner":args.owner,"repository":args.repo,"repository_id":info["id"],"public_key":public}
        if args.source.exists() and read(args.source)!=source: raise ValueError("已有更新信任配置不同，未覆盖")
        write_json(args.source,source)
        print(json.dumps({"status":"private_repository_ready","repository":info["html_url"],"private":info["private"],"source":str(args.source)},ensure_ascii=True)); return
    source=read(args.source)
    token=maintainer_token(source["owner"]) if args.maintainer or args.action.startswith("publish") else load_read_token(data/"local/github_updates.dpapi")
    client=(GitHubPublisher if args.action.startswith("publish") else GitHubReader)(source["owner"],source["repository"],token)
    info=client.repository_info()
    if info["id"]!=source["repository_id"]: raise ValueError("私有仓库身份发生变化")
    store=RuleStore(data,source["public_key"])
    if args.action=="check":
        result=store.sync(client,tag=args.tag,channel="validation" if args.validation else "stable")
        print(json.dumps(result,ensure_ascii=True)); return
    if not args.tag or not args.sequence or args.sequence<1: raise ValueError("发布需要标签和递增序号")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,90}",args.tag): raise ValueError("发布标签格式无效")
    if args.action=="publish-rules":
        if not args.rules or not args.evidence: raise ValueError("发布前需要实际规则和验收证据")
        bundle=RuleBundle.parse(args.rules.read_bytes()); evidence=read(args.evidence)
        if evidence.get("status")!="verified" or evidence.get("bundle_sha256")!=bundle.sha256: raise ValueError("规则未完成对应内容的验收")
        program=None
        try:
            previous=client.release(); assets=[a for a in previous["assets"] if a["name"]=="update.json"]
            if len(assets)!=1: raise ValueError("上一稳定发布缺少签名清单")
            prior=store._payload(json.loads(client.download_asset(assets[0],limit=128*1024)))
            if args.sequence<=prior["sequence"]: raise ValueError("发布序号必须递增")
            program=prior["program"]
        except UpdateUnavailable as error:
            if error.code!=404: raise
        notes="已验证的审图规则数据包。没有商品图片、登录凭据或审核日志。"
        draft=client.draft(args.tag,notes,prerelease=args.validation)
        rules=client.upload(draft["id"],"rules.json",bundle.raw)
        descriptor={"id":rules["id"],"size":len(bundle.raw),"sha256":bundle.sha256,"version":bundle.version}
    else:
        if not args.program or not args.evidence: raise ValueError("发布程序需要程序包及对应交付验收证据")
        proof=read(args.evidence)
        if proof.get("sha256")!=hashlib.sha256(args.program.read_bytes()).hexdigest(): raise ValueError("程序包与交付验收证据不符")
        runtime=read(Path(proof["runtime_evidence"])/"result.json")
        if (runtime.get("status")!="verified" or runtime["self_test"].get("app_version")!=args.program_version
            or runtime["executable_sha256"]!=proof["executable_sha256"]): raise ValueError("程序版本未通过对应exe验收")
        client.timeout=180
        existing=client.release(); manifests=[a for a in existing["assets"] if a["name"]=="update.json"]
        if len(manifests)!=1: raise ValueError("现有稳定规则发布不完整")
        old=store._payload(json.loads(client.download_asset(manifests[0],limit=128*1024)))
        if old["repository_id"]!=info["id"]: raise ValueError("现有规则仓库身份不符")
        if args.sequence<=old["sequence"]: raise ValueError("发布序号必须递增")
        descriptor=old["rules"]
        draft=client.draft(args.tag,"Windows私有更新程序包；规则保持已验证版本。")
        raw=args.program.read_bytes(); asset=client.upload(draft["id"],"soutui-windows.zip",raw)
        program={"id":asset["id"],"size":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),"version":args.program_version}
    payload={"format":1,"sequence":args.sequence,"repository_id":info["id"],"channel":"validation" if args.validation else "stable",
             "rules":descriptor,"program":program,"revoked":[]}
    envelope=sign_payload(payload,private)
    verify_payload(envelope,source["public_key"])
    client.upload(draft["id"],"update.json",canonical(envelope))
    published=client.publish(draft["id"])
    result={"status":"published","repository_private":True,"release":published["html_url"],"sequence":args.sequence,
            "rules_sha256":descriptor["sha256"],"program":program,"includes_product_images":False,"includes_credentials":False}
    target=data/"update_publications"/(args.tag+".json"); write_json(target,result)
    print(json.dumps(result,ensure_ascii=True))


if __name__=="__main__": main()
