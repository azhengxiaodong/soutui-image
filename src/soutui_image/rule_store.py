"""U_GITHUB规则仓库：签名、哈希、兼容性校验后激活，版本不可变，商品任务固定规则。"""

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from .rules_bundle import RuleBundle, builtin_bundle, pinned_builtin_bundles, canonical
from .update_security import verify_payload


def read(path): return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path,value):
    """只在指定父目录原子写入自有文件，拒绝符号链接和目录跳转。"""
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink() or getattr(path,"is_junction",lambda:False)(): raise ValueError("更新文件不能是链接")
    temporary=path.parent/(".update-"+uuid.uuid4().hex+".tmp")
    with temporary.open("xb") as handle: handle.write(canonical(value)); handle.flush(); os.fsync(handle.fileno())
    temporary.replace(path)


def policy_id(bundle):
    """原规则保持旧身份以续用真实缓存；新规则身份同时绑定版本及完整内容。"""
    original=builtin_bundle()
    if bundle.sha256==original.sha256: return original.version
    if bundle.version==original.version: raise ValueError("已发布版本内容不能改写，需发布新版本")
    for pinned in pinned_builtin_bundles():
        if bundle.sha256==pinned.sha256: return pinned.version
        if bundle.version==pinned.version: raise ValueError("已绑定任务的旧规则内容不能改写")
    return bundle.version+"@"+bundle.sha256[:16]


class RuleStore:
    """输入当前用户数据目录与固定信任公钥；输出已验证规则和商品版本绑定。"""
    def __init__(self,data_directory,public_key):
        self.data=Path(data_directory).resolve(); self.root=self.data/"rules"; self.public_key=public_key

    def _payload(self,envelope):
        payload=verify_payload(envelope,self.public_key)
        expected={"format","sequence","repository_id","channel","rules","program","revoked"}
        if not isinstance(payload,dict) or set(payload)!=expected or payload["format"]!=1:
            raise ValueError("更新清单结构不兼容")
        if type(payload["sequence"]) is not int or payload["sequence"]<1: raise ValueError("更新序号无效")
        if payload["channel"] not in ("stable","validation") or not isinstance(payload["revoked"],list): raise ValueError("更新通道无效")
        if any(not isinstance(v,str) for v in payload["revoked"]): raise ValueError("撤销列表无效")
        return payload

    def _descriptor(self,descriptor):
        if (not isinstance(descriptor,dict) or set(descriptor)!={"id","size","sha256","version"}
            or type(descriptor["id"]) is not int or type(descriptor["size"]) is not int
            or not 0<descriptor["size"]<=160*1024*1024
            or not isinstance(descriptor["sha256"],str) or not re.fullmatch(r"[a-f0-9]{64}",descriptor["sha256"])):
            raise ValueError("发布资产清单无效")
        return descriptor

    def install(self,envelope,raw,*,repository_id,channel="stable"):
        """校验签名、仓库、通道和规则内容，再落盘并切换；任何失败不改变旧稳定指针。"""
        payload=self._payload(envelope); descriptor=self._descriptor(payload["rules"])
        if payload["repository_id"]!=repository_id or payload["channel"]!=channel: raise ValueError("更新不属于此私有仓库或通道")
        if len(raw)!=descriptor["size"] or hashlib.sha256(raw).hexdigest()!=descriptor["sha256"]: raise ValueError("规则包内容校验失败")
        bundle=RuleBundle.parse(raw)
        if bundle.sha256!=descriptor["sha256"] or bundle.version!=descriptor["version"]: raise ValueError("规则包身份不符")
        if bundle.sequence < builtin_bundle().sequence:
            raise ValueError("远端规则早于本程序内置规则，继续使用已安装的新规则")
        identity=policy_id(bundle)
        current=self.root/"stable.json"
        if current.exists():
            previous=self._payload(read(current))
            if payload["sequence"]<previous["sequence"]: raise ValueError("拒绝过期的更新清单")
            if payload["sequence"]==previous["sequence"] and canonical(payload)!=canonical(previous): raise ValueError("同一发布序号不能替换内容")
        versions=self.root/"versions.json"; known=read(versions) if versions.exists() else {}
        if bundle.version in known and known[bundle.version]!=bundle.sha256: raise ValueError("同版本的规则内容已改变")
        target=self.root/"bundles"/(bundle.sha256+".json")
        write_json(target,bundle.data); write_json(target.with_suffix(".proof.json"),envelope)
        known[bundle.version]=bundle.sha256; write_json(versions,known)
        write_json(current,envelope)
        return {"status":"updated","version":bundle.version,"policy_id":identity,"sha256":bundle.sha256,"sequence":payload["sequence"]}

    def sync(self,reader,*,tag=None,channel="stable"):
        """只从已配置私有仓库下载签名清单和规则；导入本模块不会联网。"""
        info=reader.repository_info(); release=reader.release(tag)
        assets=[a for a in release["assets"] if a["name"]=="update.json"]
        if len(assets)!=1: raise ValueError("发布中没有唯一签名更新清单")
        envelope=json.loads(reader.download_asset(assets[0],limit=128*1024))
        payload=self._payload(envelope)
        if payload["repository_id"]!=info["id"] or payload["channel"]!=channel: raise ValueError("签名清单仓库或通道不符")
        available=self.root/"available.json"
        if available.exists():
            previous=self._payload(read(available))
            if payload["sequence"]<previous["sequence"] or payload["sequence"]==previous["sequence"] and canonical(payload)!=canonical(previous):
                raise ValueError("拒绝过期或同序号篡改的发布清单")
        # 先保存可信的可用版本信息；规则不兼容时仍能得知应升级哪个程序，稳定规则指针不变。
        write_json(available,envelope)
        descriptor=self._descriptor(payload["rules"])
        local=self.root/"bundles"/(descriptor["sha256"]+".json")
        raw=local.read_bytes() if local.exists() else reader.download_asset(descriptor,limit=512*1024)
        result=self.install(envelope,raw,repository_id=info["id"],channel=channel)
        result["release_url"]=release["html_url"]
        return result

    def load(self,sha256):
        """历史任务按固定哈希回读；本机缓存也重新校验签名与内容。"""
        original=builtin_bundle()
        if sha256==original.sha256: return original
        if not re.fullmatch(r"[a-f0-9]{64}",sha256): raise ValueError("规则哈希无效")
        for pinned in pinned_builtin_bundles():
            if sha256==pinned.sha256: return pinned
        path=self.root/"bundles"/(sha256+".json")
        payload=self._payload(read(path.with_suffix(".proof.json")))
        descriptor=self._descriptor(payload["rules"]); raw=path.read_bytes()
        bundle=RuleBundle.parse(raw)
        if descriptor["sha256"]!=sha256 or bundle.sha256!=sha256 or descriptor["size"]!=len(raw): raise ValueError("本机规则缓存已改变")
        return bundle

    def latest(self):
        pointer=self.root/"stable.json"
        if not pointer.exists(): return builtin_bundle()
        payload=self._payload(read(pointer))
        bundle=self.load(payload["rules"]["sha256"])
        if bundle.sequence < builtin_bundle().sequence:
            bundle = builtin_bundle()
        available=self.root/"available.json"
        revocations=self._payload(read(available))["revoked"] if available.exists() else payload["revoked"]
        if policy_id(bundle) in revocations: raise ValueError("当前规则已被维护端撤销")
        return bundle

    def for_product(self,product,tasks,*,upgrade=False):
        """旧商品绑定原规则，新商品绑定最新稳定版；显式升级时才改变旧商品规则身份。"""
        pin=Path(tasks)/f"product_{product.product_id}_rules.json"
        if pin.exists() and not upgrade:
            record=read(pin)
            if record["source_sha256"]!=product.sha256: raise ValueError("商品规则记录与当前白底原图不符")
            bundle=self.load(record["bundle_sha256"])
        else:
            historical=list((Path(tasks)/product.product_id).glob("group_*/round_*.json"))
            bundle=builtin_bundle() if historical and not upgrade else self.latest()
            write_json(pin,{"product_id":product.product_id,"source_sha256":product.sha256,"bundle_sha256":bundle.sha256,
                            "policy_id":policy_id(bundle),"created_at":time.time()})
        pointer=self.root/"available.json"
        if not pointer.exists(): pointer=self.root/"stable.json"
        if pointer.exists() and policy_id(bundle) in self._payload(read(pointer))["revoked"]:
            raise ValueError("该商品使用的规则已撤销，请明确升级复核后继续")
        return bundle
