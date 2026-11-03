"""U_GITHUB传输：私有仓库只读下载与维护端发布分离，授权不随CDN重定向转发。"""

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from .rules_bundle import canonical


class UpdateUnavailable(RuntimeError):
    def __init__(self,message,code=None): super().__init__(message); self.code=code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def maintainer_token(username=None):
    """仅维护入口向Git凭据管理器申请github.com凭据；令牌只保留在内存。"""
    env=dict(os.environ); env["GIT_TERMINAL_PROMPT"]="0"; env["GCM_INTERACTIVE"]="never"
    query="protocol=https\nhost=github.com\n"+("username="+username+"\n" if username else "")+"\n"
    result=subprocess.run(["git","credential","fill"],input=query,text=True,capture_output=True,env=env,timeout=20,
                          creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    values=dict(line.split("=",1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode or not values.get("password"): raise UpdateUnavailable("请在维护电脑正常登录GitHub后再发布")
    return values["password"]


class GitHubReader:
    """客户端只提供GET；输入本机授权，输出仓库元数据或经过边界检查的资产字节。"""
    def __init__(self,owner,repository,token,*,timeout=30):
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}",v) or v in (".","..") for v in (owner,repository)):
            raise ValueError("GitHub仓库名无效")
        if not token or any(ord(c)<33 or ord(c)>126 for c in token): raise ValueError("GitHub授权不可用")
        self.owner,self.repository,self._token,self.timeout=owner,repository,token,timeout
        self.prefix=f"/repos/{owner}/{repository}"
        self.opener=urllib.request.build_opener(_NoRedirect())

    def _request(self,method,path,*,value=None,raw=None,limit=4*1024*1024,accept="application/vnd.github+json",host="api.github.com"):
        if path!=self.prefix and not path.startswith(self.prefix+"/") and path not in ("/user","/user/repos"):
            raise ValueError("请求超出指定GitHub仓库")
        if method!="GET" and not isinstance(self,GitHubPublisher): raise ValueError("客户端没有发布权限")
        data=canonical(value) if value is not None else raw
        headers={"Authorization":"Bearer "+self._token,"Accept":accept,"User-Agent":"soutui-updates/0.2",
                 "Content-Type":"application/json" if raw is None else "application/octet-stream"}
        request=urllib.request.Request("https://"+host+path,data=data,headers=headers,method=method)
        try:
            with self.opener.open(request,timeout=self.timeout) as response:
                content=response.read(limit+1)
                if len(content)>limit: raise UpdateUnavailable("GitHub响应超过大小上限")
                return content
        except urllib.error.HTTPError as error:
            if method=="GET" and accept=="application/octet-stream" and error.code in (301,302,303,307,308):
                target=error.headers.get("Location",""); error.close()
                parsed=urllib.parse.urlsplit(target)
                if parsed.scheme!="https" or parsed.hostname not in ("release-assets.githubusercontent.com","objects.githubusercontent.com") or parsed.username or parsed.password:
                    raise UpdateUnavailable("GitHub资产重定向地址不受信任") from None
                # 资产CDN只拿一次签名URL，不携带GitHub的Authorization；不跟随第二次跳转。
                try:
                    with self.opener.open(urllib.request.Request(target,headers={"User-Agent":"soutui-updates/0.2"}),timeout=self.timeout) as response:
                        content=response.read(limit+1)
                        if len(content)>limit: raise UpdateUnavailable("更新资产超过大小上限")
                        return content
                except (urllib.error.URLError,TimeoutError): raise UpdateUnavailable("更新资产下载失败，原版本保留") from None
            code=error.code; error.close()
            raise UpdateUnavailable(f"GitHub返回HTTP {code}，请检查本机授权、仓库访问或请求额度",code) from None
        except (urllib.error.URLError,TimeoutError):
            raise UpdateUnavailable("GitHub连接失败，原版本保留") from None

    def get(self,path): return json.loads(self._request("GET",path))

    def repository_info(self):
        info=self.get(self.prefix)
        if info.get("private") is not True: raise UpdateUnavailable("更新源必须是私有仓库")
        return info

    def release(self,tag=None):
        """默认只读最新稳定发布；测试时显式读取指定标签，跳过草稿。"""
        self.repository_info()
        suffix="/releases/tags/"+urllib.parse.quote(tag,safe="") if tag else "/releases/latest"
        release=self.get(self.prefix+suffix)
        if release.get("draft"): raise UpdateUnavailable("尚未正式发布的更新不可供客户端使用")
        return release

    def download_asset(self,asset,*,limit=160*1024*1024):
        """只按指定仓库的资产ID取文件，不从服务端提供的任意URL下载。"""
        if type(asset.get("id")) is not int or not 0<=asset.get("size",limit+1)<=limit:
            raise ValueError("更新资产ID或大小无效")
        raw=self._request("GET",self.prefix+f"/releases/assets/{asset['id']}",accept="application/octet-stream",limit=limit)
        if len(raw)!=asset["size"]: raise UpdateUnavailable("下载大小与发布清单不符")
        return raw


class GitHubPublisher(GitHubReader):
    """只在维护入口实例化；创建私有仓库、草稿、资产及正式发布，客户端不调用这些方法。"""
    def create_private_repository(self):
        try:
            return self.repository_info()
        except UpdateUnavailable as error:
            if error.code!=404: raise
            # 先确认账号身份，避免请求失败时在错误归属下新建同名仓库。
            user=self.get("/user")
            if user["login"].casefold()!=self.owner.casefold(): raise ValueError("当前发布账号与仓库归属不符")
            result=json.loads(self._request("POST","/user/repos",value={"name":self.repository,"private":True,
                "description":"搜推生图私有规则与Windows更新发布；不存储账号会话或商品图片","auto_init":True}))
            if result.get("private") is not True: raise UpdateUnavailable("新建仓库不是私有状态，停止发布")
            return result

    def draft(self,tag,notes,*,prerelease=False):
        self.repository_info()
        return json.loads(self._request("POST",self.prefix+"/releases",value={"tag_name":tag,"name":tag,"body":notes,
            "draft":True,"prerelease":prerelease,"target_commitish":"main"}))

    def upload(self,release_id,name,raw):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}",name): raise ValueError("发布文件名无效")
        path=self.prefix+f"/releases/{int(release_id)}/assets?name="+urllib.parse.quote(name)
        return json.loads(self._request("POST",path,raw=raw,host="uploads.github.com"))

    def publish(self,release_id):
        return json.loads(self._request("PATCH",self.prefix+f"/releases/{int(release_id)}",value={"draft":False}))
