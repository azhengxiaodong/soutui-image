"""桌面更新服务：按固定私有仓库取稳定规则；无凭据和断网时不阻断可用的原规则。"""

import json
from pathlib import Path

from .github_updates import GitHubReader,maintainer_token,UpdateUnavailable
from .update_config import update_source,make_rule_store,APP_VERSION
from .update_security import load_read_token,save_read_token
from .rule_store import write_json,read,policy_id


class UpdateService:
    def __init__(self,data_directory):
        self.data=Path(data_directory).resolve(); self.source=update_source(); self.store=make_rule_store(self.data)

    def _token(self):
        path=self.data/"local/github_updates.dpapi"
        if path.exists(): return load_read_token(path)
        setting=self.data/"local/update_maintainer.json"
        if setting.exists() and read(setting)=={"use_existing_git_credentials":True}:
            return maintainer_token(self.source["owner"])
        raise UpdateUnavailable("尚未配置私有仓库只读授权；当前使用本机已验证规则")

    def reader(self,token=None):
        return GitHubReader(self.source["owner"],self.source["repository"],token or self._token())

    def configure_read_access(self,token):
        """先真实验证私有仓库读取，再本机加密保存；不保存错误授权，不触发审图。"""
        reader=self.reader(token); info=reader.repository_info()
        if info["id"]!=self.source["repository_id"]: raise ValueError("只读授权的仓库身份不符")
        result=self.store.sync(reader)
        save_read_token(token,self.data/"local/github_updates.dpapi")
        result.update(message="私有规则已同步："+result["version"]+"，新商品使用此版",program_version=APP_VERSION)
        write_json(self.data/"updates/last_check.json",result)
        return result

    def check(self):
        """返回可显示状态。失败时校验本机版本后保留；本机签名损坏则明确报错。"""
        try:
            result=self.store.sync(self.reader())
            result["message"]="私有规则已同步："+result["version"]+"，新商品使用此版"
        except (UpdateUnavailable,OSError,ValueError) as error:
            current=self.store.latest()
            result={"status":"using_cached","version":current.version,"policy_id":policy_id(current),
                    "sha256":current.sha256,"message":str(error)}
        result["program_version"]=APP_VERSION
        write_json(self.data/"updates/last_check.json",result)
        return result

    def program_offer(self):
        """只查看签名清单，不自动关闭任务或替换正在运行的程序。"""
        path=self.store.root/"available.json"
        if not path.exists(): path=self.store.root/"stable.json"
        if not path.exists(): return None
        payload=self.store._payload(read(path)); item=payload["program"]
        if item is None: return None
        self.store._descriptor(item)
        def version(value):
            parts=value.split(".")
            if len(parts)!=3 or not all(p.isdigit() for p in parts): raise ValueError("程序版本无效")
            return tuple(map(int,parts))
        return item if version(item["version"])>version(APP_VERSION) else None
