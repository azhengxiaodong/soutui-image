"""U_GITHUB实测：私有发布下载、签名校验、两个规则版本及任务固定；不发送商品图片。"""

import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src")); sys.path.insert(0,str(ROOT))
from soutui_image.github_updates import GitHubReader,GitHubPublisher,maintainer_token
from soutui_image.rules_bundle import builtin_bundle,RuleBundle,canonical
from soutui_image.rule_store import RuleStore
from soutui_image.update_security import sign_payload
from tools.check_frozen import check_frozen


def read(path): return json.loads(Path(path).read_text(encoding="utf-8"))
def sha(path): return hashlib.sha256((ROOT/path).read_bytes()).hexdigest()


def main():
    target=ROOT/"validation/updates/U_GITHUB_final"; target.mkdir(parents=True,exist_ok=False)
    config=read(ROOT/"src/soutui_image/resources/update_source.json")
    token=maintainer_token(config["owner"])
    reader=GitHubReader(config["owner"],config["repository"],token)
    info=reader.repository_info(); assert info["private"] is True
    try:
        urllib.request.urlopen(urllib.request.Request(info["html_url"],headers={"User-Agent":"soutui-update-validation"}),timeout=20).close()
        raise AssertionError("私有仓库不应匿名读取成功")
    except urllib.error.HTTPError as error:
        assert error.code==404; error.close()
    store=RuleStore(target/"client",config["public_key"])
    first=store.sync(reader)
    original=builtin_bundle(); assert store.latest().sha256==original.sha256
    tasks=target/"client/tasks"; p=SimpleNamespace(product_id="100",sha256="a"*64)
    pinned=store.for_product(p,tasks)
    # 纯传输测试版本只改元数据，不改提示词，不作为正式识图效果验收；作为预发布不进入默认稳定通道。
    data=original.data; data.update(version="2026-10-29.5-transport-test",sequence=2)
    changed=RuleBundle.parse(json.dumps(data).encode()); assert changed.data["prompts"]==original.data["prompts"]
    publisher=GitHubPublisher(config["owner"],config["repository"],token)
    release=publisher.draft("transport-validation-20261029","仅验证私有更新传输与任务固定；不改变审图文字，不作为生产规则。",prerelease=True)
    asset=publisher.upload(release["id"],"rules.json",changed.raw)
    descriptor={"id":asset["id"],"size":len(changed.raw),"sha256":changed.sha256,"version":changed.version}
    envelope=sign_payload({"format":1,"sequence":2,"repository_id":info["id"],"channel":"validation",
        "rules":descriptor,"program":None,"revoked":[]},ROOT/"data/local/update_signing_key.dpapi")
    publisher.upload(release["id"],"update.json",canonical(envelope)); publisher.publish(release["id"])
    second=store.sync(reader,tag=release["tag_name"],channel="validation")
    assert store.for_product(p,tasks).sha256==pinned.sha256
    assert store.for_product(SimpleNamespace(product_id="101",sha256="b"*64),tasks).sha256==changed.sha256
    before=(store.root/"stable.json").read_bytes()
    bad=json.loads(json.dumps(envelope)); bad["payload"]["sequence"]=3
    try: store.install(bad,changed.raw,repository_id=info["id"],channel="validation")
    except ValueError: pass
    else: raise AssertionError("篡改签名被接受")
    try: store.install(envelope,changed.raw+b"\n",repository_id=info["id"],channel="validation")
    except ValueError: pass
    else: raise AssertionError("损坏包被接受")
    assert before==(store.root/"stable.json").read_bytes()
    assert store.latest().sha256==changed.sha256  # 完全本地回读，不依赖网络成功。
    assert reader.release()["tag_name"]=="rules-2026-10-29.5", "验收预发布不能改变默认稳定更新"
    assert not check_frozen(ROOT)
    files=["src/soutui_image/update_security.py","src/soutui_image/github_updates.py","src/soutui_image/rule_store.py",
           "src/soutui_image/resources/update_source.json","run_updates.py","tests/test_private_updates.py","tools/validate_private_update.py"]
    result={"status":"verified","private_repository":info["html_url"],"anonymous_read_rejected":True,
        "two_real_github_releases":[first["release_url"],second["release_url"]],"existing_task_pinned":True,"new_task_selects_new_version":True,
        "tampered_signature_rejected":True,"corrupt_bundle_rejected":True,"local_offline_load_verified":True,
        "default_channel_unchanged":True,"cloud_image_calls":0,"includes_product_images":False,"client_transport_methods":["GET"],
        "authentication_scope":"本机维护账号凭据用于独立GET客户端实测，未把该凭据发给其他电脑；其他电脑只读令牌尚需各机配置",
        "scope":"真实GitHub私有仓库及发布资产传输、本机版本固定与失败保留；非跨电脑或新视觉准确率验收",
        "files":{f:sha(f) for f in files}}
    (target/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    manifest=ROOT/"validation/frozen_modules.json"; previous=manifest.read_bytes(); data=json.loads(previous)
    assert "U_GITHUB" not in data["modules"]
    (target/"frozen_before.json").write_bytes(previous)
    data["modules"]["U_GITHUB"]={"status":"verified","scope":result["scope"],"files":result["files"],"evidence":["validation/updates/U_GITHUB_final/result.json"]}
    manifest.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    with (ROOT/"docs/模块验收记录.md").open("a",encoding="utf-8") as handle:
        handle.write("\n\n## U_GITHUB私有发布与客户端下载独立验收\n\n已实际建立私有仓库并匿名读取被拒绝，发布原样规则和纯元数据预发布两个版本，客户端真实下载并验签。原任务固定旧版、新任务使用新版；损坏包、篡改签名被拒绝，本地稳定规则保留。测试客户端只GET，本机使用维护账号的现有凭据，未声称已在其他电脑验证只读令牌。执行`python tools/validate_private_update.py`，证据`validation/updates/U_GITHUB_final/result.json`。没有商品图片或账号凭据发布，未调用视觉模型。统一入口未接入。\n\n| 文件 | 实际SHA-256 |\n| --- | --- |\n")
        for f,v in result["files"].items(): handle.write(f"| `{f}` | `{v}` |\n")
    print(json.dumps({k:v for k,v in result.items() if k!="files"},ensure_ascii=True))


if __name__=="__main__": main()
